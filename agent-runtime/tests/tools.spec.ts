import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { apply, CLARIFICATION_NOTE, PENDING_CONFIRMATION_NOTE } from '../src/index.ts'
import { resolveGatewayBase, GatewayConfigError } from '../src/gateway.ts'

const GATEWAY = 'https://fpa.example.com/api/v1/agent'
const TOKEN = 'ctx-token-abc123'

/** 服务端下发的标准 JSON Schema（形状取自 `backend/fpa/kernel/capability.py:343-383`）。 */
const CATALOG = {
  code: 'OK',
  message: '',
  request_id: 'req-1',
  data: {
    tools: [
      {
        name: 'pond_create',
        description: '新建塘口。需要 pond.create 权限。',
        capability: 'pond.create',
        parameters: {
          type: 'object',
          additionalProperties: false,
          properties: {
            code: { type: 'string', maxLength: 64, description: '塘口编号' },
            name: { type: 'string', description: '塘口名称' },
            area_id: { type: 'integer', description: '所属区域' },
          },
          required: ['code', 'name', 'area_id'],
        },
      },
      {
        name: 'feeding_verify',
        description: '核验投喂（扣库存 + 计成本）。（高危操作：需用户确认后才会真正执行）',
        capability: 'feeding.verify',
        // 服务端下发"需要幂等键"时插件才生成它（DECISIONS.md Q19）。
        // 夹具必须带上这个字段，否则测的是"一律不带键"这条错路径。
        requires_idempotency_key: true,
        parameters: {
          type: 'object',
          properties: {
            feeding_id: { type: 'integer', description: '路径参数 feeding_id' },
            expected_version: { type: 'integer', minimum: 1, description: '乐观锁版本号' },
          },
          required: ['feeding_id', 'expected_version'],
        },
      },
    ],
  },
}

interface CapturedTool {
  name: string
  description: string
  parameters: Record<string, unknown>
  output: { render(args: unknown, value: unknown): { type: string; text: string }[] }
  execute(args: Record<string, unknown>, exec: unknown): Promise<unknown>
}

function makeContext() {
  const registered: CapturedTool[] = []
  return {
    registered,
    tools: {
      register(definition: unknown) {
        registered.push(definition as CapturedTool)
        return () => {}
      },
    },
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

interface Call {
  url: string
  method: string
  headers: Record<string, string>
  body: string
}

function stubFetch(handler: (call: Call) => Response | Promise<Response>): Call[] {
  const calls: Call[] = []
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const headers: Record<string, string> = {}
    const raw = init?.headers
    if (raw && typeof raw === 'object') {
      for (const [key, value] of Object.entries(raw as Record<string, string>)) {
        headers[key] = String(value)
      }
    }
    const call: Call = {
      url: String(input),
      method: init?.method ?? 'GET',
      headers,
      body: typeof init?.body === 'string' ? init.body : '',
    }
    calls.push(call)
    return handler(call)
  }) as typeof fetch
  return calls
}

/** 走与 Harness 完全相同的路径：execute → output.render → 拼出模型看到的文本。 */
async function runTool(
  ctx: ReturnType<typeof makeContext>,
  name: string,
  args: Record<string, unknown>,
  callId = 'call-1',
): Promise<string> {
  const tool = ctx.registered.find((item) => item.name === name)
  if (!tool) throw new Error(`工具未注册：${name}`)
  // 属性名是 **rootCallId**（Harness 的 `ToolExecution` 定义），不是 `callId`。
  // 插件初版读的是 `exec.callId`，永远是 undefined，于是幂等键静默不发。
  // 这个测试夹具跟着改过来，才能在类型和运行期都反映真实契约。
  const value = await tool.execute(args, {
    rootCallId: callId,
    signal: new AbortController().signal,
  })
  return tool.output
    .render(args, value)
    .map((block) => block.text)
    .join('\n')
}

const originalFetch = globalThis.fetch

