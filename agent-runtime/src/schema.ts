/**
 * 标准 JSON Schema ⇄ Harness 参数 spec 的唯一适配点。
 *
 * ## 为什么需要这个文件（这是与任务描述的一处**事实冲突**，已登记并上报）
 *
 * 任务描述写的是："`defineTool` 的 `parameters` 支持标准形态，不要自己重新映射成紧凑键名"。
 * 核实 Harness 真实产物后，这句话**不成立**：
 *
 *   - `defineTool` 的 `parameters` 类型是 `ParameterSchemaSpec`
 *     （`packages/core/tools/lib/types/schema.d.ts:71-85`）——它是一个**逐属性的紧凑 spec 映射**
 *     （`{ [key]: ValueSchemaSpec & { required?: true } }`），必填性靠**每个属性上的** `required: true`；
 *   - 我们关心的"标准 JSON Schema"是 `ParameterJsonSchema`（同文件 `:82-85`），它是
 *     **紧凑 spec 的投影结果**，不是 `defineTool` 的入参；
 *   - `defineTool` 实现在 `lib/index.js:846` 做的是
 *     `const parameters = parameterSchemaSpecToJsonSchema(options.parameters)`，
 *     即 **紧凑 spec → 标准 JSON Schema**（方向与任务描述相反）；
 *   - 注册入口只有 `register(definition: ToolDefinition)`
 *     （`lib/types/index.d.ts:602`），**没有任何接受裸 JSON Schema 的注册路径**。
 *
 * 所以适配层是**框架强制**的，不是风格选择。本文件与任务描述要禁止的东西有本质区别：
 *
 *   禁止的是：把字段名压成 `n=` / `d=` / `m=` 这类**手写缩写键**（早期版本为塞进环境变量发明的那套）。
 *   本文件做的是：**逐字保留属性名、类型、必填性、enum、描述** 的机械转换，不做任何改名或缩写。
 *
 * ## 保真度：哪些约束无法承载
 *
 * 紧凑 spec **没有** `maxLength` / `minimum` / `maximum` / `pattern` / `format` 这些关键字槽位。
 * 而服务端**确实会下发它们**：
 *   - `backend/fpa/kernel/capability.py:354-360` 给 `page` / `page_size` 下发 `minimum` / `maximum`；
 *   - `backend/fpa/kernel/capability.py:369-374` 给 `expected_version` 下发 `minimum`；
 *   - `backend/fpa/kernel/fields.py:125-127` 给字段下发 `maxLength` / `minimum`。
 *
 * 三种处置里，静默丢弃（会丢信息）与直接抛错（会让每一条真实 schema 都注册失败）都不可接受，
 * 因此选择**折叠进 `description`**：模型仍能在自然语言里看到约束，而**服务端始终是校验权威**
 * （`WRITE_CONTRACT.md`：校验在第 1 步，失败即 `failed`，什么都没写）。
 */
import type { ParameterSchemaSpec } from '@deepseek-ai/dsh-tools'

export class ToolSchemaError extends Error {
  constructor(toolName: string, message: string) {
    super(`工具 ${toolName} 的参数 schema 无法转换：${message}`)
    this.name = 'ToolSchemaError'
  }
}

/** 紧凑 spec 的一个属性节点（`ValueSchemaSpec` 的常用子集）。 */
interface PropertySpec {
  type: string
  description?: string
  title?: string
  default?: unknown
  examples?: unknown
  enum?: readonly unknown[]
  const?: unknown
  items?: PropertySpec
  properties?: Record<string, PropertySpec>
  additionalProperties?: boolean
  required?: true
}

const PRIMITIVE_TYPES = new Set(['string', 'number', 'integer', 'boolean', 'null'])

/** 紧凑 spec 的表达力之外、只能折叠进 description 的 JSON Schema 关键字。 */
const FOLDED_KEYWORDS = [
  'minLength',
  'maxLength',
  'minimum',
  'maximum',
  'exclusiveMinimum',
  'exclusiveMaximum',
  'multipleOf',
  'pattern',
  'format',
  'minItems',
  'maxItems',
  'uniqueItems',
  'minProperties',
  'maxProperties',
] as const

