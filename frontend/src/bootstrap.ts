import { createApp, type App as VueApp } from 'vue'
import App from './App.vue'
import './styles/tokens.css'

/**
 * 应用装配。
 *
 * 早期版本把装配直接写在 `main.ts`（4 行副作用式 `createApp(App).use(router).mount('#app')`），
 * 无法被测，覆盖率里永远是 0。这里抽成函数。
 *
 * `router` 用**动态 import**：这样只测 `mountFpaApp` 的用例不会在模块加载期
 * 就把真实 router 建起来。真实 router 创建时会启动初始导航，
 * 在 jsdom（没有后端、没有登录态）里会打出
 * 「Detected a possibly infinite redirection」告警——是测试噪声而非产品缺陷。
 */
export async function mountFpaApp(selector = '#app'): Promise<VueApp<Element>> {
  const { router } = await import('./router')
  const app = createApp(App)
  app.use(router)
  app.mount(selector)
  return app
}
