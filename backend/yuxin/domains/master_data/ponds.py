"""塘口服务与能力声明。

**这是本项目的模板**：其余业务域照这个结构写。它演示了五件事怎么串起来：

    ① 字段声明一次（`Annotated[T, Field(...)]`）
       -> 同时供给 Pydantic 式校验、前端表单、Agent 工具 schema、OpenAPI
    ② 能力声明一次（`Capability(...)`）
       -> 同时供给 REST 路由、权限码、DataScope 谓词、幂等策略、确认闸门
    ③ 状态机声明一次（`Workflow(...)`）
       -> 同时供给 `status_dict`、`row_actions`、合法转移校验、`to_status` 的动态候选
    ④ 服务自己再校验一次（第三层防御）——**不信任调用方**
    ⑤ 审计与业务同事务（由 `CapabilityRunner` 保证）

## 一个反直觉的注意点：`f_ref` 与 `f_int` 的区别

`area_id` 用 `f_ref`，`aerator_count` 用 `f_int`。两者最终都是整数，
但语义不同：前者是**外键**（前端要拉下拉、后端要校验存在性），
后者是**数量**。用 `f_int` 声明外键会让前端渲染成输入框——
用户得自己知道区域 id 是多少。**类型要表达语义，不只是存储形态。**
"""

from __future__ import annotations

from typing import Any

from yuxin.kernel.workflow import allowed_actions_for
from yuxin.kernel import capability as cap
from yuxin.kernel.errors import DomainError, ErrorCode, not_found, validation
from yuxin.kernel.fields import f_int, f_num, f_ref, f_str, f_enum, text
from yuxin.kernel.scope import Scope
from yuxin.kernel.uow import UnitOfWork
from yuxin.kernel.workflow import RowAction

from .service import POND_STATUS_WORKFLOW, POND_WORKFLOW

#: 允许作为**初始**状态的业务状态（继承旧 `master_data_service.py:29`）。
CREATE_POND_STATUSES = ("build", "stocked")

#: 容量上限（继承旧 `master_data_service.py:26` 的 MAX_CAPACITY_MU）。
MAX_CAPACITY_MU = 100000


