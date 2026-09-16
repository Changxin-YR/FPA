"""audit 域：`audit.log.list`（registry §1.3 的 1 条能力）。

## 为什么它必须是**独立域目录**

与 `identity` 域同样的理由：`tests/test_architecture.py`
`::test_capability_domain_matches_its_declaring_directory` 把 `Capability.domain`
与**声明目录**钉在一起，而 registry §1.3 把这条能力归在 **audit 域**
（小节标题"### 1.3 audit — 1 条"）。把它声明在 `access/capabilities.py` 里只有两种
结局：`domain="access"` 与文档域归属矛盾，或 `domain="audit"` 与目录对不上。
两种都是红。

## 与账号管理共享**零**代码，这是刻意的

初稿把 `list_audit_logs` 写在 `domains/access/admin.py` 里（理由是"两者的表都由 001
建立、按表分域本来就是一家"）。那条理由站不住：**分域的判据是能力归属，不是建表时间**。
`audit_logs` 与 `users` 是不同的事实——一个记"发生过什么"，一个说"谁是谁"。
按建表时间分域会让"哪些能力属于哪个域"变成一条需要额外解释的规则，
而 archtest 需要的那条规则简单得多：**声明在哪，就属于哪**。

所以本模块自带：

  * 5 个过滤维度（registry §1.3 从早期版本的 9 个里保留的那 5 个）；
  * `result_label` 的中文映射；
  * 分页与 `allowed_actions` 之外的一组派生列。

## 快照字段（`before_json` / `after_json` / `detail_json`）不返回

它们在 `kernel/audit.py::redact` 里被脱敏过（键名含 password / token / session 等
一律替换成 `[REDACTED]`），但"已经脱敏"不等于"可以给任意能读审计的人看"：

审计页要回答的是 registry §1.3 保留的那 5 个维度——**谁、在哪个域、执行了哪条能力、
什么时候、结果如何**。快照是排查用途，按 `request_id` 单独查更合适。
少返回一列比多返回一列安全，且这一列没有任何消费方（前端表格的列由
`Resource.columns` 声明，里面没有它）。
"""

from __future__ import annotations

from typing import Any

from yuxin.kernel import capability as cap
from yuxin.kernel.capability import REGISTRY
from yuxin.kernel.date_bounds import upper_bound
from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.scope import Scope
from yuxin.kernel.uow import UnitOfWork
from yuxin.kernel.workflow import module_label

#: `audit_logs.result` 的取值 → 中文。
#:
#: 与 `State.label` 同一口径：**文案只在这里出现一次**，前端表格的 `result_label`
#: 列直接用它。早期版本把同一个结果在多个页面各自翻译。
AUDIT_RESULT_LABELS: dict[str, str] = {
    "success": "成功",
    "failure": "失败",
    "denied": "被拒",
    "pending": "待确认",
}

#: 过滤维度参数的元数据（参数名 → 列名 / 说明）。
#:
#: 写成一张表而不是四处 `if`：**这是文档里那条"保留 5 个维度"的可执行形态**，
#: 加维度时只改这里一处，而不会忘了同步其中一个分支。
#: 参数名与 registry §1.3 的旧字段名逐字对齐（`module_code` / `action_code`），
#: 因为那是契约里写的名字——前端与文档都按它拼查询串。
AUDIT_FILTERS: tuple[tuple[str, str], ...] = (
    ("user_id", "user_id"),
    ("module_code", "domain"),
    ("action_code", "capability"),
)

#: 时间区间维度（参数名 → SQL 比较符）。它们不成对出现时可以只给一边。
AUDIT_DATE_FILTERS: tuple[tuple[str, str], ...] = (
    ("created_from", ">="),
    ("created_to", "<="),
)

#: 排序列。**必须显式给 ORDER BY**：`LIMIT/OFFSET` 命中的行由存储顺序决定，
#: 分页在没有全序时可能重复或漏行（`tools/registry_reconcile.py` 的 [H] 表
#: 专门扫这一类位置）。
_AUDIT_ORDER = "a.id DESC"


