<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import { errorText } from '../api/errors'
import { useMeta } from '../meta/meta.store'
import type { CapabilityField } from '../types.gen'
import type { Page } from '../api/models'
import ActionButton from './ActionButton.vue'

/**
 * 元数据驱动的表单。**零硬编码字段名**——INTERFACES.md §2 约束 1。
 *
 * 早期版本实测缺陷：25 个薄页面把 `:fields="[{ key:'material_id', label:'饲料物料 ID' }]"`
 * 手写在 template 里（如 `layers/product/daily-farming/FeedPlanPage.vue` 9 行全是
 * 模板 props），与后端字段白名单各写一遍并已漂移（前端用 `material_name`，
 * 契约是 `material_id`）。新实现从 `CapabilityField[]` 渲染，前端物理上没有字段名可写。
 *
 * 支持 INTERFACES.md §2 的 9 种类型：string | text | integer | number | boolean |
 * date | datetime | enum | ref。
 */

const props = withDefaults(
  defineProps<{
    fields: CapabilityField[]
    /** 编辑模式下用现有值初始化。 */
    // 可选：新建场景不传，表单按字段声明的 default 初始化
    // eslint-disable-next-line vue/require-default-prop
    initial?: Record<string, unknown>
    submitLabel?: string
    busy?: boolean
  }>(),
  { submitLabel: '保存', busy: false },
)

const emit = defineEmits<{
  submit: [payload: Record<string, unknown>]
}>()

/** 元数据仓库：
ref 字段的列表地址取自它的 ResourceMeta.list_path。 */
const meta = useMeta()

/** 表单值。`ref` 类型存 id，`boolean` 存布尔，其余按控件语义存字符串/数字。 */
const values = ref<Record<string, unknown>>({})
const errors = ref<Record<string, string>>({})

/** `ref` 类型字段的候选项：`字段 key -> 候选项数组`。 */
const refOptions = ref<Record<string, { value: string; label: string }[]>>({})
const refLoading = ref<Record<string, boolean>>({})
const refError = ref<Record<string, string>>({})
/**
 * 每个 `ref` 字段**实际**解析出的目标资源名（多态字段随取值变化）。
 *
 * 单独存一份而不是在渲染时再算一次：候选为空时的提示文案要用它，
 * 而渲染期再算一次就会与"候选是按哪个资源拉的"分叉——那正是本项目反复
 * 处理的"两处描述同一件事"。
 */
const refTargets = ref<Record<string, string>>({})

/**
 * 多态 ref 的"类型还没选"状态。
 *
 * 模板用它决定：禁用下拉 + 用提示代替 `请选择`（空下拉会让用户以为
 * "系统里确实没有可选项"，而真实原因是**他还没选类型**）。
 */
function needsTargetType(field: CapabilityField): boolean {
  return Boolean(field.ref?.resource_field) && !refTargets.value[field.key]
}

/** 多态 ref 依赖的那个"类型字段"的中文标签（用于提示文案，不硬编码）。 */
function sourceFieldLabel(field: CapabilityField): string {
  const key = field.ref?.resource_field
  if (!key) return ''
  const source = props.fields.find((item) => item.key === key)
  return source?.label ?? key
}

/** `ref` 下拉空选项的文案：三态（待选类型 / 加载中 / 请选择）。 */
function refPlaceholder(field: CapabilityField): string {
  if (needsTargetType(field)) return `请先选择${sourceFieldLabel(field)}`
  if (refLoading.value[field.key]) return '加载中…'
  return '请选择'
}

/**
 * 候选项真的为空时的说明。**必须显式说出来**，不能静默给一个空下拉。
 *
 * 只在"已选类型、请求成功、确实一个候选也没有"时给出——加载中、加载失败、
 * 未选类型各有各的文案。四种状态混成一句"没有可选项"会把原因也一起藏起来。
 */
