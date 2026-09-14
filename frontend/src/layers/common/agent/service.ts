import { api } from '../api/client'
import { apiUrl } from '../api/base'
import { ApiError } from '../api/errors'
import { getCsrfToken } from '../security/csrf'

/**
 * 智能体对话协议——INTERFACES.md §3（冻结版）。
 *
 * ## 与早期版本的差别
 *
 * 1. **端点改名**：早期版本 `/agent/turn` / `/agent/confirm` / `/agent/cancel`；
 *    新契约是 `/agent/turns`、`/agent/turns/stream`、
 *    `/agent/confirmations/{id}/confirm|cancel`。
 * 2. **流式行结构变化**：早期版本 `result` 行携带 `{ type:'result', data: … }`，
 *    新契约是 `{ type:'result', result: <四种 kind 之一> }`
 *    （INTERFACES.md §3）。字段名 `data` → `result`，不能照抄早期代码。
 * 3. **写操作只能由 `result` 行交付**（INTERFACES.md §3 原文）：
 *    「写操作**只能**通过 `result` 行交付，绝不允许把待确认操作'流式渲染成已完成'。
 *    前端在收到 `result` 前不得清理 `busy` 状态。」
 *    因此 `streamAgentTurn` 只有拿到 `result` 行才 resolve；`status`/`delta`
 *    只用于显示，**不产生任何状态迁移**。
 * 4. **流式请求必须绕过统一客户端**（要读 `response.body`），但补齐早期版本漏掉的东西：
 *    CSRF 注入、错误归一化为 `ApiError`、缺 `result` 行时抛 `AGENT_PROTOCOL_ERROR`。
 */

export type AgentTurnKind = 'assistant' | 'clarification' | 'confirmation_required' | 'executed' | 'cancelled'

export interface AgentHistoryItem {
  role: 'user' | 'assistant'
  text: string
}

/** 确认卡片的一行「字段名 → 值」，一律中文。 */
export interface AgentConfirmationRow {
  label: string
  value: string
}

export interface AgentConfirmation {
  id: number
  /** 一次性令牌，仅返回一次。 */
  token: string
  capability: string
  title: string
  target: string
  rows: AgentConfirmationRow[]
  /** 影响面描述，例如「扣减 1 号仓 1号饲料 50kg」。 */
  impact: string[]
  expires_at: string
}

export interface AgentExecutedResult {
  capability: string
  /**
   * 被这次写入牵动的**资源名**（与 `ResourceMeta.name` / `Capability.resource`
   * 同一命名空间）。
   *
   * 为什么必须由服务端给、而不是前端从 `capability` 反推：能力名到资源名**不是**
   * 字符串切分能得出的关系 —— `pond_status_change.request` 的资源是
   * `pond_status_change`，而 `sales_order.approve` 的资源是 `sales_order`；
   * 前端反推就是第二处描述同一件事，且必然会漂移。
   *
   * 页面用它判断「这次写的正好是我这个列表吗」：写的不是当前资源时**不刷新**
   * （刷了也没用，数据在另一个页面上）。
   */
  resource: string
  resource_id: number | null
  url: string
  /**
   * 当前迭代牵动过的**全部**写入（契约附加字段）。
   *
   * 一次对话里模型可能写多次（实测：先建往来单位、再建批次），而顶层的
   * `result` 只能带一条（取最后一条）。刷新需要的是**资源集合**，
   * 不是"最后一个是谁"，所以多写的情形靠这个字段不失真。
   */
  data?: { executed: { capability: string; resource: string; resource_id: number | null }[] }
}

export type AgentTurnResult =
  | { kind: 'assistant'; conversation_id: string; message: string }
  | {
      kind: 'clarification'
      conversation_id: string
      question: string
      options: string[]
      allow_free_text: boolean
      /**
       * 模型这一轮说的话（附加字段，可以没有）。
       *
       * `question` 只说"要问什么"，而模型在提问前后往往还给了上下文 ——
       * 实测 `ask_user` 那轮它补了「（可选补充：联系人、电话、地址、结算天数、信用额度。）」，
       * 那部分不在 `question` 里。丢了它，用户就只能看到一句被截短的问句。
       */
      message?: string
    }
  | {
      kind: 'confirmation_required'
      conversation_id: string
      message: string
      confirmation: AgentConfirmation
      /**
       * 同一轮签发的**全部**待确认卡片（附加字段）。
       *
       * 一次对话里模型可以对多个对象各签一张卡（实测：一句话要求归档 3 个草稿区域
       * → 3 张卡）。`confirmation` 是其中第一张，所以按 §3 ③ 原文写的客户端行为不变。
       *
       * **为什么一张都不能少显示**：确认令牌是一次性的、**只在签发它的那一次响应里
       * 出现**，界面上少渲染一张，那张卡就再也无法确认了（没有补发的入口）。
       */
      confirmations?: AgentConfirmation[]
    }
  | {
      kind: 'executed'
      conversation_id: string
      message: string
      result: AgentExecutedResult
      /** 同轮已经写入、又产生高风险操作时，卡片必须与写入刷新信号一起交付。 */
      confirmation?: AgentConfirmation
      confirmations?: AgentConfirmation[]
    }
  | { kind: 'cancelled'; conversation_id?: string }

