<script setup lang="ts">
import { computed } from 'vue'
import type { ColumnMeta, Tone } from '../types.gen'
import { displayColumns, toneForCell, useMeta } from '../meta/meta.store'

/**
 * 元数据驱动的列表。列由 `ColumnMeta[]` 决定，配色由 `status_dict` 决定。
 *
 * ## 与早期版本的关键差别（.local/recon-frontend.md 问题 P-2 / P-14）
 *
 * 1. **tone 的键是状态码，不是中文标签。**
 *    早期实现 `DataTablePage.vue:91` 是
 *    `column.tones?.[String(row[column.key])]`，而全仓 20 处 `tones` 声明里
 *    有 15 处用中文当键（如 `returnModel.ts:43` 的 `{ 草稿: 'slate', … }`）。
 *    跨文件复用后必然查不到，结果**静默退化为默认灰色**，且因为"颜色错也不报错"，
 *    测试抓不到。新实现要求列声明 `tone_key` 指出状态码所在列，
 *    配色一律经 `status_dict` 解析。
 * 2. **未知状态显式落到 `neutral`，且有测试断言。**
 *    早期版本的失败模式是"什么都不发生"。这里 `toneForCell` 对空值/未知值
 *    返回 `'neutral'`，`tests/data-table.spec.ts` 断言该行为，
 *    并断言配色类名永不为空串。
 * 3. **不含客户端分页。**
 *    INTERFACES.md §8 规定分页由服务端统一提供；早期版本 `DataTablePage` 同时存在
 *    `serverSide` 与客户端两套逻辑，且 `serverSide=true` 时既不筛选也不分页
 *    （`:50` / `:72`），是潜伏缺陷。新组件只渲染服务端给的一页。
 * 4. **行内动作的中文来自服务端**（`meta.rowActionLabel`）。
 *    本组件原先把 `allowed_actions` 里的 token 直接渲染出来，于是"操作"列
 *    显示 `view` / `archive`，而同一行的状态列是中文（`养殖中`）。
 *    根因是动作词没有标签落点——现在有了（`kernel/workflow.py::ACTION_LABELS`
 *    经 `/meta/capabilities` 的 `actions.row_action_labels` 下发）。
 *    **当前迭代修的是同一件事的另一半**：标签与状态标签遵守同一条纪律，
 *    前端不持有任何动作文案表。
 */

const props = withDefaults(
  defineProps<{
    resource: string
    columns: ColumnMeta[]
    rows: Record<string, unknown>[]
    emptyText?: string
    loading?: boolean
    /** 行主键字段名，默认 `id`。 */
    rowKey?: string
  }>(),
  { emptyText: '暂无数据', loading: false, rowKey: 'id' },
)

const emit = defineEmits<{
  action: [name: string, row: Record<string, unknown>]
}>()

const meta = useMeta()

const allowedTones: readonly Tone[] = ['neutral', 'info', 'success', 'warning', 'danger']

/** 有中文派生列时隐藏机器字段，避免把状态码/枚举码直接展示给用户（规则见 `displayColumns`）。 */
const visibleColumns = computed(() => displayColumns(props.columns))

function toneClass(column: ColumnMeta, row: Record<string, unknown>): string {
  const tone = toneForCell(props.resource, column.key, row, column.tone_key)
  // 双保险：即使 status_dict 未来给出非法 tone，也不产生空类名。
  const safe = allowedTones.includes(tone) ? tone : 'neutral'
  return `tone-${safe}`
}

/** 单元格显示文本。`status_dict` 里登记过的状态列优先取中文标签。 */
function cellText(column: ColumnMeta, row: Record<string, unknown>): string {
  const raw = row[column.key]
  if (raw === null || raw === undefined || raw === '') return '—'
  if (column.tone_key !== undefined) {
    const code = row[column.tone_key]
    if (code !== null && code !== undefined && code !== '') {
      return meta.getStatusLabel(props.resource, String(code))
    }
  }
  return String(raw)
}

/**
 * 该列是否应按数字右对齐。
 *
 * ## 为什么用"值的形态"而不是元数据
 * `ColumnMeta` 只有 `key`/`label`/`tone_key`，**没有类型**。为了不改契约就拿到
 * 右对齐，这里按值判定，且**刻意保守**：
 *   * 只认"带小数点"的数值串（`100.000` / `100.00`）—— 因为编号 `02` 与日期
 *     `2026-09-02` 也会命中朴素的 `\d` 判定，把它们右对齐是错的；
 *   * 只要该列出现过**一个**非数字值，就整列不右对齐（列内必须一致）。
 * 若将来元数据补上列类型，应当改用类型并删掉这个启发式 —— 那时它是第二处判据。
 */
function isNumericColumn(column: ColumnMeta): boolean {
  let seen = 0
  for (const row of props.rows) {
    const value = row[column.key]
    if (value === null || value === undefined || value === '') continue
    seen += 1
    if (typeof value === 'number') continue
    if (typeof value === 'string' && /^-?\d+\.\d+$/.test(value)) continue
    return false
  }
  return seen > 0
}

function actionsOf(row: Record<string, unknown>): string[] {
  const value = row.allowed_actions
  return Array.isArray(value) ? (value as string[]) : []
}

