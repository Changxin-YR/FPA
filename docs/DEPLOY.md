# 部署与运行

> **一句话现状**：仓库里**唯一被真实验证过的运行形态是「本地开发」**（后端 waitress + 前端 Vite dev server）。
> 生产部署产物（Dockerfile / Nginx / CI）**没有** —— 这是**有意留下的欠账**，清单见 §4，别误当成"漏提交"。

---

## 1. 本地开发（已验证）

```powershell
cd <repo>

# 后端 5101（**必须带齐这些 env，漏一个就静默失败**）
$env:PYTHONPATH='<repo>\backend'
$env:MYSQL_USER='yuxin'; $env:MYSQL_PASSWORD='yuxin_dev_password'
$env:MYSQL_DATABASE='yuxin'; $env:PORT='5101'; $env:APP_ENV='development'
$env:AGENT_DSH_HOME='<repo>\.dsh-home'
$env:AGENT_HARNESS_ROOT='<harness-runtime>'
$env:AGENT_DSH_BIN='<repo>\agent-runtime\bin\run.cmd'
$env:AGENT_GATEWAY_URL='http://127.0.0.1:5101/api/v1/agent'
python tools\serve_dev.py 5101

# 前端 5273（另一个终端）
cd frontend; npm run dev
```

浏览器 <http://127.0.0.1:5273/>，账号 `demo` / `Demo1234!`。

**改过 Agent 插件（`agent-runtime/`）后必须重建 + 重装**：Harness 加载的是
`<DSH_HOME>/profiles/sdk/node_modules/@yuxin/dsh-biz-tools/` 的**副本**，不是 `agent-runtime/src`——
只跑 `tsc` 不会生效（实测踩过：改了 `ask_user` 描述，模型行为没变）。

```powershell
cd agent-runtime; npm run build; cd ..
python tools\live_agent_e2e.py      # 第 5 步重建/重装插件，并跑一遍真模型闭环
# 或手动：把 agent-runtime\lib\* 与 package.json 复制到
#   <DSH_HOME>\profiles\sdk\node_modules\@yuxin\dsh-biz-tools\
# 然后**重启后端**（Harness 会话按会话池存活，不重启会继续用旧插件）。
```

人格与内建工具禁用表在仓库根 `agent-runtime\cordis.patch.yml`（后端以 `patches=` 下发），
改它只需重启后端，不需要重装插件。

**两个端口的耦合**：`frontend/vite.config.ts` 的 `server.proxy` 把 `/api` 反代到
`127.0.0.1:5101`（与 `tools/serve_dev.py` 的默认端口配套）—— **改一个就要改另一个**。

**改了后端必须重启**：`tools/serve_dev.py` 用 waitress，**没有 reloader**。
判断"跑的是不是新代码"的判据：现算磁盘注册表条数，与
`GET /api/v1/meta/capabilities` 的在线条数比对；不等就是进程旧。

## 2. 依赖

```powershell
python -m pip install -r backend\requirements.txt      # Flask / PyMySQL / waitress / pydantic / pytest …
```

| 依赖 | 说明 |
|---|---|
| Python **3.14** | `yuxin` **未安装为 editable**，必须给 `PYTHONPATH=<repo>\backend` |
| MySQL **9.7** @ `127.0.0.1:3306` | 库 `yuxin`，账号 `yuxin` / `yuxin_dev_password` |
| Node | 前端开发服务器；`frontend/node_modules` 已就位（`npm ci` 可重建） |
| **`waitress`** | 实际的 WSGI 服务器。**Windows 上 gunicorn 不可用**（依赖 `fcntl`），所以 requirements 里同时列着 gunicorn 与 waitress，生产按平台二选一 |
| **`deepseek-harness-sdk`** | 由**本地运行时 wheel** 提供（dev 环境是 editable 版）；根目录在 `AGENT_HARNESS_ROOT` |
| `DEEPSEEK_API_KEY` | **机器级环境变量**。只引用变量名，**绝不写进任何文件** |

## 3. 数据库