beforeEach(() => {
  delete process.env['FPA_AGENT_CONTEXT_TOKEN']
  delete process.env['FPA_AGENT_GATEWAY_URL']
})

afterEach(() => {
  globalThis.fetch = originalFetch
})

describe('工具清单拉取与注册', () => {
  it('按服务端返回的清单逐条注册，数量一致（每个能力一个真工具）', async () => {
    stubFetch(() => jsonResponse(CATALOG))
    const ctx = makeContext()
    await apply(ctx as never, { gatewayUrl: GATEWAY, contextToken: TOKEN })

    const catalogNames = (CATALOG.data.tools as { name: string }[]).map((tool) => tool.name)
    for (const name of catalogNames) {
      expect(ctx.registered.map((tool) => tool.name)).toContain(name)
    }
    // 清单里的每一条都注册了，另外补一个 ask_user。
    expect(ctx.registered.length).toBe(catalogNames.length + 1)
    expect(ctx.registered.map((tool) => tool.name)).toContain('ask_user')
  })

  it('参数 schema 保真：属性名、必填性、描述、maxLength 折叠都在', async () => {
    stubFetch(() => jsonResponse(CATALOG))
    const ctx = makeContext()
    await apply(ctx as never, { gatewayUrl: GATEWAY, contextToken: TOKEN })

    const tool = ctx.registered.find((item) => item.name === 'pond_create')
    const properties = tool?.parameters['properties'] as Record<string, Record<string, unknown>>
    expect(Object.keys(properties).sort()).toEqual(['area_id', 'code', 'name'])
    expect(tool?.parameters['required']).toEqual(['code', 'name', 'area_id'])
    expect(properties['code']?.['description']).toContain('塘口编号')
    // 紧凑 spec 没有 maxLength 槽位，必须折叠进 description 而不是静默丢弃。
    expect(properties['code']?.['description']).toContain('maxLength=64')
  })

  it('清单拉取失败必须抛出，而不是静默注册 0 个工具', async () => {
    stubFetch(() => jsonResponse({ code: 'UNAVAILABLE', message: '服务不可用', data: null }, 503))
    const ctx = makeContext()
    await expect(
      apply(ctx as never, { gatewayUrl: GATEWAY, contextToken: TOKEN }),
    ).rejects.toThrow(/服务不可用/)
    expect(ctx.registered.length).toBe(0)
  })

  it('网络不可达同样抛出', async () => {
    globalThis.fetch = (async () => {
      throw new Error('ECONNREFUSED')
    }) as typeof fetch
    const ctx = makeContext()
    await expect(
      apply(ctx as never, { gatewayUrl: GATEWAY, contextToken: TOKEN }),
    ).rejects.toThrow(/无法连接 FPA Gateway/)
  })
})

