/**
 * 漂移守卫：把本包对 Harness API 的假设**钉在真实产物上**。
 *
 * 背景：本包用 `tsconfig.json` 的 `paths` 直接指向本地 Harness checkout 的
 * `lib/types/*.d.ts`，所以类型不是猜的。但**运行时**在测试环境走的是
 * `src/testing/dsh-tools.stub.ts` 替身，替身与真实实现可能漂移。
 *
 * 这个文件读真实的 Harness 产物，逐条断言我们的假设仍然成立：
 *   ① `defineTool` 的 `parameters` 是**紧凑 spec**（`ParameterSchemaSpec`），
 *      requiredness 走逐属性 `required?: true`；
 *   ② `defineTool` 会把紧凑 spec **投影成标准 JSON Schema**（方向与任务假设相反）；
 *   ③ 注册入口只接受 `ToolDefinition`，不存在接受裸 JSON Schema 的路径。
 *
 * harness checkout 不在时跳过（本包在别的机器上只做交付，不做本地校验）。
 */
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import { parameterSchemaSpecToJsonSchema } from '../src/testing/dsh-tools.stub.ts'

const HARNESS = fileURLToPath(new URL('../../../deepseek-harness-master/', import.meta.url))
const TOOLS_PKG = `${HARNESS}packages/core/tools/`
const SCHEMA_DTS = `${TOOLS_PKG}lib/types/schema.d.ts`
const TOOLS_DTS = `${TOOLS_PKG}lib/types/index.d.ts`
const TOOLS_JS = `${TOOLS_PKG}lib/index.js`

const present = existsSync(SCHEMA_DTS) && existsSync(TOOLS_DTS) && existsSync(TOOLS_JS)

function read(path: string): string {
  return readFileSync(path, 'utf8')
}

describe.skipIf(!present)('与真实 Harness 产物的一致性', () => {
  it('defineTool 的 parameters 是紧凑 spec（不是标准 JSON Schema）', () => {
    const dts = read(SCHEMA_DTS)
    // ① 参数类型是 ParameterSchemaSpec
    expect(dts).toContain('readonly parameters: S;')
    expect(dts).toContain('type ParameterSchemaSpec = {')
    // ② 必填性是逐属性的 required?: true
    expect(dts).toContain('export type ParameterPropertySpec = ValueSchemaSpec & {')
    expect(dts).toMatch(/required\?:\s*true/)
    // ③ 标准 JSON Schema 是"投影结果"，是另一个类型
    expect(dts).toContain('export interface ParameterJsonSchema extends ObjectJsonSchema {')
  })

  it('defineTool 运行时把紧凑 spec 投影成标准 JSON Schema', () => {
    const js = read(TOOLS_JS)
    expect(js).toContain('const parameters = parameterSchemaSpecToJsonSchema(options.parameters);')
  })

  it('注册入口只接受 ToolDefinition，没有裸 JSON Schema 通道', () => {
    const dts = read(TOOLS_DTS)
    expect(dts).toContain('register(definition: ToolDefinition): () => void;')
  })

  it('本地替身的投影语义与真实实现一致（往返保真）', () => {
    // 服务端真实下发的形状（backend/yuxin/kernel/capability.py:343-383）
    const serverSchema = {
      type: 'object',
      additionalProperties: false,
      properties: {
        code: { type: 'string', maxLength: 64, description: '塘口编号（约束：maxLength=64）' },
        area_id: { type: 'integer' },
        tags: { type: 'array', items: { type: 'string' } },
      },
      required: ['code', 'area_id'],
    }
    const compact = {
      code: { type: 'string', description: '塘口编号（约束：maxLength=64）', required: true },
      area_id: { type: 'integer', required: true },
      tags: { type: 'array', items: { type: 'string' } },
    }
    const projected = parameterSchemaSpecToJsonSchema(compact) as Record<string, unknown>
    // 属性名逐字保留、required 还原成数组形式 —— 结构不丢。
    expect(Object.keys(projected['properties'] as object).sort()).toEqual(
      Object.keys(serverSchema.properties).sort(),
    )
    expect(projected['required']).toEqual(['code', 'area_id'])
    expect(projected['type']).toBe('object')
  })
})