/** 完全无法表达的构造——宁可显式失败，也不静默降级。 */
const UNSUPPORTED_KEYWORDS = [
  '$ref',
  'allOf',
  'anyOf',
  'not',
  'patternProperties',
  'dependencies',
  'dependentSchemas',
  'if',
  'then',
  'else',
] as const

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function foldConstraints(schema: Record<string, unknown>): string | null {
  const parts: string[] = []
  for (const keyword of FOLDED_KEYWORDS) {
    const value = schema[keyword]
    if (value === undefined || value === null) continue
    parts.push(`${keyword}=${JSON.stringify(value)}`)
  }
  return parts.length > 0 ? `${parts.join('、')}` : null
}

function withFoldedDescription(
  base: string | undefined,
  schema: Record<string, unknown>,
): string | undefined {
  const folded = foldConstraints(schema)
  if (folded === null) return base
  const note = `（约束：${folded}）`
  return base ? `${base}${note}` : note
}

function convertProperty(
  raw: unknown,
  toolName: string,
  path: string,
): PropertySpec {
  if (!isRecord(raw)) {
    throw new ToolSchemaError(toolName, `${path} 不是对象`)
  }

  for (const keyword of UNSUPPORTED_KEYWORDS) {
    if (raw[keyword] !== undefined) {
      throw new ToolSchemaError(
        toolName,
        `${path} 使用了 ${keyword}（紧凑 spec 无法表达，拒绝静默降级）`,
      )
    }
  }

  const type = raw['type']
  if (typeof type !== 'string') {
    throw new ToolSchemaError(toolName, `${path} 缺少 type`)
  }

  const spec: PropertySpec = { type }
  const description = withFoldedDescription(
    typeof raw['description'] === 'string' ? raw['description'] : undefined,
    raw,
  )
  if (description !== undefined) spec.description = description
  if (typeof raw['title'] === 'string') spec.title = raw['title']
  if (raw['default'] !== undefined) spec.default = raw['default']
  if (Array.isArray(raw['examples'])) spec.examples = raw['examples']
  if (raw['const'] !== undefined) spec.const = raw['const']

  if (raw['enum'] !== undefined) {
    const values = raw['enum']
    if (!Array.isArray(values)) {
      throw new ToolSchemaError(toolName, `${path}.enum 不是数组`)
    }
    if (!PRIMITIVE_TYPES.has(type)) {
      throw new ToolSchemaError(toolName, `${path}.enum 只支持标量类型，当前是 ${type}`)
    }
    for (const value of values) {
      if (!scalarMatchesType(value, type)) {
        throw new ToolSchemaError(
          toolName,
          `${path}.enum 含与 type=${type} 不符的取值 ${JSON.stringify(value)}`,
        )
      }
    }
    spec.enum = values
  }

  if (type === 'array') {
    if (raw['items'] !== undefined) {
      spec.items = convertProperty(raw['items'], toolName, `${path}[]`)
    }
    return spec
  }

  if (type === 'object') {
    const properties = raw['properties']
    if (properties !== undefined && !isRecord(properties)) {
      throw new ToolSchemaError(toolName, `${path}.properties 不是对象`)
    }
    const converted: Record<string, PropertySpec> = {}
    if (isRecord(properties)) {
      for (const [key, value] of Object.entries(properties)) {
        converted[key] = convertProperty(value, toolName, `${path}.${key}`)
      }
    }
    spec.properties = converted
    // 嵌套对象必须显式声明开放性（紧凑 spec 的硬要求）。
    spec.additionalProperties = raw['additionalProperties'] !== false
    return spec
  }

  if (!PRIMITIVE_TYPES.has(type)) {
    throw new ToolSchemaError(toolName, `${path} 的 type=${type} 不受支持`)
  }
  return spec
}

function scalarMatchesType(value: unknown, type: string): boolean {
  switch (type) {
    case 'string':
      return typeof value === 'string'
    case 'number':
      return typeof value === 'number' && Number.isFinite(value)
    case 'integer':
      return typeof value === 'number' && Number.isInteger(value)
    case 'boolean':
      return typeof value === 'boolean'
    case 'null':
      return value === null
    default:
      return false
  }
}

