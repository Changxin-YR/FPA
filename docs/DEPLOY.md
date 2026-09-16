# 部署与运行

> 说明：本文档里出现的 `/fpa/`、`fpa-next.service`、`/etc/fpa/`、`adp_new` 等，都是**服务器上仍然存在的遗留部署标识**（上一代站点与老库），不是本项目现在的名字。
> 保留它们是因为回滚命令与老库迁移命令必须照着服务器上的真实名字写。

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
# ★ 还要放一份「只含 insert」的发布副本，且**不能**直接拷 agent-runtime\cordis.patch.yml
#   （那是部署层 patch，带 52 条 disabled；两处都描述安全边界会被 harness_tools_audit 判红）：
#   python -c "import sys;sys.path.insert(0,'tools');from pathlib import Path;from live_agent_e2e import _write_publish_manifest as w;w(Path('agent-runtime/cordis.patch.yml'), Path(r'<DSH_HOME>\profiles\sdk\node_modules\@yuxin\dsh-biz-tools\cordis.patch.yml'))"
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

---

## 附：从老库（`adp_new` 等旧库名）迁移到 `yuxin`

一次性运维步骤，只在「库里已有数据、不想重建」时需要。

```powershell
$env:MYSQL_ROOT_PASSWORD='<root 密码>'
python tools\bootstrap_db.py            # 建 yuxin 库 + yuxin 账号 + 授权（幂等）

$mysql = 'C:\Program Files\MySQL\MySQL Server 9.7\bin'
cmd /c "`"$mysql\mysqldump.exe`" -u root -p<root 密码> --single-transaction --routines --triggers --events --set-gtid-purged=OFF adp_new > old.sql"
cmd /c "`"$mysql\mysql.exe`" -u root -p<root 密码> --default-character-set=utf8mb4 yuxin < old.sql"
```

### 坑 1：导入后必须重建审计触发器

`mysqldump` 会把 `DEFINER=<旧账号>@127.0.0.1` 一并带过来，而旧账号在新库上没有
`TRIGGER` 权限 —— 应用账号更新 `audit_logs` 会报
`1142 TRIGGER command denied to user '<旧账号>'`，表现是
`tools/db_selfcheck.py` 的两条审计断言变红。
**以应用账号（`yuxin`）重建即可**，DDL 见 `database/migrations/001_identity_access_governance.sql`：

```sql
DROP TRIGGER IF EXISTS audit_logs_no_update;
DROP TRIGGER IF EXISTS audit_logs_no_delete;
CREATE TRIGGER audit_logs_no_update BEFORE UPDATE ON audit_logs FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs is append-only';
CREATE TRIGGER audit_logs_no_delete BEFORE DELETE ON audit_logs FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs is append-only';
```

### 坑 2：`migrate.py verify` 的 checksum 漂移是既有问题

`python tools\migrate.py verify` 会对 10 个迁移报「应用后被修改过」。这是**迁移之前就存在**的：
老库建于 2026-09-15，此后迁移文件被编辑过（已用 `git show <改名前的 commit>` 逐个 A/B 证实，
与本次重命名无关）。它不影响 `migrate.py apply` 与运行时；要彻底消除需重建库或重新登记
`schema_migrations.checksum`。

---

## 生产部署记录：`https://23331.cloud/yuxin/`

> 2026-09-16 实际部署到 `root@1.14.148.15`（CentOS 7.6 / nginx 1.20.1 / MySQL 8.4 / Python 3.14.4）。
> 老站点 `https://23331.cloud/fpa/`（上一代 FPA 部署）**未改动**，两者并行。

