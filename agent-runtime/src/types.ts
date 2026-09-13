/**
 * 插件内共享的类型。
 *
 * `ToolOutcome` 是 `docs/WRITE_CONTRACT.md` 规则 1 的判别联合在插件侧的镜像：
 * `executed`（真的写了）与 `confirmation_required`（**什么都没写**）在类型上互斥。
 * 插件是**搬运工**：只读 `kind` 决定怎么转交，绝不改写 `kind`。
 */

/**
 * 交给模型的值必须是无损 JSON。
 *
 * Harness 的 `output.schema` 约束 `execute` 的返回类型；`{ type: 'json' }` 对应的
 * `JsonValue` 定义在 `@deepseek-ai/dsh-util-values`，本包只在 peer 里依赖 Harness、
 * 不直接依赖那个内部包，因此在这里声明一个**结构等价**的类型，把边界收在插件内。
 */
export type JsonLike =
  | null
  | boolean
  | number
  | string
  | JsonLike[]
  | { [key: string]: JsonLike }

export interface GatewayConfig {
  readonly gatewayUrl: string
  readonly contextToken: string
}

export interface ExecutedOutcome {
  readonly kind: 'executed'
  readonly message?: string
  readonly resource_id?: number
  readonly data?: JsonLike
}

export interface ConfirmationOutcome {
  readonly kind: 'confirmation_required'
  readonly message?: string
  readonly confirmation?: JsonLike
}

export interface ReadOutcome {
  readonly kind: 'read'
  readonly message?: string
  readonly data?: JsonLike
}

export interface FailedOutcome {
  readonly kind: 'failed'
  readonly code: string
  readonly message: string
}

export type ToolOutcome = ExecutedOutcome | ConfirmationOutcome | ReadOutcome | FailedOutcome
