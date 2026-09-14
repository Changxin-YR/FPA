import { execFileSync } from 'node:child_process'
import { join } from 'node:path'

/**
 * 整轮 E2E 结束后的收尾。
 *
 * ## 为什么必须有它（P2 实测）
 *
 * `create-then-list` / `list-refresh` 各建一条往来单位，`master-data-maintain` 建一个
 * 区域（再归档）—— 三者都**没有清理**：`partner` / `area` 压根没有删除能力。
 * 于是"跑完全绿"之后库会越来越脏（实测往来单位累积到 7 条，而交付文档写 3 条）。
 * 测试必须幂等：**跑完之后的库要和跑之前一样**。
 *
 * 收尾交给 `tools/cleanup_ui_probes.py`（按探针前缀 + "未被业务单据引用"两个判据删，
 * 与仓库其余 e2e 工具的清理口径一致）。它没有删除能力可用，所以只能在这一层收。
 */
export default function globalTeardown(): void {
  // Playwright 的 cwd 是 config 所在目录（`frontend/`），仓库根在上一层。
  const repoRoot = join(process.cwd(), '..')
  // 显式把环境转发给子进程：清理脚本读 `MYSQL_PASSWORD`。子进程本来会继承，
  // 但显式传递让"用户在 shell 里导出的口令"有一处可见的落点；
  // 脚本自身对开发库默认口令也有兜底（见 `tools/cleanup_ui_probes.py`）。
  execFileSync('python', [join('tools', 'cleanup_ui_probes.py')], {
    cwd: repoRoot,
    stdio: 'inherit',
    env: process.env,
  })
}
