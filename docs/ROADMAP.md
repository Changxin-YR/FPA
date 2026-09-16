# 进度与待办

> 本文记「做到哪了、还剩什么」，架构与开发约定见 `ARCHITECTURE.md` 与 `DEVELOPMENT.md`。
> 下面所有数字都是实测读数，命令都写在文中，可以自己复跑。

---

## 1. 怎么确认这份代码是健康的

按顺序跑，全绿即可：

```powershell
$env:PYTHONPATH='<repo>\backend'
$env:MYSQL_USER='yuxin'; $env:MYSQL_PASSWORD='yuxin_dev_password'; $env:MYSQL_DATABASE='yuxin'

python tools\preflight.py                 # 一秒：语法 / 逐模块 import / 组合根 / 格式门禁
python -m pytest tests -q                 # 380 passed / 29 skipped
python tools\registry_reconcile.py --check
python tools\check_source_hygiene.py
python tools\gen_contract_docs.py --check
```

需要真实 MySQL 的那些（`tools\*_e2e.py`、`tools\live_agent_e2e.py`）见 `DEVELOPMENT.md`。
`preflight.py` 的 exit=1 不一定是坏事：它把「装载健康」（§1–§3）与「格式门禁」（§4）分开报。

---

## 2. 已完成

* **内核**：能力声明 → 路由 / 权限码 / DataScope 谓词 / 幂等策略 / 确认闸门 / 审计字段 /
  Agent Tool schema 全部派生；18 类声明式不变量挂在执行器上统一执行。
* **六个业务域**全部落地：`master_data`、`production`、`warehouse`、`purchase`、`sales`、
  `cost`，外加 `identity`、`access`、`audit`。运行时注册 82 条能力。
* **智能体**：63 个业务工具挂进 Harness，逃逸类内建工具已关闭；三层防御（工具白名单 →
  Agent Gateway 权限校验 → 业务服务二次校验）逐层有测试；人工确认闸门前端有确认卡。
  闭环（自然语言 → 模型 → 工具 → 真实数据库）已实测。
* **前端**：19 个入口共用一条动态路由，列表 / 表单 / 详情 / 行内动作全部按元数据渲染；
  筛选、分页、写后刷新、弹窗可访问性都有用例。

---

## 3. 验证方式（几条值得单独说）

* `tools\registry_reconcile.py --check`：把「代码里实际注册的能力」和
  `docs/CAPABILITY_REGISTRY.md` 里声明的条目对账，防止文档漂移。
* `tools\gen_contract_docs.py --check`：文档里的生成区必须与内核输出一致。
* `tools\live_agent_e2e.py`：真实模型 + 真实工具 + 真实库，验证的是"模型真的把数据写进去了"，
  而不是"函数被调用了"。跑得慢，需要机器级 API key。
* `tests\test_architecture.py`：把三条边界（内核不 import Flask/PyMySQL/domains 等）变成断言。

---

## 4. 未完成 / 已知问题

### 4.1 P0

* `farm.list`：资源「归属对象类型 = 基地」的候选列表取不到值，导致基地维度的下拉是空的。

### 4.2 P1

* 详情页：目前只有塘口有独立详情页，其余资源点「查看」落到通用详情页，字段与关联信息的
  呈现还不够。
* 写后刷新：列表页在跨表写入（例如审批改变了关联单据状态）后不会自动刷新。

### 4.3 P2

* 工作台待办：`workbench` 域只有读能力，待办聚合还没做。
* 生产化：进程管理、反向代理、静态资源与 HTTPS 都还没做（见 `DEPLOY.md` 的欠账清单）；
  Windows 上现在只能用 waitress 起服务。
* `frontend/src/layers/common/types.gen.ts` 由后端契约生成，改后端字段后记得重新生成。

---

## 5. 加一个域的固定步骤

见 `DEVELOPMENT.md` §3，按那七步走；`cost`、`purchase`、`sales`、`warehouse` 都是照它做的，
可以当模板抄。
