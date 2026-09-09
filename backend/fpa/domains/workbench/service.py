"""工作台的待办查询。"""

from __future__ import annotations

from typing import Any

from fpa.domains._base import Page
from fpa.domains.master_data.service import POND_STATUS_WORKFLOW
from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError, ErrorCode
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import RESOURCES, Resource


_STATUS_LABELS = {state.code: state.label for state in POND_STATUS_WORKFLOW.states}
_REQUEST_STATUS_LABELS = {
    "submitted": "待核验",
    "verified": "已核验",
    "cancelled": "已取消",
}


RESOURCES.register(
    Resource(
        name="work_item",
        title="待办",
        module="workbench",
        list_path="/api/v1/work-items",
        table="pond_status_change_requests",
        columns=(
            ("title", "待办事项"),
            ("pond_name", "塘口"),
            ("from_status", "原状态"),
            ("to_status", "目标状态"),
            ("requested_by_name", "申请人"),
            ("requested_at", "申请时间"),
            ("status_label", "状态"),
        ),
    )
)


class WorkbenchService:
    def list_work_items(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> cap.HandlerResult:
        ctx.require("pond.status.verify")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))
        predicate = scope.predicate("p")
        where = f"r.status='submitted' AND ({predicate.fragment})"
        total = int(
            tx.query_scalar(
                "SELECT COUNT(*) FROM pond_status_change_requests r "
                "JOIN ponds p ON p.id=r.pond_id "
                f"WHERE {where}",
                list(predicate.params),
            )
            or 0
        )
        rows = tx.query_all(
            "SELECT r.id, r.pond_id, p.name AS pond_name, r.from_status, r.to_status, "
            "u.display_name AS requested_by_name, r.requested_at, r.status, "
            "p.row_version AS pond_version "
            "FROM pond_status_change_requests r "
            "JOIN ponds p ON p.id=r.pond_id "
            "JOIN users u ON u.id=r.requested_by "
            f"WHERE {where} ORDER BY r.requested_at DESC, r.id DESC LIMIT %s OFFSET %s",
            [*predicate.params, page.size, page.offset],
        )
        items = []
        for row in rows:
            from_status = str(row["from_status"])
            to_status = str(row["to_status"])
            status = str(row["status"])
            items.append(
                {
                    **row,
                    "title": f"塘口状态变更：{_STATUS_LABELS.get(from_status, from_status)} → "
                    f"{_STATUS_LABELS.get(to_status, to_status)}",
                    "from_status": _STATUS_LABELS.get(from_status, from_status),
                    "to_status": _STATUS_LABELS.get(to_status, to_status),
                    "requested_at": str(row["requested_at"]),
                    "status_label": _REQUEST_STATUS_LABELS.get(status, status),
                    # ★ 待办**不给行内动作**：原先写死 `["view"]`，而 `work_item.view`
                    # 这条能力根本不存在 —— 点了只会得到"服务端未登记动作「view」"（实测）。
                    # 注册一条 `work_item.verify` 曾试过，但它与
                    # `pond_status_change.verify` **共用同一路由**（同一能力两条声明），
                    # 被 `tests/test_architecture.py::test_no_duplicate_capability_names_or_routes`
                    # 当场拒绝 —— 那是对的：同一件事不该有两条能力。
                    # 复核入口收敛到**塘口详情页**的「核验通过」（`/ponds/<id>`），
                    # 待办保持"只看不做"，`row_action_notes` 会记下原因。
                    "allowed_actions": [],
                    "pond_version": int(row.get("pond_version") or 1),
                    # 能力字段名是 `request_id`，而待办行的主键叫 `id`。
                    # 前端通用循环按 `field.key` 从行里取值 —— 不发这个别名，
                    # 核验请求就会缺 `request_id`（实测会 400）。
                    "request_id": int(row["id"]),
                }
            )
        return cap.HandlerResult(data=page.to_result(items, total), message="")


__all__ = ["WorkbenchService"]
