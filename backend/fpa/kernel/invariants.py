"""业务不变量：可组合的声明式规则，由内核统一执行。

早期版本的做法是把不变量写在仓储里**紧贴 SQL**：
    * 负存塘  → `早期版本 production_store.py:273-280`
    * 负库存  → `早期版本 warehouse_ledger_store.py:181-202`
    * 未审批不得收货 → `早期版本 purchase_posting.py:19-20`
    * 付款不得超余额 → `早期版本 purchase_payment_store.py:114,154`
    * 出塘数量一致 → `早期版本 sales_source_control.py:22-24`
    * 期间锁定 + 成本去重 → `早期版本 cost_enterprise_repository.py:86-92,117-150`

这些 `FOR UPDATE` 行锁的写法是**正确的**，要继承。但把它们写在仓储里有两个代价：
    1. 只能靠真实 MySQL 验证，无法单元测试；
    2. 无法被第二个调用方复用 —— 这正是为什么"经办人≠审批人"只在 `sales_service.py:121-122`
       一处实现，而采购/仓储/生产/成本全都没有。

新系统把规则提取成对象，由 `run_invariants()` 在**同一个事务内、业务写入之后、提交之前**
统一执行（`kernel/runner.py` 的顺序：`_maybe_load_before` → `_call_service` →
`run_invariants` → `_reload_after` → 审计 → 提交）。因此：

    * 失败仍然整体回滚，**不产生部分写入**；
    * 但不变量看到的是**写入之后**的状态 —— 账本类规则直接判后验余额，不要自己再加
      增量（`SUM()` 已含本次写入，再加一次是重复计算，会把合法操作误拒）；
    * 需要区分"本次刚写的那一行"的规则（唯一性类）用 `_invariant_exclude_id`。

仓储仍然负责行锁（这是它的职责）；不变量负责"断言什么条件下不允许写"。

**人工入口与 Agent 入口走同一份判断** —— 这是需求里"防止智能体绕过人工页面权限"的
可证明形态：不是两套实现碰巧一致，而是根本只有一套。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fpa.kernel.errors import DomainError, ErrorCode
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import RESOURCES, Workflow


def lock_period_for_date(
    tx: UnitOfWork,
    *,
    organization_id: int,
    occurred_on: Any,
) -> dict[str, Any] | None:
    """定位并锁住租户内包含日期的会计期间。"""
    return tx.query_one(
        "SELECT * FROM accounting_periods "
        "WHERE organization_id=%s AND period_start <= %s AND period_end >= %s "
        "ORDER BY period_start DESC, id DESC LIMIT 1 FOR UPDATE",
        (organization_id, occurred_on, occurred_on),
    )


def _require(payload: dict[str, Any], field_name: str, label: str) -> Any:
    value = payload.get(field_name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            f"{label}不能为空",
            data={"field": field_name},
        )
    return value


# ---------------------------------------------------------------------------
# RequiredField —— 必填字段的**业务**版本
#
# 与 Field(required=True) 的区别：Field.required 管的是"请求体里必须出现"，
# 而 RequiredField 管的是"业务上必须给出有效值"（例如核验成本时必须说明来源单号，
# 而这个字段在 create 时可以留空、在 confirm 时不能）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequiredField:
    """指定字段在本次操作中必须非空。

    用于 `cost.entry.confirm` 的降级形态（裁决 Q5）：不做附件后，
    "核验必须有凭据"降级为"必填来源单号代替凭据"。
    """

    fields: tuple[str, ...]
    labels: tuple[str, ...] = ()

    name = "RequiredField"

    def __post_init__(self) -> None:
        if self.labels and len(self.labels) != len(self.fields):
            raise ValueError("RequiredField 的 labels 与 fields 数量必须一致")

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        for index, field_name in enumerate(self.fields):
            label = self.labels[index] if self.labels else field_name
            # 允许从 before 里取值（更新场景下字段可能未随请求提交）
            value = payload.get(field_name)
            if value is None or (isinstance(value, str) and not value.strip()):
                value = (before or {}).get(field_name)
            _require({field_name: value}, field_name, label)


# ---------------------------------------------------------------------------
# DistinctActors —— 经办人 ≠ 审批人
#
# 早期版本只在 sales 域实现（`早期版本 sales_service.py:121-122`）。这是**合规规则跨域
# 不一致**的典型：同一个公司里，销售单要求双人复核，采购单却允许自审。
# 新系统推广到全部 11 条核验/审批能力（裁决 Q8），且不留例外。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DistinctActors:
    """核验人不得与经办人相同。

    ``creator_field`` / ``verifier_field`` 都指向**业务行上的列**，而不是请求体
    字段——因为"谁创建的"是既成事实，不能由本次请求声称。
    """

    creator_field: str = "created_by"
    verifier_field: str = "verified_by"

    name = "DistinctActors"

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        if before is None:
            # 没有原始快照 = 没有"经办人"这个事实（新建、或该能力不走回读路径）。
            # 此时本规则不适用 —— 报 INTERNAL_ERROR 会把"不适用"伪装成内核故障。
            return
        creator = before.get(self.creator_field)
        if creator is None:
            return
        if int(creator) == actor_id:
            raise DomainError(
                ErrorCode.FORBIDDEN,
                "经办人不能核验自己提交的单据，请由他人复核",
                data={"rule": "DISTINCT_ACTORS"},
            )


# ---------------------------------------------------------------------------
# NoNegativeStock —— 不得负库存 / 不得负存塘
#
# 早期版本两处实现（物料库存与塘内存塘），`FOR UPDATE` 行锁是正确的。
# 新系统把"求和 + 比对"的语义声明化，但**行锁仍在仓储里做**——
# 不变量不负责并发，并发是仓储的职责。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# NoNegativeStock —— 不得负库存 / 不得负存塘（registry §4 #1、#2）
#
# 早期版本两处实现（物料库存与塘内存塘），`FOR UPDATE` 行锁是正确的，继承。
# 新系统把"求和 + 比对"的语义声明化；行锁仍在仓储/本不变量发起的查询里做。
#
# **判定的是"写入后"的余额**：不变量在 `_call_service()` 之后执行，`SUM()` 已经包含
# 本次写入，所以判定式就是 `SUM(column) < 0`。旧形态从 `_ledger_lines` 自己算增量再
# `available + delta`，等于算成 `available_before + 2×delta`，对负增量会**误拒合法操作**
# （这是 评审结论里点名的第二个问题）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NoNegativeStock:
    """**写入后**的账本余额不得为负。

    与早期实现的三个差别，每一个都对应一次真实故障：

    1. **判定后验余额**：见上方注释。
    2. **多列**：存塘要同时保证"数量不为负"和"重量不为负"，单列表达不了
       （registry §4 #2 要求 `quantity_delta` + `weight_delta_kg` 都判）。
    3. **不依赖调用方回传做算术**：旧形态的 `if not lines: return` 意味着"能力挂了
       不变量、但服务忘了回传 → 静默通过、负库存照写"，与 `fields=` 忘传是同一个
       失败形态（DEVELOPMENT §4："修法不是记得传，而是删掉那个可选性"）。

    ``_ledger_lines`` 仍然要传，但它只用来**定位分组**（本次写的是哪些
    `group_by` 组合），不参与算术；缺它就显式报错，绝不静默放行。

    典型用法（投喂扣物料库存 / 出塘减存塘）::

        NoNegativeStock(
            table="inventory_ledger",
            columns=("quantity_delta",),
            group_by=("warehouse_id", "material_id", "inventory_lot_id"),
        )
        NoNegativeStock(
            table="batch_stock_records",
            columns=("quantity_delta", "weight_delta_kg"),
            group_by=("batch_id", "pond_id"),
            label="存塘",
        )
    """

    table: str
    columns: tuple[str, ...] = ("quantity_delta",)
    group_by: tuple[str, ...] = ()
    label: str = "库存"
    #: 兼容写法：等价于 `columns=(quantity_column,)`。新代码请直接用 `columns`。
    quantity_column: str = ""

    name = "NoNegativeStock"

    def __post_init__(self) -> None:
        if isinstance(self.columns, str):
            # 单列误写成字符串时纠正：否则 `SUM(q)` 会变成按字符遍历元组。
            object.__setattr__(self, "columns", (self.columns,))
        if self.quantity_column:
            merged = (self.quantity_column, *self.columns)
            object.__setattr__(self, "columns", tuple(dict.fromkeys(merged)))
        if not self.columns:
            raise ValueError("NoNegativeStock 至少要声明一个数量列")

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        """校验写入后的账本余额。

        分组来自 ``payload["_ledger_lines"]``（服务经
        `HandlerResult.data["_invariant_context"]` 回传的、本次写入的账本行）——
        行里的列名与库表列名一致（`warehouse_id` / `material_id` / `inventory_lot_id` /
        `batch_id` / `pond_id` …），这正是"只有服务知道本次动的是哪个分组"的原因。
        """
        lines: list[dict[str, Any]] = [
            line for line in (payload.get("_ledger_lines") or []) if isinstance(line, dict)
        ]
        if not lines:
            # 缺上下文 = 规则无法判定。**报错而不是放行** —— 放行会让负库存一路写进库。
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"{self.label}不变量缺少账本行上下文，无法校验写入后的余额",
                data={"table": self.table, "group_by": list(self.group_by)},
            )

        groups: list[tuple[Any, ...]] = []
        for line in lines:
            key: list[Any] = []
            for column in self.group_by:
                value = line.get(column)
                if value is None:
                    raise DomainError(
                        ErrorCode.INTERNAL_ERROR,
                        f"{self.label}不变量的账本行缺少分组列 {column}，无法定位要校验的分组",
                        data={"table": self.table, "column": column},
                    )
                key.append(value)
            if tuple(key) not in groups:
                groups.append(tuple(key))

        select = ", ".join(
            f"COALESCE(SUM({column}),0) AS {column}" for column in self.columns
        )
        where = " AND ".join(f"{column}=%s" for column in self.group_by)
        for key in groups:
            # FOR UPDATE 与早期版本一致：并发的两个扣减必须串行判定（行锁是仓储/查询的职责），
            # 同时保留"没有行时 SUM 返回 0"的语义。
            row = tx.query_one(
                f"SELECT {select} FROM {self.table} WHERE {where} FOR UPDATE",
                list(key),
            )
            if row is None:
                raise DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    f"{self.label}不变量的余额查询没有返回结果，无法校验",
                    data={"table": self.table, "group": list(key)},
                )
            detail = "、".join(
                f"{column}={value}" for column, value in zip(self.group_by, key, strict=True)
            )
            for column in self.columns:
                balance = _as_decimal(_row_value(row, column) or 0)
                if balance < 0:
                    raise DomainError(
                        ErrorCode.CONFLICT,
                        f"{self.label}不足，写入后余额为 {balance}（{detail}）",
                        data={
                            "rule": "NO_NEGATIVE_STOCK",
                            "column": column,
                            "balance": str(balance),
                            "group": detail,
                        },
                    )


#: `accounting_periods.status` 的取值域（registry §3.6 只声明了这两个）。
#: 「该日期没有登记任何期间 → 放行」是刻意的：期间管理尚未启用时不该把所有写入拦死；
#: 但**期间存在且状态不是目标状态**一律拒绝（fail-closed），包括将来新增的状态值。
PERIOD_STATUSES: tuple[str, ...] = ("open", "closed")


@dataclass(frozen=True, slots=True)
class PeriodOpen:
    """目标日期所在会计期间必须处于 ``status``（默认 `open`）状态。

    ## 谓词里必须有租户键（不改这一条就等于没修）

    `accounting_periods` 的 `organization_id` 是 NOT NULL：**每个企业各有一份自己的
    期间行**。查询只按日期区间取行、不带租户键时，命中的是"任一企业的期间"——
    实测 `tools/repro_period_open_tenant.py` 两方向都能复现：

      * 命中邻家已关账的期间 -> **误拦**本企业的合法写入（吵闹，用户会投诉）；
      * 命中邻家未关账的期间 -> **静默放行**本企业已关账期间的写入（安静，没人发现）。

    ## 为什么缺租户键时报错而不是"跳过这条规则"

    按 `ARCHIVED_DECISIONS.md` R2 的三分法：**业务事实缺失 → 跳过；基础设施缺失 → 报错**。
    "本次操作有没有发生日期"是业务事实（没有就跳过，字段必填由字段声明负责）；
    而"内核能不能把这条规则跑起来"是基础设施——`tenant_keys` 取不到值，说明
    **声明与装配不一致**（例如服务忘了经 `_invariant_context` 回传 `organization_id`）。
    此时若放宽成"不带租户键也查"，规则会以"看起来在强制"的形式跨企业串号；
    若放宽成"跳过"，这条规则就退化成建议。两者都是本项目的主导失败模式。
    """

    table: str = "accounting_periods"
    date_field: str = "occurred_on"
    status: str = "open"
    status_field: str = "status"
    #: 参与期间反查的租户键。`accounting_periods` 只有 `organization_id` 一列分租键
    #: （`farms` / `areas` 才带 `farm_id` / `area_id`），所以默认只有它一个；
    #: 将来这张表若补齐更细的分租键，把列名加进来即可，不需要改 `check()`。
    #:
    #: **不允许为空**：留空口子等于把"跨企业串号"变成一次声明即可绕过。
    tenant_keys: tuple[str, ...] = ("organization_id",)
    label: str = "该日期所在的会计期间"

    name = "PeriodOpen"

    def __post_init__(self) -> None:
        if self.status not in PERIOD_STATUSES:
            raise ValueError(
                f"PeriodOpen 的 status 只能是 {PERIOD_STATUSES} 之一，收到 {self.status!r}"
            )
        if not self.tenant_keys:
            # 与 `NoOverlappingSource.tenant_keys` 同一条理由：空元组能让"跨企业查期间"
            # 变成一次声明即可绕过，那是把安全属性交给调用者的自觉。
            raise ValueError(
                "PeriodOpen 必须声明 tenant_keys（分租键）："
                "没有分租键就无法把期间反查限定在本企业内（会计期间每个企业各一份）"
            )

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        value = _value_from((payload, before), self.date_field)
        if value is None:
            return  # 没有发生日期：不是本规则能判定的场景（字段必填由字段声明负责）

        # 租户键：**要么全有、要么报错**（与 NoOverlappingSource 同一口径）。
        # 取不到就报错而不是"去掉条件"，因为去掉之后命中的可能是**别家企业的期间行**，
        # 而"别家已关账"会误拦本企业、"别家未关账"会静默放行本企业的已关账期间。
        keys: list[tuple[str, Any]] = []
        for key in self.tenant_keys:
            key_value = _value_from((payload, "_invariant_context", before), key)
            if key_value is None:
                raise DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    f"{self.label}无法执行：缺少分租键 {key}，"
                    "无法把期间反查限定在本企业内（会计期间每个企业各有一份，"
                    "不带租户键会跨企业串号）",
                    data={
                        "rule": "PERIOD_OPEN",
                        "missing_tenant_key": key,
                        "tenant_keys": list(self.tenant_keys),
                    },
                )
            keys.append((key, key_value))

        # 共享 helper 统一维护租户谓词、ORDER BY 与 FOR UPDATE，避免写入口之间
        # 在期间判定和并发协议上发生漂移。
        if (
            self.table != "accounting_periods"
            or self.status_field != "status"
            or self.tenant_keys != ("organization_id",)
        ):
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "PeriodOpen 只能用于标准 accounting_periods.status",
            )
        period_row = lock_period_for_date(
            tx, organization_id=int(keys[0][1]), occurred_on=value
        )
        if period_row is None:
            return  # 本企业没有登记该期间的会计期间 —— 期间管理未启用，不拦
        current = _row_value(period_row, self.status_field)
        if current is None or str(current) == self.status:
            return
        raise DomainError(
            ErrorCode.CONFLICT,
            f"{self.label}已关账，不能再写入",
            data={
                "rule": "PERIOD_CLOSED",
                "period_status": str(current),
                "expected": self.status,
            },
        )


@dataclass(frozen=True, slots=True)
class RequiredWhen:
    """当 ``when_field`` 取 ``when_values`` 之一时，``required_fields`` 必须非空。"""

    when_field: str
    when_values: tuple[str, ...]
    required_fields: tuple[str, ...]
    labels: tuple[str, ...] = ()

    name = "RequiredWhen"

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        trigger = payload.get(self.when_field) or (before or {}).get(self.when_field)
        if str(trigger) not in self.when_values:
            return
        for index, field_name in enumerate(self.required_fields):
            label = self.labels[index] if self.labels else field_name
            value = payload.get(field_name)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise DomainError(
                    ErrorCode.VALIDATION_ERROR,
                    f"{label}不能为空",
                    data={"field": field_name},
                )


# ---------------------------------------------------------------------------
# 取值与查询的公共配件
#
# 每个不变量都从两个来源取值：`payload`（本次请求体）与 `before`（执行前的行快照）。
# 两者语义不同 —— payload 里只有"真正提交了的字段"（缺失 = 不提交），before 一行全有。
# 既有 `RequiredWhen` / `PeriodOpen` 已经示范了"先 payload、再回退 before"的写法，
# 下面把它抽成函数，避免 12 个新类型各写一遍 —— 那正是本项目最反对的
# "两处描述同一件事"。
# ---------------------------------------------------------------------------


def _value_from(sources: tuple[dict[str, Any] | None, ...], *names: str) -> Any:
    """按 ``names`` 的优先级在 ``sources`` 里取值；取不到返回 None。

    为什么 ``names`` 会有多个：同一个事实在本项目里有两种叫法 ——
    **入参是能力载荷里的字段名**（`payment_id`），**行快照是表里的列名**（`id`）。
    需要跨两者取值的不变量（OptimisticLock / ReferencedStatus / AmountWithin 等）
    必须同时认这两个名字。写死一个名字会在另一侧静默取到 None，于是校验被
    "跳过"且不报错 —— 这正是早期版本那些"声明了却从不生效"的规则的产生方式。
    """
    for source in sources:
        if not isinstance(source, dict):
            continue
        for name in names:
            value = source.get(name)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
    return None


def _row_value(row: dict[str, Any], *names: str) -> Any:
    """从一行数据库结果里取值；列名不匹配时依次回退。取不到返回 None。"""
    return _value_from((row,), *names)


#: 本次写入产生的主键，由服务通过 `HandlerResult.data["_invariant_context"]` 回传。
#:
#: ---------------------------------------------------------------------------
#: 为什么需要它（这是一个真实缺陷的修法，不是预防性设计）
#: ---------------------------------------------------------------------------
#: 不变量在**业务写入之后**执行（`runner._invoke_once`：`_call_service` ->
#: `run_invariants`）。因此"查一下有没有冲突行"这类不变量必然会把**本次刚写进去的
#: 那一行**查出来，于是它把自己判定成冲突：
#:
#:     第一次登记一笔手工费用 -> 不变量查到刚插入的手工费用行 ->
#:     「已由 manual_expense 自动归集过成本，不能重复归集」
#:
#: 症状是最坏的一种：**第一次本来就合法的写入被拒绝**，而错误消息指向一个不存在
#: 的既有冲突（消息里的来源类型是声明里的第一个值，与实际匹配到的行无关）。
#:
#: 两种可能的修法，选后者的理由：
#:
#:   * 把不变量改到写入**之前**执行 —— 语义更干净，但会改变 `run_invariants` 的
#:     时机契约，而 `NoNegativeStock` 依赖"账本增量已算出"、`DistinctActors` 依赖
#:     `before` 快照，改动面覆盖全部 21 条规则的 12 个实现；
#:   * **让服务告诉不变量"哪一行是我刚写的"** —— 只影响"查重"这一类不变量
#:     (`NoOverlappingSource` / `AtMostOnePending`)，其余 10 个无需知道。
#:
#: 选后者：**修一个缺陷时，改动面应当与缺陷的成因一样大。**
#:
#: 为什么用 payload 而不是新加一个 `check()` 参数：`Invariant` 是 Protocol，
#: 12 个实现都按同一个签名写；加参数要改全部实现与全部调用点，而 payload 本来
#: 就是"本次调用的上下文"，把上下文里的一项放进去不需要改任何签名。
_EXCLUDE_ID_KEY = "_invariant_exclude_id"

#: "没传"的哨兵：`CumulativeWithin.new_row_counted` 用它把"忘传"与"显式传 False"区分开。
#: 不能用 `None`（字段类型是 bool），也不能用 `False` 当默认（那等于默认选了登记类）。
_UNSET_BOOL: Any = object()


def _is_self_match(payload: dict[str, Any], row: dict[str, Any]) -> bool:
    """这一行是不是"本次写入产生的那一行"。

    取不到 `_invariant_exclude_id` 时返回 False（保持"不排除任何行"的原语义）——
    **降级方向必须是"更严格"**：退化时可能产生误报（如上面那个缺陷），
    但绝不会放过真实的重复。
    """
    exclude_id = payload.get(_EXCLUDE_ID_KEY)
    if exclude_id is None:
        return False
    return _same(exclude_id, _row_value(row, "id"))


def _resolve_table(payload: dict[str, Any]) -> str:
    """解析"当前能力写的是哪张表"。

    三级回退：不变量自己声明的 `table` → 执行器注入的 `_resource_table` →
    用 `_resource` 去 `RESOURCES` 里查 `Resource.table`。
    第三级让"忘了在能力声明里传表名"变成"仍然能正确解析"，而不是静默跳过校验。
    """
    table = str(payload.get("_resource_table") or "")
    if table:
        return table
    resource_name = str(payload.get("_resource") or "")
    if not resource_name:
        return ""
    resource = RESOURCES.find(resource_name)
    return resource.table if resource is not None else ""


def _same(a: Any, b: Any) -> bool:
    """比较两个标识；能转整数时按整数比，否则按字符串比。"""
    if a is None or b is None:
        return False
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _as_int(value: Any, what: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, f"{what}必须是整数", data={"field": what}
        ) from exc


def _as_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _build_where(header: dict[str, Any]) -> tuple[str, list[Any]]:
    """把"分组键 -> 值"渲染成 ``col=%s AND col=%s`` 与参数列表。

    列名全部来自能力声明（代码字面量），不接受用户输入 —— 与 `scope._safe_alias`
    同一口径；值一律走占位符绑定。
    """
    fragment = " AND ".join(f"{column}=%s" for column in header)
    return fragment, list(header.values())


def _require_writable(*, what: str, field_name: str, value: Any) -> None:
    """字段类校验的统一拒绝形态（与 `_require` 同一文案口径）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, f"{what}不能为空", data={"field": field_name}
        )