| 项 | 值 |
|---|---|
| 发布目录 | `/opt/adp/releases/yuxin-<时间戳>/`（含 `backend/`、`database/`、`tools/`、`frontend/dist/`） |
| 生效指针 | `/opt/yuxin/current` → 发布目录（软链，回滚只改它） |
| Python 环境 | 复用 `/opt/adp/login-registration/.venv`（Python 3.14.4，依赖版本与本仓 requirements 一致） |
| 环境变量 | `/etc/yuxin/yuxin.env`（权限 600） |
| 服务单元 | `yuxin.service`（gunicorn 2×2，`127.0.0.1:5004`，`yuxin.wsgi:app`）+ `yuxin.service.d/resources.conf`（内存/CPU 限额） |
| 数据库 | MySQL `yuxin` 库 + `yuxin@localhost` / `yuxin@127.0.0.1` 账号（口令在 env 文件） |
| nginx | `/etc/nginx/snippets/yuxin-location.conf`，由 `/etc/nginx/conf.d/23331.cloud.conf` include（原文件备份 `23331.cloud.conf.bak-yuxin`） |
| 路径映射 | `/yuxin/` → 静态 dist；`/yuxin/api/` → 重写为 `/api/` 反代 5004；`/yuxin/healthz` 由 nginx 直接返回 200 |

### 发布（增量）

```powershell
# 1) 本地：按子路径构建前端
cd frontend; $env:VITE_PUBLIC_BASE_PATH='/yuxin/'; npx vite build --outDir dist-yuxin
# 2) 打包（backend/ database/ tools/ + frontend/dist ← dist-yuxin）并上传
tar -czf $env:TEMP\yuxin.tgz -C <stage> .
scp -i $env:USERPROFILE\.ssh\adp_server_ed25519 $env:TEMP\yuxin.tgz root@1.14.148.15:/tmp/yuxin.tgz
```

```bash
# 3) 服务器：解包 → 切软链 → 迁移 → 重启（nginx -t 通过才 reload）
NEW=/opt/adp/releases/yuxin-$(date +%Y%m%d-%H%M%S)
mkdir -p $NEW && tar -xzf /tmp/yuxin.tgz -C $NEW && rm -f /tmp/yuxin.tgz
chown -R adp:adp $NEW
chmod -R u=rwX,g=rX,o=rX $NEW        # ★ 必须给 o+rX，否则 nginx 读不到 → 首页 404
ln -sfn $NEW /opt/yuxin/current.tmp && mv -Tf /opt/yuxin/current.tmp /opt/yuxin/current
set -a; . /etc/yuxin/yuxin.env; set +a; cd /opt/yuxin/current
/opt/adp/login-registration/.venv/bin/python tools/migrate.py apply
systemctl restart yuxin && nginx -t && systemctl reload nginx
```

### 回滚

```bash
ln -sfn /opt/adp/releases/<上一个 release> /opt/yuxin/current.tmp \
  && mv -Tf /opt/yuxin/current.tmp /opt/yuxin/current \
  && systemctl restart yuxin
```

### 已知取舍

- **智能体链路未接**：`AGENT_DSH_HOME=/opt/yuxin/dsh-home` 是空目录，AI 页面会提示暂不可用。
  要接需在服务器上装 `@yuxin/dsh-biz-tools` 插件并配 `AGENT_HARNESS_ROOT` / `DSH_RUNTIME_MODE`
  （服务器已有 `/opt/deepseek-harness-20260907` 与 `/opt/adp-agent-runtime-20260907`）。
- **验收种子已写进生产库**：`tools/seed_acceptance.py` 造了 `demo` / `qa-maker` / `qa-checker`
  与 8 个演示角色。演示完按提示摘掉：`DELETE FROM roles WHERE code LIKE 'yuxin-demo-%';`
- 触发器由 `yuxin@localhost` 创建，DEFINER 与该账号一致，不会出现跨库 `1142 TRIGGER command denied`。
- nginx 的 SPA 回落必须是 `try_files $uri /index.html =404;`——只写 `$uri =404` 会让
  `/yuxin/auth/login` 这类深链接返回 nginx 404（实测踩到）。

### 线上 agent（塘小助）接线

