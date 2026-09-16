/**
 * 插件包：把渔芯的每一条能力注册成**一个真实工具、带真实 JSON Schema**。
 *
 * ## 与早期实现的根本差别
 *
 * 早期实现（`deepseek-harness-master/packages/extensions/yuxin-agent-tools/src/index.ts`，3 KB）
 * 只注册 `biz_query` / `biz_mutation` **两个元工具**，把全部业务操作名塞进一段长
 * `description` 字符串里让模型去挑。本实现为**每一条能力注册一个独立工具**，
 * schema 直接取自服务端下发的标准 JSON Schema —— 模型不需要"从长描述里挑字符串"，
 * 参数在模型这一侧就被 schema 约束住。
 *
 * ## 三条不可违反的纪律
 *
 * ① **`kind` 原样交给模型**（`docs/WRITE_CONTRACT.md` 规则 1）。
 *    本文件里**不存在**任何生成"完成/成功"措辞的代码路径 —— 文案全部由服务端渲染。
 * ② **`confirmation_required` 之后必须让模型停下**。待确认的工具结果里带一条 `note`
 *    明确指令模型立即结束当前迭代输出。少了这一条，模型会顺着自己的推理把"待确认"说成"已完成"。
 * ③ **启动失败必须显式**。`GET /tools` 不可达时**抛出**并中止插件加载，
 *    绝不静默注册 0 个工具 —— 静默注册会让模型以为"什么都干不了"，
 *    用户看到的是"AI 变笨了"而不是"服务没起来"。
 */
import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'

import { createGatewayClient, resolveGatewayBase } from './gateway.js'
import type { GatewayClient, RemoteToolSpec } from './gateway.js'
import { parameterSpecFromJsonSchema } from './schema.js'
import type { GatewayConfig, JsonLike, ToolOutcome } from './types.js'

export const name = 'yuxin-biz-tools'

/** 本插件依赖 Harness 的工具注册表服务。 */
export const inject = ['tools']

export interface FpaBizToolsConfig {
  /** 形如 `https://host/yuxin/api/v1/agent`；只允许 http/https。 */
  readonly gatewayUrl: string
  /** 单轮上下文令牌；由 Harness 侧从受控配置读取，**不落 env、不落日志**。 */
  readonly contextToken: string
}

/**
 * 待确认时必须原样转交的控制指令。
 *
 * 措辞刻意避开任何"完成/成功/已执行"字样 —— 这条 note 本身也在测试的禁用词断言范围内。
 */
export const PENDING_CONFIRMATION_NOTE =
  '本条调用尚未写入任何数据，只生成了一张待确认卡片。请立即停止当前迭代输出，等待用户在界面上确认或取消；在收到确认结果之前，不要向用户报告任何执行结果。'

/** 反问用户后的停下指令（沿用参考实现的措辞，它已被验证有效）。 */
export const CLARIFICATION_NOTE = '已向用户提问，请立即停止当前迭代输出。'

/**
 * `ask_user` 的用途边界（**实测缺陷的修复点**）。
 *
 * 实测：用户说「帮我核验付款单 PAY-2026-002」，模型把它理解成"要先问用户是否确认"，
 * 调 `ask_user` 反问「请回复确认核验」——于是那一轮返回的是 `clarification`，
 * 界面**不会**出现 HITL 确认卡片（卡片只由**直接调用业务工具**触发）。
 * 用户看到的正是"模型让我确认，却没有卡片"。
 *
 * 所以这条描述必须写死"不要用它请求写操作确认"。
 */
export const ASK_USER_DESCRIPTION =
  '当用户指令缺少**必填参数**、或存在**多个候选业务对象**无法唯一确定时，用本工具向用户提问。' +
  '**不要**用它请求用户确认写操作（是否确认 / 是否核验 / 是否执行）——写操作请**直接调用对应业务工具**，' +
  '系统会自动生成待确认卡片。调用后必须立即停止当前迭代输出。'

