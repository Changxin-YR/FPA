"""仓库自身的写路径：`warehouse.create` / `warehouse.update` / `warehouse.archive`。

## 与 `receipt_write.py` 的分工

本文件只管**仓库这一行的台账字段**（编号 / 名称 / 地址 / 联系人 / 电话）。
入库、出库、库存账本全在 `receipt_write.py` 与 `ledger.py` ——
"仓库"与"仓储单据"是两个资源，能力也分属两个 `resource`。

## 写能力的统一形状（`docs/DEVELOPMENT.md` §3 的固定七步）

    ctx.require(...) -> _scope_row -> 状态机校验 -> 写库 + HandlerResult(resource_id=...)

`resource_id` **不是可选的**：执行器按它回读校验（`docs/WRITE_CONTRACT.md` 规则 2）。

## 两处值得写下来的决定

### 一、`warehouse.create` 的初始状态是 `draft`（2026-09-15 改）

**改之前**：初始状态是 `verified`，理由是"没有 `warehouse.submit` / `verify` 能力，
`draft` 会永远无法被使用（所有收发货路径都要求 `verified`）"。

**为什么改**：用户报「仓库新建后自动核验完成」。那条理由说明的是**缺一条路径**，
而不是"仓库不该有核验"——正确修法是**把路径补齐**：

* 新增 `warehouse.submit`（草稿 → 待核验）与 `warehouse.verify`（待核验 → 已核验）；
* 初始状态改回 `draft`，与区域 / 物料同一口径；
* 顺带补上**原本缺失的 `archived` 状态**：`warehouse.archive` 一直写
  `status='archived'`，而状态表里没有这个值 —— 归档后的行会退化成裸英文 `archived`
  且没有任何允许动作（"声明与能力不一致"的另一种表现）。

只有 `verified` 的仓库才能被收发货引用这件事没变：

* `receipt.create` / `receipt.verify` 要求 `warehouses.status = 'verified'`
  （`receipt_write.py` 的 `WAREHOUSE_NOT_VERIFIED`）；
* `ledger._default_warehouse_id` 只在 `status='verified'` 的仓里选。

初始状态是 `verified` 的**历史种子行不受影响**（006 迁移直接写死 `verified`）——
它们仍是可用的；只有新建的仓库走三步核验。

### 二、`is_default` **不回给客户端**

`warehouses.is_default` 决定 `feeding.verify` 从哪个仓扣库存
（006 迁移的注释解释了为什么它是显式列而不是"取 id 最小的仓"）。
它**不在** t18 的字段表里，也不在 registry §2.7 的任何字段表里——
把它开放给表单会让"投喂的库存从哪个仓出"变成一个可以被随手改的参数，
而那个决定影响的是**库存的去向**。它的维护维持现状：由迁移种子决定。
"""

from __future__ import annotations

from typing import Any

from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError, ErrorCode
from fpa.kernel.fields import f_int, f_str
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import RowAction

from fpa.domains.master_data._scope_keys import tenant_keys_for_create

from .inventory import WarehouseQueryService
from .service import WAREHOUSE_WORKFLOW


def _non_null(**fields: Any) -> dict[str, Any]:
    """只保留**提交了**的字段（None 视为"没提交"）。与 `ponds_write._non_null` 同形。"""
    return {key: value for key, value in fields.items() if value is not None}


