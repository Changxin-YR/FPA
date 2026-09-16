"""往来单位的写路径：`partner.create`。

## 形状与塘口写路径一致

    ctx.require(...)                      第三层权限校验（不信任执行器）
    解析分租键                            由服务端从数据范围解析（客户端说了不算）
    写库 + 返回 HandlerResult(resource_id) resource_id **不是可选的**
    __yuxin_load_by_id__                    执行器按它回读校验

## 与 `pond.create` 的一处刻意差别：没有 `area_id` 入参

`docs/CAPABILITY_REGISTRY.md` §2.5 的 `partner.create` 字段表里**没有任何归属对象字段**，
而 `business_partners.area_id` 是 NOT NULL。按 §0.7 规则 1（create 不接收也不返回
三个分租键），归属只能来自**服务端解析的数据范围**——见 `_scope_keys.tenant_keys_for_create`。
解析不出具体区域时 fail-closed 抛 `DATA_SCOPE_UNRESOLVED`，不给默认值。
"""

from __future__ import annotations

from typing import Any

from yuxin.kernel import capability as cap
from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.fields import Choice, f_enum, f_int, f_num, f_str, text
from yuxin.kernel.scope import Scope
from yuxin.kernel.uow import UnitOfWork

from ._scope_keys import tenant_keys_for_create
from .resources_read import PartnerService

#: `business_partners.partner_type` 的 ENUM 值（003 迁移）。
#: 与数据库 ENUM 必须一致——这里写第二遍**是有意的**：前端下拉、Agent tool schema、
#: 服务校验都从这份声明派生，而数据库那份只在写入时兜底。两处的值由 e2e 断言比对。
PARTNER_TYPES = ("supplier", "customer")

PARTNER_TYPE_LABELS = {"supplier": "供应商", "customer": "客户"}

#: 下拉选项。**中文只在这里写一次**（`PARTNER_TYPE_LABELS`），`Choice.label` 从它派生——
#: 与 `ponds_write._choice_tuple` 同一口径：状态的文案只能有一个来源。
PARTNER_TYPE_CHOICES = tuple(
    Choice(value=code, label=PARTNER_TYPE_LABELS[code]) for code in PARTNER_TYPES
)


class PartnerWriteService(PartnerService):
    """往来单位的写路径。继承 `PartnerService` 复用 `_scope_row` / `_decorate` / `load_partner`。"""

    def create_partner(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        partner_type: f_enum("单位类型", PARTNER_TYPE_CHOICES, required=True),
        code: f_str("单位编号", required=True, max_length=64),
        name: f_str("单位名称", required=True, max_length=100),
        contact_name: f_str("联系人", max_length=40),
        phone: f_str("联系电话", max_length=32),
        address: f_str("地址", max_length=200),
        settlement_days: f_int("账期（天）", minimum=0),
        credit_limit: f_num("信用额度", minimum=0),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        ctx.require("partner.create")

        keys = tenant_keys_for_create(tx, scope)
        try:
            tx.execute(
                "INSERT INTO business_partners "
                "(organization_id, farm_id, area_id, partner_type, code, name, "
                " contact_name, phone, address, settlement_days, credit_limit, note, "
                " status, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s)",
                (
                    keys["organization_id"],
                    keys["farm_id"],
                    keys["area_id"],
                    str(partner_type),
                    code,
                    name,
                    contact_name,
                    phone,
                    address,
                    settlement_days,
                    credit_limit,
                    note,
                    ctx.actor.user_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            # 唯一键 `uq_partners_org_type_code` = (organization_id, partner_type, code)。
            # 不变量 `UniqueCode` 已经在应用层给出可读的 409；这里兜的是**并发**下
            # 两个请求同时通过预检的情形（唯一键是并发下唯一可信的判定）。
            if "uq_partners_org_type_code" in str(exc):
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"该类型下已存在编号为「{code}」的往来单位",
                    data={"field": "code"},
                ) from exc
            raise

        partner_id = tx.last_insert_id()
        row = self.load_partner(tx, scope=scope, record_id=partner_id)
        assert row is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(row, scope, ctx.actor.permissions),
                # 回传不变量需要的分租键。`UniqueCode(scope=("organization_id",
                # "partner_type"))` 里 `organization_id` 来自**服务端解析的数据范围**，
                # 不在请求体里（§0.7 规则 1），所以必须由服务显式回传。
                "_invariant_context": {"organization_id": int(row["organization_id"])},
            },
            resource_id=partner_id,
            message=f"已新建{PARTNER_TYPE_LABELS.get(str(partner_type), '往来单位')}「{name}」",
        )


# ---------------------------------------------------------------------------
# 回读函数声明（`WRITE_CONTRACT.md` 规则 2 / `ROLLOUT_CONTRACT.md` §3）
# ---------------------------------------------------------------------------
#
# 与 `ponds_write.py` 末尾同形、同理由：**逐个显式挂**，不在类上挂一个靠继承兜底。
# `kernel/runner.py` 三处按 `getattr(handler, "__yuxin_load_by_id__")` 解析，
# 类级挂载会让"新加写方法却漏挂"静默通过。

_WRITE_HANDLERS: tuple[str, ...] = ("create_partner",)
# 挂载与守卫都不在这里，理由与 `ponds_write.py` 末尾逐字相同（见那段注释）：
# 挂载归内核 `Capability.loader=`，守卫归 `capabilities.py::assert_reload_wired()`。

__all__ = ["PARTNER_TYPES", "PARTNER_TYPE_LABELS", "PartnerWriteService"]
