# FPA 开发说明

> 本地环境、加一个域的固定步骤、以及实际踩过的坑。先读本文，再读 `docs/ARCHITECTURE.md`；
> 产品能力清单在 `docs/CAPABILITY_REGISTRY.md`。

---

## 0. 一句话现状

**框架已建成并被证明**，闭环（自然语言 → 模型 → 工具 → 数据库）已跑通。
**六个业务域的代码已全部落地**：运行时注册 **82 条能力**，文档声明 86 条；
单元测试基线 **380 passed / 29 skipped**。

> 下面的数字都是**实测**读数，命令附在文中，可以自己复跑。

## 0.1 本地测试链接（把系统真跑起来）

> 前端 `5273` → 反代 `/api` → 后端 `5101` → 真实 MySQL。两个端口是**配套默认值**
> （`frontend/vite.config.ts` 的 `server.proxy` 与 `tools/serve_dev.py`），改一个要改两个。

    cd <repo>
    $env:MYSQL_USER='fpa'; $env:MYSQL_PASSWORD='fpa_dev_password'; $env:APP_ENV='development'
    $env:PYTHONPATH='<repo>\backend'

    # 后端：gunicorn 在 Windows 上不可用（依赖 fcntl），所以用 waitress 起同一个 app
    Start-Process python -ArgumentList 'tools\serve_dev.py' -WorkingDirectory '<repo>'

    # 前端
    Start-Process cmd -ArgumentList '/c','npm run dev' -WorkingDirectory '<repo>\frontend'

浏览器打开 **http://127.0.0.1:5273/** ，用 **`demo` / `Demo1234!`** 登录。

| 账号 | 权限 | 用途 |
|---|---|---|
| `demo` | 全部 51 条 + 基地级数据范围（`farm_id=1`） | **点这个** —— 能看到六个域的真实数据 |
| `web_e2e_user` | 只有 `pond.create` | 自检夹具，**不适合用来点**（只有一条权限） |

**不要只看端口判断服务活着**，要发一个真请求：

    curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:5101/api/v1/auth/me   # 未登录应返回 401

`fpa` 包**未安装为 editable**，必须给 `PYTHONPATH=<repo>\backend`（`tools/` 下各脚本是在文件内注入 `sys.path` 的，
所以那些脚本不用设）。

---

## 1. 什么是"已完成"（可复跑验证，不要重做）

### 命令清单

```powershell
cd <repo>
$env:MYSQL_USER='fpa'; $env:MYSQL_PASSWORD='fpa_dev_password'

# 后端 7 套自检（全绿才算健康）
python tools\kernel_smoke.py          # 内核：能力声明/范围/事务/不变量/状态机
python tools\migrate_selftest.py      # 迁移 runner
python tools\db_selfcheck.py          # 真库结构与行为
python tools\schema_parity.py          # 真库形状 vs 迁移文件（migrate verify 的盲区）
python tools\schema_parity_selftest.py # 证明上面那个比对器真的会红
python tools\runner_e2e.py            # 执行器（真实 MySQL）
python tools\agent_e2e.py             # Agent 网关三层防御 + 一次性令牌（真实 MySQL）
python tools\web_e2e.py               # HTTP 全链路（真实 Flask + 真实 MySQL）
python tools\check_source_hygiene.py  # BOM/CRLF/编码

# 契约文档生成区校验
python tools\gen_contract_docs.py --check

# ★ 闭环证明（真实 MySQL + 真实 Flask + 真实 Harness + 真实模型）
python tools\live_agent_e2e.py
```

`live_agent_e2e.py` 的结论形态：模型自主调用 `pond_create` 工具，
然后**直接查库**确认那一行存在（不是"模型说它建了"）。

### 前端

```powershell
cd frontend
npm run typecheck; npm run test; npm run test:cov; npm run lint; npm run build; npm run e2e
```
上次实测：184 个测试全绿、覆盖率 95.41/84.58/95.23/95.41（门槛 85/80/85/85）。

### 插件

```powershell
cd agent-runtime
npx tsc --noEmit; npx vitest run    # 19 个测试
```

### 已落地的结构