def _workflow_for(
    machine: str | Workflow,
    field_name: str | None,
    payload: dict[str, Any],
) -> tuple[Workflow, str]:
    """在 `RESOURCES` 里解析状态机（`machine` 取注册的资源名，如 `pond`/`batch`）。

    `field_name` 省略时取 ``status``（记录生命周期）；传 ``pond_status`` /
    ``batch_status`` 这类业务状态字段的名字即可校验业务状态机的转移 ——
    双状态资源（pond / batch）正是需要这个参数的原因。

    `machine` 取 ``"*"`` 时表示"用**当前能力声明的资源**的状态机"：调用方不必把资源名
    再抄一遍（抄一遍就是"两处描述同一件事" —— 改了 `Capability.resource` 而忘了改
    不变量声明时，规则会静默失效）。执行器通过 ``payload["_resource"]`` 提供资源名。
    """
    if isinstance(machine, Workflow):
        # 直接传实例（给"没有列表页、不值得注册 Resource"的资源用）。
        return machine, field_name or "status"
    if machine == "*":
        machine = str(payload.get("_resource") or "")
        if not machine:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "StateTransition(machine='*') 需要执行器提供当前资源的名字",
            )
    resource = RESOURCES.find(machine)
    if resource is None or resource.workflow is None:
        raise DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"内核状态机 {machine} 未注册，无法校验状态转移",
            data={"available": [item.name for item in RESOURCES.all()]},
        )
    return resource.workflow, field_name or "status"


