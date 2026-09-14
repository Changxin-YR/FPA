import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import DataTable from '../src/layers/common/ui/DataTable.vue'
import {
  __resetMetaForTest,
  getStatusLabel,
  getStatusTone,
  loadMeta,
  toneForCell,
} from '../src/layers/common/meta/meta.store'
import type { ColumnMeta, ResourceMeta } from '../src/layers/common/types.gen'

/**
 * 用例 4（任务说明 E.4）：**`DataTable` 的 tone 解析与未知状态处理**。
 *
 * 这是对早期版本最严重的"静默失败"类缺陷的回归保护
 * （.local/recon-frontend.md 问题 P-2）：
 *
 * 早期实现 `DataTablePage.vue:91` 是 `column.tones?.[String(row[column.key])]`，
 * 而全仓 20 处 `tones` 声明里有 15 处用**中文标签**当键
 * （`returns/returnModel.ts:43` 的 `{ 草稿:'slate', 待核验:'amber', … }`）。
 * 跨文件复用后必然查不到 → 全部退化为默认灰 → **颜色错也不报错，测试抓不到**。
 *
 * 新设计把"显示值"与"取色依据"分开：
 *   - `ColumnMeta.key`         → 显示哪一列（可能是 `status_label` 中文）
 *   - `ColumnMeta.tone_key`    → 用哪个字段去 `status_dict` 查配色（状态码）
 * 并显式约定：未知状态 → `neutral`，且配色类名**永不为空串**。
 */

function metaPayload(): { capabilities: []; resources: ResourceMeta[] } {
  return {
    capabilities: [],
    resources: [
      {
        name: 'pond',
        title: '塘口',
        list_path: '/api/v1/ponds',
        columns: [
          { key: 'code', label: '塘口编号' },
          { key: 'name', label: '塘口名称' },
          { key: 'status_label', label: '状态', tone_key: 'status' },
        ],
        status_dict: [
          { value: 'build', label: '待建设', tone: 'neutral' },
          { value: 'stocked', label: '已放苗', tone: 'info' },
          { value: 'farming', label: '养殖中', tone: 'success' },
          { value: 'rest', label: '休整', tone: 'warning' },
          { value: 'closed', label: '已结束', tone: 'danger' },
        ],
      },
    ],
  }
}

function stubMetaFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ code: 'OK', message: '', data: metaPayload(), request_id: 'r' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    ),
  )
}

const COLUMNS: ColumnMeta[] = [
  { key: 'code', label: '塘口编号' },
  { key: 'status_label', label: '状态', tone_key: 'status' },
]

beforeEach(() => {
  __resetMetaForTest()
  vi.unstubAllGlobals()
})

describe('status_dict 是状态中文与配色的唯一来源', () => {
  it('已登记状态返回服务端下发的中文与 tone', async () => {
    stubMetaFetch()
    await loadMeta()

    expect(getStatusLabel('pond', 'farming')).toBe('养殖中')
    expect(getStatusTone('pond', 'farming')).toBe('success')
    expect(getStatusTone('pond', 'closed')).toBe('danger')
  })

  it('未登记状态：中文回退为原值（不改写），tone 落到 neutral', async () => {
    stubMetaFetch()
    await loadMeta()

    expect(getStatusLabel('pond', 'brand_new_state')).toBe('brand_new_state')
    expect(getStatusTone('pond', 'brand_new_state')).toBe('neutral')
  })

  it('未知资源名不抛异常，返回 neutral', async () => {
    stubMetaFetch()
    await loadMeta()

    expect(getStatusTone('nonexistent', 'farming')).toBe('neutral')
    expect(getStatusLabel('nonexistent', 'farming')).toBe('farming')
  })
})

describe('toneForCell：用 tone_key 取状态码，而不是用显示值当键', () => {
  it('列声明了 tone_key 时，取色看的是状态码列', async () => {
    stubMetaFetch()
    await loadMeta()

    // 行里 status_label 是中文、status 是状态码
    const row = { code: 'P-001', status_label: '养殖中', status: 'farming' }
    expect(toneForCell('pond', 'status_label', row, 'status')).toBe('success')

    // 反过来：若误用中文当键（早期版本的做法），必然查不到
    expect(toneForCell('pond', 'status_label', row, undefined)).toBe('neutral')
  })

  it('这是早期版本 RETURN_TONES 缺陷的对照断言', async () => {
    stubMetaFetch()
    await loadMeta()

    // 早期版本 `returns/returnModel.ts:43` 的形状：中文当键
    const oldStyleTones: Record<string, string> = {
      草稿: 'slate',
      待核验: 'amber',
      已核验: 'teal',
    }
    // 而取色时用的是状态码 'submitted' —— 键对不上，所以早期实现永远拿不到颜色
    expect(oldStyleTones['submitted']).toBeUndefined()

    // 新实现：状态码进 status_dict，拿到的是新枚举里的 tone
    expect(getStatusTone('pond', 'stocked')).toBe('info')
  })

  it('空值与 null 落到 neutral', () => {
    expect(toneForCell('pond', 'status_label', { status_label: '', status: '' }, 'status')).toBe('neutral')
    expect(toneForCell('pond', 'status_label', {}, 'status')).toBe('neutral')
  })
})

