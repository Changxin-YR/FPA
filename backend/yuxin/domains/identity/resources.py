"""identity 域的资源声明（前端列表页的列与状态字典的唯一来源）。

## 为什么需要它：`Capability.resource` 必须能解析

`Capability.resource` 是前端"按资源聚合能力"的键，而
`tests/test_architecture.py::test_capability_resources_resolve` 要求它必须在
`RESOURCES` 里存在。本次落盘的 7 条能力里有三条引用了此前不存在的资源名
（`access_user` / `access_role` / `audit_log`），所以这里把三个资源登记上。

## 为什么放在 identity 域，而 access / audit 的能力也引用它

按**表的归属**分域：三个资源对应 `users` / `roles` / `audit_logs`，
而账号与改密改的是同一张 `users` 表——所以 `access_user` 归 identity 域拥有，
`domains/access/capabilities.py` 引用它（跨域引用资源是允许的，
`docs/ROLLOUT_CONTRACT.md` §2 禁止的是直接读写另一个域的**表**，
而这里只是共享一份展示元数据）。

## 这三个资源**没有状态机**，这是刻意的

`Resource.workflow` 表示"这台状态机属于谁"，并从它派生 `status_dict` 与
`row_actions`。而：

* **账号**只有 `users.status` 一列，且可设置值只有两个
  （`active` / `disabled`，见 `access/admin.py::SETTABLE_USER_STATUSES`）。
  把它写成 `Workflow` 会凭空造出一台状态机：`row_actions` 需要"每个状态允许什么动作"，
  而账号上的动作（改状态、改授权）**都是从任意状态出发的 action**，
  不存在"草稿→待核验→已核验"那种沿状态推进的路径。
* **角色**同理（只有 `active` / `disabled`）。
* **审计日志是 append-only 的**（001 迁移用触发器在数据库层禁止 UPDATE/DELETE）。
  给一个不可变的流声明状态机，等于声明一台永远不会转移的机器——
  `docs/ROW_ACTIONS.md` §3.1 把这类"声明了却推不动"的东西归为应当删掉的动作。

没有状态机的资源在元数据里就没有 `status_dict` / `transitions`，
前端据此不渲染状态列与行内按钮。**不渲染 > 渲染出来再失败**。

## 与 `docs/CAPABILITY_REGISTRY.md` 的关系：只补**表中没有的**列

本文件列出的列名**必须**是迁移里真实存在的列。两处具体的偏离，理由都在
`access/admin.py` 的模块 docstring 里（registry §2.4 的字段表沿用了早期版本的列名
`name` / `phone` / `login_name` / `assigned_by`，而 001 迁移建的是
`username` / `display_name`，且没有 phone 与 login_name 两列）：

  * `display_name`（不是 `name`）、`username`（不是 `phone` + `login_name`）
  * `access.user.create` 的 `note` 字段**不做**：`users` 表没有这一列。
    要它需要一次迁移加列，而那属于 db-migration 的工作，不属于"补齐 7 条能力"。
    声明一个写不进去的字段，是"看起来能用"的同一类缺陷。
"""

from __future__ import annotations

from yuxin.domains.access.admin import STATUS_LABELS
from yuxin.kernel.fields import Choice
from yuxin.kernel.workflow import RESOURCES, FilterKind, FilterSpec, Resource

RESOURCES.register(
    Resource(
        name="access_user",
        title="账号",
        module="access",
        list_path="/api/v1/admin/users",
        detail_path="/api/v1/admin/users/{user_id}",
        # 表名显式声明：兜底公式（`<module>_<name>s`）会算出 `access_access_users`。
        # 这与 cost 域的 `cost_entry` → `cost_cost_entrys` 是同一个坑：
        # 猜错了**不报错**，只有真正用到表名的规则才会在很远的地方撞上"表不存在"。
        table="users",
        columns=(
            ("username", "登录名"),
            ("display_name", "姓名"),
            ("role_names", "角色"),
            ("scope_names", "数据范围"),
            # 原始状态码：`status_label` 供人看，`status` 是**筛选控件的取值列**。
            # 两者同时存在是刻意的（与 `unit` / `unit_label` 同一条纪律）：
            # 筛选条上的候选项是一个集合，而"取哪个值"必须能在一行数据里取到。
            ("status", "状态码"),
            ("status_label", "状态"),
        ),
        # 对应 `AdminService.list_users` 的 `params.get(...)`。
        # 关键词列是 `u.username` / `u.display_name`。
        #
        # 为什么**没有** `role_code`：那个参数的取值是**角色码**（如 `admin`），
        # 而列里只有 `role_names`（角色名）。`roles` 表没有 list 能力，
        # 因此既不能靠列取值、也没有可引用的列表接口——照实不声明。
        # 收口方向：给角色加一条读能力（`Resource(name="access_role")` 已在册，
        # 缺的是 `access.role.list` 能力），之后它就能作为 ref 筛选下发。
        search=True,
        filters=(
            # `users.status` 的 ENUM 有 4 个值而**可设置**的只有 2 个
            # （`pending` / `retired` 见 `admin.py::SETTABLE_USER_STATUSES` 的说明）。
            # 这里用 ENUM 而不是 `status`：账号没有状态机（`Resource.workflow=None`），
            # 前端拿不到 `status_dict`，用 STATUS 会渲染出一个空的候选集合。
            # 候选值与中文都取自 `STATUS_LABELS`（中文的唯一来源）。
            FilterSpec(
                "status",
                "状态",
                FilterKind.ENUM,
                choices=tuple(
                    Choice(code, STATUS_LABELS[code])
                    # `retired` **不在** `SETTABLE_USER_STATUSES` 里（不能被设置），
                    # 但它是**可筛选**的：默认列表排除了它，筛选条必须能把它找回来，
                    # 否则退役账号变成一个看不见也搜不到的黑洞。
                    for code in ("active", "disabled", "retired")
                ),
            ),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="access_role",
        title="角色",
        module="access",
        list_path="/api/v1/admin/roles",
        detail_path="/api/v1/admin/roles/{role_id}",
        table="roles",
        columns=(
            ("code", "角色码"),
            ("name", "角色名称"),
            ("description", "说明"),
            ("status_label", "状态"),
        ),
    )
)

RESOURCES.register(
    Resource(
        name="access_scope",
        title="数据范围",
        module="access",
        list_path="/api/v1/admin/scopes",
        table="data_scopes",
        columns=(
            ("code", "范围编号"),
            ("name", "范围名称"),
            ("scope_type_label", "范围类型"),
            ("scope_target_label", "生效范围"),
            ("status_label", "状态"),
        ),
    )
)

__all__ = ["RESOURCES"]
