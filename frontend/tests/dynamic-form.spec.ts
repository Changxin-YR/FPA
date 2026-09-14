import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { __resetMetaForTest, loadMeta } from '../src/layers/common/meta/meta.store'
import { __resetClientState } from '../src/layers/common/api/client'
import { mount } from '@vue/test-utils'
import DynamicForm from '../src/layers/common/ui/DynamicForm.vue'
import type { CapabilityField, RefTarget } from '../src/layers/common/types.gen'

/**
 * 用例 3（任务说明 E.3）：**`DynamicForm` 对九种字段类型的渲染**。
 *
 * 九种类型来自 INTERFACES.md §2 的 `CapabilityField.type`：
 * `string | integer | number | boolean | date | datetime | enum | ref | text`。
 * 与后端 `kernel/fields.py::FieldType` 的 9 个取值逐一对应。
 */

function field(
  partial: Partial<CapabilityField> & { key: string; type: CapabilityField['type'] },
): CapabilityField {
  return { label: partial.key, required: false, ...partial } as CapabilityField
}

const ALL_NINE: CapabilityField[] = [
  field({ key: 'name', label: '塘口名称', type: 'string', max_length: 64, placeholder: '如 P-001' }),
  field({ key: 'note', label: '备注', type: 'text' }),
  field({ key: 'capacity_mu', label: '养殖面积（亩）', type: 'number' }),
  field({ key: 'aerator_count', label: '增氧机数量', type: 'integer' }),
  field({ key: 'is_active', label: '是否启用', type: 'boolean' }),
  field({ key: 'build_date', label: '建成日期', type: 'date' }),
  field({ key: 'stocked_at', label: '放苗时间', type: 'datetime' }),
  field({
    key: 'status',
    label: '状态',
    type: 'enum',
    choices: [
      { value: 'build', label: '待建设' },
      { value: 'stocked', label: '已放苗' },
    ],
  }),
  field({
    key: 'area_id',
    label: '所属区域',
    type: 'ref',
    required: true,
    ref: { resource: 'area', label_key: 'name' },
  }),
]

beforeEach(() => {
  __resetClientState()
  __resetMetaForTest()
  vi.unstubAllGlobals()
})

function mountForm(fields: CapabilityField[], initial?: Record<string, unknown>) {
  return mount(DynamicForm, {
    props: { fields, initial },
    global: { stubs: { ActionButton: { template: '<button type="submit"><slot /></button>' } } },
  })
}