/**
 * 工具结果 → 交给模型的值。
 *
 * 这是一个**纯搬运**函数：只按 `kind` 分派，`message` / `resource_id` / `confirmation`
 * 全部原样透传，不做任何措辞加工。唯一的"新增"是两个控制性 `note`。
 */
export function toModelPayload(outcome: ToolOutcome): JsonLike {
  switch (outcome.kind) {
    case 'confirmation_required':
      return {
        kind: 'confirmation_required',
        ...(outcome.message ? { message: outcome.message } : {}),
        confirmation: outcome.confirmation ?? null,
        note: PENDING_CONFIRMATION_NOTE,
      }
    case 'executed':
      return {
        kind: 'executed',
        message: outcome.message ?? '',
        resource_id: outcome.resource_id ?? null,
        data: outcome.data ?? null,
      }
    case 'read':
      return {
        kind: 'read',
        message: outcome.message ?? '',
        data: outcome.data ?? null,
      }
    case 'failed':
      return { kind: 'failed', code: outcome.code, message: outcome.message }
    default: {
      const never: never = outcome
      return { kind: 'failed', code: 'INTERNAL_ERROR', message: String(never) }
    }
  }
}

/**
 * 把上下文令牌挡在子进程环境之外（纵深防御）。
 *
 * 本实现**从不**把令牌写进 `process.env`。但 Harness 子进程会继承父进程环境，
 * 因此如果外部（旧启动脚本、`.env` 加载器）已经把它放进去了，这里主动清掉。
 * 参考实现也做了同样的清理（`delete process.env.YUXIN_AGENT_CONTEXT_TOKEN`），
 * 说明这是真实发生过的泄漏面。
 */
function scrubCredentialsFromEnv(keys: readonly string[]): void {
  if (typeof process === 'undefined' || !process.env) return
  for (const key of keys) {
    if (process.env[key] !== undefined) delete process.env[key]
  }
}

const SCRUBBED_ENV_KEYS = ['YUXIN_AGENT_CONTEXT_TOKEN', 'YUXIN_AGENT_GATEWAY_URL'] as const
/**
 * 幂等键的**本地兜底**：上游没有给 callId 时用它。
 *
 * 只需满足服务端的格式契约（8–128 个 `[A-Za-z0-9._:-]`）且**单进程内唯一**。
 * 刻意不追求"跨重试稳定"——那种场景由 `rootCallId` 负责，根本走不到这里；
 * 而"没有键 → 服务端 400 → 写能力整体不可用"是必须消灭的失败模式。
 */
let localCallSeq = 0
function nextLocalCallId(): string {
  localCallSeq += 1
  return `noid-${Date.now().toString(36)}-${localCallSeq.toString(36)}`
}