```powershell
$env:MYSQL_ROOT_PASSWORD='1234'        # 只有 reset 需要 root
python tools\bootstrap_db.py           # 建库建账号（幂等）
python tools\migrate.py apply          # 或 status | verify
python tools\migrate.py reset          # ★ 仅开发环境（APP_ENV=production 会拒绝）
python tools\seed_permissions.py --demo-role  # 派生权限码 + 验收演示角色
python tools\seed_acceptance.py        # 业务角色 / 数据范围 / demo+qa 账号（重置后必跑）
# 业务数据（P-*/PO-2026-* 数据集）：
#   mysql -u root -p yuxin < database\test_data.sql
python tools\schema_parity.py          # 真库形状 vs 迁移文件（migrate verify 的盲区）
```

> **为什么 `reset` 之后必须跑 `seed_acceptance.py`**：迁移只建表 + 结构种子
> （企业/基地/区域/物料/仓库），`seed_permissions.py` 只种权限码与一个演示角色；
> 验收/自检依赖的 8 个业务角色、数据范围与 `demo`/`qa-maker`/`qa-checker` 账号
> **不在任何迁移里**。少了这一步，`demo` 登录不了，整套门禁会以"登录失败"的名义全红。
>
> **交付文档自动生成**：`python tools\gen_rebuilt_data.py` 从当前库现算
> `docs/CAPABILITY_REGISTRY.md`（与 `/admin/*` 及各资源列表逐行一致），不要再手工维护。

`tools/schema_parity.py` 的退出码：`0` 一致、`1` 有差异、`2` **未能证明一致（≠ 一致）**。
它有一个已知例外清单 `TOOL_OWNED_TABLES`（`agent_ponds` / `loader_probe_rows` /
`selftest_ponds` / `web_ponds` 是 e2e 工具自建的表，不是漂移）。

## 4. 生产化欠账（明确清单）

| 欠账 | 说明 |
|---|---|
| **没有 Dockerfile / docker-compose** | 仓库里只有 `deploy/`（空目录） |
| **没有 Nginx 配置** | 静态资源目前由 Vite dev server 直接提供 |
| **前端没有"构建产物 + 托管"的说明** | `frontend/dist` 能构建出来（`npm run build`），但没有接入任何静态托管 |
| **没有 CI** | 门禁靠手工跑（命令见 `README.md` 的「验证」一节） |
| **没有备份/回滚脚本** | 迁移**没有 down**；回滚靠数据库备份 + `migrate verify` / `schema_parity` 比对 |
| **单进程** | waitress 单实例；多 worker 下的幂等/限流语义未验证（`idempotency_keys` / `rate_limits` 在库里，设计上支持，但没有实测多进程） |
| 生产 WSGI | Linux 建议 gunicorn（`-w` 与 `idempotency_keys` 的配合需要复测）；Windows 用 waitress |

## 5. 上线前 / 上线后的验证

```powershell
python tools\preflight.py                 # 一秒：装载健康 + 格式门禁
python -m pytest tests -q                 # 408 passed / 1 skipped（以实际运行为准）
python tools\registry_reconcile.py --check # 能力台账对账
python tools\migrate.py verify            # 迁移登记值 vs 文件
python tools\schema_parity.py             # 真库形状 vs 迁移文件
python tools\web_e2e.py                   # HTTP 全链路（真 Flask + 真 MySQL）
python tools\live_agent_e2e.py            # 闭环：真模型 → 工具 → 真库（需 API key）
```

**不要只看端口判断服务活着**：

```powershell
curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:5101/api/v1/auth/me   # 未登录应为 401
```

## 6. 回滚

* 迁移**只有前进**（`up` 语义），没有 `down`。回滚 = 恢复数据库备份 + 重新 `migrate verify`。
* 代码回滚：本仓库是普通 git 仓库（无 remote），`git log` 里每一轮收口都有提交点。
* 数据回滚：写路径全部在**单个事务**内（`docs/WRITE_CONTRACT.md` 规则 1），
  失败即整体回滚；审计表在**数据库层**禁止 UPDATE / DELETE。