describe('kind 三态原样透传', () => {
  it('executed：resource_id 与 message 原样交给模型', async () => {
    const executed = {
      code: 'OK',
      data: {
        kind: 'executed',
        message: '已登记投喂：3 号塘 50kg 1号饲料，库存剩 420kg',
        resource_id: 991,
        data: { id: 991 },
      },
    }
    stubFetch((call) =>
      call.url.endsWith('/tools') ? jsonResponse(CATALOG) : jsonResponse(executed),
    )
    const ctx = makeContext()
    await apply(ctx as never, { gatewayUrl: GATEWAY, contextToken: TOKEN })

    const text = await runTool(ctx, 'feeding_verify', { feeding_id: 7, expected_version: 1 })
    const payload = JSON.parse(text) as Record<string, unknown>
    expect(payload['kind']).toBe('executed')
    expect(payload['resource_id']).toBe(991)
    // 服务端渲染的文案原样转交（数字来自数据库，不由插件生成）。
    expect(payload['message']).toBe('已登记投喂：3 号塘 50kg 1号饲料，库存剩 420kg')
    // 只有 executed 才允许出现服务端的成功文案。
    expect(payload['note']).toBeUndefined()
  })

  it('confirmation_required：带 confirmation.token，且不含任何"已完成/成功"字样', async () => {
    const pending = {
      code: 'OK',
      data: {
        kind: 'confirmation_required',
        message: '即将为 3 号塘登记投喂 50kg 1号饲料。',
        confirmation: {
          id: 128,
          token: 'confirm-token-xyz',
          capability: 'feeding.verify',
          title: '登记投喂',
          target: '3 号塘 / 批次 B-2026-007',
          expires_at: '2026-09-12T12:34:56Z',
        },
      },
    }
    stubFetch((call) =>
      call.url.endsWith('/tools') ? jsonResponse(CATALOG) : jsonResponse(pending),
    )
    const ctx = makeContext()
    await apply(ctx as never, { gatewayUrl: GATEWAY, contextToken: TOKEN })

    const text = await runTool(ctx, 'feeding_verify', { feeding_id: 7, expected_version: 1 })
    const payload = JSON.parse(text) as Record<string, unknown>

    expect(payload['kind']).toBe('confirmation_required')
    const confirmation = payload['confirmation'] as Record<string, unknown>
    expect(confirmation['token']).toBe('confirm-token-xyz')
    expect(confirmation['title']).toBe('登记投喂')
    expect(payload['note']).toBe(PENDING_CONFIRMATION_NOTE)

    // ★ 本项目的核心契约：待确认不得被表述成任何形式的"已完成"。
    for (const word of ['已完成', '成功', '已执行', '操作成功', '已经完成']) {
      expect(text).not.toContain(word)
    }
    // 明确告知模型"什么都没写"。
    expect(text).toContain('尚未写入任何数据')
  })

  it('failed：不被当成成功，也不带 note', async () => {
    const failed = {
      code: 'OK',
      data: { kind: 'failed', code: 'CONFLICT', message: '物料库存不足，当前可用 30，本次需要 50' },
    }
    stubFetch((call) =>
      call.url.endsWith('/tools') ? jsonResponse(CATALOG) : jsonResponse(failed),
    )
    const ctx = makeContext()
    await apply(ctx as never, { gatewayUrl: GATEWAY, contextToken: TOKEN })

    const text = await runTool(ctx, 'feeding_verify', { feeding_id: 7, expected_version: 1 })
    const payload = JSON.parse(text) as Record<string, unknown>
    expect(payload['kind']).toBe('failed')
    expect(payload['code']).toBe('CONFLICT')
    expect(payload['message']).toContain('库存不足')
    expect(payload['note']).toBeUndefined()
  })

  it('HTTP 4xx/5xx 转成 failed（不抛，保留服务端中文原因）', async () => {
    stubFetch((call) =>
      call.url.endsWith('/tools')
        ? jsonResponse(CATALOG)
        : jsonResponse({ code: 'HUMAN_ONLY', message: '该操作只能由本人在系统页面完成' }, 409),
    )
    const ctx = makeContext()
    await apply(ctx as never, { gatewayUrl: GATEWAY, contextToken: TOKEN })

    const text = await runTool(ctx, 'feeding_verify', { feeding_id: 7, expected_version: 1 })
    const payload = JSON.parse(text) as Record<string, unknown>
    expect(payload['kind']).toBe('failed')
    expect(payload['code']).toBe('HUMAN_ONLY')
    expect(payload['message']).toContain('只能由本人')
  })
})

