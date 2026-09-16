/**
 * 渔芯 Gateway 的 HTTP 客户端（插件侧）。
 *
 * 契约来源：`docs/INTERFACES.md` §4「Harness ↔ Gateway 协议」（**冻结**）。
 * 服务端实现：`backend/yuxin/agent/gateway.py`（`AgentToolGateway`）。
 *
 *   GET  {gatewayUrl}/tools                  → { code, data: { tools: [...] }, ... }
 *   POST {gatewayUrl}/tools/{tool_name}/call → { code, data: <TurnOutcome>, ... }
 *
 * 两条硬性纪律：
 *
 *  ① **`kind` 原样透传**。本模块**不解释、不改写、不补充** `kind` 与 `message` 的语义。
 *     全部文案由服务端渲染（`docs/WRITE_CONTRACT.md` 规则 3：`message` 由服务端从真实数据渲染）。
 *  ② **配置错误在启动期抛，业务错误在调用期返回 `failed`**。
 *     启动期静默降级会让模型以为"什么都干不了"（用户看到的是"AI 变笨了"而不是"服务没起来"）；
 *     调用期抛异常则会让模型只看到一个没有中文解释的通用失败 —— 两者都不允许。
 */
import type { GatewayConfig, ToolOutcome } from './types.js'

export class GatewayConfigError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'GatewayConfigError'
  }
}

export class GatewayUnavailableError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options as ErrorOptions)
    this.name = 'GatewayUnavailableError'
  }
}

export interface RemoteToolSpec {
  readonly name: string
  readonly description: string
  readonly parameters: unknown
  readonly capability: string
  /**
   * 服务端是否要求本次调用携带 `Idempotency-Key`。
   *
   * 由 `ToolSpec.to_meta()` 下发（`DECISIONS.md` Q19）。插件据此决定是否生成键——
   * 而不是"一律带"或"一律不带"：
   *   * 一律带：无害，但服务端失去「非幂等能力不该收到键」这个契约违规信号
   *   * 一律不带：重复敏感操作会被服务端拒绝
   */
  readonly requires_idempotency_key?: boolean
}

export interface CallOptions {
  readonly signal?: AbortSignal
  readonly idempotencyKey?: string
}

export interface GatewayClient {
  readonly base: string
  fetchToolCatalog(options?: CallOptions): Promise<RemoteToolSpec[]>
  callTool(toolName: string, args: Record<string, unknown>, options?: CallOptions): Promise<ToolOutcome>
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * 校验并归一化网关地址。
 *
 * 只允许 `http:` / `https:`（沿用参考实现的这条校验）。其他协议（`file:`、`ftp:`、
 * `javascript:` 等）一律拒绝 —— 插件的 fetch 目标必须是一个明确的业务网关。
 */
export function resolveGatewayBase(gatewayUrl: string): string {
  const raw = (gatewayUrl ?? '').trim()
  if (!raw) throw new GatewayConfigError('渔芯 Gateway 地址未配置')

  let parsed: URL
  try {
    parsed = new URL(raw)
  } catch {
    throw new GatewayConfigError(`渔芯 Gateway 地址无效：${raw}`)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new GatewayConfigError(`渔芯 Gateway 只允许 HTTP(S) 地址，当前是 ${parsed.protocol}`)
  }
  return parsed.toString().replace(/\/+$/, '')
}

/** 从响应信封里取出 `data`；非 OK 的信封抛 `GatewayUnavailableError`。 */
function unwrapEnvelope(payload: unknown, what: string): unknown {
  if (!isRecord(payload)) {
    throw new GatewayUnavailableError(`${what}：响应不是 JSON 对象`)
  }
  const code = typeof payload['code'] === 'string' ? payload['code'] : null
  if (code !== 'OK') {
    const message = typeof payload['message'] === 'string' && payload['message'].length > 0
      ? payload['message']
      : `${what}失败`
    throw new GatewayUnavailableError(code ? `${message}（${code}）` : message)
  }
  return payload['data']
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    return null
  }
}

function envelopeMessage(payload: unknown, fallback: string): { code: string; message: string } {
  if (!isRecord(payload)) return { code: 'GATEWAY_UNAVAILABLE', message: fallback }
  const code = typeof payload['code'] === 'string' && payload['code'].length > 0
    ? payload['code']
    : 'GATEWAY_UNAVAILABLE'
  const message = typeof payload['message'] === 'string' && payload['message'].length > 0
    ? payload['message']
    : fallback
  return { code, message }
}