describe('DataTable 渲染', () => {
  it('加载中显示状态文案', () => {
    const wrapper = mount(DataTable, {
      props: { resource: 'pond', columns: COLUMNS, rows: [], loading: true },
    })
    expect(wrapper.get('[role="status"]').text()).toContain('正在加载')
  })

  it('空列表显示 emptyText，不渲染表格', () => {
    const wrapper = mount(DataTable, {
      props: { resource: 'pond', columns: COLUMNS, rows: [], emptyText: '当前授权范围内暂无塘口' },
    })
    expect(wrapper.text()).toContain('当前授权范围内暂无塘口')
    expect(wrapper.find('table').exists()).toBe(false)
  })

  it('状态列显示 status_dict 的中文标签，并带上配色类名（永不为空串）', async () => {
    stubMetaFetch()
    await loadMeta()

    const wrapper = mount(DataTable, {
      props: {
        resource: 'pond',
        columns: COLUMNS,
        rows: [{ id: 1, code: 'P-001', status_label: '养殖中', status: 'farming', allowed_actions: [] }],
      },
    })

    const cells = wrapper.findAll('tbody td')
    expect(cells[0].text()).toBe('P-001')
    expect(cells[1].text()).toBe('养殖中')

    const toneAttr = cells[1].attributes('data-tone')
    expect(toneAttr).toBe('tone-success')
    // 关键：绝不能是空串——早期版本的失败模式就是"配色为空但不报错"
    expect(toneAttr).not.toBe('')
    expect(toneAttr).toBeTruthy()
    expect(['tone-neutral', 'tone-info', 'tone-success', 'tone-warning', 'tone-danger']).toContain(toneAttr)
  })

  it('有中文派生列时不展示原始机器字段', async () => {
    stubMetaFetch()
    await loadMeta()

    const wrapper = mount(DataTable, {
      props: {
        resource: 'pond',
        columns: [
          { key: 'code', label: '编号' },
          { key: 'status', label: '状态码' },
          { key: 'status_label', label: '状态', tone_key: 'status' },
        ],
        rows: [{ id: 1, code: 'P-001', status: 'farming', status_label: '养殖中', allowed_actions: [] }],
      },
    })

    expect(wrapper.find('thead').text()).toContain('状态')
    expect(wrapper.find('thead').text()).not.toContain('状态码')
    expect(wrapper.find('tbody').text()).toContain('养殖中')
    expect(wrapper.find('tbody').text()).not.toContain('farming')
  })

  it('未知状态渲染为 neutral 而非空配色', async () => {
    stubMetaFetch()
    await loadMeta()

    const wrapper = mount(DataTable, {
      props: {
        resource: 'pond',
        columns: COLUMNS,
        rows: [{ id: 1, code: 'P-002', status_label: '???', status: 'unknown_state', allowed_actions: [] }],
      },
    })

    const toneAttr = wrapper.findAll('tbody td')[1].attributes('data-tone')
    expect(toneAttr).toBe('tone-neutral')
  })

  it('行内动作只渲染服务端给的 allowed_actions', async () => {
    stubMetaFetch()
    await loadMeta()

    const wrapper = mount(DataTable, {
      props: {
        resource: 'pond',
        columns: COLUMNS,
        rows: [
          { id: 1, code: 'P-001', status_label: '草稿', status: 'build', allowed_actions: ['view', 'edit'] },
        ],
      },
    })

    const buttons = wrapper.findAll('.data-table__row-action').map((node) => node.text())
    expect(buttons).toEqual(['view', 'edit'])
    // 服务端没给 delete，就绝不能出现
    expect(buttons).not.toContain('delete')
  })

  it('点击行内动作时抛出 action 事件，带动作名与整行数据', async () => {
    stubMetaFetch()
    await loadMeta()

    const row = { id: 1, code: 'P-001', status_label: '草稿', status: 'build', allowed_actions: ['view'] }
    const wrapper = mount(DataTable, { props: { resource: 'pond', columns: COLUMNS, rows: [row] } })
    await wrapper.get('.data-table__row-action').trigger('click')

    expect(wrapper.emitted('action')?.[0]).toEqual(['view', row])
  })

  it('所有行都没有 allowed_actions 时隐藏操作表头和整列', async () => {
    stubMetaFetch()
    await loadMeta()

    const wrapper = mount(DataTable, {
      props: {
        resource: 'pond',
        columns: COLUMNS,
        rows: [
          { id: 1, code: 'P-001', status: 'build' },
          { id: 2, code: 'P-002', status: 'rest', allowed_actions: [] },
        ],
      },
    })

    expect(wrapper.find('thead').text()).not.toContain('操作')
    expect(wrapper.findAll('thead th')).toHaveLength(COLUMNS.length)
    expect(wrapper.findAll('tbody tr')[0].findAll('td')).toHaveLength(COLUMNS.length)
  })

  it('部分行有动作时保留操作列，无动作行保持空白', async () => {
    stubMetaFetch()
    await loadMeta()

    const wrapper = mount(DataTable, {
      props: {
        resource: 'pond',
        columns: COLUMNS,
        rows: [
          { id: 1, code: 'P-001', status: 'build', allowed_actions: ['view'] },
          { id: 2, code: 'P-002', status: 'rest', allowed_actions: [] },
        ],
      },
    })

    expect(wrapper.find('thead').text()).toContain('操作')
    expect(wrapper.findAll('tbody tr')[0].findAll('td')).toHaveLength(COLUMNS.length + 1)
    expect(wrapper.findAll('tbody tr')[1].findAll('td')).toHaveLength(COLUMNS.length + 1)
    expect(wrapper.findAll('tbody tr')[1].find('td:last-child').text()).toBe('')
    expect(wrapper.find('.data-table__no-action').exists()).toBe(false)
  })
})