class PondService:
    """塘口的读写。

    每个方法都做三件事：**校验（服务自己的第二遍）-> 写库 -> 返回可回读的结果**。
    `HandlerResult.resource_id` 不是可选的——执行器要按它回读校验。
    """

    # -- 内部工具 -------------------------------------------------------------

    @staticmethod
    def _scope_row(tx: UnitOfWork, scope: Scope, pond_id: int) -> dict[str, Any]:
        """按主键取一行，**并校验它在数据范围内**。

        这是第三层防御里最关键的一步：它校验的不是"令牌对不对"，而是
        **这一行数据属不属于当前用户**。两种校验的失效模式不同——
        第二层被绕过时，这一层仍然拦得住，因为它看的是数据本身。
        """
        row = tx.query_one(
            """
            SELECT p.*, a.name AS area_name
            FROM ponds AS p
            LEFT JOIN areas AS a ON a.id = p.area_id
            WHERE p.id = %s
            """,
            (pond_id,),
        )
        if row is None:
            raise not_found("塘口")
        if not scope.allows_row(row):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, "该塘口不在当前账号的数据范围内"
            )
        return row

    @staticmethod
    def _decorate(row: dict[str, Any], scope: Scope, permissions: frozenset[str]) -> dict[str, Any]:
        """给一行加上前端需要的派生字段。

        三个派生项，每一个都对应早期版本的一处缺陷：

        * `status_label` / `pond_status_label` —— 中文文案**只在这里产生**。
          早期版本把同一状态在 14 处各自翻译（`verified` 在成本页叫「待确认」、
          在其他页叫「已核验」、在 agent 词典里叫「已提交」）。
        * `allowed_actions` —— 由状态机算，前端只渲染不推导。
        * `version` —— 统一叫 `version` 而不是 `row_version`，
          因为前端在 4 处地方猜过这个字段名。
        """
        record_status = str(row.get("status") or "")
        pond_status = str(row.get("pond_status") or "")

        # 行内动作 = 记录生命周期的动作，按权限过滤。
        # 权限过滤在这里做（而不是只在前端）是因为 allowed_actions 是**渲染依据**，
        # 把无权动作渲染出来再让点击时失败，是把校验成本转嫁给用户。
        actions = allowed_actions_for(POND_WORKFLOW, "pond", record_status, permissions)

        decorated = dict(row)
        decorated["version"] = int(row.get("row_version") or 1)
        decorated["status_label"] = _label_of(record_status)
        decorated["pond_status_label"] = _label_of(pond_status)
        decorated["allowed_actions"] = actions
        if row.get("capacity_mu") is not None:
            decorated["capacity_mu"] = str(row["capacity_mu"])
        return decorated

    @staticmethod
    def _attach_pending(
        tx: UnitOfWork, rows: list[dict[str, Any]], permissions: frozenset[str]
    ) -> list[dict[str, Any]]:
        """把「当前是否有待核验的状态变更申请」下发到塘口行上。

        ## 为什么需要它（实测缺陷）

        `PondDetailPage` 的「核验通过」按钮判据是
        `canVerifyChange = allowedActions.includes('verify') && pendingRequest`，
        而这两个输入**后端一个都没给**：`pending_status_change` 从不下发；
        已核验塘口的 `allowed_actions` 只有 `[view, archive]`（业务状态变更的核验
        不属于记录生命周期的动作）。结果是**复核在页面上没有任何入口**，只能走 Agent/API。

        ## 为什么不把 `verify` 塞进 `allowed_actions`

        因为 `RecordActions` 按 `allowed_actions` 渲染按钮，多出来的 `verify` 会被它
        当成**记录生命周期**的核验（`pond.verify`），点下去必然状态不符报错 ——
        那正是"渲染一个点了会失败的按钮"。

        所以另给两个**服务端算出来**的字段：
          * `pending_status_change` —— 待核验申请本身（没有则不下发）；
          * `can_verify_status_change` —— 当前账号持 `pond.status.verify` 且确实有申请。
        前端只渲染，不推导。
        """
        ids = [int(r["id"]) for r in rows if r.get("id") is not None]
        if not ids:
            return rows
        placeholders = ",".join(["%s"] * len(ids))
        pending = tx.query_all(
            "SELECT id, pond_id, from_status, to_status, reason, requested_at "
            "FROM pond_status_change_requests "
            f"WHERE status='submitted' AND pond_id IN ({placeholders})",
            ids,
        )
        by_pond = {int(item["pond_id"]): item for item in pending}
        can_verify = "pond.status.verify" in permissions
        for row in rows:
            item = by_pond.get(int(row.get("id") or 0))
            if item is None:
                continue
            row["pending_status_change"] = {
                "id": int(item["id"]),
                "from_status": str(item["from_status"]),
                "to_status": str(item["to_status"]),
                "reason": item.get("reason") or "",
                "requested_at": str(item.get("requested_at") or ""),
            }
            row["can_verify_status_change"] = can_verify
        return rows

    # -- 读 -------------------------------------------------------------------

    def list_ponds(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        query: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """塘口列表。"""
        ctx.require("pond.view")
        params = query or {}
        page = _page_params(params)

        where = ["1=1"]
        values: list[Any] = []
        scope_fragment, scope_values = scope.where_clause("p")
        if scope_fragment and scope_fragment != "1=1":
            where.append(f"({scope_fragment})")
            values.extend(scope_values)

        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            where.append("(p.code LIKE %s OR p.name LIKE %s)")
            values.extend([f"%{keyword}%", f"%{keyword}%"])
        status = str(params.get("status") or "").strip()
        if status:
            where.append("p.status = %s")
            values.append(status)
        pond_status = str(params.get("pond_status") or "").strip()
        if pond_status:
            where.append("p.pond_status = %s")
            values.append(pond_status)

        clause = " AND ".join(where)
        total_row = tx.query_one(f"SELECT COUNT(*) AS n FROM ponds AS p WHERE {clause}", values)
        total = int((total_row or {}).get("n", 0))

        rows = tx.query_all(
            f"""
            SELECT p.*, a.name AS area_name
            FROM ponds AS p
            LEFT JOIN areas AS a ON a.id = p.area_id
            WHERE {clause}
            ORDER BY p.id DESC
            LIMIT %s OFFSET %s
            """,
            [*values, page.size, page.offset],
        )
        return cap.HandlerResult(
            data=page.to_result(
                self._attach_pending(
                    tx,
                    [self._decorate(row, scope, ctx.actor.permissions) for row in rows],
                    ctx.actor.permissions,
                ),
                total,
            ),
            message="",
        )

    def get_pond(self, tx: UnitOfWork, ctx, scope: Scope, **_: Any) -> cap.HandlerResult:
        raise DomainError(
            ErrorCode.INTERNAL_ERROR,
            "get_pond 需要通过 path_params 传 pond_id；见 pond.get 的声明",
        )

    def get_pond_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        **_: Any,
    ) -> cap.HandlerResult:
        """单个塘口详情。

        详情里额外带上 `available_transitions`——前端渲染状态变更表单时
        需要知道**从当前状态出发**能去哪些状态。这就是 registry §2.5
        要求的"服务端按转移表过滤 choices"的运行时形态。
        """
        ctx.require("pond.view")
        raw_pond_id = (path_params or {}).get("pond_id")
        if raw_pond_id is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "塘口详情需要 path_params 传 pond_id",
            )
        pond_id = int(raw_pond_id)
        row = self._scope_row(tx, scope, pond_id)
        decorated = self._attach_pending(
            tx, [self._decorate(row, scope, ctx.actor.permissions)], ctx.actor.permissions
        )[0]
        decorated["available_transitions"] = [
            {"value": state.code, "label": state.label}
            for state in POND_STATUS_WORKFLOW.available_transitions(str(row["pond_status"]))
        ]
        # ⚠️ **读能力不带 `resource_id`**：`web/app.py::_ok` 见到它就会把响应再包一层
        # （`data = {resource_id, record: result.data}`），于是 `data.record.record` ——
        # 前端按契约读 `data.record.code` 得到 `undefined`，**每个格子显示「—」且不报错**。
        # （`PondDetailPage.vue` 曾用 `result.record ?? result` 兼容两种形状把它盖住，
        #  那属于「两处描述同一件事」，已随本补丁收敛成一种。）
        return cap.HandlerResult(data={"record": decorated})

    def load_pond(self, tx: UnitOfWork, *, scope: Scope, record_id: int) -> dict[str, Any] | None:
        """回读函数。

        执行器在提交前调它确认"写入真的落了库"——`executed` 不是
        "我们调用了 INSERT"，而是"读回来的行确实是我们想要的样子"
        （见 `docs/WRITE_CONTRACT.md` 规则 2）。
        """
        row = tx.query_one(
            "SELECT p.*, a.name AS area_name FROM ponds AS p "
            "LEFT JOIN areas AS a ON a.id = p.area_id WHERE p.id = %s",
            (record_id,),
        )
        return None if row is None else {**row, "version": int(row.get("row_version") or 1)}