class WarehouseWriteService(WarehouseQueryService):
    """仓库的写路径。

    继承 `WarehouseQueryService` 以复用 `_scope_row`（范围校验）与 `_decorate`
    （`status_label` / `allowed_actions` / `version` / `is_default_label`）——
    读写共用同一套范围校验与派生逻辑，避免"读看到一个样、写看到另一个样"。
    """

    TITLE = "仓库"

    def create_warehouse(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("仓库编号", required=True, max_length=64),
        name: f_str("仓库名称", required=True, max_length=100),
        address: f_str("地址", max_length=200),
        contact_name: f_str("联系人", max_length=40),
        phone: f_str("联系电话", max_length=32),
    ) -> cap.HandlerResult:
        """`warehouse.create`（registry §1.6，t18 补）。

        归属来自数据范围（`tenant_keys_for_create`）：`warehouses` 的
        `organization_id` / `farm_id` / `area_id` 三列都是 NOT NULL，而
        registry §0.7 规则 1 明令 `create` 不接收它们——客户端指定归属就等于
        "用户自报家门"。解析不出具体区域就 `DATA_SCOPE_UNRESOLVED`（fail-closed）。

        初始状态是 `draft`（**2026-09-15 改**：此前是 `verified`）。与区域 / 物料同一口径：
        建出来是草稿，走 `warehouse.submit` → `warehouse.verify` 才到 `verified`，
        而**只有 `verified` 的仓库**才能被收发货引用 / 选为默认出库仓
        （见 `receipt_write.WAREHOUSE_NOT_VERIFIED` 与 `ledger._default_warehouse_id`）。
        """
        ctx.require("warehouse.create")

        keys = tenant_keys_for_create(tx, scope)
        try:
            tx.execute(
                "INSERT INTO warehouses "
                "(organization_id, farm_id, area_id, code, name, address, contact_name, "
                " phone, status, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s)",
                (
                    keys["organization_id"],
                    keys["farm_id"],
                    keys["area_id"],
                    code,
                    name,
                    address,
                    contact_name,
                    phone,
                    ctx.actor.user_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            # 唯一键 `uq_warehouses_farm_code(farm_id, code)`：**基地内**唯一。
            # 不变量 `UniqueCode` 已在应用层给出可读的 409，这里兜并发。
            if "uq_warehouses_farm_code" in str(exc):
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"该基地下已存在编号为「{code}」的仓库",
                    data={"field": "code"},
                ) from exc
            raise

        warehouse_id = tx.last_insert_id()
        row = self.load_warehouse(tx, scope=scope, record_id=warehouse_id)
        assert row is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    row, WAREHOUSE_WORKFLOW, ctx.actor.permissions, "warehouse"
                ),
                # `UniqueCode(scope=("farm_id",))` 的 `farm_id` 由服务端解析
                # （不在请求体里），必须显式回传——取不到范围列会让不变量报
                # INTERNAL_ERROR 而不是静默放宽。见 `create_area` 的同段说明。
                "_invariant_context": {"farm_id": int(row["farm_id"])},
            },
            resource_id=warehouse_id,
            message=f"已新建仓库「{name}」（编号 {code}）",
        )

    def update_warehouse(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        warehouse_id: f_int("仓库 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
        name: f_str("仓库名称", max_length=100),
        address: f_str("地址", max_length=200),
        contact_name: f_str("联系人", max_length=40),
        phone: f_str("联系电话", max_length=32),
    ) -> cap.HandlerResult:
        """`warehouse.update`。`code` 不可改（与 `pond.update` 一致：编码是对外标识）。"""
        ctx.require("warehouse.update")
        row = self._scope_row(tx, scope, int(warehouse_id))
        WAREHOUSE_WORKFLOW.require_action(str(row["status"]), RowAction.EDIT)

        patch = _non_null(
            name=name, address=address, contact_name=contact_name, phone=phone
        )
        if not patch:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "没有需要更新的内容（仓库编号不可修改，请改动名称等字段）",
            )

        assignments = ", ".join(f"{key}=%s" for key in patch)
        affected = tx.execute(
            f"UPDATE warehouses SET {assignments}, row_version=row_version+1 "
            "WHERE id=%s AND row_version=%s",
            [*patch.values(), int(warehouse_id), int(expected_version)],
        )
        if affected == 0:
            current = self._scope_row(tx, scope, int(warehouse_id))
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该仓库已被他人修改，请刷新后重试",
                data={"current_version": int(current["row_version"])},
            )

        updated = self.load_warehouse(tx, scope=scope, record_id=int(warehouse_id))
        assert updated is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    updated, WAREHOUSE_WORKFLOW, ctx.actor.permissions, "warehouse"
                )
            },
            resource_id=int(warehouse_id),
            message="仓库已更新",
        )

    # -- 记录生命周期：提交 / 核验（2026-09-15 新增，与 `material.submit/verify` 同形）--

    def submit_warehouse(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        warehouse_id: f_int("仓库 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """`warehouse.submit`：草稿 → 待核验。"""
        return self._record_transition(
            tx, ctx, scope,
            warehouse_id=int(warehouse_id),
            expected_version=int(expected_version),
            from_state="draft",
            to_state="submitted",
            action=RowAction.SUBMIT,
            permission="warehouse.submit",
            message="仓库已提交核验",
        )

    def verify_warehouse(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        warehouse_id: f_int("仓库 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """`warehouse.verify`：待核验 → 已核验。

        核验通过后仓库才**可被收发货引用**（`receipt_write.WAREHOUSE_NOT_VERIFIED`）
        与**选为默认出库仓**（`ledger._default_warehouse_id` 只挑 `verified`）。
        """
        return self._record_transition(
            tx, ctx, scope,
            warehouse_id=int(warehouse_id),
            expected_version=int(expected_version),
            from_state="submitted",
            to_state="verified",
            action=RowAction.VERIFY,
            permission="warehouse.verify",
            message="仓库已核验",
        )

    def _record_transition(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        warehouse_id: int,
        expected_version: int,
        from_state: str,
        to_state: str,
        action: RowAction,
        permission: str,
        message: str,
    ) -> cap.HandlerResult:
        """提交/核验共用的转移：权限 → 范围 → 状态机动作 → 状态机转移 → 乐观锁写入。

        与 `masterdata_write.MaterialWriteService._transition` 同一形状（本仓的通用写法）。
        `DistinctActors`（经办人 ≠ 核验人）由 `warehouse.verify` 的不变量声明强制，
        不在这里手写第二遍 —— 人工页面与 Agent 走同一份判断。
        """
        ctx.require(permission)
        row = self._scope_row(tx, scope, warehouse_id)
        WAREHOUSE_WORKFLOW.require_action(str(row["status"]), action)
        WAREHOUSE_WORKFLOW.require_transition(
            str(row["status"]), to_state, action=permission
        )
        affected = tx.execute(
            "UPDATE warehouses SET status=%s, row_version=row_version+1 "
            "WHERE id=%s AND row_version=%s AND status=%s",
            (to_state, warehouse_id, expected_version, from_state),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT, "该仓库已被他人修改，请刷新后重试"
            )
        updated = self.load_warehouse(tx, scope=scope, record_id=warehouse_id)
        assert updated is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    updated, WAREHOUSE_WORKFLOW, ctx.actor.permissions, "warehouse"
                )
            },
            resource_id=warehouse_id,
            message=f"{message}「{updated['name']}」",
        )

    def archive_warehouse(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        warehouse_id: f_int("仓库 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """`warehouse.archive`：停用（归档）一个仓库。

        ## 归档后哪些东西被锁住

        | 被锁住 | 由谁保证 |
        |---|---|
        | 被 `receipt.create` / `issue.create` 选为收发货仓 | `referenced` 校验在 `receipt.verify`（`WAREHOUSE_NOT_VERIFIED`）+ 下拉候选（默认列表排除 archived） |
        | 被 `ledger._default_warehouse_id` 选为默认出库仓 | 那里只挑 `status='verified'` |
        | 被继续编辑 | `archived` 是终态，状态机不给 `EDIT` |
        | 被再次归档 | 同上（`ARCHIVE` 在 `archived` 下不允许） |

        ## 放行什么

          * 它**仍可被查到**（`?status=archived`）与读到；
          * 引用它的**历史**单据与库存账本**不受影响**（外键 RESTRICT，行还在；
            账本的 `warehouse_id` 只是一个 BIGINT）；
          * 已核验的历史领用/到货行的仓库名仍能解析出来（`LEFT JOIN warehouses`）。
        """
        ctx.require("warehouse.archive")
        row = self._scope_row(tx, scope, int(warehouse_id))
        current = str(row["status"])
        WAREHOUSE_WORKFLOW.require_action(current, RowAction.ARCHIVE)

        affected = tx.execute(
            "UPDATE warehouses SET status='archived', row_version=row_version+1 "
            "WHERE id=%s AND row_version=%s AND status=%s",
            (int(warehouse_id), int(expected_version), current),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT, "该仓库已被他人修改，请刷新后重试"
            )

        updated = self.load_warehouse(tx, scope=scope, record_id=int(warehouse_id))
        assert updated is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(
                    updated, WAREHOUSE_WORKFLOW, ctx.actor.permissions, "warehouse"
                )
            },
            resource_id=int(warehouse_id),
            message=f"仓库「{updated['name']}」已停用（归档）",
        )


__all__ = ["WarehouseWriteService"]