# ---------------------------------------------------------------------------
# StateTransition —— 状态转移合法（registry §4 #14、#20）
#
# 早期版本同一件事有**三处**独立定义：DB ENUM、`common/governance/lifecycle.py:48-61`
# 的 POLICIES、以及各域常量（`production_service.py:15`、`master_data_service.py:21-24`），
# 三处互不一致。新系统的状态机只有一份（`kernel/workflow.py`，资源注册时登记），
# 本不变量只是把它接到执行器上。
#
# `machine` 是资源名（不是表名）：查的是 `workflow.RESOURCES` 里注册的那台状态机。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateTransition:
    """状态必须沿声明的转移表变化。

    ## 两种触发形态（第二种是**纯增量**，见 `from_context_fields`）

    **形态一（字段触发，既有行为，逐字未改）**：本次请求**提交了状态字段**
    （``field_name``）→ 从 payload 取目标、从 ``before`` 快照取当前值。
    没提交状态字段的能力（例如 `pond.update` 只改名称）直接跳过 —— 否则规则会变成
    "每一步都要显式声明状态"，而早期版本的 `require_transition` 只在真有状态变更时调用。
    这与「无变更即放行」是同一口径：``from == to`` 不构成转移。

    **形态二（上下文触发，新增）**：迁移的"从哪个状态到哪个状态"**只有服务端知道**
    （例如 `pond_status_change.verify`：当前状态在**塘口行**上，目标状态在**申请行**的
    `to_status` 列上，两个值都不在请求体里、也不在同一个快照里）。此时声明
    ``from_context_fields={"from_field": ..., "to_field": ...}``，规则改从
    `_invariant_context`（服务回传、执行器平铺进 payload）取这两个值。
    两个值都取到才判定；**只取到一个**说明装配缺一半 → `INTERNAL_ERROR`
    （按 R2：这是"内核能不能把规则跑起来"的基础设施缺失，不是业务事实缺失）。

    为什么必须能声明而不是让服务自己判：不声明的话，"状态机在服务里、不变量里没有"
    这句话**在声明处看不出来** —— 那正是本项目要消灭的"声明了却不生效"。
    """

    #: 状态机：**资源名**（`RESOURCES` 里注册的，如 `"pond"` / `"batch"`）、
    #: `"*"`（用当前能力声明的资源）、或**直接给一个 `Workflow` 实例**。
    #:
    #: 为什么允许传实例：`inventory_lot` 这类实体没有列表页，为了让它能被
    #: `machine="inventory_lot"` 引用而硬注册一个 `Resource`，会逼出一堆没有意义的
    #: `list_path` / `columns` —— 那是为了迁就实现而伪造声明。传实例让"声明式"完整，
    #: 又不需要假的资源声明。
    machine: str | Workflow
    field_name: str = "status"
    #: 拒绝时给用户的动作名（默认取机器的资源名）。
    label: str = ""
    #: **形态二**的取值路径：`{"from_field": "<context 键>", "to_field": "<context 键>"}`。
    #: 两个键都从 `_invariant_context` 取。`None`（默认）= 走形态一，行为逐字不变。
    from_context_fields: dict[str, str] | None = None

    name = "StateTransition"

    def __post_init__(self) -> None:
        if self.from_context_fields is not None:
            missing = {"from_field", "to_field"} - set(self.from_context_fields)
            if missing:
                raise ValueError(
                    "StateTransition.from_context_fields 必须同时给出 from_field 与 to_field，"
                    f"缺少 {sorted(missing)}"
                )

    def _action_name(self) -> str:
        """拒绝时给用户看的动作名：`label` 优先，否则取状态机所属资源名。"""
        if self.label:
            return self.label
        return self.machine if isinstance(self.machine, str) else self.machine.resource

    def _require_transition(
        self,
        source: str,
        destination: str,
        *,
        payload: dict[str, Any],
        field_name: str,
    ) -> None:
        """判定 + 报错整形。**两种形态共用这一份**。

        为什么不各写一遍：状态机的判定与错误 data 的整形只该有一处实现 ——
        否则"同一件事两处实现"会以"两种形态"的形式复活。
        """
        workflow, resolved_field = _workflow_for(self.machine, field_name, payload)
        try:
            workflow.require_transition(source, destination, action=self._action_name())
        except DomainError as error:
            # 统一附上规则名：前端与 Agent 都按 `rule` 做文案复用。
            data = dict(error.data or {})
            data.setdefault("rule", "STATE_TRANSITION")
            data.setdefault(
                "machine",
                self.machine if isinstance(self.machine, str) else self.machine.resource,
            )
            data.setdefault("field", resolved_field)
            raise DomainError(
                error.code,
                error.message,
                status=error.status,
                data=data,
            ) from error

    def _check_from_context(self, *, payload: dict[str, Any]) -> None:
        """形态二的取值与判定（形态一完全不经过这里）。"""
        assert self.from_context_fields is not None  # 调用点已保证
        nested = payload.get("_invariant_context")
        context = nested if isinstance(nested, dict) else {}
        from_field = self.from_context_fields["from_field"]
        to_field = self.from_context_fields["to_field"]
        # 两个来源都认：执行器平铺进 payload 的、以及嵌套的 `_invariant_context`
        source_value = _value_from((payload, context), from_field)
        target_value = _value_from((payload, context), to_field)

        if source_value is None or target_value is None:
            missing = [
                name
                for name, value in ((from_field, source_value), (to_field, target_value))
                if value is None
            ]
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "状态转移无法执行：声明了从上下文取迁移两端，但缺少 "
                f"{missing}。请检查服务是否经 `_invariant_context` 回传了这两个值"
                "（半个迁移判定比不判更危险：它会给出错误的拒绝或放行）",
                data={
                    "rule": "STATE_TRANSITION",
                    "missing": missing,
                    "resource": str(payload.get("_resource") or ""),
                },
            )

        source = str(source_value)
        destination = str(target_value)
        if source == destination:
            return  # 无变更即放行（与形态一同一口径）
        self._require_transition(
            source,
            destination,
            payload=payload,
            field_name=self.field_name,
        )

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        # ---- 形态二：迁移两端由服务经 `_invariant_context` 提供 ----
        if self.from_context_fields is not None:
            self._check_from_context(payload=payload)
            return

        # ---- 形态一：字段触发（既有行为，逐字保持）----
        if self.field_name not in payload:
            # 本次请求没有要求改状态 —— 不是这条规则管的事，交给其他不变量与状态机动作校验。
            return
        target = _value_from((payload,), self.field_name)
        if target is None:
            return  # 交了字段名但没值：等同于没提交，不是本规则能判定的场景
        current = _value_from((before,), self.field_name)
        if current is None:
            # ★ 本次**交了状态字段**（说明规则适用于本次操作），但拿不到"从哪个状态来"。
            #   要看两件事：
            #     * `before is None` 且**没有** `_resource_id` → 这是**新建**
            #       （服务端 insert 出来的行没有"前一个状态"），跳过是正确的；
            #     * `before is None` **却有** `_resource_id` → 执行器知道是哪一行、却读不到
            #       它的旧值 → 只能是**该能力没有回读函数**（t13/t15 修的那类装配缺口）。
            #       静默跳过 = 状态转移校验在这条能力上不生效，而声明处看起来挂上了。
            #       按三分法：基础设施缺失 → 报错。
            record_id = _value_from((payload, "_invariant_context"), "_resource_id")
            if before is None and record_id is not None:
                raise DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    f"状态转移无法执行：拿不到「{self.field_name}」的当前值。"
                    "这通常说明该能力没有提供回读函数（__fpa_load_by_id__），"
                    "执行器因此读不到改动前的行",
                    data={
                        "rule": "STATE_TRANSITION",
                        "field": self.field_name,
                        "resource": str(payload.get("_resource") or ""),
                    },
                )
            return  # 有 before 或能定位到行，但没有该字段值：不是本规则能判定的场景

        source = str(current)
        destination = str(target)
        if source == destination:
            return
        self._require_transition(
            source,
            destination,
            payload=payload,
            field_name=self.field_name,
        )



