import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

/**
 * 覆盖率必须配门槛。
 *
 * 早期版本实测缺陷（.local/recon-frontend.md 问题 P-16）：前端既没有
 * `@vitest/coverage-v8` 也没有覆盖率脚本，实跑
 * `npx vitest run --coverage.enabled --coverage.provider=v8` 直接报
 * `MISSING DEPENDENCY`；而同一份 CI 对后端却有 `--cov-fail-under=85`。
 * 没有度量就无法判断重写是否真的更好，因此这里把门槛前置。
 */
export default defineConfig({
  plugins: [vue()],
  test: {
    globals: true,
    environment: 'jsdom',
    include: ['tests/**/*.spec.ts'],
    exclude: ['tests/e2e/**', 'node_modules/**'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json-summary'],
      include: ['src/**/*.ts', 'src/**/*.vue'],
      exclude: [
        'src/**/*.d.ts',
        // 生成物，由 tools/gen_frontend_types.py 覆盖，统计它没有意义
        'src/layers/common/types.gen.ts',
      ],
      thresholds: {
        lines: 85,
        functions: 85,
        statements: 85,
        branches: 80,
      },
    },
  },
})