function emptyRefNote(field: CapabilityField): string {
  if (needsTargetType(field)) return ''
  if (refLoading.value[field.key]) return ''
  if (refError.value[field.key]) return ''
  // 没进过 `loadRefOptions` 的字段（`refOptions` 里没有键）不代表"空"，
  // 它只是还没开始加载——不能替它下结论。
  if (!(field.key in refOptions.value)) return ''
  if ((refOptions.value[field.key] ?? []).length > 0) return ''
  const resource = refTargets.value[field.key]
  return resource ? `「${resource}」下暂无可选项` : '暂无可选项'
}

function isBlank(value: unknown): boolean {
  return value === null || value === undefined || value === ''
}

function reset(): void {
  const next: Record<string, unknown> = {}
  for (const field of props.fields) {
    const initialValue = props.initial?.[field.key]
    if (initialValue !== undefined) next[field.key] = initialValue
    else if (field.default !== undefined) next[field.key] = field.default
    else if (field.type === 'boolean') next[field.key] = false
    else if (field.type === 'array' && (field.ref || (field.choices?.length ?? 0) > 0)) {
      // 多选下拉的 v-model **必须**是数组：Vue 3 在 `<select multiple>` 上绑到
      // `undefined` 会打印 `expects an Array or Set value for its binding`，
      // 而绑到字符串会让"选中第一项"变成"选中它包含的每一个字符"。
      // 只有**多选下拉**这一支需要初值；逗号分隔输入那一支保持字符串（与其余
      // 单值控件同形），转换交给 `payload()`。
      next[field.key] = []
    } else next[field.key] = ''
  }
  values.value = next
  errors.value = {}
}

/**
 * 解析一个 `ref` 字段**当前**指向的资源名。
 *
 * 两种形态（见 `RefTarget`）：
 *   * 静态：`resource` 就是答案；
 *   * **多态**：`resource_field` 给出"本能力另一个字段的 key"，**那个字段的当前
 *     取值**才是资源名（`cost.entry.create` 的「归属对象」就是这一种：选"区域"
 *     就指向 `area` 资源、选"塘口"就指向 `pond` 资源）。
 *
 * `undefined` 表示"还没选类型"——这不是错误，是"还没有目标"。调用方据此把下拉
 * 禁用并说明原因，而不是拿空资源名去拼出 `/api/v1/s` 这种地址。
 *
 * **这里没有映射表**：取值到资源的对应关系只有后端一处（`ResourceRegistry`
 * 的资源名与 `target_type` 的枚举值同属一个命名空间）。前端写一张
 * `{area: …}` 就等于把同一件事描述第二遍，后端加一个取值时必然漂移。
 */
function resolveRefResource(field: CapabilityField): string | undefined {
  const target = field.ref
  if (!target) return undefined
  if (!target.resource_field) return target.resource
  const chosen = values.value[target.resource_field]
  const name = chosen === null || chosen === undefined ? '' : String(chosen).trim()
  return name === '' ? undefined : name
}

/**
 * 类型字段变化时的动作：**清空**旧的已选值 + 重拉候选。
 *
 * 两件事必须一起做。只重拉不清空的话，用户会留下一个**属于旧类型的 id**
 * （在"区域"里选了 2 号，再切到"塘口"，提交的就是
 * `target_type=pond, target_id=2`）——那要么是一个不存在的对象，要么在塘口表里
 * 恰好撞上另一条无关记录。这个判断**不能**推给服务端：服务端只能报"对象不存在"，
 * 而用户从界面上看不出自己哪一步做错了。
 */
function applyRefTargetChange(sourceKey: string): void {
  const dependents = dependentRefFields.value.get(sourceKey)
  if (!dependents) return
  for (const field of dependents) {
    values.value[field.key] = ''
    delete errors.value[field.key]
    void loadRefOptions(field)
  }
}

/**
 * 拉一个 `ref` 字段的候选项。
 *
 * 列表地址取自元数据的 `ResourceMeta.list_path`（不拼接）；取不到时**显式报错**，
 * 而不是渲染空下拉让用户以为「确实没有可选项」。
 * （早期版本 `DataTablePage` 的筛选控件在 `serverSide` 模式下「什么都不发生」是同类静默失败。）
 */