# ---------------------------------------------------------------------------
# StatusAllowsEdit —— 核验后记录只读（registry §4 #12）
#
# 早期实现：`common/governance/lifecycle.py:76-78` 的 `require_editable()`，
# 策略表在 `:48-61`。早期版本把它写在每个 store 的 update 里，只有一个域记得调。
# 新系统把它声明在能力上，与状态机共用同一份状态表。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatusAllowsEdit:
    """行状态必须在可编辑集合内，否则视为只读。

    幂等形态：`before` 为 None（新建/无前置快照）时不判定 —— 没有"核验过的记录"
    可供拒绝。
    """

    statuses: tuple[str, ...] = ("draft", "submitted")
    status_field: str = "status"
    label: str = "记录"

    name = "StatusAllowsEdit"

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        current = _value_from((before,), self.status_field)
        if current is None:
            return
        code = str(current)
        if code in self.statuses:
            return
        raise DomainError(
            ErrorCode.CONFLICT,
            f"该{self.label}已核验，不能修改",
            data={
                "rule": "RECORD_READ_ONLY",
                "status": code,
                "editable": list(self.statuses),
            },
        )


# ---------------------------------------------------------------------------
# OptimisticLock —— 乐观锁（registry §4 #13）
#
# 早期版本靠 76 处 `UPDATE ... WHERE row_version=%s` + `rowcount != 1` 判定
# （`production_store.py:167-169`、`lifecycle.py:71-73`）。新系统仍然只做一处 UPDATE
# （仓储的职责），但"版本对不对"的判定改成声明式：执行器在写库**之前**先比一次，
# 于是并发冲突的文案与错误码不再由 14 个 store 各写一遍。
#
# 表名与主键由执行器通过 `payload["_invariant_context"]` 注入（见 `runner.py`
# 的 `_invariant_extra`），因为内核的 `Capability` 只声明资源名、不声明表名。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptimisticLock:
    """行版本必须与请求携带的期望版本一致。

    ``expect`` 是**入参字段名**（`expected_version`），``column`` 是**表列名**
    （`row_version`）。两者刻意分开：入参名是前端契约（不许改），列名是数据库契约。
    """

    column: str = "row_version"
    expect: str = "expected_version"
    label: str = "该记录"

    name = "OptimisticLock"

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        expected = _value_from((payload,), self.expect)
        if expected is None:
            # 没有携带期望版本：由能力字段声明的 required 去拒绝，这里不重复报错。
            return

        # `before` 优先：执行器已经读过这一行，不必再查一次库。
        current = _value_from((before,), self.column)
        if current is None:
            table = _resolve_table(payload)
            record_id = _value_from((payload, "_invariant_context"), "_resource_id")
            if not table or record_id is None:
                # ★ 基础设施缺失 → 报错（不是跳过）。
                # 本次**确实提交了期望版本**（说明这条规则适用于本次操作），但内核拿不到
                # "去哪一行比版本"的定位信息：能力声明了 `OptimisticLock()`，却没有给
                # `Resource.table`、也没有路径参数/`resource_id`。此时静默跳过 = 乐观锁
                # **在这条能力上根本没生效**，而调用方以为生效了 —— 与
                # `fields=` 忘传、`_ledger_lines` 忘传是同一个失败形态。
                raise DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    "乐观锁无法执行：解析不出记录所在表或记录主键，请检查该能力的 "
                    "`Resource.table` 声明与路径参数（{pond_id} 之类）",
                    data={
                        "rule": "OPTIMISTIC_LOCK",
                        "column": self.column,
                        "resource": str(payload.get("_resource") or ""),
                        "resource_table": table,
                        "resource_id": record_id,
                    },
                )
            key_column = str(payload.get("_resource_key_column") or "id")
            row = tx.query_one(
                f"SELECT {self.column} AS _version FROM {table} WHERE {key_column}=%s",
                (record_id,),
            )
            current = _row_value(row or {}, "_version", self.column)
        if current is None:
            # ★ 定位信息齐全、却读不到版本值 → 结构/数据不一致（声明了 `row_version`，
            #   那一行却是 NULL，或列名拼错）。同样报错，不要静默放行。
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"乐观锁无法执行：读不到记录的行版本（列 {self.column}）",
                data={"rule": "OPTIMISTIC_LOCK", "column": self.column},
            )

        expected_int = _as_int(expected, "乐观锁版本")
        if _as_int(current, "乐观锁版本") == expected_int:
            return
        raise DomainError(
            ErrorCode.VERSION_CONFLICT,
            f"{self.label}已被他人修改，请刷新后重试",
            data={
                "rule": "OPTIMISTIC_LOCK",
                "current_version": _as_int(current, "乐观锁版本"),
            },
        )

# ---------------------------------------------------------------------------
# ReferencedStatus —— 引用对象的业务状态必须允许本次操作（registry §4 #3、#18）
#
# 早期实现散在两处：`purchase_posting.py:19-20`（未审批不得收货）与
# `warehouse_ledger_store.py:184-193`（核验时再查仓库 active + 物料 verified）。
# 同一件事在创建时和核验时各写一遍，且只有采购域写了。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReferencedStatus:
    """被引用的那一行必须存在、在数据范围内、且状态属于允许集合。

    ``statuses`` 是**允许的状态集**（不是排除集）：声明的人必须写出"什么状态可以"，
    这比"什么状态不可以"更难写错 —— 后者在枚举新增一个状态时会静默放行。
    """

    field: str
    table: str
    statuses: tuple[str, ...]
    #: 表里的状态列名（本项目的状态列统一叫 `status`）。
    status_field: str = "status"
    label: str = "引用对象"
    #: 引用字段缺失时是否拒绝。默认不拒绝（引用字段本身可选，如 `receipt.create` 的采购单）。
    required: bool = False

    name = "ReferencedStatus"

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        reference = _value_from((payload, before), self.field)
        if reference is None:
            if self.required:
                _require_writable(what=self.label, field_name=self.field, value=None)
            return

        reference_id = _as_int(reference, self.label)
        row = tx.query_one(
            f"SELECT * FROM {self.table} WHERE id=%s",
            (reference_id,),
        )
        if row is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"{self.label}不存在或已被删除",
                data={"field": self.field, "rule": "REFERENCED_STATUS"},
            )
        if not scope.allows_row(row):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED,
                f"{self.label}不在当前账号的数据范围内",
                data={"field": self.field, "rule": "REFERENCED_STATUS"},
            )

        status = _row_value(row, self.status_field)
        if status is None:
            # 该表没有状态列：无从判定，不假装校验过。
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"{self.table} 没有 {self.status_field} 列，无法校验{self.label}状态",
            )
        if str(status) in self.statuses:
            return
        raise DomainError(
            ErrorCode.CONFLICT,
            f"{self.label}当前状态为「{status}」，不允许本次操作",
            data={
                "field": self.field,
                "rule": "REFERENCED_STATUS",
                "status": str(status),
                "allowed": list(self.statuses),
            },
        )


# ---------------------------------------------------------------------------
# SameTenant —— 物料/仓库/批次/塘口必须同企业且有效（registry §4 #18）
#
# 早期实现：`warehouse_store.py:73-97` 的 `_scoped()`，对四种引用各写一遍
# "回查 organization/farm/area + 状态"；域外的调用方（production / sales）
# 根本没有这层校验 —— 于是"跨塘口选批次"这类错配只靠前端下拉避免。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SameTenant:
    """若干引用对象必须属于同一个企业/基地/区域。

    每个引用字段通过 ``tables`` 映射到它所在的表（``{"material_id": "materials"}``）；
    映射里缺失的字段**跳过不查** —— 让同一个不变量声明能覆盖"某些引用是可选的"
    场景（例如 `issue.create` 的 `pond_id`）。

    分租键固定为 org / farm / area 三列：表里没有的列自动忽略（本项目按 §0.7
    规则 1 给业务表补齐四级分租键，但不是每张表都有 area）。
    """

    fields: tuple[str, ...]
    tables: Mapping[str, str] | None = None
    scope_fields: tuple[str, ...] = ("organization_id", "farm_id", "area_id")
    labels: tuple[str, ...] = ()
    required: bool = False

    name = "SameTenant"

    def __post_init__(self) -> None:
        if self.tables is None:
            # frozen dataclass 里用 None 当哨兵，构造期换成空映射。
            object.__setattr__(self, "tables", {})
        if self.labels and len(self.labels) != len(self.fields):
            raise ValueError("SameTenant 的 labels 与 fields 数量必须一致")

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        anchors: dict[str, Any] = {}
        anchor_field = ""
        for index, field_name in enumerate(self.fields):
            label = self.labels[index] if self.labels else field_name
            table = (self.tables or {}).get(field_name)
            if not table:
                continue  # 未声明表的引用字段：本不变量不负责（存在性由服务或外键管）

            reference = _value_from((payload, before), field_name)
            if reference is None:
                if self.required:
                    _require_writable(what=label, field_name=field_name, value=None)
                continue

            row = tx.query_one(
                f"SELECT * FROM {table} WHERE id=%s",
                (_as_int(reference, label),),
            )
            if row is None:
                raise DomainError(
                    ErrorCode.FIELD_INVALID,
                    f"{label}不存在或已被删除",
                    data={"field": field_name, "rule": "SAME_TENANT"},
                )
            if not scope.allows_row(row):
                raise DomainError(
                    ErrorCode.DATA_SCOPE_DENIED,
                    f"{label}不在当前账号的数据范围内",
                    data={"field": field_name, "rule": "SAME_TENANT"},
                )

            for key in self.scope_fields:
                value = _row_value(row, key)
                if value is None:
                    continue
                if anchor_field and key in anchors and not _same(anchors[key], value):
                    raise DomainError(
                        ErrorCode.CONFLICT,
                        f"{label}与{anchor_field}不属于同一个企业或基地",
                        data={
                            "rule": "SAME_TENANT",
                            "field": field_name,
                            "conflict": key,
                        },
                    )
                anchors.setdefault(key, value)
            if not anchor_field:
                anchor_field = label