class AuditLogService:
    """审计日志的只读查询。"""

    def list_audit_logs(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> cap.HandlerResult:
        """操作日志列表。

        `path_params` / `query` 必须**逐字命名**声明：执行器只在处理器签名里真的有
        这个名字时才传（`runner._call_service` 的 `accepts` 裁剪）。写成 `**_` 的
        后果是筛选与分页静默失效——页面永远只看得到第一页且筛不出东西，
        而日志里什么都没有。
        """
        from yuxin.domains._base import Page

        ctx.require("audit.view")
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))

        where = ["1=1"]
        values: list[Any] = []
        for param_name, column in AUDIT_FILTERS:
            raw = str(params.get(param_name) or "").strip()
            if not raw:
                continue
            if param_name == "user_id":
                try:
                    values.append(int(raw))
                except ValueError as exc:
                    raise DomainError(
                        ErrorCode.FIELD_INVALID,
                        "user_id 必须是整数",
                        data={"field": "user_id"},
                    ) from exc
            else:
                values.append(raw)
            where.append(f"a.{column} = %s")
        for param_name, operator in AUDIT_DATE_FILTERS:
            raw = str(params.get(param_name) or "").strip()
            if not raw:
                continue
            # 日期字符串直接交给 MySQL 解析：格式错会抛 DataError，
            # 由 `kernel/uow.py::translate_mysql_error` 翻成 FIELD_INVALID。
            # 刻意**不**在 Python 里再解析一遍——那会变成第二处日期格式判断，
            # 而两处对"什么算合法日期"的判断迟早会不一致。
            #
            # 上界（`<=`）走 `kernel/date_bounds.upper_bound`：`a.created_at` 是
            # **DATETIME** 列，直接 `<= '2026-09-13'` 会把当天 00:00 之后的行全部排除
            # （用户看到"填了今天却一条都没有"）。下界不受影响，原样渲染。
            if operator == "<=":
                fragment, bound = upper_bound("a.created_at", raw)
            else:
                fragment, bound = f"a.created_at {operator} %s", raw
            where.append(fragment)
            values.append(bound)

        clause = " AND ".join(where)
        total = int(
            tx.query_scalar(f"SELECT COUNT(*) FROM audit_logs AS a WHERE {clause}", values) or 0
        )
        rows = tx.query_all(
            "SELECT a.id, a.request_id, a.user_id, a.username, a.is_agent, a.capability, "
            "       a.domain, a.permission, a.object_type, a.object_id, a.object_ref, "
            "       a.result, a.reason, a.ip_address, a.created_at "
            f"FROM audit_logs AS a WHERE {clause} "
            f"ORDER BY {_AUDIT_ORDER} LIMIT %s OFFSET %s",
            [*values, page.size, page.offset],
        )
        return cap.HandlerResult(
            data=page.to_result([self._decorate(row) for row in rows], total), message=""
        )

    @staticmethod
    def _decorate(row: dict[str, Any]) -> dict[str, Any]:
        """派生前端需要的字段。与各域 `_decorate` 同一口径。"""
        decorated = dict(row)
        decorated["is_agent"] = bool(row.get("is_agent"))
        decorated["result_label"] = AUDIT_RESULT_LABELS.get(
            str(row.get("result") or ""), str(row.get("result") or "")
        )
        capability_code = str(row.get("capability") or "")
        capability = REGISTRY.find(capability_code)
        decorated["capability_label"] = (
            capability.title if capability is not None else f"未知操作（{capability_code}）"
        )
        domain_code = str(row.get("domain") or "")
        translated_domain = module_label(domain_code)
        decorated["domain_label"] = (
            translated_domain
            if translated_domain != domain_code
            else f"未知模块（{domain_code}）"
        )
        if decorated.get("created_at") is not None:
            decorated["created_at"] = str(decorated["created_at"])
        return decorated


__all__ = ["AUDIT_DATE_FILTERS", "AUDIT_FILTERS", "AUDIT_RESULT_LABELS", "AuditLogService"]