async function loadRefOptions(field: CapabilityField): Promise<void> {
  const target = field.ref
  if (!target) return

  // 多态字段：先按当前取值解析目标资源。还没选类型就**不发请求**、也不报错，
  // 清空状态后交给模板说明"请先选择××××"——空下拉与"加载中"都会骗人。
  const resourceName = resolveRefResource(field)
  refTargets.value[field.key] = resourceName ?? ''
  if (resourceName === undefined) {
    refOptions.value[field.key] = []
    refError.value[field.key] = ''
    refLoading.value[field.key] = false
    return
  }

  refLoading.value[field.key] = true
  refError.value[field.key] = ''
  try {
    // 列表地址一律取自元数据的 ResourceMeta.list_path，绝不拼接。
    //
    // 第一版这里写的是 `/api/v1/${target.resource}s`（自己拼个 "s"）——那是个猜测，
    // 而 frozen registry 的资源名含 `pond-groups`、`daily-operations` 这类不规则复数
    // （GET /api/v1/pond-groups），拼接必然打错地址。元数据已经把 list_path 给出来了，
    // 猜它等于把「渲染而非声明」的原则又违背一次。
    // 列表地址的取法，按可靠性从高到低：
    //   ① 字段自己带来的 ref.list_path（INTERFACES.md §9 承诺的形态）
    //   ② 元数据 resources[].list_path（按资源名查）
    //   ③ 约定路径 /api/v1/<resource>s
    // 前两者是**声明**；第三个是退路，只在元数据尚未加载时用（例如表单先于
    // meta 渲染），成功后不会走到这里。取不到就报错，不渲染空下拉。
    // 注意用 `||` 而不是 `??`：后端 `RefTarget.list_path` 的默认值是空字符串 "",
    // 而 `??` 只在 null/undefined 时短路——空串会一路传下去，最终请求到 `/api/v1/`。
    // 空串与未提供在语义上没有区别，都必须落到下一级。
    const listPath =
      target.list_path ||
      meta.resourcesByName.value.get(resourceName)?.list_path ||
      `/api/v1/${resourceName}s`
    const separator = listPath.includes('?') ? '&' : '?'
    const page = await api.get<Page<Record<string, unknown>>>(`${listPath}${separator}page=1&page_size=100`)
    refOptions.value[field.key] = page.items.map((item) => ({
      value: String(item.id ?? ''),
      label: String(item[target.label_key] ?? item.id ?? ''),
    }))
  } catch (caught) {
    refOptions.value[field.key] = []
    refError.value[field.key] = errorText(caught, `无法加载「${field.label}」的可选项`)
  } finally {
    refLoading.value[field.key] = false
  }
}

const refFields = computed(() =>
  props.fields.filter((field) => field.type === 'ref' || (field.type === 'array' && field.ref)),
)

/** 依赖"类型字段"的 ref 字段：`类型字段 key -> 依赖它的 ref 字段[]`。 */
const dependentRefFields = computed(() => {
  const grouped = new Map<string, CapabilityField[]>()
  for (const field of refFields.value) {
    const source = field.ref?.resource_field
    if (!source) continue
    const bucket = grouped.get(source) ?? []
    bucket.push(field)
    grouped.set(source, bucket)
  }
  return grouped
})

// **同步**先初始化一次。
//
// 为什么不能只放在 `onMounted` 里：Vue 的挂载顺序是"先渲染、后跑 mounted 钩子"，
// 于是**首次渲染时 `values` 还是空对象**。对 `<select multiple v-model>` 来说那一帧
// 的绑定值是 `undefined`，Vue 会打印
// `expects an Array or Set value for its binding`；更危险的是若绑到字符串，
// "选中第一项"会变成"选中它包含的每一个字符"（与 `dynamic-form.spec.ts` 里
// `select 的 v-model` 那组回归用例是同一类问题）。
//
// 属性初始化在 setup 里是安全的：此刻 `props`（含 `fields` / `initial`）已经就绪。
// `onMounted` 里的那一次**保留**——它覆盖"挂载后 `fields` 才被父组件填上"的场景。
reset()