```
backend/fpa/kernel/       errors scope uow fields capability invariants
                          audit idempotency confirmation runner password workflow
                          invariants.py：18 种声明式规则类型，覆盖 registry §4 的
                          21 条业务规则（用法见 docs/INVARIANT_TYPES.md）
backend/fpa/web/          app(能力自动生成路由) security(全局CSRF) routes_auth
                          routes_agent context workflow_meta
backend/fpa/agent/        gateway（三层防御 + 固定业务路由）
backend/fpa/harness/      session（子进程池 + 环境隔离）
backend/fpa/domains/      _base access master_data
database/migrations/      000-003
agent-runtime/            Harness 插件（@fpa/dsh-biz-tools）+ bin/run.cmd 启动器
docs/                     ARCHITECTURE INTERFACES DECISIONS WRITE_CONTRACT
                          CAPABILITY_REGISTRY（68 条能力权威清单，150KB）
                          INVARIANT_TYPES（不变量类型契约与用法）
```

---

## 2. 剩余工作（第 3 阶段，已收窄到 5 件）

权威判据工具：`python tools\registry_reconcile.py --check`
（它把“文档声明”与“运行时注册”对账，并把缺口分成 [A][B][C][D][E][F][G][H] 八张表；
**[A][B][D][F] 四表清空 = 可交付**，其余是提供线索的候选清单。

| # | 任务 | 量级 | 状态 |
|---|---|---|---|
| 1 | 真库漂移对齐：修 004/007 漂移 + 应用 008_sales（销售四表在库里**不存在**） | 小 | **已完成**（`migrate verify` 10/10，销售四表已建成且 0 行） |
| 2 | **`PeriodOpen` 无租户谓词** → 跨租户串号（真缺陷，7 条能力受影响） | 小但严重 | **已完成**（谓词带 `tenant_keys`、缺键按 R2 报 `INTERNAL_ERROR`、`LIMIT 1` 带 `ORDER BY period_start DESC, id DESC`；7 条能力全部回传 `organization_id`；8 条单测 + 回归守卫脚本；独立验证 4/4 通过） |
| 3 | **[B]** `AtMostOnePending` 未挂到 `pond_status_change.request`（规则退化成建议） | 极小 | **已完成**（根因在 `runner._invariant_extra` 的排除键取值顺序——路径参数优先会把它指向塘口；已改为取本次写入行。[B] 表清空） |
| 4 | **[D]** 4 条能力挂错/漏挂不变量（harvest.verify、pond.update、pond_status_change.×2） | 小 | **部分完成**：`harvest.verify` 补 `DistinctActors`（含自审被拒的反例断言）、`pond_status_change.request` 补 `AtMostOnePending` 已生效；`pond.update` 的 `RequiredField` 判为**文档错**（否命题被读成声明）已改文档；`pond_status_change.verify` 的 `StateTransition`/`RequiredField` 需内核支持（"服务端决定的状态"可取、该步无 `reason` 入参），**待 评审结论** |
| 5 | **[A]** 7 条能力未实现（access×5、audit×1、auth.password.change×1） + **[F]** row_actions 三处不匹配 | 中 | 进行中 |

### 已完成（勿重做）

- 内核：18 种声明式不变量类型，覆盖 registry §4 的 22 条业务规则
- 五个业务域的能力与服务（production 12 / warehouse 7 / purchase 10 / sales 12 / cost 5）
- per-domain 迁移 003–009、实现脚本、e2e 工具
- 神经网络不会重做的部分：web 层能力自动生成路由、agent 三层防御、harness 子进程池、agent-runtime 插件

### 不在本阶段范围

前端业务页面、Nginx/Gunicorn 部署链路、README。

## 3. 加一个新业务域的**固定七步**（照 master_data 抄）

1. **迁移**：`database/migrations/00N_<域>.sql`
   - 建表用 `CREATE TABLE IF NOT EXISTS`（迁移必须**可重入**）
   - **自包含**：不要假设前置数据存在（见下面第 5 节的坑）
   - 能表达成唯一键/CHECK 的不变量，就放数据库层