describe('DynamicForm：九种字段类型', () => {
  it('为每个字段渲染一个带中文标签的控件', () => {
    const wrapper = mountForm(ALL_NINE)

    // 9 个字段 → 9 个 label，且标签是后端下发的中文
    const labels = wrapper.findAll('.dynamic-form__label').map((node) => node.text())
    expect(labels).toHaveLength(9)
    expect(labels[0]).toContain('塘口名称')
    expect(labels[7]).toContain('状态')

    // 必填标记只出现在 required=true 的字段上
    expect(labels[8]).toContain('*')
    expect(labels[0]).not.toContain('*')
  })

  it('string → type="text"，并带上 maxlength 与 placeholder', () => {
    const wrapper = mountForm([ALL_NINE[0]])
    const input = wrapper.get('input')
    expect(input.attributes('type')).toBe('text')
    expect(input.attributes('maxlength')).toBe('64')
    expect(input.attributes('placeholder')).toBe('如 P-001')
  })

  it('text → <textarea> 且占满整行', () => {
    const wrapper = mountForm([ALL_NINE[1]])
    expect(wrapper.find('textarea').exists()).toBe(true)
    expect(wrapper.find('input').exists()).toBe(false)
    expect(wrapper.get('.dynamic-form__field').classes()).toContain('dynamic-form__field--wide')
  })

  it('number → type="number" 且 step="any"（支持小数金额/数量）', () => {
    const wrapper = mountForm([ALL_NINE[2]])
    const input = wrapper.get('input')
    expect(input.attributes('type')).toBe('number')
    expect(input.attributes('step')).toBe('any')
  })

  it('integer → type="number" 且不带 step', () => {
    const wrapper = mountForm([ALL_NINE[3]])
    const input = wrapper.get('input')
    expect(input.attributes('type')).toBe('number')
    expect(input.attributes('step')).toBeUndefined()
  })

  it('boolean → 复选框，默认值为 false', () => {
    const wrapper = mountForm([ALL_NINE[4]])
    const input = wrapper.find('input[type="checkbox"]')
    expect(input.exists()).toBe(true)
    expect((input.element as HTMLInputElement).checked).toBe(false)
  })

  it('date → type="date"', () => {
    const wrapper = mountForm([ALL_NINE[5]])
    expect(wrapper.get('input').attributes('type')).toBe('date')
  })

  it('datetime → type="datetime-local"', () => {
    const wrapper = mountForm([ALL_NINE[6]])
    expect(wrapper.get('input').attributes('type')).toBe('datetime-local')
  })

  it('enum → <select>，选项来自后端 choices，另有一个空选项', () => {
    const wrapper = mountForm([ALL_NINE[7]])
    const options = wrapper.findAll('option').map((node) => node.text())
    expect(options).toEqual(['请选择', '待建设', '已放苗'])
  })

  it('ref → <select>，候选项由资源列表拉取（请求失败时显式报错而非渲染空下拉）', async () => {
    const wrapper = mountForm([ALL_NINE[8]])
    // 未 stub fetch：jsdom 下 fetch 不存在，组件应把错误显示出来而不是静默空列表
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    // 错误文案来自统一客户端的中文归一化，且被挂在对应字段下
    expect(wrapper.get('[role="alert"]').text()).toBeTruthy()
    expect(wrapper.get('[role="alert"]').text()).not.toContain('Failed to fetch')
  })
})

describe('DynamicForm：校验与请求体转换', () => {
  it('必填项为空时阻止提交并给出中文错误', async () => {
    const wrapper = mountForm(ALL_NINE)
    await wrapper.get('form').trigger('submit')

    const error = wrapper.findAll('.dynamic-form__error').map((node) => node.text())
    expect(error.some((text) => text.includes('请填写所属区域'))).toBe(true)
    expect(wrapper.emitted('submit')).toBeUndefined()
  })

  it('按字段类型转换请求体：integer/number/boolean/ref 转成正确类型', async () => {
    const fields: CapabilityField[] = [
      field({ key: 'code', label: '编号', type: 'string' }),
      field({ key: 'capacity_mu', label: '面积', type: 'number' }),
      field({ key: 'aerator_count', label: '增氧机', type: 'integer' }),
      field({ key: 'is_active', label: '启用', type: 'boolean' }),
      field({ key: 'area_id', label: '区域', type: 'ref' }),
    ]
    const wrapper = mountForm(fields)
    const vm = wrapper.vm as unknown as { setValues: (v: Record<string, unknown>) => void }
    vm.setValues({
      code: '  P-001  ',
      capacity_mu: '12.5',
      aerator_count: '3',
      is_active: true,
      area_id: '7',
    })

    await wrapper.get('form').trigger('submit')
    const emitted = wrapper.emitted('submit')
    expect(emitted).toBeDefined()
    expect(emitted?.[0]?.[0]).toEqual({
      code: 'P-001',
      capacity_mu: 12.5,
      aerator_count: 3,
      is_active: true,
      area_id: 7,
    })
  })

  it('可选项为空时不出现在请求体里', async () => {
    const fields: CapabilityField[] = [
      field({ key: 'code', label: '编号', type: 'string', required: false }),
      field({ key: 'note', label: '备注', type: 'text', required: false }),
    ]
    const wrapper = mountForm(fields)
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('submit')?.[0]?.[0]).toEqual({})
  })

  it('max_length 超限时给出中文错误', async () => {
    const wrapper = mountForm([field({ key: 'code', label: '编号', type: 'string', max_length: 4 })])
    const vm = wrapper.vm as unknown as { setValues: (v: Record<string, unknown>) => void }
    vm.setValues({ code: 'TOO-LONG' })
    await wrapper.get('form').trigger('submit')

    expect(wrapper.get('.dynamic-form__error').text()).toContain('不能超过 4 个字符')
    expect(wrapper.emitted('submit')).toBeUndefined()
  })
})

