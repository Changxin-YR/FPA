"""区域与物料的写路径：`create` / `update` / `archive`（停用）。

## 写能力的统一形状（`docs/DEVELOPMENT.md` §3 的固定七步；与 `partners_write.py` 同形）

每个写能力都做四件事，顺序固定：

    1. ctx.require(...)                 —— 第三层权限校验（不信任执行器）
    2. _scope_row(tx, scope, id)        —— 第三层范围校验（校验的是**具体那一行**）
    3. 状态机校验                        —— require_action / require_transition
    4. 写库 + 返回 HandlerResult(resource_id=...)

第 4 步的 `resource_id` **不是可选的**：执行器要按它回读校验
（`docs/WRITE_CONTRACT.md` 规则 2）。少了它，`executed` 就退化成
"我们调用了 INSERT"而不是"数据真的落库了"。

## 为什么三个资源（区域/物料/仓库）的写路径形状相同，而塘口单独写

`create` 这一步对它们逐字相同：解析分租键 -> 校验归属对象在范围内 ->
`INSERT ... status='verified'` -> 回读。差别只在"写入哪些列"与"归属键解析函数"。

而塘口不同：它有双状态机（`status` + `pond_status`），且 `pond_status` 只能经
两步审批变更（`DECISIONS.md` Q2）。**共用的判据是"写法的形状是否相同"，
不是"资源类型是否相同"**（与 `resources_read.py` 顶部那条判据同源）。

## 「停用」= 归档，不是物理删除

全系统 `delete` 能力为 **0**（registry §0.2 的裁决：早期版本 5 处同构的
`except IntegrityError -> DELETE_NOT_ALLOWED` 是纯负债）。所以"停用主数据"落地为

    status -> 'archived'  且  row_version 前进一格

**数据保留、审计线索保留**。归档之后的具体语义：

| 问题 | 答案 | 由谁保证 |
|---|---|---|
| 还能查到它吗？ | **能**，但要显式 `?status=archived` | `resources_read._DEFAULT_EXCLUDED_STATUS` |
| 还能被下拉选到吗？ | **不能**（默认列表里没有它） | 同上（`DynamicForm` 不带 `status` 参数） |
| 引用它的历史业务数据会坏吗？ | **不会**（外键是 RESTRICT，行还在；归档只改 `status` 一列） | 003 / 006 迁移的真实外键约束 |
| 新的业务还能不能引用它？ | **不能**：下拉里选不到；手工提交 id 会撞服务端的归属/范围校验（`_scope_row`）与引用状态校验（`ReferencedStatus`） | 服务的第三层防御 + 内核不变量 |
| 还能改它吗？ | **不能**：`archived` 是终态，状态机不给 `EDIT` 也不给 `ARCHIVE` | `EDITABLE_LIFECYCLE` |

## 一个刻意的"不给"：`code` 不在 update 里

`area.update` / `material.update` / `warehouse.update` 都**不接受 `code`**——
与 `pond.update` 逐字一致（它也不接受 `code`），也是早期版本
`RESERVED_FIELDS` 的意图。理由不是"实现难度"：编码是**其他系统引用这条记录时的
稳定标识**（批次号、单据号、外部对账），改它就等于让已经发出去的单据指向另一个东西。
要换编码就停用旧行、新建一行——**那会留下审计线索，而改名不会**。
"""

from __future__ import annotations

from typing import Any

from yuxin.kernel import capability as cap
from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.fields import f_int, f_num, f_str, text
from yuxin.kernel.scope import Scope
from yuxin.kernel.uow import UnitOfWork
from yuxin.kernel.workflow import RowAction

from ._scope_keys import area_keys_for_create, tenant_keys_for_create
from .resources_read import AreaService, FarmService, MaterialService


def _non_null(**fields: Any) -> dict[str, Any]:
    """只保留**提交了**的字段（None 视为"没提交"）。

    与 `ponds_write._non_null` 同形同理由：`validate_payload` 把缺失字段统一填成
    `None`，而更新场景下"没提交"与"提交了 None"含义不同（前者不动、后者清空）。
    本版不允许清空（没有 `null` 语义），所以只做"提交了才动"。
    """
    return {key: value for key, value in fields.items() if value is not None}