/** 流式行。 */
export type AgentStreamLine =
  | { type: 'status'; text: string }
  | { type: 'delta'; text: string }
  | { type: 'result'; result: AgentTurnResult }

const AGENT_TURN_KINDS: readonly AgentTurnKind[] = [
  'assistant',
  'clarification',
  'confirmation_required',
  'executed',
  'cancelled',
]

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function protocolError(): ApiError {
  return new ApiError('AGENT_PROTOCOL_ERROR', '智能助手的流式响应格式异常，请重试', 502)
}

function parseTurnResult(value: unknown): AgentTurnResult {
  if (
    !isRecord(value) ||
    typeof value.kind !== 'string' ||
    !AGENT_TURN_KINDS.includes(value.kind as AgentTurnKind)
  ) {
    throw protocolError()
  }
  return value as AgentTurnResult
}

export interface AgentTurnRequest {
  message: string
  conversation_id?: string
  page_context?: string
  history?: AgentHistoryItem[]
}

/** 非流式单轮。 */
export function sendAgentTurn(
  payload: AgentTurnRequest,
  options: { idempotencyKey?: string } = {},
): Promise<AgentTurnResult> {
  return api.post<AgentTurnResult>('/api/v1/agent/turns', payload, options)
}

/** 确认执行。响应是 `executed` 形态；重复提交返回 `CONFIRMATION_INVALID`。 */
export function confirmAgent(id: number, token: string): Promise<AgentTurnResult> {
  return api.post<AgentTurnResult>(`/api/v1/agent/confirmations/${id}/confirm`, { token })
}

/** 取消待确认操作。 */
export function cancelAgent(id: number): Promise<AgentTurnResult> {
  return api.post<AgentTurnResult>(`/api/v1/agent/confirmations/${id}/cancel`, {})
}

export interface StreamHandlers {
  onStatus?: (text: string) => void
  onDelta?: (text: string) => void
}

/**
 * 流式单轮。返回值即 `result` 行交付的结果。
 *
 * 早期版本的问题是流式异常时已渲染文本被丢弃且不进历史
 * （.local/recon-frontend.md 问题 P-6）：旧 `AgentPanel.vue:106-109` 的 `finally`
 * 无条件 `streamingText=''`，而 `streamingText` 从不写入 `messages`。新实现把
 * 「已显示的 delta」交给调用方保管，协议层不做清理——见 AgentPanel 的处理。
 */
export async function streamAgentTurn(
  payload: AgentTurnRequest,
  handlers: StreamHandlers = {},
): Promise<AgentTurnResult> {
  const response = await fetch(apiUrl('/api/v1/agent/turns/stream'), {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/x-ndjson',
      'X-CSRF-Token': await getCsrfToken(),
    },
    body: JSON.stringify(payload),
  })

  if (!response.ok || !response.body) {
    const text = await response.text().catch(() => '')
    throw new ApiError('AGENT_UNAVAILABLE', text || '智能助手暂时不可用，请稍后重试', response.status || 0)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let delivered: AgentTurnResult | undefined

  function handleLine(line: string): void {
    const trimmed = line.trim()
    if (!trimmed) return
    let parsed: unknown
    try {
      parsed = JSON.parse(trimmed) as unknown
    } catch {
      throw protocolError()
    }
    if (!isRecord(parsed) || typeof parsed.type !== 'string') throw protocolError()
    if (parsed.type === 'status' || parsed.type === 'delta') {
      if (typeof parsed.text !== 'string') throw protocolError()
      if (parsed.type === 'status') handlers.onStatus?.(parsed.text)
      else handlers.onDelta?.(parsed.text)
      return
    }
    if (parsed.type !== 'result') throw protocolError()
    delivered = parseTurnResult(parsed.result)
  }

  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) handleLine(line)
  }
  handleLine(buffer)

  if (!delivered) {
    throw new ApiError('AGENT_PROTOCOL_ERROR', '智能助手这一轮没有返回结果，请重试', 502)
  }
  return delivered
}
