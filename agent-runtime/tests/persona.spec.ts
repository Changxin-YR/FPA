import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { ASK_USER_DESCRIPTION } from '../src/index.ts'

/**
 * review 回归：模型**不能先用 `ask_user` 反问"是否确认"**。
 *
 * 实测缺陷：用户说「帮我核验付款单 PAY-2026-002」，模型把它理解成"要先问用户是否确认"，
 * 调 `ask_user` 反问「请回复确认核验」——那一轮返回 `clarification`，界面**不会**出现
 * HITL 确认卡片（卡片只由**直接调用业务工具**触发）。用户看到的就是
 * "模型让我确认，却没有卡片"。
 *
 * 这条规则有两处落点（部署人格 + ask_user 工具描述），模型两个都会读到；
 * 任何一处被改回去，这里就会红。
 */

const here = dirname(fileURLToPath(import.meta.url))
const patch = readFileSync(join(here, '..', 'cordis.patch.yml'), 'utf-8')

describe('确认卡片契约：不得先用 ask_user 反问确认', () => {
  it('ask_user 工具描述禁止用它请求写操作确认，并要求直接调用业务工具', () => {
    expect(ASK_USER_DESCRIPTION).toContain('不要')
    expect(ASK_USER_DESCRIPTION).toContain('确认')
    expect(ASK_USER_DESCRIPTION).toContain('直接调用对应业务工具')
  })

  it('部署人格（cordis.patch.yml）里钉了同一条纪律', () => {
    expect(patch).toContain('不要')
    expect(patch).toContain('直接调用对应业务工具')
    expect(patch).toContain('confirmation_required')
  })
})