describe('请求头与幂等键', () => {
  it('每次调用都带 X-Agent-Context', async () => {
    const calls = stubFetch((call) =>
      call.url.endsWith('/tools')
        ? jsonResponse(CATALOG)
        : jsonResponse({ code: 'OK', data: { kind: 'read', data: [] } }),
    )
    const ctx = makeContext()
    await apply(ctx as never, { gatewayUrl: GATEWAY, contextToken: TOKEN })
    expect(calls[0]?.headers['X-Agent-Context']).toBe(TOKEN)

    await runTool(ctx, 'feeding_verify', { feeding_id: 7, expected_version: 1 })
    const call = calls[calls.length - 1]
    expect(call?.headers['X-Agent-Context']).toBe(TOKEN)
    expect(call?.url).toBe(`${GATEWAY}/tools/feeding_verify/call`)
    expect(call?.method).toBe('POST')
    expect(JSON.parse(call?.body ?? '{}')).toEqual({
      arguments: { feeding_id: 7, expected_version: 1 },
    })
  })

  it('写调用带稳定的 Idempotency-Key', async () => {
    const calls = stubFetch((call) =>
      call.url.endsWith('/tools')
        ? jsonResponse(CATALOG)
        : jsonResponse({ code: 'OK', data: { kind: 'read', data: [] } }),
    )
    const ctx = makeContext()
    await apply(ctx as never, { gatewayUrl: GATEWAY, contextToken: TOKEN })

    await runTool(ctx, 'feeding_verify', { feeding_id: 7, expected_version: 1 }, 'call-abc')
    const call = calls[calls.length - 1]
    expect(call?.headers['Idempotency-Key']).toBe('fpa:feeding_verify:call-abc')
  })
})

describe('ask_user 工具', () => {
  it('返回 clarification 形状并给出停下指令', async () => {
    stubFetch(() => jsonResponse(CATALOG))
    const ctx = makeContext()
    await apply(ctx as never, { gatewayUrl: GATEWAY, contextToken: TOKEN })

    const text = await runTool(ctx, 'ask_user', {
      question: '请确认使用哪个物料：',
      options: ['1号饲料', '2号饲料'],
      allow_free_text: true,
    })
    const payload = JSON.parse(text) as Record<string, unknown>
    expect(payload['kind']).toBe('clarification')
    expect(payload['question']).toBe('请确认使用哪个物料：')
    expect(payload['options']).toEqual(['1号饲料', '2号饲料'])
    expect(payload['allow_free_text']).toBe(true)
    expect(payload['note']).toBe(CLARIFICATION_NOTE)
    expect(text).not.toContain('已完成')
  })
})

describe('启动期校验', () => {
  it('网关地址非 http(s) 一律拒绝', () => {
    expect(() => resolveGatewayBase('ftp://fpa.example.com')).toThrow(GatewayConfigError)
    expect(() => resolveGatewayBase('file:///etc/passwd')).toThrow(GatewayConfigError)
    expect(() => resolveGatewayBase('javascript:alert(1)')).toThrow(GatewayConfigError)
    expect(() => resolveGatewayBase('')).toThrow(GatewayConfigError)
    expect(resolveGatewayBase('https://fpa.example.com/api/v1/agent/')).toBe(GATEWAY)
  })

  it('非 http(s) 地址让插件加载直接失败（不注册任何工具）', async () => {
    stubFetch(() => jsonResponse(CATALOG))
    const ctx = makeContext()
    await expect(
      apply(ctx as never, { gatewayUrl: 'ftp://fpa.example.com', contextToken: TOKEN }),
    ).rejects.toThrow(/只允许 HTTP\(S\)/)
    expect(ctx.registered.length).toBe(0)
  })

  it('缺少上下文令牌时拒绝注册', async () => {
    stubFetch(() => jsonResponse(CATALOG))
    const ctx = makeContext()
    await expect(
      apply(ctx as never, { gatewayUrl: GATEWAY, contextToken: '   ' }),
    ).rejects.toThrow(/上下文令牌/)
    expect(ctx.registered.length).toBe(0)
  })

  it('主动把凭据从子进程环境中清除（纵深防御）', async () => {
    process.env['FPA_AGENT_CONTEXT_TOKEN'] = 'leaked-token'
    process.env['FPA_AGENT_GATEWAY_URL'] = 'https://leaked.example.com'
    stubFetch(() => jsonResponse(CATALOG))
    await apply(makeContext() as never, { gatewayUrl: GATEWAY, contextToken: TOKEN })
    expect(process.env['FPA_AGENT_CONTEXT_TOKEN']).toBeUndefined()
    expect(process.env['FPA_AGENT_GATEWAY_URL']).toBeUndefined()
  })
})