describe('ref 字段的列表地址取自元数据，不靠拼接', () => {
  const REF_FIELD: CapabilityField = {
    key: 'pond_group_id',
    label: '塘口分组',
    type: 'ref',
    required: false,
    ref: { resource: 'pond-group', label_key: 'name' },
  }

  function stubMeta(listPath: string | null) {
    const resources = listPath
      ? [
          {
            name: 'pond-group',
            title: '塘口分组',
            list_path: listPath,
            columns: [],
            status_dict: [],
          },
        ]
      : []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        if (url.startsWith('/api/v1/meta/capabilities')) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                code: 'OK',
                message: '',
                data: { capabilities: [], resources },
                request_id: 'r',
              }),
              { status: 200, headers: { 'Content-Type': 'application/json' } },
            ),
          )
        }
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'OK',
              message: '',
              data: {
                items: [{ id: 3, name: '东港分组' }],
                page: 1,
                page_size: 100,
                total: 1,
                has_next: false,
              },
              request_id: 'r2',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        )
      }),
    )
  }

  it('用 list_path（不规则复数 /api/v1/pond-groups）而不是拼 /api/v1/pond-groups 的猜测', async () => {
    stubMeta('/api/v1/pond-groups')
    await loadMeta()

    const urls: string[] = []
    const base = globalThis.fetch as unknown as (i: RequestInfo | URL) => Promise<Response>
    vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
      urls.push(String(input))
      return base(input)
    })

    const wrapper = mountForm([REF_FIELD])
    await flushPromises()

    expect(urls.some((url) => url.startsWith('/api/v1/pond-groups?page=1'))).toBe(true)
    // 下拉里出现的是真实候选项，而不是空列表 + 看不出原因
    const options = wrapper.findAll('option').map((n) => n.text())
    expect(options).toContain('东港分组')
  })

  it('候选请求失败时显式报错，不渲染"看起来没有可选项"的空下拉', async () => {
    stubMeta(null)
    await loadMeta()
    // 让候选请求本身失败（网络层）
    const base = globalThis.fetch as unknown as (i: RequestInfo | URL) => Promise<Response>
    vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
      if (String(input).startsWith('/api/v1/meta/')) return base(input)
      return Promise.reject(new TypeError('Failed to fetch'))
    })

    const wrapper = mountForm([REF_FIELD])
    await flushPromises()

    const alert = wrapper.find('[role="alert"]')
    expect(alert.exists()).toBe(true)
    // 文案是统一客户端归一化后的中文，不含英文底层错误
    expect(alert.text()).not.toContain('Failed to fetch')
  })

  it('元数据与字段都没给地址时，退化为约定路径 /api/v1/<resource>s（不作为首选）', async () => {
    stubMeta(null)
    await loadMeta()
    const urls: string[] = []
    const base = globalThis.fetch as unknown as (i: RequestInfo | URL) => Promise<Response>
    vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
      urls.push(String(input))
      return base(input)
    })

    mountForm([REF_FIELD])
    await flushPromises()
    expect(urls.some((u) => u.startsWith('/api/v1/pond-groups?'))).toBe(true)
  })
})