/**
 * 服务端下发的标准 JSON Schema → `defineTool` 接受的紧凑参数 spec。
 *
 * 属性名、类型、必填性、enum、描述**逐字保留**；无法承载的数值/字符串约束折叠进 description。
 */
export function parameterSpecFromJsonSchema(
  schema: unknown,
  toolName: string,
): ParameterSchemaSpec {
  if (!isRecord(schema)) {
    throw new ToolSchemaError(toolName, '根节点不是对象')
  }
  if (schema['type'] !== 'object') {
    throw new ToolSchemaError(toolName, `根节点 type 必须是 object，当前是 ${String(schema['type'])}`)
  }

  const properties = schema['properties']
  if (!isRecord(properties)) {
    throw new ToolSchemaError(toolName, '根节点缺少 properties')
  }

  const rawRequired = schema['required']
  const required = new Set<string>()
  if (rawRequired !== undefined) {
    if (!Array.isArray(rawRequired) || rawRequired.some((item) => typeof item !== 'string')) {
      throw new ToolSchemaError(toolName, 'required 必须是字符串数组')
    }
    for (const item of rawRequired as string[]) required.add(item)
  }

  const spec: Record<string, PropertySpec> = {}
  for (const [key, value] of Object.entries(properties)) {
    const converted = convertProperty(value, toolName, key)
    if (required.has(key)) converted.required = true
    spec[key] = converted
  }

  for (const key of required) {
    if (!(key in spec)) {
      throw new ToolSchemaError(toolName, `required 里的 ${key} 不在 properties 中`)
    }
  }

  return spec as unknown as ParameterSchemaSpec
}

/**
 * 紧凑参数 spec → 标准 JSON Schema。
 *
 * 与 Harness 的 `parameterSchemaSpecToJsonSchema()`（`lib/index.js:846` 调用）语义一致：
 * 属性映射本身是**隐式开放对象根**，必填性来自逐属性 `required: true`。
 * 这里实现它有两个用途：① 测试环境的 `defineTool` stub；② 往返保真性断言。
 */
export function parameterSpecToJsonSchema(
  spec: ParameterSchemaSpec,
): Record<string, unknown> {
  const source = spec as unknown as Record<string, PropertySpec>
  const properties: Record<string, unknown> = {}
  const required: string[] = []

  for (const [key, value] of Object.entries(source)) {
    const { required: isRequired, ...rest } = value
    properties[key] = valueSpecToJsonSchema(rest)
    if (isRequired === true) required.push(key)
  }

  const schema: Record<string, unknown> = { type: 'object', properties }
  if (required.length > 0) schema['required'] = required
  return schema
}

function valueSpecToJsonSchema(spec: PropertySpec): Record<string, unknown> {
  const out: Record<string, unknown> = { type: spec.type }
  if (spec.description !== undefined) out['description'] = spec.description
  if (spec.title !== undefined) out['title'] = spec.title
  if (spec.default !== undefined) out['default'] = spec.default
  if (spec.examples !== undefined) out['examples'] = spec.examples
  if (spec.enum !== undefined) out['enum'] = spec.enum
  if (spec.const !== undefined) out['const'] = spec.const
  if (spec.type === 'array' && spec.items !== undefined) {
    out['items'] = valueSpecToJsonSchema(spec.items)
  }
  if (spec.type === 'object') {
    const nested: Record<string, unknown> = {}
    const nestedRequired: string[] = []
    for (const [key, value] of Object.entries(spec.properties ?? {})) {
      const { required: isRequired, ...rest } = value
      nested[key] = valueSpecToJsonSchema(rest)
      if (isRequired === true) nestedRequired.push(key)
    }
    out['properties'] = nested
    if (nestedRequired.length > 0) out['required'] = nestedRequired
    out['additionalProperties'] = spec.additionalProperties ?? true
  }
  return out
}

export type { PropertySpec }