function registerCatalogTool(
  ctx: Context,
  client: GatewayClient,
  // 用 **RemoteToolSpec** 而不是内联对象类型。
  //
  // 初版这里是内联的 `{ name; description; parameters; capability }`。我在
  // `gateway.ts` 给接口加了 `requires_idempotency_key` 之后它**没跟着更新**，
  // 于是 `spec.requires_idempotency_key` 报 TS2339。
  //
  // 这类"两处各自描述同一个形状"的问题，正解是**只留一处**：
  // 形状在 `gateway.ts` 定义一次，这里引用它。
  spec: RemoteToolSpec,
  seen: Set<string>,
): void {
  if (seen.has(spec.name)) {
    // 同名工具会让模型的选择变得不确定 —— 显式失败，不覆盖。
    throw new Error(`工具名重复：${spec.name}（来自能力 ${spec.capability}）`)
  }
  seen.add(spec.name)

  const parameters = parameterSpecFromJsonSchema(spec.parameters, spec.name)

  ctx.tools.register(
    defineTool({
      name: spec.name,
      description: spec.description,
      parameters,
      output: {
        schema: { type: 'json' },
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
      },
      async execute(args, exec) {
        // 幂等键的语义：**服务端声明 `idempotent=true` 的能力必须收到它**。
        //
        // 取值优先级：`rootCallId` → `callId` → 本地兜底。
        //
        // 2026-09-15 线上实测（本地不复现）：Harness + qwen-plus 返回的 tool_call
        // **没有 id**，`callId` 被记为 `""`，`rootCallId` 随之也是空串。旧写法
        // `needsKey && rootCallId ? … : undefined` 对空串判假，于是幂等键**被静默
        // 丢掉** —— 请求与响应都成功，只有 header 没发出去，服务端直接拒绝。
        // 后果：48 个 `idempotent=true` 的写能力全部失效，模型只能报告
        // 「系统强制要求 Idempotency-Key 而工具接口不支持」。
        //
        // 本地跑不出来：deepseek 系模型每次调用都带 `call_00_…`，永远走稳定分支。
        // 这就是「本地绿、线上红」的成因。
        //
        // 兜底只保证**写入能落地**；跨重试去重退化为服务端的 request_hash 去重。
        // 上游补上 callId 后自动回到稳定键，不需要再改这里。
        const execIds = exec as { rootCallId?: string; callId?: string }
        const stableCallId = execIds.rootCallId || execIds.callId
        const needsKey = spec.requires_idempotency_key === true
        const idempotencyKey = needsKey
          ? `yuxin:${spec.name}:${stableCallId || nextLocalCallId()}`
          : undefined

        const outcome = await client.callTool(
          spec.name,
          args as Record<string, unknown>,
          {
            ...(exec?.signal ? { signal: exec.signal } : {}),
            ...(idempotencyKey ? { idempotencyKey } : {}),
          },
        )
        return toModelPayload(outcome)
      },
    }),
  )
}

function registerAskUserTool(ctx: Context): void {
  ctx.tools.register(
    defineTool({
      name: 'ask_user',
      description: ASK_USER_DESCRIPTION,
      parameters: {
        question: { type: 'string', required: true, description: '要问用户的问题，简体中文' },
        options: {
          type: 'array',
          items: { type: 'string' },
          description: '可选项列表；确实没有候选项时省略本参数',
        },
        allow_free_text: { type: 'boolean', description: '是否允许用户自由作答，默认 true' },
      },
      output: {
        schema: { type: 'json' },
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
      },
      async execute(args) {
        const question = typeof args.question === 'string' ? args.question : ''
        const options = Array.isArray(args.options) ? args.options : undefined
        const allowFreeText = typeof args.allow_free_text === 'boolean' ? args.allow_free_text : true
        return {
          kind: 'clarification',
          question,
          ...(options ? { options } : {}),
          allow_free_text: allowFreeText,
          note: CLARIFICATION_NOTE,
        }
      },
    }),
  )
}

/**
 * 插件入口。
 *
 * 刻意声明为 `async`：让 `GET /tools` 的失败**在插件加载阶段**冒泡成加载错误。
 * 若改成同步 fire-and-forget，失败只会落进一个无人 await 的 Promise，
 * 表现就是"静默注册 0 个工具"。
 */
export async function apply(ctx: Context, config: FpaBizToolsConfig): Promise<void> {
  scrubCredentialsFromEnv(SCRUBBED_ENV_KEYS)

  // 先在启动期校验地址与令牌：配置错误不应该等到第一次工具调用才暴露。
  const base = resolveGatewayBase(config?.gatewayUrl ?? '')

  const gatewayConfig: GatewayConfig = {
    gatewayUrl: base,
    contextToken: (config?.contextToken ?? '').trim(),
  }
  const client = createGatewayClient(gatewayConfig)

  const catalog = await client.fetchToolCatalog()

  const seen = new Set<string>()
  for (const spec of catalog) {
    registerCatalogTool(ctx, client, spec, seen)
  }
  // 目录里若已有同名工具，registerAskUserTool 之前会先被 seen 命中；
  // 这里显式让位给服务端目录（服务端是权威），但保留 seen 的重复检测。
  if (!seen.has('ask_user')) {
    registerAskUserTool(ctx)
  }
}

export type { JsonLike, ToolOutcome } from './types.js'