# ---------------------------------------------------------------------------
# UniqueCode —— 编码在范围内唯一（registry §4 #19）
#
# 早期版本把 DB 唯一键的报错靠**异常消息文本**翻译成业务错误
# （`production_store.py:142-146`、`warehouse_store.py:138,155` 的 `if key not in str(exc)`），
# 脆弱且随 MySQL 版本变化。新系统的分工是：
#     * **正确性底线**：DB 唯一键（并发下唯一可信的判定）；
#     * **可读 409**：本不变量 —— 写库前查一次，给出"编码已存在"的具体文案。
# 两者都要有，与本项目对 #21 的口径一致。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UniqueCode:
    """``fields`` 的组合在 ``scope`` 声明的范围内必须唯一。

    范围内若取不到分租值（新建时服务端还没补全 `organization_id`），**拒绝**而不是
    退化成全表查 —— 静默放宽唯一性比报错危险得多。
    """

    fields: tuple[str, ...] = ("code",)
    scope: tuple[str, ...] = ()
    table: str = ""
    labels: tuple[str, ...] = ()
    #: 主键列名：自排除（"除了我以外还有没有同编码"）用它。
    key_column: str = "id"

    name = "UniqueCode"

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        table = self.table or _resolve_table(payload)
        if not table:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "UniqueCode 未声明表名，执行器也没有提供当前资源的表名",
                data={"resource": str(payload.get("_resource") or "")},
            )

        identity: dict[str, Any] = {}
        for index, field_name in enumerate(self.fields):
            label = self.labels[index] if self.labels else field_name
            value = _value_from((payload, before), field_name)
            if value is None:
                return  # 编码本身没提交（更新场景）：没有可查的东西
            identity[field_name] = value

        scoped: dict[str, Any] = {}
        for column in self.scope:
            value = _value_from((payload, before), column)
            if value is None:
                raise DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    f"编码唯一性校验缺少范围列 {column}：新建时服务端必须先补全分租键，"
                    "否则唯一性会被静默放宽到全表",
                )
            scoped[column] = value

        where, params = _build_where({**scoped, **identity})
        # ★ 与 `NoOverlappingSource` / `AtMostOnePending` 同一处理：排除**下推到 SQL**。
        #   本不变量在**写入之后**执行，所以新建场景下必然查到
        #   自己刚插入的那一行（"新建第一张采购单 -> 单号已存在"）。
        #   下推之后语义是字面的"除了我以外还有没有同编码的行"，
        #   不再依赖 `LIMIT 1` 取到的是哪一行；下面的 `_is_self_match` 只作兜底。
        exclude_id = payload.get(_EXCLUDE_ID_KEY)
        exclude_sql = ""
        if exclude_id is not None:
            exclude_sql = f" AND {self.key_column} <> %s"
            params.append(exclude_id)
        row = tx.query_one(
            f"SELECT id FROM {table} WHERE {where}{exclude_sql} LIMIT 1", params
        )
        if row is None:
            return
        # 兜底：与 `AtMostOnePending` / `NoOverlappingSource` 共用同一个判定函数。
        # 它覆盖两种"查到的就是我自己"的场景：
        #   * `before` 非空、更新时值没变（下面那行 `current` 判定管这个）；
        #   * `before` 为空、新建时查到自己（本行管这个）—— 旧形态只判了前者，
        #     于是"新建第一张采购单 → 单号已存在"，任何挂 `UniqueCode` 的 create
        #     都会必然失败。
        if _is_self_match(payload, row):
            return
        existing = _row_value(row, "id")
        current = _value_from((before,), "id")
        if current is not None and _same(existing, current):
            return  # 就是自己（更新时值没变）
        label = self.labels[0] if self.labels else self.fields[0]
        raise DomainError(
            ErrorCode.CONFLICT,
            f"{label}已存在，请换一个",
            data={
                "rule": "UNIQUE_CODE",
                "field": self.fields[0],
                "existing_id": existing,
            },
        )