onMounted(() => {
  reset()
  for (const field of refFields.value) void loadRefOptions(field)
})

// 字段声明变化（例如切换资源）时重建表单
watch(() => props.fields, reset)

// 多态 ref：监听"类型字段"的取值，切换时清空并重拉依赖它的字段。
//
// 用 `watch` 而不是在模板上挂 `@change`：类型字段不一定渲染成 <select>（声明期
// 并不知道），也可能被 `setValues()` 程序化赋值。盯**值**比盯某个具体控件可靠。
// 不开 `immediate`——初次加载由上面的 `onMounted` 负责，开了会让每个依赖字段
// 被拉两次。
for (const sourceKey of dependentRefFields.value.keys()) {
  watch(
    () => values.value[sourceKey],
    (next, previous) => {
      if (String(next ?? '') === String(previous ?? '')) return
      applyRefTargetChange(sourceKey)
    },
  )
}

function validate(): boolean {
  const next: Record<string, string> = {}
  for (const field of props.fields) {
    const value = values.value[field.key]
    if (field.required && isBlank(value) && field.type !== 'boolean') {
      next[field.key] = `请填写${field.label}`
      continue
    }
    if (field.max_length && typeof value === 'string' && value.length > field.max_length) {
      next[field.key] = `${field.label}不能超过 ${field.max_length} 个字符`
    }
  }
  errors.value = next
  return Object.keys(next).length === 0
}

/**
 * 把 `array` 字段的控件值拆成数组。
 *
 * 两种控件形态各有一条路径（见模板里的 `field.type === 'array'` 分支）：
 *   * 有 `choices`（如权限码）→ 多选下拉，值已经是数组；
 *   * 没有 `choices`（如角色 / 数据范围 id）→ 逗号分隔输入，这里按逗号拆。
 *
 * **不去重、不判空**：`role_ids: [1,1]` 是服务端该拒的输入（服务端会去重后
 * 判定"至少一个"），前端替它决定等于把一条业务规则复制到第二处。
 */
function splitArray(value: unknown, field: CapabilityField): unknown[] {
  const raw = String(value ?? '')
  const parts = raw
    .split(',')
    .map((item) => item.trim())
    .filter((item) => item !== '')
  return field.items === 'integer' ? parts.map((item) => Number.parseInt(item, 10)) : parts
}

/** 按字段类型把控件原始值转成请求体值。 */
function payload(): Record<string, unknown> {
  const result: Record<string, unknown> = {}
  for (const field of props.fields) {
    const value = values.value[field.key]
    if (isBlank(value) && !field.required) continue
    switch (field.type) {
      case 'integer':
        result[field.key] = Number.parseInt(String(value), 10)
        break
      case 'number':
        result[field.key] = Number(value)
        break
      case 'ref':
        result[field.key] = Number(value)
        break
      case 'boolean':
        result[field.key] = Boolean(value)
        break
      case 'array':
        /*
         * 数组字段的值在控件里是**字符串**（多选下拉的选中值、或逗号分隔输入），
         * 这里按 `field.items` 转成服务端要的数组。
         *
         * 为什么必须在**客户端**转对：服务端的 `_coerce` 对 `array` 会检查
         * "是不是数组"，传一个字符串过去会被拒（`必须是数组`）——那是对的，
         * 但错误出现在提交之后，而用户在页面上看不出自己填错了什么。
         * 类型转换的落点是 `payload()`（与 integer/number/ref 同一处）。
         */
        result[field.key] = Array.isArray(value) ? value : splitArray(value, field)
        break
      default:
        result[field.key] = typeof value === 'string' ? value.trim() : value
    }
  }
  return result
}

function onSubmit(): void {
  if (!validate()) return
  emit('submit', payload())
}

/** 仅测试/父组件使用：程序化填值。 */
function setValues(next: Record<string, unknown>): void {
  values.value = { ...values.value, ...next }
}

defineExpose({ values, errors, validate, payload, setValues, reset })
</script>