describe('select 的 v-model 绑定（回归：动态选项 + 字符串 value 混用）', () => {
  it('enum 选择后 v-model 真的收到值（空选项必须是 :value=""）', async () => {
    const wrapper = mountForm([
      {
        key: 'status',
        label: '状态',
        type: 'enum',
        required: true,
        choices: [
          { value: 'build', label: '待建设' },
          { value: 'stocked', label: '已放苗' },
        ],
      },
    ])
    await wrapper.get('select').setValue('stocked')
    await flushPromises()
    await wrapper.get('form').trigger('submit')

    // 之前这里会红：<option value=""> 与动态 :value 混用导致 v-model 永远收不到值
    expect(wrapper.emitted('submit')?.[0]?.[0]).toEqual({ status: 'stocked' })
  })

  it('ref 选择后按字段类型转成整数', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'OK',
              message: '',
              data: { items: [{ id: 7, name: '东区' }], page: 1, page_size: 100, total: 1, has_next: false },
              request_id: 'r',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        ),
      ),
    )
    const wrapper = mountForm([
      {
        key: 'area_id',
        label: '所属区域',
        type: 'ref',
        required: true,
        ref: { resource: 'area', label_key: 'name' },
      },
    ])
    await flushPromises()
    await wrapper.get('select').setValue('7')
    await flushPromises()
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('submit')?.[0]?.[0]).toEqual({ area_id: 7 })
  })

  it('ref 候选地址优先用字段带的 list_path（不靠拼接）', async () => {
    const urls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        urls.push(String(input))
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'OK',
              message: '',
              data: { items: [{ id: 1, name: 'x' }], page: 1, page_size: 100, total: 1, has_next: false },
              request_id: 'r',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        )
      }),
    )
    mountForm([
      {
        key: 'pond_group_id',
        label: '塘口分组',
        type: 'ref',
        required: false,
        ref: { resource: 'pond-group', label_key: 'name', list_path: '/api/v1/pond-groups' },
      },
    ])
    await flushPromises()
    expect(urls.some((u) => u.startsWith('/api/v1/pond-groups?'))).toBe(true)
  })
  it('ref.list_path 为空字符串时必须继续回退（不能用 ?? 短路）', async () => {
    const urls: string[] = []
    const base = globalThis.fetch as unknown as (i: RequestInfo | URL) => Promise<Response>
    vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
      urls.push(String(input))
      return base(input)
    })

    mountForm([
      {
        key: 'area_id',
        label: '所属区域',
        type: 'ref',
        required: false,
        // 后端 RefTarget.list_path 默认值就是 ""，不能当成有效地址
        ref: { resource: 'area', label_key: 'name', list_path: '' },
      },
    ])
    await flushPromises()
    // 绝不能请求到 /api/v1/（空路径）
    expect(urls.some((u) => u === '/api/v1/' || u.startsWith('/api/v1/?'))).toBe(false)
  })
})

/**
 * 用例：**第 10 种字段类型 `array`**。
 *
 * 它在 access 域七条能力落地时补进内核（registry §2.4 的 `role_ids` /
 * `scope_ids` / `permission_codes` 是 `integer[]` / `string[]`）。
 *
 * 这一组断言存在的理由与其余类型相同，但多一条：**类型不能撒谎**。
 * 在没有 `array` 之前，这三个字段只能声明成 `string`，于是
 * schema 说是字符串、而服务端心里想的是列表——前端渲染一个单行文本框、
 * Agent 的 tool schema 也写 `string`，请求体到服务端被拒。
 * 所以这里既断渲染形态，也断**请求体里出来的是数组**。
 */
