/**
 * 测试环境下的 `@deepseek-ai/dsh-tools` 替身 **（仅测试用）**。
 *
 * 为什么需要它：本包把 Harness 的两个包声明为 `peerDependencies`（运行时由 Harness 提供）。
 * npm 上的 `@deepseek-ai/dsh-tools` 是 `0.0.1-rc.1`，而本地 checkout 是 `0.1.2-alpha.5` ——
 * 版本不一致，装了反而会掩盖真实差异。所以测试环境用一个**语义等价**的最小实现。
 *
 * **类型不受此文件影响**：`tsconfig.json` 的 `paths` 指向本地 checkout 里真实的
 * `lib/types/index.d.ts`，`tsc --noEmit` 校验的是真 API。
 * `tests/harness-api.spec.ts` 另有断言把本替身与真实产物对齐，防止漂移。
 *
 * 语义对齐依据（`packages/core/tools/lib/index.js:837-868`）：
 *   - `timeoutMs` 必须是正的有限数，否则抛错；
 *   - `parameters`（紧凑 spec）会被投影成标准 JSON Schema 挂在返回的 `parameters` 上；
 *   - `execute` 在调用前做参数校验。
 * 这里实现了前两条与第三条的**结构**校验，足够覆盖插件的可测行为。
 */
import { parameterSpecToJsonSchema } from '../schema.js'

export interface StubToolDefinition {
  readonly name: string
  readonly description: string
  readonly parameters: Record<string, unknown>
  readonly output: Record<string, unknown>
  readonly timeoutMs?: number
  execute(args: Record<string, unknown>, exec: unknown): Promise<unknown>
}

interface StubOptions {
  name: string
  description: string
  parameters: Record<string, unknown>
  output: Record<string, unknown>
  timeoutMs?: number
  execute(args: Record<string, unknown>, exec: unknown): Promise<unknown>
}

export function defineTool(options: StubOptions): StubToolDefinition {
  if (
    options.timeoutMs !== undefined &&
    (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0)
  ) {
    throw new Error(`defineTool(${options.name}): timeoutMs must be a positive finite number`)
  }
  const parameters = parameterSpecToJsonSchema(options.parameters as never)
  const definition: StubToolDefinition = {
    name: options.name,
    description: options.description,
    parameters,
    output: options.output,
    ...(options.timeoutMs !== undefined ? { timeoutMs: options.timeoutMs } : {}),
    async execute(args, exec) {
      return options.execute(args, exec)
    },
  }
  return definition
}

/** 与真实包同名导出，避免 import 面不一致。 */
export function parameterSchemaSpecToJsonSchema(spec: unknown): Record<string, unknown> {
  return parameterSpecToJsonSchema(spec as never)
}