<template>
  <form class="dynamic-form dynamic-form__grid" novalidate @submit.prevent="onSubmit">
    <div
      v-for="field in fields"
      :key="field.key"
      class="dynamic-form__field"
      :class="{
        'dynamic-form__field--wide': field.type === 'text',
        'dynamic-form__field--readonly': field.readonly === true,
      }"
      :data-readonly="field.readonly === true ? 'true' : undefined"
    >
      <label :for="`field-${field.key}`" class="dynamic-form__label">
        {{ field.label }}<span v-if="field.required" aria-hidden="true"> *</span>
      </label>

      <!--
      readonly（INTERFACES.md §2）：字段出现在响应与表单里，但不接受客户端提交。
      每个控件用 :disabled="field.readonly === true" 禁用，并在下方说明原因，
      让用户看得见自己的数据范围落在哪里。
      -->
      <!-- text：多行 -->
      <textarea
        v-if="field.type === 'text'"
        :id="`field-${field.key}`"
        :name="field.key"
        v-model="values[field.key] as string"
        :disabled="field.readonly === true"
        class="dynamic-form__control"
        rows="3"
        :maxlength="field.max_length"
        :placeholder="field.placeholder"
        :aria-invalid="Boolean(errors[field.key])"
      />

      <!-- boolean：复选框 -->
      <input
        v-else-if="field.type === 'boolean'"
        :id="`field-${field.key}`"
        :name="field.key"
        v-model="values[field.key] as boolean"
        :disabled="field.readonly === true"
        class="dynamic-form__checkbox"
        type="checkbox"
        :aria-invalid="Boolean(errors[field.key])"
      />

      <!-- array：有引用或固定选项时使用多选下拉，其他数组保留逗号输入。 -->
      <select
        v-else-if="field.type === 'array' && (field.ref || (field.choices?.length ?? 0) > 0)"
        :id="`field-${field.key}`"
        :name="field.key"
        v-model="values[field.key] as (string | number)[]"
        :disabled="field.readonly === true || refLoading[field.key] || needsTargetType(field)"
        class="dynamic-form__control"
        multiple
        :aria-invalid="Boolean(errors[field.key])"
      >
        <option
          v-for="choice in field.choices ?? refOptions[field.key] ?? []"
          :key="String(choice.value)"
          :value="choice.value"
        >
          {{ choice.label }}
        </option>
      </select>
      <input
        v-else-if="field.type === 'array'"
        :id="`field-${field.key}`"
        :name="field.key"
        v-model="values[field.key] as string"
        :disabled="field.readonly === true"
        class="dynamic-form__control"
        type="text"
        :placeholder="field.placeholder || '多个值用英文逗号分隔'"
        :aria-invalid="Boolean(errors[field.key])"
      />

      <!--
        enum：下拉，选项来自后端声明的 choices。
        空选项写成 :value="''"（而不是 value=""）：Vue 3 对 <select v-model> 用
        _value 作为选项值，动态绑定的选项与字符串 value 混用会让两边永远不相等，
        选择后 v-model 收不到值 —— 表现为"必填校验一直说没填"。
      -->
      <select
        v-else-if="field.type === 'enum'"
        :id="`field-${field.key}`"
        :name="field.key"
        v-model="values[field.key] as string"
        :disabled="field.readonly === true"
        class="dynamic-form__control"
        :aria-invalid="Boolean(errors[field.key])"
      >
        <option :value="''">请选择</option>
        <option v-for="choice in field.choices ?? []" :key="choice.value" :value="choice.value">
          {{ choice.label }}
        </option>
      </select>

      <!--
        ref：下拉，选项来自对应资源列表。

        两种形态：
          * 静态（`ref.resource`）：候选固定来自同一类对象；
          * **多态**（`ref.resource_field`）：候选来自"类型字段当前取值"所指的那类
            对象。类型没选之前**禁用**并说明原因 —— 渲染一个空下拉会让用户以为
            "系统里确实没有可选项"，而真实原因是"你还没选类型"。
      -->
      <select
        v-else-if="field.type === 'ref'"
        :id="`field-${field.key}`"
        :name="field.key"
        v-model="values[field.key] as string"
        :disabled="field.readonly === true || refLoading[field.key] || needsTargetType(field)"
        class="dynamic-form__control"
        :aria-invalid="Boolean(errors[field.key])"
      >
        <option :value="''">{{ refPlaceholder(field) }}</option>
        <option
          v-for="option in refOptions[field.key] ?? []"
          :key="String(option.value)"
          :value="String(option.value)"
        >
          {{ option.label }}
        </option>
      </select>

      <!-- date / datetime -->
      <input
        v-else-if="field.type === 'date'"
        :id="`field-${field.key}`"
        :name="field.key"
        v-model="values[field.key] as string"
        :disabled="field.readonly === true"
        class="dynamic-form__control"
        type="date"
        :aria-invalid="Boolean(errors[field.key])"
      />
      <input
        v-else-if="field.type === 'datetime'"
        :id="`field-${field.key}`"
        :name="field.key"
        v-model="values[field.key] as string"
        :disabled="field.readonly === true"
        class="dynamic-form__control"
        type="datetime-local"
        :aria-invalid="Boolean(errors[field.key])"
      />

      <!-- integer / number / string -->
      <input
        v-else
        :id="`field-${field.key}`"
        :name="field.key"
        v-model="values[field.key] as string"
        :disabled="field.readonly === true"
        class="dynamic-form__control"
        :type="field.type === 'integer' || field.type === 'number' ? 'number' : 'text'"
        :step="field.type === 'number' ? 'any' : undefined"
        :maxlength="field.max_length"
        :placeholder="field.placeholder"
        :aria-invalid="Boolean(errors[field.key])"
      />

      <p v-if="field.help" class="dynamic-form__help">{{ field.help }}</p>
      <p
        v-if="field.readonly"
        class="dynamic-form__help dynamic-form__readonly-note"
        data-testid="field-readonly"
      >
        该项由服务端根据当前账号的数据范围解析，不接受修改
      </p>
      <p v-if="needsTargetType(field)" class="dynamic-form__help" data-testid="ref-needs-target">
        请先选择「{{ sourceFieldLabel(field) }}」，候选项随它切换
      </p>
      <p v-if="emptyRefNote(field)" class="dynamic-form__help" data-testid="ref-empty">
        {{ emptyRefNote(field) }}
      </p>
      <p v-if="refError[field.key]" class="dynamic-form__error" role="alert">
        {{ refError[field.key] }}
      </p>
      <p v-if="errors[field.key]" class="dynamic-form__error" role="alert">{{ errors[field.key] }}</p>
    </div>

    <div class="dynamic-form__actions">
      <ActionButton type="submit" variant="primary" :loading="busy" :label="submitLabel">
        {{ busy ? '提交中…' : submitLabel }}
      </ActionButton>
    </div>
  </form>
</template>

<style scoped>
.dynamic-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.dynamic-form__field--wide {
  grid-column: 1 / -1;
}
.dynamic-form__field {
  display: grid;
  gap: 6px;
}
.dynamic-form__label {
  font-size: 13px;
  font-weight: 600;
  color: var(--tone-ink-soft);
}
.dynamic-form__control {
  width: 100%;
  min-height: 40px;
  padding: 8px 12px;
  border: 1px solid var(--tone-line, var(--tone-line));
  border-radius: 10px;
  font: inherit;
  font-size: 14px;
  color: var(--tone-ink);
  background: var(--tone-surface);
}
.dynamic-form__control[aria-invalid='true'] {
  border-color: var(--tone-danger);
}
.dynamic-form__checkbox {
  width: 18px;
  height: 18px;
}
.dynamic-form__help {
  margin: 0;
  font-size: 12px;
  color: var(--tone-muted);
}
.dynamic-form__error {
  margin: 0;
  font-size: 12px;
  color: var(--tone-danger);
}
.dynamic-form__actions {
  grid-column: 1 / -1;
  display: flex;
  justify-content: flex-end;
}
@media (max-width: 768px) {
  .dynamic-form {
    grid-template-columns: 1fr;
  }
}
</style>