2. **状态机 + 资源声明**：`domains/<域>/service.py`
   - `Workflow(states=(State(code, label, tone, actions), ...), transitions=(...))`
   - `RESOURCES.register(Resource(name, title, module, list_path, workflow, columns))`
   - **状态的 label 只在这里出现一次**，其他地方一律引用
3. **读路径**：`domains/<域>/<资源>.py`
   - 列表 / 详情 / `load_<资源>`（回读函数，执行器要用）
   - 服务自己校验权限与范围（第三层防御）
   - 派生字段：`status_label` / `allowed_actions` / `version`
4. **写路径**：`domains/<域>/<资源>_write.py`
   - 每个写能力：`ctx.require` → `_scope_row` → 状态机校验 → 写库 → 返回 `HandlerResult(resource_id=...)`
   - 乐观锁只做一处：`UPDATE ... WHERE row_version=%s`
   - **不传 `fields=`** —— 内核会自动从处理器标注派生（见第 5 节）
5. **能力声明**：`domains/<域>/capabilities.py`
   - 用 `if REGISTRY.find("xxx") is not None: return` 包住，防重复导入炸
   - 写能力显式声明 `confirmation=`（`risk=NORMAL` 默认 `NEVER`）
6. **端到端测试**：`tools/<域>_e2e.py`
7. **跑全套 7 套检查**，确认无回归

---

## 4. 一条必须遵守的工程纪律

**任何"两处描述同一件事"的地方，都要删掉一处。**

这条是本项目全部重构的论点，也是我在实现中反复踩到的坑：

- 字段白名单：后端一处 + 前端手写一遍 → 删前端那处（元数据驱动渲染）
- 状态中文：14 处各自翻译 → 只留 `State.label`
- 能力策略：路由 + 字段表 + URL 正则 + OpenAPI → 只留 `Capability`
- 契约文档的错误码表 → 改成从内核生成

**我自己也犯过**：`Capability` 要求显式传 `fields=`，而处理器签名里已有相同标注 →
我 9 条能力全忘传 → 字段集为空、校验放行一切、前端渲染不出表单、**全程不报错**。
修法不是"记得传"，而是删掉那个可选性（`__post_init__` 自动派生）。

---

## 5. 环境与工具链的坑（都踩过，别再踩）

### 5.1 写文件：**用 Python 脚本，不要用 PowerShell**

PowerShell 在本环境**不可靠**，本会话造成过这些损伤：

| 手段 | 后果 |
|---|---|
| `Set-Content -Encoding UTF8` | 写入 BOM → MySQL 报 `syntax error near '\ufeff'` |
| here-string `@"..."@` | 静默失败，或被引号吃掉内容 |
| 含三引号的 Python 片段 | `unterminated triple-quoted string literal` |
| 提交信息含中文引号 | git 把它当 pathspec |

**可靠做法**：写一个 `.py` 脚本，用 `Path.write_text(content, encoding='utf-8', newline='\n')`；
含三引号的内容用 `chr(34) * 3` 拼。

`tools/check_source_hygiene.py` 会拦 BOM / CRLF / U+FFFD——**提交前跑它**。

### 5.2 遍历目录：用 `os.walk` + `dirnames` 剪枝，不要用 `Path.rglob`

`rglob` **无法剪枝**，会进入 `node_modules` / `.dsh-home`（193 个 npm 包）再逐个过滤。
实测 `migrate_selftest` 30 秒超时 → 改用剪枝后 0.1 秒。

### 5.3 Harness 运行时

