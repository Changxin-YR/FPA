"""塘口的写能力：create / update / submit / verify / archive + 状态变更两步审批。

## 写能力的统一形状

每个写能力都做四件事，顺序固定：

    1. ctx.require(...)          —— 第三层权限校验（不信任执行器）
    2. scope.assert_allows_row   —— 第三层范围校验（校验的是**具体那一行**）
    3. 状态机校验                 —— require_action / require_transition
    4. 写库 + 返回 HandlerResult(resource_id=...)

第 4 步的 resource_id **不是可选的**：执行器要按它回读校验。
少了它，executed 就退化成"我们调用了 INSERT"而不是"数据真的落库了"。

## 一处刻意的"拒绝"

pond.update 拒绝 pond_status：它被声明为 readonly，提交会被 validate_payload
以 FORBIDDEN 拒绝并说明原因。业务状态只能走两步审批（DECISIONS.md Q2）。
"""

from __future__ import annotations

from typing import Any

from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError, ErrorCode, not_found
from fpa.kernel.fields import f_enum, f_int, f_num, f_ref, f_str, text
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import RowAction

from ._scope_keys import area_scope_row
from .ponds import CREATE_POND_STATUSES, MAX_CAPACITY_MU, PondService
from .service import POND_STATUS_WORKFLOW, POND_WORKFLOW


def _choice_tuple(codes: tuple[str, ...]) -> tuple:
    """把状态码元组转成 Choice 元组，label 取自状态机。

    为什么不在这里写死中文：**状态的文案只能有一个来源**（State.label）。
    手写第二遍就是早期版本那 14 处独立翻译的起点——同一个 verified
    在成本页叫「待确认」、在其他页叫「已核验」、在 agent 词典里叫「已提交」。
    """
    from fpa.kernel.fields import Choice

    labels = {state.code: state.label for state in POND_WORKFLOW.states}
    return tuple(Choice(value=code, label=labels.get(code, code)) for code in codes)


def _label_of(code: str) -> str:
    for state in POND_WORKFLOW.states:
        if state.code == code:
            return state.label
    return code


def _non_null(**fields: Any) -> dict[str, Any]:
    """只保留**提交了**的字段（None 视为"没提交"）。

    与 validate_payload 把缺失字段填成 None 配套：后者统一了"缺字段 = None"，
    这里把"没提交"与"提交了 None"区分开——更新场景下两者含义不同
    （前者不动、后者清空）。
    """
    return {key: value for key, value in fields.items() if value is not None}


