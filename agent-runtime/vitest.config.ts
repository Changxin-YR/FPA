import { defineConfig } from 'vitest/config'
import { fileURLToPath } from 'node:url'

/**
 * 测试只替换 **一个** 模块：`@deepseek-ai/dsh-tools`。
 *
 * 为什么需要替换：本包把 harness 的两个包声明为 `peerDependencies`（它们由 Harness 运行时
 * 提供，不在 npm 上以同一版本发布——npm 上是 `0.0.1-rc.1`，本地 checkout 是
 * `0.1.2-alpha.5`）。测试环境里没有 Harness 运行时，因此用 `src/testing/dsh-tools.stub.ts`
 * 提供**语义等价**的 `defineTool`（含紧凑参数 → 标准 JSON Schema 的投影）。
 *
 * 类型解析不走这里——`tsconfig.json` 的 `paths` 直指本地 checkout 里**真实的**
 * `lib/types/index.d.ts`，所以 `tsc --noEmit` 校验的是真 API，不是 stub。
 * `tests/harness-api.spec.ts` 另外把 stub 与真实产物做一致性断言，防止漂移。
 */
export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.spec.ts'],
  },
  resolve: {
    alias: {
      '@deepseek-ai/dsh-tools': fileURLToPath(
        new URL('./src/testing/dsh-tools.stub.ts', import.meta.url),
      ),
      '@deepseek-ai/cordis': fileURLToPath(
        new URL('./src/testing/cordis.stub.ts', import.meta.url),
      ),
    },
  },
})
