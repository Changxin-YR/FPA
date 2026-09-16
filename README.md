# 渔芯AI水产养殖一体化系统

水产养殖企业的生产与经营管理后台。后端是 Python（Flask + PyMySQL + MySQL），前端是
Vue 3 + TypeScript，另外接了一层能直接读写业务数据的智能体（DeepSeek Harness + 业务工具）。

系统真正在意的东西只有一个：**一个业务能力（Capability）只声明一次，其余全部派生**。

```
                        一处 Capability 声明
                                 │
        ┌──────────┬────────────┬───────────┬───────────┬───────────┐
     REST 路由    权限码      DataScope   幂等 / 确认闸门   审计字段    不可变量强执点
        │                                                            │
        └──────────────► 前端表单 / 列表 / 动作 ◄──── Agent Tool schema
```

因此在页面上加一个按钮、在表里加一列、给智能体加一个工具，都不需要四处改代码，
而是回到那一处声明。前端不写任何业务字段名与中文文案，全部按元数据渲染。

## 功能范围

| 域 | 覆盖内容 |
|---|---|
| `master_data` | 基地 / 塘口 / 批次 / 物料 / 往来单位等主数据，含状态机与归属校验 |
| `production` | 投苗、投喂、用药、捕捞等生产记录 |
| `warehouse` | 入库、出库、库存台账与结存校验（不允许负库存） |
| `purchase` | 采购订单（提交 → 审批 → 执行）、应付与付款 |
| `sales` | 销售订单、发货、收款与应收核销 |
| `cost` | 成本归集、期间锁定、经办人与审批人分离 |
| `identity` / `access` | 账号、角色、权限码、分租数据范围（DataScope） |
| `audit` | 操作审计日志与前后值留痕 |
| `agent` | 智能体网关：会话绑定、工具注册、参数校验、人工确认闸门 |

业务规则里有 18 类声明式**不变量**（Invariant），例如「同一塘口同期不得重复归集库存成本」
「经办人 ≠ 审批人」「单据状态迁移必须带上期望版本号」，由内核在人工页面与智能体两个入口
统一执行——不走两套判断。

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11+（开发环境 3.14）、Flask 3.1、PyMySQL 手写 SQL、waitress |
| 数据库 | MySQL 8+，迁移是手写编号 SQL + 单一 runner（不用 ORM / Alembic） |
| 前端 | Vue 3.5、TypeScript 5.7、Vite 6、vue-router 4 |
| 智能体 | DeepSeek Harness 运行时 + TypeScript 插件（`agent-runtime/`）注册业务工具 |
| 测试 | pytest（后端）、Vitest + Playwright（前端） |

不在依赖里：SQLAlchemy、Alembic、Redis、任何 ORM。

## 快速开始

需要一个可用的 MySQL 实例（默认 `127.0.0.1:3306`）。

```powershell
git clone git@github.com:Changxin-YR/FPA.git
cd FPA
```

**1. 建库建号**

```powershell
cd FPA
$env:MYSQL_ROOT_PASSWORD='<你的 root 密码>'    # bootstrap 需要 root
python tools\bootstrap_db.py                   # 幂等
```

**2. 建表 + 权限码 + 演示数据**

```powershell
$env:PYTHONPATH='<你的路径>\backend'
$env:MYSQL_USER='fpa'; $env:MYSQL_PASSWORD='fpa_dev_password'; $env:MYSQL_DATABASE='fpa'

python tools\migrate.py apply        # 也可用 status / verify / reset（reset 仅开发环境）
python tools\seed_permissions.py     # 从能力注册表派生权限码，幂等
python tools\seed_acceptance.py      # 可选：造一套能点的演示数据
```

**3. 起后端（5101）**

```powershell
$env:PORT='5101'; $env:APP_ENV='development'
python tools\serve_dev.py 5101       # waitress，没有 reloader，改完后端要重启
```

**4. 起前端（5273）**

```powershell
cd frontend
npm install
npm run dev                          # http://127.0.0.1:5273/
```

打开 <http://127.0.0.1:5273/>，用 `demo` / `Demo1234!` 登录。
`demo` 是演示账号，持有全部权限码和基地级数据范围；库里另有一个只带一条权限的
`web_e2e_user`，那是自动化用例的夹具，不适合拿来手点。