# ---------------------------------------------------------------------------
# CumulativeWithin —— 累计到货/交付不得超过单据数量（registry §4 #9、#10）
#
# 早期实现：`purchase_posting.py:25-34`（`received > ordered`）与
# `sales_posting.py:35-37`（`delivered < 0 or delivered > ordered`），
# 两处各写一条 SUM 查询、各自维护"哪些状态算已发生"。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CumulativeWithin:
    """子行的累计数量不得超过目标数量。

    ``target_dims`` 是"子行用什么列指向目标行"（到货单里是 `purchase_order_id`），
    ``target_field`` 是目标数量列名，``count_field`` 是子行数量列名。

    ``statuses`` 只放**算数**的状态（默认 `verified`）。用排除集（`exclude_statuses`）
    很容易漏掉新加的终止态，于是作废单据也被算进累计 —— 必须写"什么算"，不能写
    "什么不算"。

    ## 判定（R1 Amendment 的字面，**必填参数**）

    ``new_row_counted: bool``（**无默认值**，不传 → 构造期 `ValueError`）：

    * ``True`` —— **核验类**（`delivery.verify` / `receipt.verify`）：服务已把本次行置成
      `statuses` 里的状态、**它已在库内合计里** → **只判 `counted`**（该分支 SQL **不**排除本次行）；
    * ``False`` —— **登记类**（`delivery.create` / `receipt.create`）：本次行还是 `draft`、
      **不在**合计里 → **判 `counted + requested`**（该分支带 `id <> %s`，对它恒等，
      作为"服务把本次行写成算数状态却声明 False"这类误用的兜底）。

    两个分支算出来的是**同一个业务量**：**本次操作生效之后的累计**。没有这层区分时，
    核验路径会把本次那一行算两次 → **误拒合法操作**（实测：100 上限、已核验 60 再核验 40，
    库内合计已是 100，再 `+40` 判成 140 超限）。

    ## ⚠ 自排除成立所依赖的假设（**没有任何地方强制它，违反会静默出错**）

       被计数的子行必须恰好是"一单一行的单据"本身，且 `_invariant_exclude_id` 等于该行主键。

    也就是说：`count_field` 所在的表是**单据表**（一行就是这次业务事实的全部贡献），
    `_invariant_exclude_id`（执行器注入的"本次记录主键"）与那一行是同一个 id。
    本项目当前四条能力（`receipt.create/verify`、`delivery.create/verify`）都满足它 ——
    `children_table` 是单据表、`_invariant_exclude_id` 就是该单据主键。

    **违反它会怎样：静默的双重计数（不报错）。** 若某域改成"按**明细行**计数"
    （例如到货单的每一行明细各算一次），`id <> X` 只能排掉**单头那一行**、排不掉它名下的
    明细行，于是本次业务事实被算两遍 —— 结果是**误拒**或（配合其它列时）**多算**，
    而**不会有任何异常**。

    **将来的域若真要按明细行计数，必须二选一**（两条都在本项目的能力范围内）：
    1. 用 `_invariant_exclude_id` 之外的方式显式传入"本次要排除的行集合"（由服务回传，
       不变量按集合排除）；或
    2. 改回"必填参数 + 由服务保证 `counted` 的语义"，并把这条假设从声明处去掉。
    **不要继续用 `id <> X` 冒充"排除了本次贡献"** —— 那正是这条假设会被悄悄违反的路径。

    ## 其它

    ``for_update`` **默认开**：并发两笔会同时读到旧累计而双双通过。

    缺少 `_invariant_exclude_id`（执行器无法定位本次那一行）时**显式报错**：
    那说明基础设施缺失，继续算下去只会得到一个错的累计。

    **两侧不做单位换算**：`counted` 是子行原始值的和，`requested` 也取原始值 ——
    只给一侧换算会让 `unit='jin'` 时量纲不一致（`counted=40` + `requested=80` = 120 > 100，
    误拒合法交付）。**单位换算属于 §4 #6 `HarvestQuantityMatch`**（出塘事实 vs 交付数量）；
    本规则（§4 #10）只回答"同一列的两部分加起来超没超限额"。若某条链路两侧量纲确实
    不同，**在服务里先统一**。
    """

    target_table: str
    target_dims: tuple[str, ...]
    children_table: str
    #: 目标表里与 ``target_dims`` 一一对应的列名；省略时认为与 ``target_dims`` 同名。
    #: 需要它的原因：子行用 `sales_order_id` 指目标，目标表的主键列却叫 `id`。
    target_columns: tuple[str, ...] = ()
    count_field: str = "quantity"
    target_field: str = "quantity"
    #: 子行自己的数量列（"本次这一行算多少"）。与 ``count_field`` 分开声明是因为
    #: 到货单表里的列叫 `quantity`、目标列也叫 `quantity`，但那是两件事。
    request_field: str = "quantity"
    #: 单位列名：**保留但不再参与算术**（负责人 终裁：本规则两侧都不换算）。
    #: 声明处仍可写它，方便读声明的人知道这条链路上有"单位"这回事。
    unit_field: str = "unit"
    statuses: tuple[str, ...] = ("verified",)
    #: **必填、无默认值**（不传 → 构造期 `ValueError`）：本次这一行**是否已被算进 `counted`**。
    #:
    #: * `True` —— **核验类**（`delivery.verify` / `receipt.verify`）：服务已把本次行置成
    #:   `statuses` 里的状态，它**在** `counted` 里 → 只判 `counted`。
    #: * `False` —— **登记类**（`delivery.create` / `receipt.create`）：本次行还是 `draft`，
    #:   它**不在** `counted` 里 → 判 `counted + requested`。
    #:
    #: 为什么不自动探测：探测意味着再查一次库、把同一件事算两遍，两边必然存在不一致的时刻。
    #: 为什么必填而不给默认：声明者在写那段服务时**本来就知道**自己是哪一类（create 写 `draft`、
    #: verify 写 `verified`）；给默认值等于把"两类语义相反"变成一次可以忘掉的默认选择。
    new_row_counted: bool = _UNSET_BOOL
    #: 是否给累计查询加行锁（**默认开**）。并发下两个请求会同时读到旧的累计而双双通过
    #: —— 那正是"判定写在 UPDATE 之后"也拦不住的那种情况。只有明确知道自己在做什么时
    #: 才关掉它（例如只读报表）。
    for_update: bool = True
    #: 子行主键列名：自排除（"除了本次这一行以外"）用它。
    key_column: str = "id"
    label: str = "累计数量"

    name = "CumulativeWithin"

    def __post_init__(self) -> None:
        if self.target_columns and len(self.target_columns) != len(self.target_dims):
            raise ValueError("CumulativeWithin 的 target_columns 与 target_dims 数量必须一致")
        if self.new_row_counted is _UNSET_BOOL:
            # 刻意在**构造期**报错：忘选 = 声明处就挡住，而不是运行期跳过或猜。
            raise ValueError(
                "CumulativeWithin 必须显式声明 new_row_counted（不给默认值）："
                "核验类传 True（本次行已被置成算数状态、已在 counted 里），"
                "登记类传 False（本次行还是 draft、不在 counted 里）。"
                "忘记选会让两类语义相反的路径共用一种判定。"
            )
        if not isinstance(self.new_row_counted, bool):
            raise ValueError("new_row_counted 必须是 True/False")

    def _requested_amount(self, *, payload: dict[str, Any], before: dict[str, Any] | None) -> Decimal:
        """本次请求自己带来的数量，**不做任何单位换算**。取不到时视为 0。

        为什么不做换算：本规则比的是"同一列的两部分相加"（本次这一行 + 其余已在
        `counted` 里的行），所以两侧必须**同量纲**。而 `counted = SUM(count_field)`
        取的是原始值 —— 只给 `requested` 一侧换算会让 `unit='jin'` 时量纲不一致
        （`counted=40` + `requested=80` = 120 > 100，**误拒合法交付**）。

        **单位换算属于 §4 #6 `HarvestQuantityMatch`**（出塘事实 vs 交付数量），不属于
        §4 #10（同一列的两部分相加）—— 把 #6 的换算照搬进来是把一条规则的知识泄漏进
        另一条。若某条链路两侧量纲确实不同，请**在服务里先统一**（或改成同一量纲的列）。
        """
        value = _value_from((payload, before), self.request_field)
        if value is None:
            return Decimal("0")
        return _as_decimal(value)

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        values: dict[str, Any] = {}
        for column in self.target_dims:
            value = _value_from((payload, before), column, f"_{column}")
            if value is None:
                return  # 未关联目标单据（如无采购单的到货）：无累计可言
            values[column] = _as_int(value, column)

        target_columns = self.target_columns or self.target_dims
        target_where, target_params = _build_where(
            {column: value for column, value in zip(target_columns, values.values(), strict=True)}
        )
        target = tx.query_one(
            f"SELECT * FROM {self.target_table} WHERE {target_where} "
            f"ORDER BY id DESC LIMIT 1",
            target_params,
        )
        if target is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                f"关联的{self.label}来源单据不存在",
                data={"rule": "CUMULATIVE_WITHIN"},
            )

        target_amount = _as_decimal(_row_value(target, self.target_field) or 0)
        children_where, children_params = _build_where(values)
        lock = " FOR UPDATE" if self.for_update else ""
        # ★ 自排除下推到 SQL："除了本次这一行以外，已经算数的有多少"。
        #
        # 两条路径同一套式子（见类 docstring 的表格）。缺 `_invariant_exclude_id` 时
        # 报错而不是继续算 —— 那意味着执行器定位不到本次那一行，累计必然会算错
        # （核验路径会当场双重计数，正是修前的缺陷）。
        exclude_id = payload.get(_EXCLUDE_ID_KEY)
        if exclude_id is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"{self.label}无法执行：执行器没有提供本次记录的主键"
                f"（{_EXCLUDE_ID_KEY}），无法按 R1 的口径判定本次这一行是否已计入",
                data={
                    "rule": "CUMULATIVE_WITHIN",
                    "children_table": self.children_table,
                },
            )
        # 登记类（`new_row_counted=False`）额外排掉本次行：它**恒等**（那行是 draft、
        # 不在合计里），同时给"服务把本行写成算数状态却声明 False"这种误用兜底。
        # 核验类（True）**不排除** —— `counted` 要如实含本次那一行，Amendment 的口径是
        # "只判 counted"。
        exclude_sql = "" if self.new_row_counted else f" AND {self.key_column} <> %s"
        counted_params = [*children_params, *self.statuses]
        if not self.new_row_counted:
            counted_params.append(exclude_id)
        counted = tx.query_one(
            f"SELECT COALESCE(SUM({self.count_field}),0) AS total "
            f"FROM {self.children_table} "
            f"WHERE {children_where} AND status IN "
            f"({', '.join(['%s'] * len(self.statuses))}){exclude_sql}{lock}",
            counted_params,
        )
        counted_amount = _as_decimal(_row_value(counted or {}, "total") or 0)
        requested_amount = self._requested_amount(payload=payload, before=before)
        # ★ R1 Amendment 的字面口径：
        #   * `new_row_counted=True`（核验类）：本次行已在 `counted` 里 → **只判 `counted`**；
        #   * `new_row_counted=False`（登记类）：本次行还是 `draft` → **判 `counted + requested`**。
        # 两者算出来的是同一个业务量：**本次操作生效之后的累计**。
        projected = counted_amount if self.new_row_counted else counted_amount + requested_amount

        if projected > target_amount:
            raise DomainError(
                ErrorCode.CONFLICT,
                f"{self.label}不能超过来源单据的 {target_amount}："
                f"库内已算数 {counted_amount}"
                + (f"，本次 {requested_amount}" if not self.new_row_counted else "")
                + f"，生效后合计 {projected}",
                data={
                    "rule": "CUMULATIVE_WITHIN",
                    "target": str(target_amount),
                    # `counted` 是**库内实际算数的合计**：核验类（new_row_counted=True）
                    # 时它含本次那一行；登记类时它不含（本次还是 draft）。
                    "counted": str(counted_amount),
                    "requested": str(requested_amount),
                    "projected": str(projected),
                    "new_row_counted": self.new_row_counted,
                },
            )


# ---------------------------------------------------------------------------
# ZeroBalance —— 批次关闭时存塘必须为 0（registry §4 #11）
#
# 早期实现：`production_store.py:228-235`：`SUM(quantity_delta) != 0 or
# SUM(weight_delta_kg) != 0 → BATCH_STOCK_NOT_ZERO`。
#
# 与 `NoNegativeStock` 的分工：那个看"本次增量会不会把余额减成负"（需要
# `_ledger_lines`），这个看"关账那一刻余额是不是零"（只读库，不需要增量行）。
# ---------------------------------------------------------------------------


#: 金额比较允许的绝对误差：Python float -> Decimal 的换算会引入尾差，
#: 而 `float(value) != 0` 这种判等在早期版本里造成过"明明归零却关不了账"。
_ZERO_EPSILON = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class ZeroBalance:
    """按 ``group_by`` 分组的账本余额必须为零。

    与早期实现的差别：早期实现把两个列的 SUM 写在一条 SQL 里、零值比较用 float；
    这里按列分别判并给出**是哪一个量不为零**的具体文案。
    """

    table: str
    group_by: tuple[str, ...] = ()
    columns: tuple[str, ...] = ("quantity_delta",)
    labels: tuple[str, ...] = ()
    label: str = ""

    name = "ZeroBalance"

    def __post_init__(self) -> None:
        if self.labels and len(self.labels) != len(self.columns):
            raise ValueError("ZeroBalance 的 labels 与 columns 数量必须一致")

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        header: dict[str, Any] = {}
        for column in self.group_by:
            value = _value_from((payload, before), column)
            if value is None:
                return  # 缺少分组键：不是本规则能判定的场景
            header[column] = _as_int(value, column)

        select = ", ".join(
            f"COALESCE(SUM({column}),0) AS {column}" for column in self.columns
        )
        where, params = _build_where(header)
        # 不加 FOR UPDATE：聚合查询下 MySQL 不会锁住不存在的行，锁的语义是假的。
        # 行锁由仓储负责（关账前的持仓行），本不变量只负责"断言余额是不是零"。
        row = tx.query_one(
            f"SELECT {select} FROM {self.table} WHERE {where}",
            params,
        )
        balances = {
            column: _as_decimal(_row_value(row or {}, column) or 0)
            for column in self.columns
        }
        for index, column in enumerate(self.columns):
            balance = balances[column]
            if balance > _ZERO_EPSILON or balance < -_ZERO_EPSILON:
                label = self.labels[index] if self.labels else column
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"{self.label or '账本'}仍有剩余{label}（{balance}），不能关闭",
                    data={
                        "rule": "ZERO_BALANCE",
                        "column": column,
                        "balance": str(balance),
                    },
                )

# ---------------------------------------------------------------------------
# AtLeastOneOf —— 至少填一个（registry §4 #16）
#
# 早期实现：`production_validation.py` 的 `require_stock_measurement()`，
# 在 `production_service.py` 四处被调用。缺一次调用就放行一条没有计量的投喂记录。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AtLeastOneOf:
    """``fields`` 里至少有一个非空。

    与 `RequiredField` 的差别：那个要求"每个都必填"，这个要求"至少一个"。
    典型场景：投喂量与重量至少填一个（两个都有也可以）。
    """

    fields: tuple[str, ...]
    labels: tuple[str, ...] = ()
    label: str = ""

    name = "AtLeastOneOf"

    def __post_init__(self) -> None:
        if not self.fields:
            raise ValueError("AtLeastOneOf 至少要声明一个字段")
        if self.labels and len(self.labels) != len(self.fields):
            raise ValueError("AtLeastOneOf 的 labels 与 fields 数量必须一致")

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        for field_name in self.fields:
            value = _value_from((payload, before), field_name)
            if value is not None:
                return
        names = "、".join(self.labels or self.fields)
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            self.label or f"{names}至少填写一个",
            data={"rule": "AT_LEAST_ONE_OF", "fields": list(self.fields)},
        )