`/yuxin/` 与老 `/fpa/` **共用**同一个 DSH home（`/var/lib/adp/agent-sidecar`）与运行时 install
（`/opt/adp-agent-runtime-20260907`），接线沿用老站约定，只改 patch 路径与端口：

| 键 | 值 |
|---|---|
| `AGENT_DSH_HOME` | `/var/lib/adp/agent-sidecar` |
| `AGENT_DSH_BIN` | `/opt/adp-agent-runtime-20260907/node_modules/.bin/dsh` |
| `AGENT_HARNESS_ROOT` | `/opt/adp-agent-runtime-20260907` |
| `AGENT_HARNESS_PATCH` | `/opt/yuxin/current/agent-runtime/cordis.patch.yml` |
| `AGENT_PROFILE` / `DSH_RUNTIME_MODE` | `sdk` / `node` |
| `AGENT_MODEL` / `DEEPSEEK_BASE_URL` | `qwen-plus` / 阿里百炼兼容端点（照抄 `/etc/fpa/fpa.env`） |

安装步骤（**三处都要，缺一处只会静默降级成「塘小助暂时不可用」**）：

1. **Python SDK**：把 `deepseek_harness` 与 `deepseek_harness_runtime` 从
   `/opt/deepseek-harness-20260907/python/{sdk/src,sdk-runtime/src}` 复制进 venv 的 site-packages，
   并在 `deepseek_harness_runtime/runtime/node/node_modules/@deepseek-ai/dsh` 建软链指向运行时里的 dsh 包。
2. **插件包必须装进「运行时 install」**：`/opt/adp-agent-runtime-20260907/node_modules/@yuxin/dsh-biz-tools/`。
   原因：ESM 解析是从 **loader 自己的位置**逐级往上找，`NODE_PATH`（只对 CJS 生效）与 profile 里那份都救不了。
   症状是 `ERR_MODULE_NOT_FOUND: Cannot find package '@yuxin/dsh-biz-tools' imported from
   .../cordis-plugin-loader/lib/index.js`，而上层只显示「智能助手通信异常」。
3. **插件同时装一份到 profile**（`<DSH_HOME>/profiles/sdk/node_modules/@yuxin/dsh-biz-tools/`），
   且 manifest 必须是**只含 insert 的发布副本**（照抄整份部署层 patch 会被 `harness_tools_audit` 判红）。

`DEEPSEEK_BASE_URL` 由后端逐次显式注入子进程（`session.py::_resolve_base_url`）——
兼容端点（阿里百炼）部署必须依赖这条转发，否则模型侧鉴权失败而表现成**空回复**。

**自检**：登录 `https://23331.cloud/yuxin/` → 打开「塘小助」问业务问题（实测答出「当前一共有 6 个塘口」）。

### `/fpa/` 已并入 `/yuxin/`（2026-09-16）

老站点不再单独运行，但**老链接继续可用**：

- nginx：`/etc/nginx/snippets/fpa-location.conf` 已改为 301 跳转（`/fpa/<path>` → `/yuxin/<path>`），
  原配置备份为同目录 `fpa-location.conf.bak-retire`；
- 服务：`systemctl disable --now fpa-next.service`（**发布文件与 `/etc/fpa/fpa.env` 均保留**）。

恢复老站（回滚）：

```bash
cp /etc/nginx/snippets/fpa-location.conf.bak-retire /etc/nginx/snippets/fpa-location.conf
systemctl enable --now fpa-next.service && nginx -t && systemctl reload nginx
```

> `/yuxin/` 的智能体**不依赖**老服务：它用自己的 `/etc/yuxin/yuxin.env`；与老站共享的只有 DSH home
> （`/var/lib/adp/agent-sidecar`）与运行时 install（`/opt/adp-agent-runtime-20260907`），两者都与
> `fpa-next.service` 无关。实测停服后塘小助照常回答（「当前一共有 6 个塘口」）。