describe('DynamicForm：array 字段（第 10 种类型）', () => {
  const intArray = field({
    key: 'role_ids',
    label: '角色',
    type: 'array',
    required: true,
    items: 'integer',
  })
  const strArrayWithChoices = field({
    key: 'permission_codes',
    label: '权限',
    type: 'array',
    required: false,
    items: 'string',
    choices: [
      { value: 'access.user.list', label: '账号列表' },
      { value: 'audit.log.list', label: '操作日志' },
    ],
  })

  it('没有 choices 时渲染单行文本（逗号分隔），并给出可填提示', () => {
    const wrapper = mountForm([intArray])
    const input = wrapper.get('input')
    expect(input.attributes('type')).toBe('text')
    expect(input.attributes('placeholder')).toContain('逗号')
    expect(wrapper.find('select').exists()).toBe(false)
  })

  it('有 choices 时渲染多选下拉，选项来自后端 choices', () => {
    const wrapper = mountForm([strArrayWithChoices])
    const select = wrapper.get('select')
    expect(select.attributes('multiple')).toBeDefined()
    expect(wrapper.findAll('option').map((node) => node.text())).toEqual(['账号列表', '操作日志'])
  })

  it('逗号分隔输入 → 请求体里是**整数数组**（items=integer）', async () => {
    const wrapper = mountForm([intArray])
    const vm = wrapper.vm as unknown as { setValues: (v: Record<string, unknown>) => void }
    vm.setValues({ role_ids: '1, 2,3' })
    await wrapper.get('form').trigger('submit')
    const emitted = wrapper.emitted('submit')
    expect(emitted).toBeTruthy()
    expect(emitted![0][0]).toEqual({ role_ids: [1, 2, 3] })
  })

  it('逗号分隔输入 → 请求体里是**字符串数组**（items=string）', async () => {
    const wrapper = mountForm([
      field({ key: 'permission_codes', label: '权限', type: 'array', items: 'string' }),
    ])
    const vm = wrapper.vm as unknown as { setValues: (v: Record<string, unknown>) => void }
    vm.setValues({ permission_codes: 'account.list, audit.log.list' })
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('submit')![0][0]).toEqual({
      permission_codes: ['account.list', 'audit.log.list'],
    })
  })

  it('多选下拉的选中值直接成为数组，不再被拼成字符串', async () => {
    const wrapper = mountForm([strArrayWithChoices])
    const vm = wrapper.vm as unknown as { setValues: (v: Record<string, unknown>) => void }
    // 多选下拉的 v-model 收到的就是数组
    vm.setValues({ permission_codes: ['audit.log.list'] })
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('submit')![0][0]).toEqual({
      permission_codes: ['audit.log.list'],
    })
  })

  it('空值不进入请求体（非必填）', async () => {
    const wrapper = mountForm([
      field({ key: 'permission_codes', label: '权限', type: 'array', items: 'string' }),
    ])
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('submit')![0][0]).toEqual({})
  })
})