# ---------------------------------------------------------------------------
# AmountWithin —— 付款/收款不得超过余额（registry §4 #4）
#
# 早期实现在同一条链路上校验了**两次**：`purchase_payment_store.py:114-115`（创建时）
# 与 `:154-157`（核验时），两处各写一遍 `amount > balance`。
# 新系统的裁决（registry §1.7）：只在 `payment.verify` 强制，`payment.create` 只做提示。
# 于是"强制点只有一处"，而不变量声明写在那一处。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AmountWithin:
    """``amount_field`` 不得超过被引用行的剩余余额。

    余额的算法显式声明（**不猜**）：
        余额 = ``balance_column``（默认 = ``target_column``，即应付/应收总额）
               − ``paid_column``（默认 ``paid_amount``，已付款/已收款）

    早期版本用的是 `effective_amount - paid_amount`（`purchase_payment_store.py:95`），
    其中 `effective_amount` 是冲销后的金额。本版没有冲销（§6 已砍），
    所以 `balance_column` 由声明方按自己的表结构给出。
    """

    amount_field: str
    table: str
    target_field: str
    target_column: str = "id"
    balance_column: str = ""
    paid_column: str = "paid_amount"
    status_field: str = "status"
    statuses: tuple[str, ...] = ()
    currency_column: str = "currency"
    currency: str = "CNY"
    label: str = "应付账款"

    name = "AmountWithin"

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        amount = _value_from((payload, before), self.amount_field)
        target_id = _value_from((payload, before), self.target_field)
        if amount is None or target_id is None:
            return  # 幂等：更新场景下这两个字段可能都不提交

        row = tx.query_one(
            f"SELECT * FROM {self.table} WHERE {self.target_column}=%s FOR UPDATE",
            (_as_int(target_id, self.target_field),),
        )
        if row is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                f"{self.label}不存在或已被删除",
                data={"field": self.target_field, "rule": "AMOUNT_WITHIN"},
            )
        if not scope.allows_row(row):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED,
                f"{self.label}不在当前账号的数据范围内",
                data={"field": self.target_field, "rule": "AMOUNT_WITHIN"},
            )

        # 写入类动作通常先推进引用账款，再运行不变量（执行器的统一顺序）。
        # 收付款服务会在更新前锁定并回传快照，避免最后一笔合法金额被更新后的
        # 余额 0 误判为超额；没有快照的登记场景仍读取当前行。
        snapshot = payload.get("_amount_within_before")
        if not isinstance(snapshot, dict):
            snapshot = None

        status = (snapshot or {}).get("status", _row_value(row, self.status_field))
        if self.statuses and status is not None and str(status) not in self.statuses:
            raise DomainError(
                ErrorCode.CONFLICT,
                f"{self.label}当前状态为「{status}」，不能再办理收付款",
                data={
                    "rule": "AMOUNT_WITHIN",
                    "status": str(status),
                    "allowed": list(self.statuses),
                },
            )

        balance_column = self.balance_column or self.target_column
        total = _as_decimal(
            (snapshot or {}).get("total", _row_value(row, balance_column)) or 0
        )
        paid = _as_decimal(
            (snapshot or {}).get("paid", _row_value(row, self.paid_column)) or 0
        )
        balance = total - paid
        request_currency = _value_from((payload,), self.currency_column)
        row_currency = (snapshot or {}).get("currency", _row_value(row, self.currency_column))
        if request_currency and row_currency and str(request_currency) != str(row_currency):
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                f"币种与{self.label}不一致，不能跨币种登记",
                data={"field": self.currency_column, "rule": "AMOUNT_WITHIN"},
            )

        requested = _as_decimal(amount)
        if requested > balance:
            raise DomainError(
                ErrorCode.CONFLICT,
                f"本次金额 {requested} 超过{self.label}余额 {balance}",
                data={
                    "rule": "AMOUNT_WITHIN",
                    "balance": str(balance),
                    "requested": str(requested),
                },
            )


# ---------------------------------------------------------------------------
# NoOverlappingSource —— 同一塘口/批次同期不得重复归集成本（registry §4 #8）
#
# 早期实现：`cost_enterprise_repository.py:117-150` 的 `require_source_not_duplicated()`：
# "手工费用"与"库存自动归集"对同一目标、同一期间重叠时会重复计入，
# 因此要先查一遍已确认的库存来源。早期实现有一条白名单
# （`manual_feed_offset` / `manual_feed_direct`）让财务显式选择"我知道会自动归集"。
# 本版保留这条白名单语义（``override_source_types``）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NoOverlappingSource:
    """同一归属对象、期间重叠、且来源类型在禁止集合内时，拒绝重复归集。

    ``source_types`` 是**禁止与本次并存的来源类型**（不是"本次的来源类型"）：
    本次提交手工费用时，要禁止的是"已存在库存自动归集"。语义写反会让规则
    永远通过，因此这里用参数名把它钉死。
    """

    table: str = "cost_entries"
    source_type_field: str = "source_type"
    source_types: tuple[str, ...] = ("warehouse_ledger",)
    target_fields: tuple[str, ...] = ("target_type", "target_id")
    period_fields: tuple[str, ...] = ("period_start", "period_end")
    #: 参与查重范围限定的分租键（企业 / 基地 / 区域）。
    #:
    #: **不允许为空**：分租键是"这条规则只在本企业内查重"的唯一保证。取不到值时
    #: 本不变量**显式报错**（见 `check()`），而不是把条件从 SQL 里去掉 ——
    #: 后者会让查重范围悄悄扩到其他企业（唯一一条"缺键后方向变宽"的规则）。
    tenant_keys: tuple[str, ...] = ("organization_id", "farm_id", "area_id")
    #: 这些来源类型表示"财务明确选择了覆盖"，直接放行（早期实现的白名单）。
    override_source_types: tuple[str, ...] = ()
    #: 主键列名：自排除（"除了我以外还有没有冲突行"）用它。
    key_column: str = "id"
    required: bool = False
    label: str = "该塘口/批次"

    name = "NoOverlappingSource"

    def __post_init__(self) -> None:
        if len(self.period_fields) != 2:
            raise ValueError("NoOverlappingSource 的 period_fields 必须是 (开始列, 结束列)")
        if not self.tenant_keys:
            # 留这个口子等于把"查重范围扩到其他企业"变成一次声明即可绕过。
            raise ValueError(
                "NoOverlappingSource 必须声明 tenant_keys（分租键）："
                "没有分租键就无法把查重限定在本企业内"
            )

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        source_type = _value_from((payload,), self.source_type_field)
        if source_type is not None and str(source_type) in self.override_source_types:
            return

        target: dict[str, Any] = {}
        for column in self.target_fields:
            value = _value_from((payload, before), column)
            if value is None:
                if self.required:
                    raise DomainError(
                        ErrorCode.VALIDATION_ERROR,
                        f"归集成本必须指定归属对象的{column}",
                        data={"field": column, "rule": "NO_OVERLAPPING_SOURCE"},
                    )
                return
            target[column] = value

        start_column, end_column = self.period_fields
        period_start = _value_from((payload, before), start_column)
        period_end = _value_from((payload, before), end_column)
        if period_start is None or period_end is None:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "期间的起止日期都必须填写",
                data={"field": start_column, "rule": "NO_OVERLAPPING_SOURCE"},
            )
        # 精确接口的备用取法：服务把期间解析结果放在 `_period_absolute` 里
        # （`{"2026-01-01": ("2026-01-01", "2026-01-31")}`），因为 `YYYY-MM` 这种
        # 输入没法在声明里算出精确的月末。
        absolute = payload.get("_period_absolute")
        if isinstance(absolute, dict):
            resolved = absolute.get(str(period_start)) or absolute.get(f"{period_start}..{period_end}")
            if isinstance(resolved, (list, tuple)) and len(resolved) == 2:
                period_start, period_end = resolved[0], resolved[1]

        where_parts: list[str] = []
        params: list[Any] = []

        placeholders = ", ".join(["%s"] * len(self.source_types))
        where_parts.append(f"{self.source_type_field} IN ({placeholders})")
        params.extend(self.source_types)

        # ★ **跨来源类型才互斥**（registry §4 #8 的口径，2026-09-13 收口）。
        #
        # 本规则要防的是"**同一笔**业务事实被两种来源各记一次"（手工费用 vs 库存自动归集）。
        # 同一来源类型下的**不同来源单号是两笔不同的业务事实**，不该互相拦：
        # `004_cost.sql` 的物理去重键 `uq_cost_entries_org_dedupe` 走的生成列
        # `ledger_dedupe_key` **含 `source_ref`**（迁移注释原文："同一归属对象、同一期间、
        # **同一来源单号**只能有一条自动归集记录"）—— DB 层本来就允许"两笔库存单各自归集"。
        #
        # 所以谓词里加 `source_type <> 本次的来源类型`：本次是 `manual_expense` 时，
        # 冲突只能来自禁止集合里的 `warehouse_ledger`，反之亦然。
        #
        # 少了这一条的后果（实测过）：**同来源类型的第二笔也被拒** —— 那是实现偏离契约，
        # 不是契约留白；`tools/cost_e2e.py` 那条断言因此从 SKIP 转回 PASS。
        #
        # 取不到本次来源类型时**不加**该条件（方向仍是"宁可多拦"：不因缺一个上下文
        # 就把跨类型检查一起关掉）。
        submitted_source_type = _value_from(
            (payload, "_invariant_context", before), self.source_type_field
        )
        if submitted_source_type is not None:
            where_parts.append(f"{self.source_type_field} <> %s")
            params.append(submitted_source_type)

        target_where, target_params = _build_where(target)
        where_parts.append(target_where)
        params.extend(target_params)

        # 期间重叠：已有期间与本次期间有交集。
        where_parts.append(f"{end_column} >= %s AND {start_column} <= %s")
        params.extend([period_start, period_end])

        # ★ 分租键：**要么给出、要么报错**（不做 JOIN —— 跨表子查询会让索引失效，
        #   分租键在写路径上由服务端补全）。
        #
        # 为什么不能"能取到就限定、取不到就跳过"：跳过会让 SQL 里的租户条件消失，
        # 查重范围**扩到其他企业** —— 一条租户隔离相关的规则在缺键时反而变宽松，
        # 方向错了（其余规则缺键最坏是"没拦住"，这条是"拦错了范围"）。
        #
        # ★ 但"**显式 NULL**"与"**键缺失**"必须分开处理（cost 的基地级归属引入）：
        #   * 键缺失 = 接线错误（服务忘了回传）⇒ 仍然报错，就是下面那条；
        #   * 键在、值为 None = 该分租列**本来就可空**（`cost_entries.area_id`：
        #     基地级成本不属于任何单个区域）⇒ 必须翻成 `area_id IS NULL`。
        #     `IS NULL` 是**收窄**条件（仍与 organization_id / farm_id 一起限定在
        #     同一企业同一基地内），与上面担心的"条件消失导致范围扩大"方向相反。
        #   实测：不区分这两者时，`target_type=farm` 会报
        #   `INTERNAL_ERROR 缺少分租键 area_id`（HTTP 500）。
        conditions = 0
        for key in self.tenant_keys:
            present = any(
                isinstance(source, dict) and key in source for source in (payload, before)
            )
            value = _value_from((payload, before), key)
            if not present:
                raise DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    f"{self.label}无法执行：缺少分租键 {key}，"
                    "无法把查重范围限定在本企业内（不允许退化成全表查）",
                    data={
                        "rule": "NO_OVERLAPPING_SOURCE",
                        "missing_tenant_key": key,
                        "tenant_keys": list(self.tenant_keys),
                    },
                )
            if value is None:
                where_parts.append(f"{key} IS NULL")
            else:
                where_parts.append(f"{key}=%s")
                params.append(value)
            conditions += 1
        if conditions == 0:  # 构造期已禁止，这里是兜底
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"{self.label}无法执行：没有可用的分租键",
                data={"rule": "NO_OVERLAPPING_SOURCE"},
            )

        # ★ 自排除**下推到 SQL**：语义是"除了我以外还有没有冲突行"。
        #
        # 为什么不能"取回任意一行再比对"：`LIMIT 1` 取到哪一行由存储引擎决定，库里
        # 有历史残留时可能先取到**旧行**，于是 `_is_self_match` 返回 False →
        # 把"我自己那一行"当成"另一笔重复归集"→ 正常路径被拒（假阳性）。
        # 缺陷形态与 DEVELOPMENT §4 同源：判定依赖了一个不该影响结论的因素（返回顺序）。
        exclude_id = payload.get(_EXCLUDE_ID_KEY)
        if exclude_id is not None:
            where_parts.append(f"{self.key_column} <> %s")
            params.append(exclude_id)

        row = tx.query_one(
            f"SELECT id FROM {self.table} WHERE {' AND '.join(where_parts)} LIMIT 1",
            params,
        )
        if row is None:
            return
        # 兜底：主键列不叫 `id`（或执行器没注入）时，仍按 payload 里的键排除。
        if _is_self_match(payload, row):
            return

        target_label = "、".join(
            f"{column}={target[column]}" for column in self.target_fields if column in target
        )
        raise DomainError(
            ErrorCode.CONFLICT,
            f"{self.label}在该期间已由「{self.source_types[0]}」自动归集过成本，不能重复归集",
            data={
                "rule": "NO_OVERLAPPING_SOURCE",
                "existing_id": _row_value(row, "id"),
                "target": target_label,
            },
        )