class _DictionaryWriteService:
    """区域 / 物料的写路径共同骨架。

    子类继承对应的**读**服务（`AreaService` / `MaterialService`），因此复用
    `_scope_row`（范围校验）、`_decorate`（派生字段）、`load_*`（回读函数）——
    读写共用同一套范围校验与派生逻辑，避免"读看到一个样、写看到另一个样"。
    这是早期版本的真实问题：`master_data_store.py` 的读路径与写路径各自拼 scope 条件。

    本基类只放**逐字相同**的三件事：`_archive`、`_update_row`、`_existing_row`。
    子类的 `create_*` / `update_*` / `archive_*` 方法**逐个显式写**——它们是
    能力字段集（`fields`）的来源，而字段集从处理器签名自动收集
    （`Capability.__post_init__`），所以每个能力的入参必须看得见地写出来。
    """

    #: 中文资源名，用于错误文案。子类覆盖。
    TITLE = "记录"

    # -- 内部工具 -------------------------------------------------------------

    def _existing_row(self, tx: UnitOfWork, scope: Scope, record_id: int) -> dict[str, Any]:
        """按主键取回一行并做第三层范围校验。

        直接用读路径的 `_scope_row`：它已经实现了"取回 + 判这一行在不在范围内 +
        不在就 DATA_SCOPE_DENIED（fail-closed）"。不另写一份判定——
        **范围判定只能有一处实现**。
        """
        return self._scope_row(tx, scope, int(record_id))

    def _update_row(
        self,
        tx: UnitOfWork,
        scope: Scope,
        ctx,
        *,
        record_id: int,
        expected_version: int,
        patch: dict[str, Any],
        loader,
    ) -> cap.HandlerResult:
        """统一 UPDATE：乐观锁只做一处（`WHERE row_version = %s`）。

        为什么冲突文案里带上**当前版本号**：`INTERFACES.md:279` 要求
        `CONFLICT` 带 `data.current_version`，前端据此提示"请刷新后重试"并可以
        直接把新版本填回表单。只报"已被他人修改"而不给版本，用户只能手动刷新。
        """
        if not patch:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                f"没有需要更新的内容（{self.TITLE}的编号不可修改，请改动名称等字段）",
            )
        assignments = ", ".join(f"{key}=%s" for key in patch)
        affected = tx.execute(
            f"UPDATE {self.SPEC.table} SET {assignments}, "
            "row_version=row_version+1 "
            f"WHERE id=%s AND row_version=%s",
            [*patch.values(), int(record_id), int(expected_version)],
        )
        if affected == 0:
            # 0 行有两种原因：并发改过（版本不符）或这一行刚不在范围内。
            # 重新读一次把**真实**版本号报给用户，而不是笼统说"失败了"。
            current = self._scope_row(tx, scope, int(record_id))
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                f"该{self.TITLE}已被他人修改，请刷新后重试",
                data={"current_version": int(current["row_version"])},
            )

        updated = loader(tx, scope=scope, record_id=int(record_id))
        assert updated is not None
        return cap.HandlerResult(
            data={"record": self._decorate(updated, scope, ctx.actor.permissions)},
            resource_id=int(record_id),
            message=f"{self.TITLE}已更新",
        )

    def _archive(
        self,
        tx: UnitOfWork,
        scope: Scope,
        ctx,
        *,
        record_id: int,
        expected_version: int,
        permission: str,
        loader,
    ) -> cap.HandlerResult:
        """`status -> 'archived'`：**停用**的统一实现。

        与 `ponds_write._lifecycle` 同形：状态机校验在前（给出可读文案），
        `UPDATE ... WHERE row_version=%s AND status IN (...)` 在后
        （乐观锁与状态前置条件**在数据库层同时生效**——这是仓储的职责）。

        `status` 放进 `WHERE` 而不是"先查再判"：并发下两个请求会都通过检查
        再都执行，而 `WHERE` 保证只有一个能改到非 0 行。**约束要放在能真正
        保证它的地方**（`INVARIANT_TYPES.md` §4 的口径）。
        """
        ctx.require(permission)
        row = self._scope_row(tx, scope, int(record_id))
        current = str(row["status"])
        # 状态机给文案：`archived` 的记录会收到"已归档的记录不支持「archive」操作"，
        # 而不是一句笼统的 409。
        self._workflow().require_action(current, RowAction.ARCHIVE)

        affected = tx.execute(
            f"UPDATE {self.SPEC.table} SET status='archived', "
            "row_version=row_version+1 "
            "WHERE id=%s AND row_version=%s AND status=%s",
            (int(record_id), int(expected_version), current),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                f"该{self.TITLE}已被他人修改，请刷新后重试",
            )

        updated = loader(tx, scope=scope, record_id=int(record_id))
        assert updated is not None
        return cap.HandlerResult(
            data={"record": self._decorate(updated, scope, ctx.actor.permissions)},
            resource_id=int(record_id),
            message=f"{self.TITLE}「{updated['name']}」已停用（归档）",
        )