class PondWriteService(PondService):
    """塘口的写路径。

    继承 PondService 以复用 _scope_row / _decorate / load_pond——
    读写共用同一套范围校验与派生逻辑，避免"读看到一个样、写看到另一个样"。
    这是早期版本的一处真实问题：master_data_store.py 里读路径与写路径
    各自拼 scope 条件，口径不一致。
    """

    @staticmethod
    def _assert_area_in_scope(scope: Scope, area: dict[str, Any]) -> None:
        """"这个区域是不是我的？"——**问对列**再交给内核判定。

        `areas` 表没有 `area_id` 列（只有 farm_id / organization_id），而
        `Scope.allows_row` 按范围类型的同名列取值（area 型取 `area_id`）。
        所以直接问一行 `areas` 行会永远判"不在范围内"（取到 None → 判定位外），
        真实后果是 **area 型范围的账号永远建不了塘口**。

        `area_scope_row` 把 `area_id` 填成**区域自身 id**（area 型范围的 `bound_id`
        本来就是 `areas.id`），于是判定回到它的本意："范围内有这个区域吗"。
        farm 型范围同样命中（farm_id 与 `areas.farm_id` 对得上）。

        用 `scope.assert_allows_row`（内核接口）而不是自己写一份判定：
        **范围判定只能有一份实现**，否则又是一处"两处描述同一件事"。
        """
        scope.assert_allows_row(area_scope_row(area), what="所属区域")

    def create_pond(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        code: f_str("塘口编号", required=True, max_length=64),
        name: f_str("塘口名称", required=True, max_length=100),
        area_id: f_ref("所属区域", "area", required=True),
        pond_status: f_enum(
            "塘口状态",
            _choice_tuple(CREATE_POND_STATUSES),
            help="新建时只能是「待建设」或「已放苗」；之后的结构性变更需经两步审批",
        ),
        species: f_str("主养品种", max_length=64),
        capacity_mu: f_num("面积（亩）", minimum=0, maximum=MAX_CAPACITY_MU),
        manager_name: f_str("负责人", max_length=40),
        location_text: f_str("位置描述", max_length=200),
        aerator_count: f_int("增氧机数量", minimum=0),
        stocking_spec: f_str("放养规格", max_length=64),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        ctx.require("pond.create")

        # 区域必须在范围内，且必须存在。
        # 为什么"存在性"也由服务端查：前端下拉可能拿到过期数据（区域刚被删），
        # 而外键报错只会给一句"关联对象不存在"——不如在这里给出具体原因。
        area = tx.query_one(
            "SELECT id, organization_id, farm_id, name FROM areas WHERE id = %s",
            (int(area_id),),
        )
        if area is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID, "所属区域不存在", data={"field": "area_id"}
            )
        # ⚠️ 这里曾经是 `scope.allows_row(area)`，那是**问错了列**：
        # `areas` 行没有 `area_id` 列（只有 farm_id / organization_id），
        # `Scope.allows_row` 取到 None 就判定位外 —— 于是 **area 型范围的账号
        # 永远建不了塘口**（farm 型也曾经不行：这里的判定对象是区域行，
        # 它只有 farm_id，而范围里若是 area 型记录则没有 farm 型记录可比）。
        # 这里曾经是 `scope.allows_row(area)` —— 问错了列，**area 型范围的账号
        # 永远建不了塘口**。改判"区域自身 id 在不在范围内"，详见下方帮助函数的说明。
        self._assert_area_in_scope(scope, area)

        # 创建时的 status 由服务端决定，不接受客户端指定——
        # 让客户端指定等于让调用方跳过流程（比如直接建成 verified）。
        tx.execute(
            "INSERT INTO ponds "
            "(organization_id, farm_id, area_id, code, name, species, capacity_mu, "
            " pond_status, manager_name, location_text, aerator_count, stocking_spec, "
            " note, status, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s)",
            (
                area["organization_id"],
                area["farm_id"],
                int(area_id),
                code,
                name,
                species,
                capacity_mu,
                pond_status or "build",
                manager_name,
                location_text,
                aerator_count,
                stocking_spec,
                note,
                ctx.actor.user_id,
            ),
        )
        pond_id = tx.last_insert_id()
        row = self.load_pond(tx, scope=scope, record_id=pond_id)
        assert row is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(row, scope, ctx.actor.permissions),
                # 服务把**服务端解析出来的**分租键回传给执行器，供声明式不变量使用：
                # `UniqueCode(scope=("farm_id",))` 判定"编码在基地内唯一"，而 `farm_id`
                # 既不在请求体里（§0.7 规则 1：create 不接收它）、也不是能力入参。
                # 不回传的后果不是"少查一次"——`UniqueCode` 取不到范围列会**显式报
                # INTERNAL_ERROR**（`invariants.py:977`），因为静默放宽到全表比报错危险。
                # 这是 `INVARIANT_TYPES.md` 记录的既有机制（服务经
                # `HandlerResult.data["_invariant_context"]` 补充不变量所需的上下文）。
                "_invariant_context": {"farm_id": int(row["farm_id"])},
            },
            resource_id=pond_id,
            message=f"已创建塘口「{name}」（编号 {code}）",
        )

    def update_pond(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        pond_id: f_int("塘口 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
        name: f_str("塘口名称", max_length=100),
        area_id: f_ref("所属区域", "area"),
        species: f_str("主养品种", max_length=64),
        capacity_mu: f_num("面积（亩）", minimum=0, maximum=MAX_CAPACITY_MU),
        manager_name: f_str("负责人", max_length=40),
        location_text: f_str("位置描述", max_length=200),
        aerator_count: f_int("增氧机数量", minimum=0),
        stocking_spec: f_str("放养规格", max_length=64),
        note: text("备注", max_length=500),
    ) -> cap.HandlerResult:
        ctx.require("pond.update")
        row = self._scope_row(tx, scope, int(pond_id))
        POND_WORKFLOW.require_action(str(row["status"]), RowAction.EDIT)

        patch = _non_null(
            name=name,
            area_id=None if area_id is None else int(area_id),
            species=species,
            capacity_mu=capacity_mu,
            manager_name=manager_name,
            location_text=location_text,
            aerator_count=aerator_count,
            stocking_spec=stocking_spec,
            note=note,
        )
        if not patch:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "没有需要更新的内容")

        if "area_id" in patch:
            area = tx.query_one(
                "SELECT id, organization_id, farm_id FROM areas WHERE id = %s",
                (patch["area_id"],),
            )
            if area is None:
                raise DomainError(
                    ErrorCode.FIELD_INVALID, "所属区域不存在", data={"field": "area_id"}
                )
            # 同 create_pond：判定对象是**区域自身**，不是"行上的 area_id 列"。
            self._assert_area_in_scope(scope, area)

        assignments = ", ".join(f"{key}=%s" for key in patch)
        affected = tx.execute(
            f"UPDATE ponds SET {assignments}, row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s",
            [*patch.values(), ctx.actor.user_id, int(pond_id), int(expected_version)],
        )
        if affected == 0:
            current = self._scope_row(tx, scope, int(pond_id))
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该塘口已被他人修改，请刷新后重试",
                data={"current_version": int(current["row_version"])},
            )

        updated = self.load_pond(tx, scope=scope, record_id=int(pond_id))
        assert updated is not None
        return cap.HandlerResult(
            data={"record": self._decorate(updated, scope, ctx.actor.permissions)},
            resource_id=int(pond_id),
            message="塘口已更新",
        )

    def submit_pond(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        pond_id: f_int("塘口 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        return self._lifecycle(
            tx, ctx, scope,
            pond_id=int(pond_id),
            expected_version=int(expected_version),
            from_states=("draft",),
            to_state="submitted",
            action=RowAction.SUBMIT,
            permission="pond.submit",
            message="塘口已提交核验",
        )

    def archive_pond(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        pond_id: f_int("塘口 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """归档：draft / verified -> archived。

        早期版本在 draft 上提供的是**物理删除**（旧 common/governance/lifecycle.py:49），
        靠唯一键/外键报错翻译成 DELETE_NOT_ALLOWED。
        新系统改为归档——已核验的数据不该消失，而草稿作废也不需要物理删除
        （归档同样让它退出业务视图，但保留了审计线索）。
        """
        return self._lifecycle(
            tx, ctx, scope,
            pond_id=int(pond_id),
            expected_version=int(expected_version),
            from_states=("draft", "verified"),
            to_state="archived",
            action=RowAction.ARCHIVE,
            permission="pond.archive",
            message="塘口已归档",
        )

    def verify_pond(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        pond_id: f_int("塘口 ID", required=True),
        expected_version: f_int("乐观锁版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """核验：submitted -> verified。**经办人不能核验自己提交的**。

        ## 这条合规规则现在**只由内核不变量**强制（Q8）

        原先这里有一份手写的 `if int(row["created_by"]) == ctx.actor.user_id: raise`，
        `UPDATE` 的 `WHERE` 里还额外带了 `AND created_by <> %s`。两处都是
        "同一件事的第二份实现"，而且第二份在**只有一人时**会把拒绝伪装成
        `VERSION_CONFLICT`（WHERE 匹配 0 行）——错误码指向并发冲突，实际原因是自审。

        现在判定统一在内核 `DistinctActors()`（见 `capabilities.py` 的 `pond.verify`）：
        人工页面与 Agent 走的是**同一份判断**，不是"两处实现碰巧一致"。

        ## 保留了什么

        * `ctx.require`（第三层权限）与 `_scope_row`（范围）——服务不信任调用方；
        * `POND_WORKFLOW.require_action`——状态机动作校验，它比不变量更早给出
          "当前状态不允许核验"的可读文案；
        * `UPDATE ... WHERE row_version=%s AND status='submitted'`——**乐观锁与状态
          条件的实际写入**，那是仓储职责（`INVARIANT_TYPES.md` §4）。
        """
        ctx.require("pond.verify")
        row = self._scope_row(tx, scope, int(pond_id))
        POND_WORKFLOW.require_action(str(row["status"]), RowAction.VERIFY)

        affected = tx.execute(
            "UPDATE ponds SET status='verified', verified_by=%s, verified_at=NOW(), "
            "row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s AND status='submitted'",
            (
                ctx.actor.user_id,
                ctx.actor.user_id,
                int(pond_id),
                int(expected_version),
            ),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该塘口状态已变化或已被他人修改，请刷新后重试",
            )

        verified = self.load_pond(tx, scope=scope, record_id=int(pond_id))
        assert verified is not None
        return cap.HandlerResult(
            data={"record": self._decorate(verified, scope, ctx.actor.permissions)},
            resource_id=int(pond_id),
            message=f"塘口「{verified['name']}」已核验",
        )

    def _lifecycle(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        pond_id: int,
        expected_version: int,
        from_states: tuple[str, ...],
        to_state: str,
        action: RowAction,
        permission: str,
        message: str,
    ) -> cap.HandlerResult:
        """生命周期动作的统一实现。

        三个动作形状相同，差别只在"从哪些状态来、到哪个状态去、需要什么权限"。
        抽成一个方法而不是复制三遍——早期版本 master_data_store.py 里每个动作
        各写一遍 UPDATE + WHERE + 冲突翻译，那是 bug 的温床（改了一处忘了另一处）。
        """
        ctx.require(permission)
        row = self._scope_row(tx, scope, pond_id)
        POND_WORKFLOW.require_action(str(row["status"]), action)
        if str(row["status"]) not in from_states:
            raise DomainError(
                ErrorCode.CONFLICT,
                "当前状态不允许该操作",
                data={"status": str(row["status"]), "allowed_from": list(from_states)},
            )

        placeholders = ",".join(["%s"] * len(from_states))
        affected = tx.execute(
            f"UPDATE ponds SET status=%s, row_version=row_version+1, updated_by=%s "
            f"WHERE id=%s AND row_version=%s AND status IN ({placeholders})",
            (to_state, ctx.actor.user_id, pond_id, expected_version, *from_states),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT, "该塘口已被他人修改，请刷新后重试"
            )

        updated = self.load_pond(tx, scope=scope, record_id=pond_id)
        assert updated is not None
        return cap.HandlerResult(
            data={"record": self._decorate(updated, scope, ctx.actor.permissions)},
            resource_id=pond_id,
            message=message,
        )

    def request_pond_status_change(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        pond_id: f_int("塘口 ID", required=True),
        to_status: f_enum(
            "目标状态",
            (),
            help="从塘口当前状态出发的合法目标（服务端按转移表过滤）",
        ),
        reason: text("变更原因", required=True, max_length=500),
        expected_pond_version: f_int("塘口版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """发起塘口业务状态变更申请（两步审批的第一步）。

        **这一步不改塘口的 pond_status**——它只建申请。真实变更在
        verify_pond_status_change 里发生，且必须由**另一人**核验。

        from_status 由服务端从库里取（不接受客户端指定）：
        让客户端说自己"从哪个状态来"，就等于让调用方定义前置条件。
        """
        ctx.require("pond.status.request")
        row = self._scope_row(tx, scope, int(pond_id))
        current = str(row["pond_status"])

        POND_STATUS_WORKFLOW.require_transition(
            current, str(to_status), action="pond_status_change.request"
        )

        try:
            tx.execute(
                "INSERT INTO pond_status_change_requests "
                "(organization_id, pond_id, from_status, to_status, reason, "
                " pond_version, requested_by) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    row["organization_id"],
                    int(pond_id),
                    current,
                    str(to_status),
                    reason,
                    int(row["row_version"]),
                    ctx.actor.user_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            # 唯一键冲突 = 已有待核验申请。
            # 这条约束在**数据库层**（uq_pond_status_active_request 走生成列），
            # 所以并发下的两个请求里必然只有一个能进来——
            # 应用层的"先查再插"做不到这一点。
            if "uq_pond_status_active_request" in str(exc):
                raise DomainError(
                    ErrorCode.CONFLICT, "该塘口已有待核验的状态变更申请，请先处理它"
                ) from exc
            raise

        request_id = tx.last_insert_id()
        return cap.HandlerResult(
            data={
                "status_change": {
                    "id": request_id,
                    "pond_id": int(pond_id),
                    "from_status": current,
                    "to_status": str(to_status),
                    "reason": reason,
                }
            },
            # ★ `resource_id` 是**本次真正写入的那一行**（申请行），不是塘口。
            #
            # 三个理由，都可核查：
            #   1. `WRITE_CONTRACT.md` 规则 2：`executed` 的含义是"读回来的行确实是我们
            #      想要的样子"——本次写入的行就是申请行；
            #   2. 执行器用 `resource_id` 当 `_invariant_exclude_id`，而
            #      `AtMostOnePending` 靠它排除刚插入的自己。指向塘口会让**成功路径被自己
            #      挡住**（实测 `已存在一条待核验的状态变更申请`）——而且
            #      `runner._invariant_extra` 无条件覆盖该键，服务想自己覆盖也会被忽略；
            #   3. 幂等回放所以能返回"我申请了什么"，而不是塘口行。
            #
            # 代价：`idempotency_keys.resource_id` 存的是申请行 id。这是准确的——
            # 该键的用途正是"这次调用的产物是哪一个对象"。
            resource_id=int(request_id),
            message="已提交状态变更申请，等待他人核验",
        )

    def verify_pond_status_change(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        pond_id: f_int("塘口 ID", required=True),
        request_id: f_int("申请 ID", required=True),
        expected_version: f_int("申请版本", required=True, minimum=1),
    ) -> cap.HandlerResult:
        """核验状态变更申请（两步审批的第二步）。**必须由另一人核验。**

        三个并发校验，任何一个不过都不改数据：

          1. 申请仍是 submitted（原子占位：UPDATE ... WHERE status='submitted'）
          2. 申请人 != 核验人（数据库 CHECK + 这里显式判断）
          3. 申请时的塘口版本 = 核验时的塘口版本
             （防"申请期间塘口被别的操作改过"——旧 pond_status_store.py 的做法，继承）
        """
        ctx.require("pond.status.verify")
        row = self._scope_row(tx, scope, int(pond_id))

        change = tx.query_one(
            "SELECT * FROM pond_status_change_requests WHERE id=%s AND pond_id=%s",
            (int(request_id), int(pond_id)),
        )
        if change is None:
            raise not_found("状态变更申请")
        if str(change["status"]) != "submitted":
            raise DomainError(
                ErrorCode.CONFLICT,
                "该申请已处理或已取消",
                data={"status": str(change["status"])},
            )
        # ⚠️ 这里保留**显式**的自审判定，与 `verify_pond` 不同 —— 原因是可核查的：
        # `DistinctActors` 比的是 `before["created_by"]`，而 before 快照来自
        # `PondService.load_pond`（**塘口行**上没有 created_by），申请行的
        # `requested_by` 不在其中。所以这条规则**结构上不适用于"基于申请核验"**。
        # 已作为缺口上报（要么补一个按申请行取值的规则类型，要么让本能力提供快照）。
        if int(change["requested_by"]) == ctx.actor.user_id:
            raise DomainError(
                ErrorCode.FORBIDDEN,
                "申请人不能核验自己的申请，请由他人复核",
                data={"rule": "DISTINCT_ACTORS"},
            )

        if int(row["row_version"]) != int(change["pond_version"]):
            raise DomainError(
                ErrorCode.CONFLICT,
                "该塘口在申请后被修改过，请重新发起变更申请",
                data={
                    "requested_at_version": int(change["pond_version"]),
                    "current_version": int(row["row_version"]),
                },
            )

        # WHERE 里不再带 `AND requested_by <> %s`：上面已显式判定过，重复条件只会让
        # "被拒"与"并发"共用同一个 `affected == 0` 分支（错误码会指向冲突）。
        claimed = tx.execute(
            "UPDATE pond_status_change_requests "
            "SET status='verified', verified_by=%s, verified_at=NOW() "
            "WHERE id=%s AND status='submitted'",
            (ctx.actor.user_id, int(request_id)),
        )
        if claimed == 0:
            raise DomainError(ErrorCode.CONFLICT, "该申请已被他人处理")

        affected = tx.execute(
            "UPDATE ponds SET pond_status=%s, row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND pond_status=%s",
            (
                str(change["to_status"]),
                ctx.actor.user_id,
                int(pond_id),
                str(change["from_status"]),
            ),
        )
        if affected == 0:
            raise DomainError(ErrorCode.CONFLICT, "塘口状态已变化，请刷新后重试")

        updated = self.load_pond(tx, scope=scope, record_id=int(pond_id))
        assert updated is not None
        return cap.HandlerResult(
            data={
                "record": self._decorate(updated, scope, ctx.actor.permissions),
                "status_change": {
                    "id": int(request_id),
                    "to_status": str(change["to_status"]),
                },
                # ★ `StateTransition`（形态二）要的两个值：迁移的**两端都不在请求体里**，
                #   也不在同一份快照里 —— 当前状态在**塘口行**、目标状态在**申请行**的
                #   `to_status`。内核读到它们才会判定；读不到会**显式 INTERNAL_ERROR**，
                #   不会静默放行（所以这两个键是声明生效的前提，不是可选装饰）。
                #
                #   为什么由服务回传而不是让内核自己查：这两行在两张表上，而"申请行"
                #   是本次操作的输入；让内核按表名去猜是哪一行，等于把调用方的语义塞进内核。
                "_invariant_context": {
                    "status_change_from": str(change["from_status"]),
                    "status_change_to": str(change["to_status"]),
                },
            },
            resource_id=int(pond_id),
            message=f"塘口状态已变更为「{_label_of(str(change['to_status']))}」",
        )

    # -- 回读函数 -------------------------------------------------------------

    def load_pond_status_change_request(
        self, tx: UnitOfWork, *, scope: Scope, record_id: int
    ) -> dict[str, Any] | None:
        """回读**状态变更申请**行（`pond_status_change.request` 的 loader）。

        `record_id` 是申请行 id —— 该能力的 `resource_id` 就是它（见
        `request_pond_status_change` 里那段说明）。执行器按它回读，审计的 `after_json`
        与幂等回放的 `data` 因此都是**本次真正写下的那一行**。

        范围校验：申请行所属的塘口必须在当前账号范围内（`areas`/`farm` 两种范围都经
        `Resource(table="ponds")` 的 area_id 列解析，与本域读路径同口径）。
        """
        row = tx.query_one(
            "SELECT r.*, p.area_id, p.farm_id, p.organization_id "
            "FROM pond_status_change_requests AS r "
            "JOIN ponds AS p ON p.id = r.pond_id "
            "WHERE r.id = %s",
            (record_id,),
        )
        return None if row is None else dict(row)


# ---------------------------------------------------------------------------
# 写路径登记表（哪些服务方法是写能力）
# ---------------------------------------------------------------------------
#
# 这张表**不做挂载**：回读函数由能力声明里的 `loader=`（`capabilities.py`）交给内核，
# 内核在 `Capability.__post_init__` 里 `setattr(handler, "__fpa_load_by_id__", loader)`。
# 让五个域各自手写那个属性名字符串，等于把"内核与执行器之间的内部约定"复制五份——
# 内核那一份已经因为一次笔误（`__fpa_read_by_id__`）让幂等回放静默退化过。
#
# 本表保留的价值是**评审可见**：读这个文件的人一眼看到"塘口有 7 条写路径，
# 其中 2 条是两步审批"。漏登记由 `tests/test_write_reload_contract.py` 与
# e2e 的注册表断言兜住（它们断言的是注册表里的写能力，而不是这张手写表）。
#
# 回读哪个资源：7 条写能力的 `resource_id` 全部是**塘口 id**，但回读的行不同 ——
#   * `pond.create/update/submit/verify/archive` -> 塘口行（`PondService.load_pond`）
#   * `pond_status_change.request` -> **申请行**（`load_pond_status_change_request`，
#     因为它真正写下的是一行申请；回读塘口会给出一条"塘口没变"的审计）

_WRITE_HANDLERS: tuple[str, ...] = (
    # 塘口自身的写路径
    "create_pond",
    "update_pond",
    "submit_pond",
    "verify_pond",
    "archive_pond",
    # 业务状态的两步审批
    "request_pond_status_change",
    "verify_pond_status_change",
)
# 挂载与守卫都**不在这里**：
#   * 挂载由内核的 `Capability.loader=` 负责（`Capability.__post_init__` 会
#     `setattr(handler, "__fpa_load_by_id__", loader)`）。属性名是内核与执行器之间的内部
#     约定，让五个域各自手写这个字符串常量等于把这个约定复制五份——内核那一份已经因为
#     一次笔误（`__fpa_read_by_id__`）让幂等回放静默退化过。
#   * 守卫在 `capabilities.py` 末尾的 `assert_reload_wired()`：它断言的是**注册表里的
#     写能力**，而回读函数只在声明被执行过之后才存在——所以守卫必须在声明之后跑。
#     写在本文件里会依赖 import 顺序（直接 import 本模块的调用方会看到"没挂"）。
#     本文件保留的事实是 `_WRITE_HANDLERS` 这行登记表：哪些方法是写路径。