# ---------------------------------------------------------------------------
# HarvestQuantityMatch —— 交付数量与出塘事实一致（registry §4 #6，Q9 已恢复）
#
# 早期实现：`sales_source_control.py:22-24`：出塘单必须是 `verified`、
# 同批次同塘口、且没有已核验的更正单；交付数量必须**等于**出塘数量：
#     unit == "tail" -> harvest.quantity
#     否则            -> harvest.weight_kg * (2 if unit == "jin" else 1)
# 这条换算规则原样继承（单位换算写在第 22 行那一处，不能散开）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HarvestQuantityMatch:
    """交付数量必须等于关联出塘单的数量（按计量单位换算）。"""

    harvest_field: str = "harvest_document_id"
    order_field: str = "unit"
    quantity_field: str = "quantity"
    harvest_table: str = "harvests"
    #: 出塘单必须处于的状态（早期实现要求 `verified`）。
    statuses: tuple[str, ...] = ("verified",)
    #: 同批次/同塘口一致性校验用的字段（子行字段 -> 出塘单列名）。
    batch_field: str = "batch_id"
    pond_field: str = "pond_id"
    tolerance: Decimal = Decimal("0.001")
    label: str = "交付数量"
    harvest_label: str = "出塘单"

    name = "HarvestQuantityMatch"

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        harvest_id = _value_from((payload, before), self.harvest_field)
        if harvest_id is None:
            # 未关联出塘单：关联字段本身的必填由字段声明负责（本不变量不重复报错）。
            return
        quantity = _value_from((payload, before), self.quantity_field)
        if quantity is None:
            return
        unit = str(_value_from((payload, before), self.order_field) or "kg")

        harvest = tx.query_one(
            f"SELECT * FROM {self.harvest_table} WHERE id=%s",
            (_as_int(harvest_id, self.harvest_label),),
        )
        if harvest is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                f"{self.harvest_label}不存在或已被删除",
                data={"field": self.harvest_field, "rule": "HARVEST_QUANTITY_MATCH"},
            )
        if not scope.allows_row(harvest):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED,
                f"{self.harvest_label}不在当前账号的数据范围内",
                data={"field": self.harvest_field, "rule": "HARVEST_QUANTITY_MATCH"},
            )

        status = _row_value(harvest, "status")
        if status is not None and str(status) not in self.statuses:
            raise DomainError(
                ErrorCode.CONFLICT,
                f"{self.harvest_label}尚未核验，不能作为交付依据",
                data={
                    "rule": "HARVEST_QUANTITY_MATCH",
                    "status": str(status),
                    "allowed": list(self.statuses),
                },
            )

        for field_name, column in (
            (self.batch_field, "batch_id"),
            (self.pond_field, "pond_id"),
        ):
            claimed = _value_from((payload, before), field_name)
            actual = _row_value(harvest, column)
            if claimed is None or actual is None:
                continue
            if not _same(claimed, actual):
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"所选出塘单不属于本次的批次或塘口，请重新选择",
                    data={
                        "rule": "HARVEST_QUANTITY_MATCH",
                        "field": field_name,
                        "harvest_value": actual,
                    },
                )

        if unit == "tail":
            fact = _as_decimal(_row_value(harvest, "quantity") or 0)
        else:
            weight = _as_decimal(_row_value(harvest, "weight_kg") or 0)
            fact = weight * (2 if unit == "jin" else 1)

        requested = _as_decimal(quantity)
        difference = requested - fact
        if difference < 0:
            difference = -difference
        if difference > self.tolerance:
            raise DomainError(
                ErrorCode.CONFLICT,
                f"{self.label}（{requested}）必须与{self.harvest_label}的出塘数量（{fact}）一致",
                data={
                    "rule": "HARVEST_QUANTITY_MATCH",
                    "fact": str(fact),
                    "requested": str(requested),
                },
            )


# ---------------------------------------------------------------------------
# AtMostOnePending —— 集合上唯一（registry §4 #21）
#
# 早期实现有两层：DB 唯一键 `uq_pond_status_active_request`（生成列 + 唯一索引）
# 与 `pond_status_store.py:35-37` 的显式预检。`DECISIONS.md` Q2 称它是
# "很好的不变量示范 —— 它证明系统能表达'集合上唯一'而不只是'字段非空'"。
#
# 施工要求（registry §4 #21）：**两层都要有** —— 唯一键是正确性底线（并发下唯一
# 可信的判定），本不变量负责给出可读的 409 文案。少了唯一键，并发请求会双写；
# 少本不变量，用户只会看到一句数据库报错。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AtMostOnePending:
    """同一 ``dims`` 上最多只有一行处于 ``pending_states``。

    触发条件是"本次操作会**产生**一行待处理记录"：只有 payload 里带齐了全部
    ``dims`` 才校验（核验/取消类能力改的是已存在的行，不是新增待办，不适用）。
    """

    dims: tuple[str, ...]
    table: str
    status_field: str = "status"
    pending_states: tuple[str, ...] = ("submitted",)
    label: str = "待处理记录"
    #: 主键列名：自排除（"除了我以外还有没有待处理行"）用它。
    key_column: str = "id"

    name = "AtMostOnePending"

    def __post_init__(self) -> None:
        if not self.dims:
            raise ValueError("AtMostOnePending 至少要声明一个分组维度")

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        header: dict[str, Any] = {}
        for column in self.dims:
            value = _value_from((payload,), column)
            if value is None:
                return  # 本次不是"新增一行"，不适用
            header[column] = _as_int(value, column)

        where, params = _build_where(header)
        placeholders = ", ".join(["%s"] * len(self.pending_states))
        # ★ 与 `NoOverlappingSource` 同一处理：把"排除本次自己"下推到 SQL，
        # 否则 `LIMIT 1` 取到历史残留行时会误判为"已有待处理记录"（假阳性）。
        exclude_id = payload.get(_EXCLUDE_ID_KEY)
        exclude_sql = ""
        if exclude_id is not None:
            exclude_sql = f" AND {self.key_column} <> %s"
            params.append(exclude_id)
        row = tx.query_one(
            f"SELECT id FROM {self.table} WHERE {where} AND {self.status_field} IN "
            f"({placeholders}){exclude_sql} LIMIT 1",
            [*params, *self.pending_states],
        )
        if row is None:
            return
        # 兜底：执行器没注入排除键时，仍按 payload 比对（并递归排除自己）。
        if _is_self_match(payload, row):
            return
        raise DomainError(
            ErrorCode.CONFLICT,
            f"已存在一条{self.label}，请先处理它",
            data={
                "rule": "AT_MOST_ONE_PENDING",
                "existing_id": _row_value(row, "id"),
                "dimensions": {column: header[column] for column in self.dims},
            },
        )


__all__ = [
    "AmountWithin",
    "AtLeastOneOf",
    "AtMostOnePending",
    "CumulativeWithin",
    "DistinctActors",
    "HarvestQuantityMatch",
    "NoNegativeStock",
    "NoOverlappingSource",
    "OptimisticLock",
    "PeriodOpen",
    "ReferencedStatus",
    "RequiredField",
    "RequiredWhen",
    "SameTenant",
    "StateTransition",
    "StatusAllowsEdit",
    "UniqueCode",
    "ZeroBalance",
]
