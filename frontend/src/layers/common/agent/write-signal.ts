import { ref } from 'vue'

/**
 * 「刚刚发生的**写入**」广播站 —— 智能体写完东西之后，当前列表要能自己刷新。
 *
 * ## 为什么需要它（用户报的缺陷）
 *
 * 智能体是通过 `Harness 子进程 → /api/v1/agent/tools/<n>/call → CapabilityRunner`
 * 写的，那是**后端内部**的写入；而 `ResourceListPage` 只在 `onMounted` /
 * `props.resource` 变化 / 页面自己提交后调 `load()`。两条路径互不知情，
 * 于是「模型说已创建塘口、列表里却没有」，用户必须手动刷新或切走再切回。
 *
 * ## 为什么是「一处发布、一处订阅」，而不是两个组件各写一套
 *
 * 判断「要不要刷新」需要两个事实：**(a) 这一轮到底写了没有**、**(b) 写的是哪个资源**。
 * 两者都只有服务端知道，且都在 `AgentPanel` 手里的那一条 `result` 里。
 * 若 `ResourceListPage` 也去猜（例如盯回复文本里有没有「已创建」），那就是第二处判定
 * 规则 —— 而文本猜测必然被「我**没有**创建成功」这类句子骗到。
 *
 * 所以：**`AgentPanel` 写这个 store，页面读它**。页面自己不算任何东西。
 *
 * ## 为什么不是「每轮都刷新」
 *
 * 那会把纯查询也重拉（「现在有几个塘口」也要多发一次列表请求），更重要的是它会让
 * 「哪些轮真的写了」这个事实变得不可观测。判据只认服务端的 `kind === 'executed'`
 * —— 网关的判别联合里只有它表示「已提交事务」（`docs/WRITE_CONTRACT.md`）。
 */

/**
 * 数据版本号。**只有真的写入才 +1。**
 *
 * 用版本号而不是布尔标记：布尔标记要求订阅方负责清掉，漏清一次之后就再也不会触发
 * （一个只在第一轮生效的刷新）。版本号单调，订阅方只比较「变了没有」。
 */
export const writeVersion = ref(0)

/**
 * 被写入牵动过的资源名（小写）。**只增不减**：一次对话可能写多个资源，
 * 而页面随时可能才挂上来 —— 清空会让「后挂载的页面」漏掉那次写入。
 */
export const dirtyResources = ref<string[]>([])

/**
 * **唯一的写入发布入口。**
 *
 * `resources` 允许为空（服务端说写了、但资源解析不出来）：此时版本号**仍然**推进，
 * 宁可让订阅方按「未知资源」再判一次，也不要静默丢掉「发生过写入」这个事实。
 */
export function publishAgentWrite(resources: readonly (string | undefined | null)[]): void {
  for (const item of resources) {
    const name = (item ?? '').trim().toLowerCase()
    if (name && !dirtyResources.value.includes(name)) dirtyResources.value.push(name)
  }
  writeVersion.value += 1
}

/**
 * 一次 `executed` 结果牵动过的**全部资源**。
 *
 * 顶层的 `result` 只带一条（模型的最后一次写入），多写的情形看 `data.executed`
 * —— 一次对话里真的会写多次（实测：先建往来单位、再建批次）。
 * 两者都拿不到时返回**空数组**：宁可让订阅方按「未知资源」再判一次，
 * 也不要在这里编一个资源名出来。
 *
 * 形状用结构化最小接口而不是 `AgentExecutedResult`：本模块不该 import 协议层，
 * 否则 store ↔ service 会互相依赖。
 */
export function executedResourcesOf(result: {
  kind: string
  result?: {
    resource?: string
    data?: { executed?: { resource?: string }[] }
  }
}): string[] {
  if (result.kind !== 'executed') return []
  const all = result.result?.data?.executed
  if (Array.isArray(all) && all.length) {
    return all.map((item) => String(item?.resource ?? '')).filter(Boolean)
  }
  const single = result.result?.resource
  return single ? [single] : []
}

/**
 * 该资源是否被写入牵动过。
 *
 * 判据用**资源名等值**，不做前缀/模糊匹配：`pond` 与 `pond_status_change` 是真实
 * 并存的两个资源名，`.startsWith()` 会把后者误判成前者。
 */
export function isResourceDirty(resource: string | undefined | null): boolean {
  const name = (resource ?? '').trim().toLowerCase()
  if (!name) return false
  return dirtyResources.value.includes(name)
}

/** 仅供测试：把模块级状态清回初始值（用例之间必须隔离）。 */
export function __resetAgentWriteSignal(): void {
  writeVersion.value = 0
  dirtyResources.value = []
}