class FarmWriteService(FarmService):
    """基地写服务。新基地只能由全数据范围账号创建。"""

    def create_farm(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("基地编号", required=True, max_length=64),
        name: f_str("基地名称", required=True, max_length=120),
    ) -> cap.HandlerResult:
        ctx.require("farm.create")
        if not scope.allow_all:
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED,
                "新建基地需要全数据范围；当前账号只能维护已分配的基地",
            )
        organization = tx.query_one(
            "SELECT id FROM organizations WHERE status='active' ORDER BY id LIMIT 1"
        )
        if organization is None:
            raise DomainError(
                ErrorCode.DATA_SCOPE_UNRESOLVED,
                "系统里没有启用的企业，无法确定基地归属",
            )
        organization_id = int(organization["id"])
        try:
            tx.execute(
                "INSERT INTO farms (organization_id, code, name, status) "
                "VALUES (%s,%s,%s,'active')",
                (organization_id, code, name),
            )
        except Exception as exc:  # noqa: BLE001
            if "uq_farms_organization_code" in str(exc):
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"当前企业已存在编号为「{code}」的基地",
                    data={"field": "code"},
                ) from exc
            raise

        farm_id = tx.last_insert_id()
        row = self.load_farm(tx, scope=scope, record_id=farm_id)
        assert row is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(row, scope, ctx.actor.permissions),
                "_invariant_context": {"organization_id": organization_id},
            },
            resource_id=farm_id,
            message=f"已新建基地「{name}」（编号 {code}）",
        )


