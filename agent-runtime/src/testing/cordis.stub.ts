/**
 * `@deepseek-ai/cordis` 的测试替身。
 *
 * 插件只把 `Context` 当**类型**用（`import type`，编译后会被完全擦除），
 * 因此运行时并不需要真正的 cordis。这里保留一个最小文件，
 * 使 `vitest.config.ts` 的 alias 始终指向一个存在的模块。
 */
export type Context = {
  readonly tools: { register(definition: unknown): () => void }
}