const hasActions = computed(() => props.rows.some((row) => actionsOf(row).length > 0))

const rowId = (row: Record<string, unknown>): string => String(row[props.rowKey] ?? JSON.stringify(row))

const isEmpty = computed(() => !props.loading && props.rows.length === 0)

defineExpose({ toneClass, cellText })
</script>

<template>
  <div class="data-table" data-testid="data-table">
    <p v-if="loading" class="data-table__state" role="status">正在加载…</p>
    <p v-else-if="isEmpty" class="data-table__state">{{ emptyText }}</p>

    <div v-else class="data-table__scroll">
      <table>
        <thead>
          <tr>
            <th v-for="column in visibleColumns" :key="column.key" scope="col">{{ column.label }}</th>
            <th v-if="hasActions" scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="rowId(row)">
            <td
              v-for="column in visibleColumns"
              :key="column.key"
              :class="[
                column.tone_key !== undefined ? toneClass(column, row) : '',
                isNumericColumn(column) ? 'data-table__cell--num' : '',
              ]"
              :data-tone="column.tone_key !== undefined ? toneClass(column, row) : undefined"
            >
              <span v-if="column.tone_key !== undefined" class="status-pill">
                {{ cellText(column, row) }}
              </span>
              <template v-else>{{ cellText(column, row) }}</template>
            </td>
            <td v-if="hasActions">
              <button
                v-for="action in actionsOf(row)"
                :key="action"
                type="button"
                class="data-table__row-action"
                :data-testid="`row-action-${action}`"
                @click="emit('action', action, row)"
              >
                {{ meta.rowActionLabel(action) }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
/* 表格：**只用行间发丝线分割**，不用通体边框；表头吸顶；行可 hover。 */
.data-table {
  /* ★ `min-width: 0` 是**必须**的：本组件是 `.resource-page`（display:grid）的子项，
     而 Grid/Flex 子项的 `min-width` 默认是 `auto` —— 它会无视内部 `overflow-x: auto`
     撑到内容宽度，于是宽表格把**整页**顶出横向滚动条（实测 390px 视口下 27 个资源页
     有 22 个整页横向溢出，采购单页多出约 750px）。加上它，横向滚动被限制在
     `.data-table__scroll` 之内。 */
  min-width: 0;
  border: 1px solid var(--tone-line);
  border-radius: var(--tone-radius);
  background: var(--tone-surface);
  box-shadow: var(--tone-shadow-card);
}
.data-table__scroll {
  overflow-x: auto;
}
.data-table table {
  width: 100%;
  border-collapse: collapse;
}
.data-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 10px 16px;
  border-bottom: 1px solid var(--tone-line);
  color: var(--tone-ink-soft);
  background: var(--tone-surface-soft);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-align: left;
  white-space: nowrap;
}
.data-table td {
  height: 44px;
  padding: 0 16px;
  border-bottom: 1px solid var(--tone-line);
  color: var(--tone-ink-soft);
  font-size: 14px;
  text-align: left;
  vertical-align: middle;
}
.data-table tbody tr:last-child td {
  border-bottom: 0;
}
.data-table tbody tr {
  transition: background-color var(--tone-motion);
}
.data-table tbody tr:hover {
  background: var(--tone-row-hover);
}
/* 首列是这一行的"身份"：给它墨色与字重，让眼睛有落点。
   先前所有单元格同色同字号，是"看不出层级"的主要来源。 */
.data-table td:first-child {
  color: var(--tone-ink);
  font-weight: 500;
}
/* 数字列右对齐。注意：必须配合 tokens.css 里的 `font-variant-numeric: tabular-nums`，
   否则等宽对不上，右对齐也看不出对齐。 */
.data-table__cell--num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.data-table__state {
  padding: 40px 0;
  color: var(--tone-muted);
  font-size: 13px;
  text-align: center;
}
.data-table__row-action {
  height: 28px;
  margin-right: 6px;
  padding: 0 10px;
  border: 1px solid var(--tone-line);
  border-radius: var(--tone-radius-sm);
  color: var(--tone-ink-soft);
  background: var(--tone-surface);
  font-size: 12.5px;
  cursor: pointer;
  transition:
    border-color var(--tone-motion),
    color var(--tone-motion);
}
.data-table__row-action:hover {
  border-color: var(--tone-primary);
  color: var(--tone-primary);
}
/* 状态胶囊：淡底 + 同色系深字。
   底色与字色**由元数据的 tone 驱动**（`tone-*` 类挂在 td 上），
   前端**不持有**"状态→颜色"的映射表 —— 那会是第二处判据。
   先前 `tone-*` 只改 `color:`、没有底色，所以状态列看起来像纯黑文字。 */
.status-pill {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: var(--tone-radius-sm);
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
}
.tone-neutral .status-pill {
  color: var(--tone-neutral);
  background: var(--tone-neutral-soft);
}
.tone-info .status-pill {
  color: var(--tone-info);
  background: var(--tone-info-soft);
}
.tone-success .status-pill {
  color: var(--tone-success);
  background: var(--tone-success-soft);
}
.tone-warning .status-pill {
  color: var(--tone-warning);
  background: var(--tone-warning-soft);
}
.tone-danger .status-pill {
  color: var(--tone-danger);
  background: var(--tone-danger-soft);
}
</style>