class AreaWriteService(_DictionaryWriteService, AreaService):
    """区域：可维护（t18 起）。`DECISIONS.md` Q21 记录了这次范围变更的理由。"""

    TITLE = "区域"

    def create_area(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("区域编号", required=True, max_length=64),
        name: f_str("区域名称", required=True, max_length=120),
    ) -> cap.HandlerResult:
        """`area.create`（registry §1.4，t18 补）。

        ## 归属来自**数据范围**，不接受客户端提交

        registry §0.7 规则 1：`create` 不接收也不返回 `organization_id` / `farm_id` /
        `area_id`。区域的归属列是 `farm_id`（`areas` 表**没有 `area_id` 列**），
        由 `area_keys_for_create` 从数据范围解析——解析不出具体基地就
        `DATA_SCOPE_UNRESOLVED`（fail-closed，绝不给默认值）。

        为什么不能复用 `tenant_keys_for_create`：后者回答"这条记录算在哪个**区域**下"，
        所以 farm 型范围那一支要求该基地**已经有一个区域**才能反推出 area_id。
        区域自己不该有这个前提——否则"某个基地的第一个区域"永远建不出来。
        详见 `_scope_keys.area_keys_for_create`。
        """
        ctx.require("area.create")

        keys = area_keys_for_create(tx, scope)
        try:
            tx.execute(
                "INSERT INTO areas "
                "(organization_id, farm_id, code, name, status, created_by) "
                "VALUES (%s,%s,%s,%s,'draft',%s)",
                (keys["organization_id"], keys["farm_id"], code, name, ctx.actor.user_id),
            )
        except Exception as exc:  # noqa: BLE001
            # 唯一键 `uq_areas_farm_code(farm_id, code)` 是并发下的正确性底线；
            # 不变量 `UniqueCode` 已在应用层给出可读的 409，这里兜的是
            # "两个请求同时通过预检"的情形。
            if "uq_areas_farm_code" in str(exc):
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"该基地下已存在编号为「{code}」的区域",
                    data={"field": "code"},
                ) from exc
            raise

        area_id = tx.last_insert_id()
        row = self.load_area(tx, scope=scope, record_id=area_id)
        assert row is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(row, scope, ctx.actor.permissions),
                # 服务把**服务端解析出来的**分租键回传给执行器，供声明式不变量使用：
                # `UniqueCode(scope=("farm_id",))` 的 `farm_id` 不在请求体里
                # （§0.7 规则 1），也不在能力入参里。不回传的后果不是"少查一次"——
                # `UniqueCode` 取不到范围列会**显式报 INTERNAL_ERROR**
                # （`invariants.py` 的 `UniqueCode.check`），因为静默放宽到全表
                # 比报错危险得多。
                "_invariant_context": {"farm_id": int(row["farm_id"])},
            },
            resource_id=area_id,
            message=f"已新建区域「{name}」（编号 {code}）",
        )

    def update_area(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        area_id: f_int("区域 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
        name: f_str("区域名称", max_length=120),
    ) -> cap.HandlerResult:
        """`area.update`。可改的只有 `name`：`code` 不可改（见模块说明）。"""
        ctx.require("area.update")
        row = self._scope_row(tx, scope, int(area_id))
        self._workflow().require_action(str(row["status"]), RowAction.EDIT)
        return self._update_row(
            tx, scope, ctx,
            record_id=int(area_id),
            expected_version=int(expected_version),
            patch=_non_null(name=name),
            loader=self.load_area,
        )

    def archive_area(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        area_id: f_int("区域 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """`area.archive`：停用（归档）一个区域。见模块顶部的语义表。"""
        return self._archive(
            tx, scope, ctx,
            record_id=int(area_id),
            expected_version=int(expected_version),
            permission="area.archive",
            loader=self.load_area,
        )


class MaterialWriteService(_DictionaryWriteService, MaterialService):
    """物料：可维护（t18 起，`DECISIONS.md` Q21 推翻了"物料只读"的原裁决）。"""

    TITLE = "物料"

    def create_material(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("物料编号", required=True, max_length=64),
        name: f_str("物料名称", required=True, max_length=120),
        # `category` 刻意**不做 enum 校验**：`materials.category` 是无约束的
        # `VARCHAR(32)`（003 迁移的原话："饲料/药品/物资的划分会随业务演进，
        # 而 ENUM 每加一个值都要动 DDL。**会变的分类不该固化成约束。**"）。
        # 把它声明成 enum 会把那条决定偷偷推翻：运维分册里的"水质改良剂"
        # 会变成非法输入，而用户看到的是"分类不在允许范围内"。
        category: f_str("分类", max_length=32, help="默认饲料；预置词表见分类列的中文映射"),
        spec: f_str("规格", max_length=64),
        unit: f_str("计量单位", max_length=16, help="默认 kg"),
        unit_price: f_num("单价", minimum=0),
        safety_stock: f_num("安全库存", minimum=0),
        shelf_life_days: f_int("保质期（天）", minimum=0),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """`material.create`（registry §1.4，t18 补；原裁决见 `DECISIONS.md` Q21）。

        归属来自数据范围（`tenant_keys_for_create`）：物料的 `area_id` 是 NOT NULL，
        由服务端解析——客户端指定归属就等于"用户自报家门"（§0.7 规则 1 / Q7）。
        """
        ctx.require("material.create")

        keys = tenant_keys_for_create(tx, scope)
        try:
            tx.execute(
                "INSERT INTO materials "
                "(organization_id, farm_id, area_id, code, name, category, spec, unit, "
                " unit_price, safety_stock, shelf_life_days, note, status, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s)",
                (
                    keys["organization_id"],
                    keys["farm_id"],
                    keys["area_id"],
                    code,
                    name,
                    category or "feed",
                    spec,
                    unit or "kg",
                    unit_price,
                    safety_stock,
                    shelf_life_days,
                    note,
                    ctx.actor.user_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            # 唯一键 `uq_materials_org_code(organization_id, code)`：**企业内**唯一，
            # 不按基地/区域切分（003 迁移的真实唯一键）。并发下的兜底。
            if "uq_materials_org_code" in str(exc):
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"已存在编号为「{code}」的物料",
                    data={"field": "code"},
                ) from exc
            raise

        material_id = tx.last_insert_id()
        row = self.load_material(tx, scope=scope, record_id=material_id)
        assert row is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(row, scope, ctx.actor.permissions),
                # `UniqueCode(scope=("organization_id",))` 需要 `organization_id`，
                # 而它由服务端解析（不在请求体里）。见 `create_area` 的同段说明。
                "_invariant_context": {"organization_id": int(row["organization_id"])},
            },
            resource_id=material_id,
            message=f"已新建物料「{name}」（编号 {code}）",
        )

    def update_material(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        material_id: f_int("物料 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
        name: f_str("物料名称", max_length=120),
        category: f_str("分类", max_length=32),
        spec: f_str("规格", max_length=64),
        unit: f_str("计量单位", max_length=16),
        unit_price: f_num("单价", minimum=0),
        safety_stock: f_num("安全库存", minimum=0),
        shelf_life_days: f_int("保质期（天）", minimum=0),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        """`material.update`。`code` 不可改（见模块说明）。"""
        ctx.require("material.update")
        row = self._scope_row(tx, scope, int(material_id))
        self._workflow().require_action(str(row["status"]), RowAction.EDIT)
        return self._update_row(
            tx, scope, ctx,
            record_id=int(material_id),
            expected_version=int(expected_version),
            patch=_non_null(
                name=name,
                category=category,
                spec=spec,
                unit=unit,
                unit_price=unit_price,
                safety_stock=safety_stock,
                shelf_life_days=shelf_life_days,
                note=note,
            ),
            loader=self.load_material,
        )

    def archive_material(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        material_id: f_int("物料 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """`material.archive`：停用一个物料。

        ⚠️ **它不会去改已核验的历史单据**。已核验的到货/领用行仍指向这个物料
        （外键 RESTRICT，行还在），而新的领用/到货会因为它不在下拉候选里而选不到它。
        这正是"停用"该有的效果——**退出的是视图与候选，不是历史**。
        """
        return self._archive(
            tx, scope, ctx,
            record_id=int(material_id),
            expected_version=int(expected_version),
            permission="material.archive",
            loader=self.load_material,
        )

    def submit_material(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        material_id: f_int("物料 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        return self._transition(
            tx, ctx, scope,
            material_id=int(material_id),
            expected_version=int(expected_version),
            from_state="draft",
            to_state="submitted",
            action=RowAction.SUBMIT,
            permission="material.submit",
            message="物料已提交核验",
        )

    def verify_material(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        material_id: f_int("物料 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        return self._transition(
            tx, ctx, scope,
            material_id=int(material_id),
            expected_version=int(expected_version),
            from_state="submitted",
            to_state="verified",
            action=RowAction.VERIFY,
            permission="material.verify",
            message="物料已核验",
        )

    def _transition(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        material_id: int,
        expected_version: int,
        from_state: str,
        to_state: str,
        action: RowAction,
        permission: str,
        message: str,
    ) -> cap.HandlerResult:
        ctx.require(permission)
        row = self._scope_row(tx, scope, material_id)
        workflow = self._workflow()
        workflow.require_action(str(row["status"]), action)
        workflow.require_transition(str(row["status"]), to_state, action=permission)
        affected = tx.execute(
            "UPDATE materials SET status=%s, row_version=row_version+1 "
            "WHERE id=%s AND row_version=%s AND status=%s",
            (to_state, material_id, expected_version, from_state),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该物料状态已变化或已被他人修改，请刷新后重试",
            )
        updated = self.load_material(tx, scope=scope, record_id=material_id)
        assert updated is not None
        return cap.HandlerResult(
            data={"record": self._decorate(updated, scope, ctx.actor.permissions)},
            resource_id=material_id,
            message=message,
        )


__all__ = ["AreaWriteService", "FarmWriteService", "MaterialWriteService"]