| 坑 | 解法 |
|---|---|
| `pnpm dsh` 在 Windows 上 EPERM | 绕过 pnpm，用 `agent-runtime/bin/run.cmd` |
| `dsh_bin` 必须是可执行文件 | 指向 `run.cmd`（`.js` 报 WinError 193） |
| SDK 的 `resolve_bundled_launch_args()` 读**父进程** `os.environ` | 给 `dsh_bin` 跳过它 |
| 机器级环境变量对已启动进程不可见 | 从注册表读（见 `harness_smoke._machine_env`） |
| 插件装在哪 | **`<DSH_HOME>/profiles/sdk/node_modules/@fpa/`**（不是运行时闭包） |
| 插件装了 ≠ 会被加载 | **必须显式给 `patches=`**。合成顺序是 bundle → `<DSH_HOME>/profiles/sdk/cordis.patch.yml`（本仓库是**空数组**）→ `<DSH_HOME>/cordis.patch.yml`（不存在）→ `--patch` 覆盖层。前两层都不给，`patches=()` 就一条都不生效：模型**没有业务工具却留着 `tool-web`/`tool-fs`/`tool-pwsh`**，于是它自己拼 HTTP 打网关（业务数据是真的，路径绕过设计、安全边界整条不在），而应用启动、pytest、浏览器 e2e **全绿**。判据 `python tools/harness_tools_audit.py` |
| 人格从哪来 | `system-prompt` 行的 `config.persona`（+ `includeHarnessIdentity: false`），写在同一个 patch 里。**别装 `@deepseek-ai/dsh-persona`**：那是仅限 agent preset 作用域的遮蔽行，挂全局会与 prompt 注册表冲突并加载失败 |

模型凭据：`DEEPSEEK_API_KEY` 是**机器级环境变量**，子进程白名单里必须放行。

### 5.4 数据库

```powershell
$env:MYSQL_ROOT_PASSWORD='1234'      # reset 需要 root
python tools\bootstrap_db.py         # 建库建账号（幂等）
python tools\migrate.py apply|status|verify|reset
```

`reset` 只在开发环境可用（`APP_ENV=production` 会拒绝）。
改了**已应用**的迁移后 `status` 会报"漂移"——这是守卫在正常工作，走 `reset`。

`migrate verify` 比的是**登记值 vs 文件**，它**看不出真库的形状跟文件差在哪**。
2026-09-13 那次真库漂移（004 的行状态列、007 的 `cancel_reason`）正是从这个盲区
溜过去的：`status` 报了漂移，但没有任何工具能回答"真库到底长什么样"。
`tools\schema_parity.py` 补这一格——它在**临时库**里从零跑一遍迁移，再与真库
逐表 diff 列/索引/唯一键/CHECK/外键。退出码：`0` 一致、`1` 有差异、`2` 未能证明一致
（**`2` 不等于一致**）。真库全程只读，临时库跑完即删。

> 判据提醒："0 处不一致"必须能自证"确实扫到了东西"，所以该工具每次都先打印
> 扫描规模（临时库建成表数 / 实际比对表数 / 累计比对结构行），并设三道自证闸门。
> 只测"一致时返回 0"是假测试——恒返回 0 的实现也能通过，所以另配
> `tools\schema_parity_selftest.py`：故意造出列类型/CHECK/列改名三类不一致，
> 确认它**真的会红**，并确认恢复后**真的会绿**。

---

## 6. 架构上的三个"不可违反"

1. **三层权限防御**的判定函数是**同一个**（`capability.authorized`）——
   所以三层不可能给出不同结论。不是"三处实现碰巧一致"。
2. **`executed` 只能由一次已提交的事务产生**。执行顺序：`commit()` → 回读校验
   → 才返回 `executed`。详见 `docs/WRITE_CONTRACT.md`。
3. **内核不 import flask / domains**；**MySQL 驱动只允许出现在 `kernel/uow.py` 一处**
   （错误码翻译与连接管理因而只有一个落点）；`web` 与 `agent` 不写 SQL。
   依赖方向由 `tests/test_architecture.py` 用 AST 强制，**且是集合相等而不是"禁止出现"**：
   静态 import pymysql 的模块集合必须恰好是 `{fpa.kernel.uow}`——这比原措辞更强，
   它同时禁止"再来一处接驱动"。
   > 原措辞"内核不 import flask / pymysql / domains"与实际实现相矛盾（`UnitOfWork` 就是
   > 事务边界、必须持有连接），且它描述的是一条**从未成立过**的约束。按本项目纪律
   > "两处描述同一件事就删一处"，删掉的是这处过期措辞，留下的判据是上面那条可执行断言。

---

## 7. 目标未变

`docs/CAPABILITY_REGISTRY.md` 是产品的权威规格，代码是它的落地形态。
`docs/CAPABILITY_REGISTRY.md` 里每条能力都有：字段表、状态机、不变量、
human_only 判定、以及到早期版本证据的追溯。

**别改需求，改代码。**