describe('ref 字段的多态形态（ref.resource_field）', () => {
  /**
   * `cost.entry.create` 的「归属对象」（`target_id`）曾经声明成 `integer` ——
   * 元数据于是告诉前端"这是个整数输入框"，前端**正确地**渲染出一个带上下箭头的
   * 数字框，而用户根本不知道该填哪个 id。用户报的「登记对象不可输入、登记成本失败」
   * 就是它。
   *
   * 修法不在前端加特例，而是让声明能表达"引用哪类对象由另一个字段的取值决定"。
   * 这一组用例钉住四件事：
   *
   * 1. 类型未选 → 下拉禁用 + 说明原因（**不是**一个看起来"系统里没数据"的空下拉，
   *    更不是发一个注定打错的请求）；
   * 2. 选「区域」拉区域列表、切「塘口」拉塘口列表，地址取自 `ResourceMeta.list_path`；
   * 3. 切换类型时**旧的已选值必须被清空** —— 否则提交的是
   *    `target_type=pond, target_id=2` 这种"属于旧类型的 id"；
   * 4. 候选确实为空 / 加载失败时**显式说明**，两种原因分开说。
   */
  const RESOURCES_META = [
    { name: 'area', title: '区域', list_path: '/api/v1/areas', columns: [], status_dict: [] },
    { name: 'pond', title: '塘口', list_path: '/api/v1/ponds', columns: [], status_dict: [] },
  ]

  /** 与后端 `cost.entry.create` 的真实声明同形：`target_id` 是多态 ref。 */
  const POLYMORPHIC: CapabilityField[] = [
    field({
      key: 'target_type',
      label: '归属对象类型',
      type: 'enum',
      choices: [
        { value: 'area', label: '区域' },
        { value: 'pond', label: '塘口' },
      ],
    }),
    field({
      key: 'target_id',
      label: '归属对象',
      type: 'ref',
      // 多态形态：`resource` 是空串，目标资源由 `resource_field` 的取值决定。
      // `dynamic: true` 与后端真实输出一致（fields.py:281 对多态 ref 标 dynamic）。
      dynamic: true,
      ref: { resource: '', label_key: 'name', resource_field: 'target_type' } as RefTarget,
    }),
  ]

  /** 按地址返回候选；缺键 / 空数组表示"该资源确实没有可选项"。返回收到的地址便于断言。 */
  function stubFetch(byPath: Record<string, { id: number; name: string }[]> = {}): string[] {
    const urls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        urls.push(url)
        if (url.startsWith('/api/v1/meta/capabilities')) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                code: 'OK',
                message: '',
                data: { capabilities: [], resources: RESOURCES_META },
                request_id: 'r',
              }),
              { status: 200, headers: { 'Content-Type': 'application/json' } },
            ),
          )
        }
        const path = url.split('?')[0]
        const items = byPath[path] ?? []
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: 'OK',
              message: '',
              data: { items, page: 1, page_size: 100, total: items.length, has_next: false },
              request_id: 'r2',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        )
      }),
    )
    return urls
  }

  function optionTexts(wrapper: ReturnType<typeof mountForm>, key = 'target_id'): string[] {
    return wrapper
      .get(`select#field-${key}`)
      .findAll('option')
      .map((node) => node.text())
  }

  it('类型未选时：渲染成下拉、禁用、说明原因，且不发任何候选请求', async () => {
    const urls = stubFetch()
    await loadMeta()

    const wrapper = mountForm(POLYMORPHIC)
    await flushPromises()

    const select = wrapper.get('select#field-target_id')
    // 关键回归：这里是 <select>，不是用户抱怨的那个数字框
    expect(select.element.tagName).toBe('SELECT')
    expect(select.attributes('disabled')).toBeDefined()
    // 空选项文案要点名"哪个字段还没选"，不能只写"请选择"
    expect(select.text()).toContain('归属对象类型')
    // 并且**显式**说明原因，而不是一个看起来"系统里没有数据"的空下拉
    expect(wrapper.get('[data-testid="ref-needs-target"]').text()).toContain('归属对象类型')
    // 没选类型就不该去猜地址：一个候选请求都不允许发出去
    expect(urls.some((url) => url.startsWith('/api/v1/area'))).toBe(false)
    expect(urls.some((url) => url.startsWith('/api/v1/pond'))).toBe(false)
  })

  it('选「区域」→ 拉区域候选；切「塘口」→ 拉塘口候选（地址取自元数据，不拼复数）', async () => {
    const urls = stubFetch({
      '/api/v1/areas': [{ id: 2, name: '南区' }],
      '/api/v1/ponds': [{ id: 11, name: '东塘' }],
    })
    await loadMeta()

    const wrapper = mountForm(POLYMORPHIC)
    await flushPromises()
    const typeSelect = wrapper.get('select#field-target_type')

    await typeSelect.setValue('area')
    await flushPromises()
    expect(optionTexts(wrapper)).toContain('南区')
    expect(optionTexts(wrapper)).not.toContain('东塘')
    expect(urls.some((url) => url.startsWith('/api/v1/areas?'))).toBe(true)

    await typeSelect.setValue('pond')
    await flushPromises()
    expect(optionTexts(wrapper)).toContain('东塘')
    expect(optionTexts(wrapper)).not.toContain('南区')
    expect(urls.some((url) => url.startsWith('/api/v1/ponds?'))).toBe(true)
    // 不猜复数：`/api/v1/area?` 这种自造地址一次都不许出现
    expect(urls.some((url) => url.startsWith('/api/v1/area?'))).toBe(false)
  })

  it('切换类型时旧的已选值被清空，半给形态不会进请求体', async () => {
    stubFetch({
      '/api/v1/areas': [{ id: 2, name: '南区' }],
      '/api/v1/ponds': [{ id: 11, name: '东塘' }],
    })
    await loadMeta()

    const wrapper = mountForm(POLYMORPHIC)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      values: Record<string, unknown>
      payload: () => Record<string, unknown>
    }

    await wrapper.get('select#field-target_type').setValue('area')
    await flushPromises()
    await wrapper.get('select#field-target_id').setValue('2')
    await flushPromises()
    expect(vm.values.target_id).toBe('2')

    await wrapper.get('select#field-target_type').setValue('pond')
    await flushPromises()

    // ① 旧值被清空（残留会变成"属于旧类型的 id"）
    expect(vm.values.target_id).toBe('')
    // ② 候选已经换成塘口的
    expect(optionTexts(wrapper)).toContain('东塘')
    expect(optionTexts(wrapper)).not.toContain('南区')
    // ③ 于是 `target_type=pond, target_id=2` 这种半给形态进不了请求体
    expect(vm.payload()).not.toHaveProperty('target_id')
  })

  it('候选确实为空时显式说明，不静默给一个空下拉', async () => {
    stubFetch({ '/api/v1/ponds': [] })
    await loadMeta()

    const wrapper = mountForm(POLYMORPHIC)
    await flushPromises()
    await wrapper.get('select#field-target_type').setValue('pond')
    await flushPromises()

    const note = wrapper.get('[data-testid="ref-empty"]').text()
    // 说清楚"是哪个资源下没有"，而不是笼统的"没有可选项"
    expect(note).toContain('pond')
    // 已选类型、请求成功 → 控件本身是可用的（禁用是有原因的，不该一直禁着）
    expect(wrapper.get('select#field-target_id').attributes('disabled')).toBeUndefined()
  })

  it('加载失败时报错，不与「空候选」混为一谈', async () => {
    stubFetch()
    await loadMeta()
    const base = globalThis.fetch as unknown as (input: RequestInfo | URL) => Promise<Response>
    vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
      if (String(input).startsWith('/api/v1/meta/')) return base(input)
      return Promise.reject(new TypeError('Failed to fetch'))
    })

    const wrapper = mountForm(POLYMORPHIC)
    await flushPromises()
    await wrapper.get('select#field-target_type').setValue('area')
    await flushPromises()

    // 失败有失败的文案；不能伪装成"确实没有可选项"
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="ref-empty"]').exists()).toBe(false)
  })

  it('选中后按 ref 的既有口径提交（id 转成整数）', async () => {
    stubFetch({ '/api/v1/ponds': [{ id: 11, name: '东塘' }] })
    await loadMeta()

    const wrapper = mountForm(POLYMORPHIC)
    await flushPromises()
    await wrapper.get('select#field-target_type').setValue('pond')
    await flushPromises()
    await wrapper.get('select#field-target_id').setValue('11')
    await flushPromises()
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('submit')?.[0]?.[0]).toEqual({ target_type: 'pond', target_id: 11 })
  })

  it('静态 ref 不回归：仍然按 ref.resource 拉候选，且不会被当成待选类型', async () => {
    stubFetch({ '/api/v1/areas': [{ id: 2, name: '南区' }] })
    await loadMeta()

    const wrapper = mountForm([
      field({
        key: 'area_id',
        label: '所属区域',
        type: 'ref',
        required: true,
        ref: { resource: 'area', label_key: 'name' },
      }),
    ])
    await flushPromises()

    expect(optionTexts(wrapper, 'area_id')).toContain('南区')
    // 静态形态没有"类型字段"，永远不该出现"请先选择…"这条提示
    expect(wrapper.find('[data-testid="ref-needs-target"]').exists()).toBe(false)
  })
})