def _label_of(code: str) -> str:
    for state in POND_WORKFLOW.states:
        if state.code == code:
            return state.label
    return code


def _page_params(params: dict[str, Any]):
    from yuxin.domains._base import Page

    return Page.parse(params.get("page"), params.get("page_size"))


def confirmation_labels(tx: UnitOfWork, payload: dict[str, Any]) -> dict[str, Any]:
    """把塘口类确认卡上的**裸 id** 换成「编号（名称）」。

    为什么必须有：实测（2026-09-15）`pond.verify` 的卡片 target 是
    `pond_id=3` —— 用户看不出这是哪个塘口。而模型**会把编号当 id**：
    它把「P-AI-03」解析成 id=3（真实 id 是 11）。裸 id 让这种错误在卡片上
    **完全隐形**；把编号写上去，用户一眼就能发现“这不是我要核验的那个”。

    失败不阻断：查不到就如实写“已不存在”，绝不让美化把卡片搞没。
    """
    try:
        pond_id = int(payload.get("pond_id"))
    except (TypeError, ValueError):
        return {}
    rows = tx.query_all("SELECT code, name FROM ponds WHERE id = %s", (pond_id,))
    if not rows:
        return {"target": f"id={pond_id}（该塘口已不存在）", "rows": {}}
    row = rows[0]
    text = f"{row['code']}（{row['name']}）"
    return {"target": text, "rows": {"塘口": text, "塘口 ID": str(pond_id)}}


__all__ = ["PondService", "confirmation_labels"]
