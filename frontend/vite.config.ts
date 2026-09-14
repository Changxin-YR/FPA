import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * `base` 必须由 `VITE_PUBLIC_BASE_PATH` 驱动。
 *
 * 早期版本实测缺陷（.local/recon-frontend.md 问题 P-8）：`frontend/dist/index.html`
 * 引用 `/assets/index-BF8LSOQj.js`（根路径），而部署契约是 `/fpa/`
 * （`deploy/nginx-fpa-shared-location.conf:56`、`deploy/deploy.sh:108` 以
 * `VITE_PUBLIC_BASE_PATH=/fpa/` 构建）。照现状直接部署会白屏，而旧 CI 只跑
 * `npm run build`（默认 `base='/'`），永远不会发现。
 *
 * 这里把公开路径变成**显式、可断言的构建输入**：
 * 默认 `/`（本地开发），部署时由 `deploy/` 传 `/fpa/`。
 */
const publicBasePath = process.env.VITE_PUBLIC_BASE_PATH ?? '/'

if (!publicBasePath.startsWith('/') || !publicBasePath.endsWith('/')) {
  throw new Error(`VITE_PUBLIC_BASE_PATH 必须是绝对路径且以 / 结尾，收到 ${JSON.stringify(publicBasePath)}`)
}

export default defineConfig({
  base: publicBasePath,
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5273,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:5101',
        changeOrigin: true,
      },
    },
  },
  build: {
    // 早期版本 src 曾含 38.9 KB 零引用死代码（占 src 字节 12%）。
    // 150 KB 作为本骨架的体积预算，防止再次无声膨胀。
    chunkSizeWarningLimit: 150,
  },
})
