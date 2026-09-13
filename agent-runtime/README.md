# @fpa/dsh-biz-tools

把 FPA 的业务能力注册成 **DeepSeek Harness 的类型化工具**：**每一条能力一个真工具、真 JSON Schema**。

## 它解决什么问题

早期实现（`deepseek-harness-master/packages/extensions/fpa-agent-tools/src/index.ts`，3 KB）只注册两个
**元工具** `biz_query` / `biz_mutation`，把全部业务操作名塞进一段长 `description` 字符串里让模型去挑，
工具目录还塞进 `FPA_AGENT_TOOL_CATALOG` 环境变量（源码注释自承是为绕开 Windows 进程环境变量上限）。

本实现：

| 维度 | 旧 | 新 |
|---|---|---|
| 工具粒度 | 2 个元工具 | **每个能力一个工具** |
| 参数约束 | 一段自然语言描述 | **标准 JSON Schema**（服务端下发） |
| 目录来源 | 环境变量 JSON | `GET /api/v1/agent/tools` |
| 权限过滤 | 提示词自觉 | **服务端 L1 过滤**：没权限的工具根本不下发 |
| 完成语义 | 文本里解析 JSON | **`kind` 结构化判别联合**，原样转交 |

## 契约（冻结）

- `docs/INTERFACES.md` §4「Harness ↔ Gateway 协议」—— 本插件**只按它实现**。
- `docs/WRITE_CONTRACT.md` —— `kind` 三态判别联合；`executed` 与 `confirmation_required` 类型互斥。

## 三条不可违反的纪律

1. **`kind` 原样交给模型。** 本包里**不存在**任何生成"完成/成功"措辞的代码路径；全部文案由服务端渲染
   （`WRITE_CONTRACT.md` 规则 3）。`tests/tools.spec.ts` 有一条断言直接钉住这点：待确认的工具结果里
   **不得出现「已完成」「成功」「已执行」**。
2. **`confirmation_required` 之后必须让模型停下。** 结果里带 `note` 明确指令模型立即结束当前迭代输出。
   少了它，模型会顺着推理把"待确认"说成"已完成"。
3. **启动失败必须显式。** `GET /tools` 不可达时 `apply()` **抛出**并中止插件加载，绝不静默注册 0 个工具——
   静默会让模型以为"什么都干不了"，用户看到的是"AI 变笨了"而不是"服务没起来"。

## 文件

| 文件 | 作用 |
|---|---|
| `src/index.ts` | 插件入口：拉清单 → 逐条注册 → 注册 `ask_user`；`toModelPayload()` 是纯搬运函数 |
| `src/gateway.ts` | HTTP 客户端：协议校验、信封解包、`X-Agent-Context` / `Idempotency-Key` |
| `src/schema.ts` | 标准 JSON Schema ⇄ Harness 紧凑参数 spec 的**唯一适配点**（见下） |
| `src/types.ts` | `ToolOutcome` 判别联合（`WRITE_CONTRACT.md` 规则 1 的插件侧镜像） |
| `src/testing/*` | 测试替身（**不参与构建、不进发布包**） |
| `cordis.patch.yml` | 注入插件 + **52 条** `disabled: true` |
| `tests/tools.spec.ts` | 19 条端到端（fetch 打桩，不需要真实后端） |
| `tests/harness-api.spec.ts` | 漂移守卫：把假设钉在真实 Harness 产物上 |

## ⚠️ 与任务描述的一处事实冲突：`defineTool.parameters` **不支持**标准 JSON Schema

任务描述写的是"`defineTool` 的 `parameters` 支持标准形态，不要自己重新映射成紧凑键名"。
核实 Harness 真实产物后，前半句**不成立**：

| 证据 | 事实 |
|---|---|
| `packages/core/tools/lib/types/schema.d.ts:178-184` | `DefineToolOptions.parameters: S`，`S extends ParameterSchemaSpec` |
| 同文件 `:71-85` | `ParameterSchemaSpec = { [key]: ValueSchemaSpec & { required?: true } }` —— **逐属性的紧凑 spec** |
| 同文件 `:82-85` | `ParameterJsonSchema`（标准 JSON Schema）是**紧凑 spec 的投影结果**，不是入参 |
| `packages/core/tools/lib/index.js:846` | `defineTool` 实做的是 `parameterSchemaSpecToJsonSchema(options.parameters)` —— 方向与描述**相反** |
| `packages/core/tools/lib/types/index.d.ts:602` | 注册入口只有 `register(definition: ToolDefinition)`，**没有接受裸 JSON Schema 的路径** |

因此 `src/schema.ts` 的适配层是**框架强制**的，不是风格选择。它与任务要禁止的东西有本质区别：

- **禁止的是**：把字段名压成 `n=` / `d=` / `m=` 这类手写缩写键（早期版本为塞进环境变量发明的那套）；
- **本包做的是**：逐字保留属性名、类型、必填性、enum、描述的**机械转换**，不做任何改名或缩写。

### 保真度：哪些约束无法承载

紧凑 spec **没有** `maxLength` / `minimum` / `maximum` / `pattern` 这些槽位，而服务端**确实会下发**它们
（`backend/fpa/kernel/capability.py:354-360,369-374`、`backend/fpa/kernel/fields.py:125-127`）。
三种处置里，静默丢弃会丢信息、直接抛错会让每条真实 schema 都注册失败，因此选择**折叠进 `description`**：
模型仍能看到约束，而**服务端始终是校验权威**（`WRITE_CONTRACT.md`：校验在第 1 步，失败即 `failed`，什么都没写）。
`fully unsupported` 的构造（`$ref` / `allOf` / `anyOf` / `not` / `if-then-else` …）**一律抛错**，不静默降级。

## 关于 `cordis.patch.yml`

`insert` 段把本插件注入 Harness；下面是 **52 条** `disabled: true`，取自早期版本生产在用的
`FPA/backend/layers/features/agent/agent-restricted.patch.yml`（参考实现那份只贡献 `insert` 形态，
它本身没有任何 `disabled` 条目）。并集 = 1 个 insert + 52 条 disabled。

这 52 条是"禁止任意 Shell / 文件系统 / 网络 / 子代理"的落地点：少了任何一条都意味着模型多一条逃逸路径。

> `config` 里用 `!!js process.env.…` 取值，是 Harness 的**配置注入机制**（patch 在配置求值阶段读取），
> 与"插件把令牌写进 env"是两回事。插件从不写 env，并且在 `apply()` 里主动把这两个键从子进程环境中清除
> （`src/index.ts` 的 `scrubCredentialsFromEnv`，`tests/tools.spec.ts` 有断言）。

## 开发

```bash
npm install
npm run verify      # typecheck + 19 条测试 + build
```

### 依赖策略

`@deepseek-ai/cordis` 与 `@deepseek-ai/dsh-tools` 声明为 **`peerDependencies`**（由 Harness 运行时提供）。
**不从 npm 安装**：npm 上的 `@deepseek-ai/dsh-tools` 是 `0.0.1-rc.1`，而本地 checkout 是
`0.1.2-alpha.5`——装了会掩盖真实差异。

- **类型不走替身**：`tsconfig.json` 的 `paths` 直接指向本地 checkout 的真实
  `lib/types/index.d.ts`，所以 `tsc --noEmit` 校验的是**真 API**。
- **运行时替身只服务测试**：`vitest.config.ts` 把 `@deepseek-ai/dsh-tools` 指向
  `src/testing/dsh-tools.stub.ts`（语义等价的最小实现）。
- **漂移守卫**：`tests/harness-api.spec.ts` 读真实产物逐条断言假设仍成立（checkout 不在时跳过）。