export function createGatewayClient(config: GatewayConfig): GatewayClient {
  const base = resolveGatewayBase(config.gatewayUrl)
  const contextToken = (config.contextToken ?? '').trim()
  if (!contextToken) {
    throw new GatewayConfigError('缺少 渔芯上下文令牌（X-Agent-Context），拒绝以匿名身份注册工具')
  }

  function headers(extra?: Record<string, string>): Record<string, string> {
    return {
      'Content-Type': 'application/json',
      // 上下文令牌随每次调用送达；**绝不写入 process.env**（见 index.ts 的说明）。
      'X-Agent-Context': contextToken,
      ...extra,
    }
  }

  return {
    base,

    async fetchToolCatalog(options = {}) {
      let response: Response
      try {
        response = await fetch(`${base}/tools`, {
          method: 'GET',
          headers: headers(),
          ...(options.signal ? { signal: options.signal } : {}),
        })
      } catch (cause) {
        throw new GatewayUnavailableError(
          `无法连接 渔芯 Gateway（${base}/tools）：${cause instanceof Error ? cause.message : String(cause)}`,
          { cause },
        )
      }
      const payload = await readJson(response)
      if (!response.ok) {
        const { message } = envelopeMessage(payload, `工具清单拉取失败（HTTP ${response.status}）`)
        throw new GatewayUnavailableError(message)
      }
      const data = unwrapEnvelope(payload, '工具清单拉取')
      if (!isRecord(data) || !Array.isArray(data['tools'])) {
        throw new GatewayUnavailableError('工具清单响应缺少 data.tools 数组')
      }
      const tools: RemoteToolSpec[] = []
      for (const item of data['tools'] as unknown[]) {
        if (!isRecord(item)) {
          throw new GatewayUnavailableError('工具清单中存在非法条目')
        }
        const name = item['name']
        const description = item['description']
        const capability = item['capability']
        if (typeof name !== 'string' || name.length === 0) {
          throw new GatewayUnavailableError('工具清单中存在缺少 name 的条目')
        }
        tools.push({
          name,
          description: typeof description === 'string' ? description : '',
          parameters: item['parameters'],
          capability: typeof capability === 'string' ? capability : name,
          // ★ 必须显式带上，否则服务端下发了也被**丢弃**。
          //
          // 初版这里是逐字段构造的，没有这一行——服务端发
          // `requires_idempotency_key: true`，插件里读到的却是 undefined，
          // 于是幂等键永远不生成、调用永远被服务端拒绝。
          //
          // 这类缺陷的形态是"**解析时静默丢字段**"：请求与响应都成功，
          // 只是某个字段没传到位。它比报错难查，因为没有错误信息可看——
          // 症状是"功能没生效"。
          requires_idempotency_key: item['requires_idempotency_key'] === true,
        })
      }
      return tools
    },

    async callTool(toolName, args, options = {}) {
      const url = `${base}/tools/${encodeURIComponent(toolName)}/call`
      const extraHeaders: Record<string, string> = {}
      if (options.idempotencyKey) extraHeaders['Idempotency-Key'] = options.idempotencyKey

      let response: Response
      try {
        response = await fetch(url, {
          method: 'POST',
          headers: headers(extraHeaders),
          body: JSON.stringify({ arguments: args }),
          ...(options.signal ? { signal: options.signal } : {}),
        })
      } catch (cause) {
        // 传输层失败不是业务失败，但对模型而言同样是"没做成"。
        // 这里**返回** failed 而不是 throw —— 见本文件头部纪律 ②。
        return {
          kind: 'failed',
          code: 'AGENT_UNAVAILABLE',
          message: `业务服务暂时不可用，请稍后重试：${cause instanceof Error ? cause.message : String(cause)}`,
        }
      }

      const payload = await readJson(response)
      if (!response.ok) {
        const { code, message } = envelopeMessage(payload, `调用未完成（HTTP ${response.status}）`)
        return { kind: 'failed', code, message }
      }
      const data = unwrapEnvelopeSafe(payload)
      if (data === null) {
        const { code, message } = envelopeMessage(payload, '调用未完成')
        return { kind: 'failed', code, message }
      }
      return data
    },
  }
}

/** `callTool` 专用：信封异常时返回 null，让调用方统一转成 `failed`。 */
function unwrapEnvelopeSafe(payload: unknown): ToolOutcome | null {
  if (!isRecord(payload)) return null
  if (payload['code'] !== 'OK') return null
  const data = payload['data']
  if (!isRecord(data)) return null
  const kind = data['kind']
  if (typeof kind !== 'string') return null
  return data as unknown as ToolOutcome
}