完整的变量清单见 `.env.example`。智能体相关的几个变量（`AGENT_*`）不配也能跑，
只是那条链路会静默降级为不可用。

## 测试与自检

```powershell
# 后端
python -m pytest tests -q                     # 380 passed, 29 skipped
python tools\preflight.py                     # 语法 / 逐模块 import / 组合根 / 格式门禁
python tools\registry_reconcile.py --check    # 能力台账对账
python tools\check_source_hygiene.py          # BOM / CRLF / 末行换行

# 域级端到端（需要真实 MySQL）
python tools\master_data_e2e.py               # 或 purchase / sales / warehouse / production / cost / runner / web

# 前端
cd frontend
npx vue-tsc --noEmit
npx vitest run
npm run lint
npx playwright test
```

`preflight.py` 的 exit=1 不等于装不起来：它把「装载健康」和「格式门禁」分开报，
第 1–3 节失败才是真的装不起来。

## 目录

```
backend/fpa/kernel/        能力声明、字段、数据范围、事务、不变量、执行器、审计、金额口径
                           —— 派生规则的唯一来源
backend/fpa/domains/       master_data / production / warehouse / purchase / sales / cost
                           ＋ access / identity / audit
backend/fpa/web/           能力自动生成路由、鉴权、Agent 网关、响应信封
backend/fpa/agent/         智能体网关（工具白名单 → 网关校验 → 业务服务二次校验，三层防御）
backend/fpa/harness/       Harness 子进程池与环境隔离
backend/fpa/bootstrap.py   组合根：按目录自动发现并装载每个域的 capabilities.py
database/migrations/       000–012，纯 SQL、可重入
frontend/src/layers/common/   元数据驱动的前端内核：DataTable / DynamicForm / ResourceListPage /
                              ListFilterBar / ResourceDetailPage / AgentPanel / meta.store
frontend/src/layers/product/  只允许三个非资源页：登录、工作台、塘口详情
agent-runtime/             Harness 插件：工具注册与 schema 生成
tools/                     20+ 个自检与端到端工具，每个都能单独跑
tests/                     单元 / 契约 / 守卫（含反向对照过的探针）
docs/                      见下
```

## 文档

| 文档 | 用途 |
|---|---|
| `docs/ARCHITECTURE.md` | 分层与三条不可违反的边界 |
| `docs/CAPABILITY_REGISTRY.md` | 权威能力清单（§1–§9）＋ 字段表、状态机、不变量 |
| `docs/INTERFACES.md` | 对外契约：`/meta/capabilities` 形状、Agent 对话协议、错误码 |
| `docs/DEVELOPMENT.md` | 上手：本地环境、加一个域的固定步骤、踩过的坑 |
| `docs/ROADMAP.md` | 当前进度、已验证的部分、已知缺口 |
| `docs/DECISIONS.md` | 设计取舍记录（含"为什么推翻某条已有决定"） |
| `docs/INVARIANT_TYPES.md` | 18 类声明式不变量的语义与用法 |
| `docs/WRITE_CONTRACT.md` / `docs/ROW_ACTIONS.md` | 写入语义（幂等、回读、乐观锁）与行内动作 |
| `docs/DISPLAY_CONTRACT.md` | 跨端显示口径（日期 / 金额小数位 / 计量单位） |
| `docs/DEPLOY.md` | 部署与运行 |

## 状态

- 运行时注册 **82 条能力**，文档声明 86 条（差的 4 条是 `meta.capabilities` 与
  `auth.login/logout/me`，由固定路由提供，不进注册表）。
- 六个业务域 + access / identity / audit 全部落地，18 类不变量覆盖注册表 §4 的 22 条规则。
- 智能体侧 63 个业务工具挂进 Harness，逃逸类内建工具已关闭，闭环（自然语言 → 模型 →
  工具 → 真库）已实测。
- 前端 19 个入口共用一条动态路由，页面按元数据渲染。
- **未完成**：`farm.list`（「归属对象类型 = 基地」取不到候选）、工作台待办、其余资源的
  详情页、以及生产化的进程/反代配置（Windows 上目前用 waitress，Nginx 与 systemd 未做）。
  详见 `docs/ROADMAP.md`。
