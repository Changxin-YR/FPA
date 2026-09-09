"""成本记录的写路径：登记、提交、确认、归档、关账。

## 写能力的统一形状（照 master_data 抄）

每个写能力都做四件事，顺序固定：

    1. ctx.require(...)            —— 第三层权限校验（不信任执行器）
    2. scope.assert_allows_row     —— 第三层范围校验（校验的是**具体那一行**）
    3. 状态机校验                   —— require_action / require_transition
    4. 写库 + 返回 HandlerResult(resource_id=...)

第 4 步的 `resource_id` **不是可选的**：执行器要按它回读校验（`_reload_after`）。
少了它，`executed` 就退化成"我们调用了 INSERT"而不是"数据真的落库了"；
而且执行器会在发现"能力声称写入成功但没有提供回读函数"时直接抛 `INTERNAL_ERROR`。

## 本域承载的需求最强证明点

`cost.entry.confirm` 同时挂两条不变量（`DECISIONS.md` Q13 原文）：

    DistinctActors(created_by, verified_by)   经办人 ≠ 审批人
    RequiredField(source_ref)                 每笔成本可追溯到底单

**为什么这两条重要**：`DistinctActors` 在早期版本里**只在 sales 域实现过一处**
（`早期版本 sales_service.py:121-122`），采购、仓储、生产、成本、主数据全部缺失 ——
那正是要根除的"合规规则跨域不一致"。`cost.entry.confirm` 是把它落到成本域的载体。

而"推广到全部核验/审批能力"这条规则在本项目里覆盖 13 条能力
（`DECISIONS.md` Q15），其中成本域贡献 1 条。

## 关账的三个并发校验

`close_period` 与塘口状态变更核验同构，任何一个不过都不改数据：

    1. 期间记录仍存在且在范围内
    2. 期间仍是 open（**原子占位**：UPDATE ... WHERE status='open'）
    3. 关账人 ≠ 无限制（期间是全局对象，scope 声明为 none —— 见文档说明）

第 2 条必须是 `WHERE` 条件而不是"先查再断言"：并发下两个请求会都通过检查再都执行，
而 `WHERE status='open'` 保证只有一个能改到 0 行以外。
**约束要放在能真正保证它的地方。**
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError, ErrorCode, not_found
from fpa.kernel.fields import (
    Choice,
    f_date,
    f_enum,
    f_int,
    f_num,
    f_ref,
    f_ref_by,
    f_str,
    text,
)
from fpa.kernel.invariants import lock_period_for_date
from fpa.kernel.scope import Scope
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import RowAction

from .entries import CostEntryService
from .service import (
    CATEGORY_ENABLED,
    MAX_AMOUNT,
    RECORD_LIFECYCLE,
    SOURCE_LEDGER,
    SOURCE_MANUAL,
    TARGET_TYPES,
    decorate,
    period_bounds,
)


#: 乐观锁版本参数的**统一声明**。
#:
#: ## 为什么它必须是一个声明出来的字段
#:
#: registry §0.7 规则 3 与 §2.11 把 `expected_version` 定为**所有 update 与 action
#: 能力的必带参数**（"不出现在表单，前端自动带"）。而它要真的到达处理器，必须
#: 出现在 `Capability.fields` 里——因为执行器调用服务前会先过 `validate_payload()`，
#: 那个函数**只保留声明过的字段**，未声明的直接丢弃（`capability.py` 的
#: `_ACCEPTED`/`cleaned` 循环）。
#:
#: 实测后果（**这是踩过的真坑，不是预防性设计**）：`cost.entry.confirm` 的处理器
#: 签名里有 `expected_version`，但字段表里没有它 ->
#: `validate_payload` 把它丢掉 -> 执行器调用 `handler(**kwargs)` 时缺参数 ->
#: `TypeError: confirm_entry() missing 1 required positional argument: 'expected_version'`。
#:
#: 这个失败形态很隐蔽：**校验层与处理器对"可接受的参数集合"有两份不同的答案**，
#: 而报错是 TypeError 而不是业务错误——排查方向会被引向"服务签名写错了"，
#: 而真实原因是字段没声明。
#:
#: 所以这里把它声明成 `Annotated`：一份声明同时供给校验、前端元数据与工具 schema。
#: `minimum=1`：版本号从 1 开始，`0` 是"没读过"的意思，不该被当成合法期望值。
_EXPECTED_VERSION = f_int(
    "乐观锁版本",
    required=True,
    minimum=1,
    help="当前记录的版本号：与库里不一致会被拒绝（防止两人同时改同一条）",
)


def _source_type_choices() -> tuple[Choice, ...]:
    """来源类型的候选值。

    label 取自模块级映射（**单一来源**）——不在这里手写第二份中文，
    否则"手工登记"与"手工录入"这类同义不同词会随文件数增长。
    """
    from .service import SOURCE_TYPE_LABELS

    return (
        Choice(SOURCE_MANUAL, SOURCE_TYPE_LABELS[SOURCE_MANUAL]),
        Choice(SOURCE_LEDGER, SOURCE_TYPE_LABELS[SOURCE_LEDGER]),
    )


def _target_type_choices() -> tuple[Choice, ...]:
    from .service import TARGET_TYPE_LABELS

    return tuple(Choice(code, TARGET_TYPE_LABELS[code]) for code in TARGET_TYPES)


#: 归属对象类型。**必须是一个具名常量**，因为 `target_id` 的多态声明要引用它的 key
#: （`f_ref_by("归属对象", "target_type")`）——写成 `f_ref_by(..., "target_type")` 的
#: 裸字符串，等于把字段名写了两遍，改一处就会静默失配。
#:
#: ## 关于 `required=False`（**不改，有依据**）
#:
#: 直觉上"要选归属对象就得先选类型"，所以很想把 `target_type` 改成必填。**但不能改**：
#:
#: * `docs/CAPABILITY_REGISTRY.md` §2.10 的字段表里 `target_type` 与 `target_id`
#:   的 required 列**都是空的**（该表用 ✔ 标必填，这两行没有 ✔）；
#: * 该表对 `target_id` 的约束原文是「**与 `target_type` 同时出现**」——它规定的是
#:   **两者一致**，不是"必须出现"；
#: * `cost.entry.create` 的 description 也写着「归属对象留空时按当前账号的数据范围解析」。
#:
#: 也就是说：**"两个都不给"是合法的业务形态**（成本落到账号数据范围解析出的区域上），
#: 非法的是"只给一个"。把它改成必填会**擅自改契约**，并让"不指定归属对象"这条
#: 合法路径消失。所以这里保留可选，把"必须同时给出"留给服务与 DB CHECK 去保证
#: （DB 层有 CHECK 兜底，服务层给出可读的字段级错误）。
#:
#: 前端不再需要靠猜：`target_id` 的多态声明（`ref.resource_field`）已经能把
#: "先选类型、再选对象"这条依赖关系直接渲染出来。
_TARGET_TYPE = f_enum(
    "归属对象类型",
    _target_type_choices(),
    help="基地/区域/塘口/批次；选了类型才能选「归属对象」",
)


def _category_choices(_current: str | None = None) -> tuple[Choice, ...]:
    """成本类别的候选值，**从库里读**（只取启用状态的）。

    ## 为什么要连库（而不是写死在代码里）

    registry §2.10 的约束是"须为**启用的**成本类别"——而"启用"是一个会变的业务
    事实，不是编译期常量。把类别枚举写死会让"停用一个类别"变成一次发版，
    而且停用后前端下拉里仍然会出现它：**让用户选一个必然失败的值，
    是把校验成本转嫁给了用户**（与 `Field.dynamic_choices` 的说明同一条理由）。

    ## 连不上库时返回空元组（有意识的降级）

    元数据接口不该因为数据库抖动而整体 500——那会让整个前端页面打不开；
    而空 choices 的后果只是"前端下拉为空"，服务端的 `_require_category` 仍然校验。
    **降级的方向是从"看得见"退到"看不见"，而不是从"校验"退到"放行"。**

    ## 为什么走内核工厂而不是自己连库

    驱动只能出现在 `fpa.kernel.uow`（`tests/test_architecture.py` 强制）。域里自建
    连接会同时违反两条约束：**依赖方向**，以及"连接配置只有一处"——在补齐本段之前，
    仓库里有 5 份各自从环境变量拼同一份配置的代码。现在配置与连接都来自
    `fpa.kernel.uow_factory`，本函数只表达"查哪些行"。
    """
    from fpa.kernel.uow_factory import query_rows

    try:
        rows = query_rows(
            "SELECT code, name FROM cost_categories WHERE status='enabled' "
            "ORDER BY sort_order, code"
        )
    except Exception:  # noqa: BLE001
        # 连不上库（或查询失败）：返回空元组，把"看不见"留给前端，
        # 真正的校验仍由 `_require_category` 在写路径上做。
        return ()

    return tuple(Choice(str(row["code"]), str(row["name"])) for row in rows)


class CostWriteService(CostEntryService):
    """成本记录的写路径。

    继承 `CostEntryService` 以复用 `_scope_row` / `decorate` / `load_entry`——
    读写共用同一套范围校验与派生逻辑，避免"读看到一个样、写看到另一个样"。
    """

    # -- 内部工具 -------------------------------------------------------------
    # `_require_category` / `_require_open_period` 在基类 `CostEntryService` 上：
    # 人工入口与跨域自动归集入口（`record_fact()`）共用同一份判据，
    # 「期间是否开放」这件事因此只有一处实现。

    # -- 登记成本 -------------------------------------------------------------

    def create_entry(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        category_code: f_enum(
            "成本类别", (),
            required=True,
            help="必须是启用状态的成本类别（候选值由服务端从类别表读取）",
            dynamic_choices=_category_choices,
        ),
        amount: f_num("金额", required=True, minimum=0.01, maximum=float(MAX_AMOUNT)),
        occurred_on: f_date("发生日期", required=True, help="所在期间须未关账"),
        source_type: f_enum(
            "来源类型",
            _source_type_choices(),
            required=True,
            help="手工登记=系统外费用（人工/水电/租金）；库存归集=由投喂/入库/领用核验自动产生",
        ),
        source_ref: f_str(
            "来源单号", required=True, max_length=128,
            help="投喂/入库/领用自动归集时指向那笔业务事实；手工登记时填外部单据号",
        ),
        note: text("备注", max_length=500),
        target_type: _TARGET_TYPE,
        # ★ 多态引用：归属对象指向**哪一类**对象，由 `target_type` 的当前取值决定。
        #
        # 改之前这里是 `f_int("归属对象", minimum=1)`，元数据便告诉前端"这是个整数
        # 输入框"——前端照做，用户看到一个要求自己填 id 的数字框，既不知道填哪个、
        # 也无从知道合法值有哪些。**类型信息丢了，界面就只好按错的类型渲染。**
        target_id: f_ref_by(
            "归属对象",
            "target_type",
            minimum=1,
            help="候选项随「归属对象类型」切换（选区域则是区域列表，选塘口则是塘口列表）",
        ),
        period_start: f_date("期间起", help="留空时按发生日期所在自然月自动确定"),
        period_end: f_date("期间止", help="留空时按发生日期所在自然月自动确定"),
    ) -> cap.HandlerResult:
        """登记一笔成本。**这是"系统外费用"的唯一入口。**

        `DECISIONS.md` Q13 推翻"成本纯派生 + 只读"的核心理由就是这一条能力：
        "成本随业务事实自动生成"只是副作用，它**完全遗漏了系统外费用**
        （人工、水电、租金）——那是覆盖面缺口，不是精简。

        ## 三个由服务端决定的值

        * `period_start` / `period_end`：**默认由发生日期所在的自然月确定**。
          registry 把这两个字段列成必填，但让客户端自己填"这笔成本属于哪个期间"
          等于让调用方定义期间边界——而期间边界是会计制度的产物，不是客户的自由。
          客户端可以显式覆盖（用于跨月分摊），但默认值必须由服务端给。
        * `status`：固定 `draft`（不接受客户端指定，否则可以跳过流程直接建成 verified）
        * 分租键 `organization_id` / `farm_id` / `area_id`：由归属对象解析后写入，
          不接受客户端提交（Q7 裁决）。
        """
        ctx.require("cost.manage")

        # 归属对象：解析出分租键，并校验对象在数据范围内。
        # `target_id` 与 `target_type` 必须同时给出（registry §2.10），
        # 这条在 DB 层也有 CHECK 兜底 —— 但 DB 的报错是"约束冲突"，
        # 而这里能说清是哪个字段的问题。
        if (target_type is None) != (target_id is None):
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "归属对象类型与归属对象必须同时给出",
                data={"fields": ["target_type", "target_id"]},
            )

        tenant = self._resolve_tenant(tx, scope, target_type, target_id)

        if source_type is None:
            source_type = SOURCE_MANUAL
        if source_type not in (SOURCE_MANUAL, SOURCE_LEDGER):
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "来源类型的取值无效",
                data={"field": "source_type", "allowed": [SOURCE_MANUAL, SOURCE_LEDGER]},
            )

        # 期间边界：默认取发生日期的自然月；显式给了就用显式值（跨月分摊场景）。
        default_start, default_end = period_bounds(
            f"{occurred_on.year:04d}-{occurred_on.month:02d}"
        )
        start = period_start or default_start
        end = period_end or default_end
        if start > occurred_on or occurred_on > end:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                "发生日期必须落在期间之内（期间起 ≤ 发生日期 ≤ 期间止）",
                data={"field": "occurred_on", "period_start": str(start), "period_end": str(end)},
            )

        self._require_category(tx, tenant["organization_id"], str(category_code))
        # 期间校验用 period_start 所属的期间（跨月分摊时以期间起点所在月为准）
        self._require_open_period(tx, start, tenant["organization_id"])
        self._require_open_period(tx, occurred_on, tenant["organization_id"])

        try:
            tx.execute(
                "INSERT INTO cost_entries "
                "(organization_id, farm_id, area_id, category_code, amount, occurred_on, "
                " period_start, period_end, source_type, source_ref, target_type, target_id, "
                " note, confirm_state, status, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending','draft',%s)",
                (
                    tenant["organization_id"], tenant["farm_id"], tenant["area_id"],
                    str(category_code), amount, occurred_on, start, end,
                    str(source_type), source_ref,
                    None if target_type is None else str(target_type),
                    None if target_id is None else int(target_id),
                    note, ctx.actor.user_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            # 唯一键冲突 = 同一笔库存事实被重复归集（§4 #8 NoOverlappingSource）。
            # 这条检查在**数据库层**（uq_cost_entries_org_dedupe 走生成列），
            # 所以并发下的两个请求里必然只有一个能进来——应用层的"先查再插"做不到。
            #
            # 判定用**键名**而不是异常消息文本：早期版本 `production_store.py:142-146`
            # 的 `if key not in str(exc)` 就是消息文本判定，随 MySQL 版本变化。
            text_of_error = str(exc)
            if "uq_cost_entries_org_dedupe" in text_of_error:
                raise DomainError(
                    ErrorCode.CONFLICT,
                    f"同一归属对象在同一期间的库存成本已归集过（来源单号 {source_ref}），"
                    "不能重复计入",
                    data={"rule": "COST_SOURCE_DUPLICATED", "source_ref": source_ref},
                ) from exc
            raise

        entry_id = tx.last_insert_id()
        row = self.load_entry(tx, scope=scope, record_id=entry_id)
        assert row is not None
        return cap.HandlerResult(
            data={
                "record": decorate(row, ctx.actor.permissions),
                # 执行器的不变量需要的数据。`_invariant_context` 由
                # `CapabilityRunner._invariant_extra()` 取出并合并进不变量 payload
                # （见 `runner.py` 的 `run_invariants(payload={**cleaned, **extra})`）。
                #
                # 为什么要回传而不是让不变量自己查：
                # `NoOverlappingSource` 要判定"同一归属对象、同期、同租户是否已有
                # 另一来源的成本"，而这些值（分租键、解析后的期间边界）是**服务算出来的**。
                # 两边各算一遍必然出现不一致，而不一致的不变量比没有不变量更危险——
                # 它会以"看起来在强制"的形式放过真实的重复归集。
                "_invariant_context": {
                    "occurred_on": occurred_on,
                    "period_start": start,
                    "period_end": end,
                    "target_type": None if target_type is None else str(target_type),
                    "target_id": None if target_id is None else int(target_id),
                    "source_type": str(source_type),
                    "source_ref": source_ref,
                    # **本次写入产生的主键**：`NoOverlappingSource` 在写入**之后**执行，
                    # 所以它查到的第一行会是本次自己。不告诉它自己的 id，
                    # 它就会把自己判定成"重复归集"并拒绝第一次合法写入。
                    # 键名由内核定义（`invariants._EXCLUDE_ID_KEY`）。
                    "_invariant_exclude_id": entry_id,
                    # 租户键：不给的话 `NoOverlappingSource` 会在**跨企业**范围内查重，
                    # 两个不同企业的同一 batch_id 会被误判为重复归集。
                    "organization_id": tenant["organization_id"],
                    "farm_id": tenant["farm_id"],
                    "area_id": tenant["area_id"],
                },
            },
            resource_id=entry_id,
            message=f"已登记成本 {amount} 元（{category_code}，来源单号 {source_ref}），待确认",
        )

    @staticmethod
    def _resolve_tenant(
        tx: UnitOfWork,
        scope: Scope,
        target_type: str | None,
        target_id: int | None,
    ) -> dict[str, int]:
        """从归属对象解析出 `organization_id` / `farm_id` / `area_id` 三个分租键。

        ## 为什么必须有归属对象

        `cost_entries` 的三个分租键是 NOT NULL —— 它们让 `resource(area_id)` 的
        DataScope 谓词能直接命中本表列并走索引（Q6 裁决：不做跨表 JOIN 解析）。
        没有归属对象的成本记录没法确定"谁有权看它"。

        所以：**未指定归属对象时，落到成本记录归属人自己数据范围内的那个区域上**。
        这不是"猜"——它用的是服务端解析出的 `scope`，客户端无法影响它
        （Q7 裁决：如果客户端能指定 area_id，"用户只能写自己区域的数据"
        就退化成"用户自报家门"）。

        指定了归属对象时，必须验证该对象存在**且在数据范围内**——
        否则用户可以往别的区域的塘口上归集成本。

        ## TODO（已裁决，方案未落地 —— 见 ROLLOUT_CONTRACT §2 的新例外）

        负责人 已裁决：**"给定 (表名, 主键) 解析分租键"属于内核按表名读取**，
        应当与 `uow_factory.query_rows` 同一处、由内核提供**唯一实现**
        （形如 `resolve_tenant_keys(table, record_id)`，只读、按主键、
        返回 `(organization_id, farm_id, area_id)`）。

        裁决理由：它与内核不变量的例外**完全同形**（都是表名参数化的通用件、不含业务语义），
        因此**一份实现**即可满足成本、warehouse、purchase、sales 的共同需求，
        不必让每个域各为对方写一个"我读你一下"的函数（那等于把 §2 的禁止换成 N 个双向依赖），
        也不需要"每来一个跨域读就登记一次例外"。

        **本文当前仍是直读对方表** —— 这是已知的 §2 偏离，**不是遗漏**。
        负责人 明确要求：在 `resolve_tenant_keys` 落地前**保留现状 + 留 TODO**，
        **不要为了让契约自洽而先改成调用一个还不存在的函数**
        （那会让 e2e 从"通过"变成"响亮失败"，而那次失败不是本域的缺陷）。

        待内核落地后的改法（一行）：
        ```python
        tenant = resolve_tenant_keys(table, int(target_id))   # 内核提供
        ```
        `_TARGET_TABLE` 随之退化为"调用内核解析器时传的表名"，不再是"cost 直连 production 的表"。
        """
        if target_type is None or target_id is None:
            return CostWriteService._tenant_from_scope(tx, scope)

        table, id_column, farm_expr, area_expr = _TARGET_TABLE[str(target_type)]
        # 逐层按"该表里哪一列代表这一层"取值；没有这一层就显式给 NULL
        # （`farms` 没有 region 这一层 -> area_id = NULL）。
        area_select = "NULL AS area_id" if area_expr is None else f"{area_expr} AS area_id"
        row = tx.query_one(
            f"SELECT organization_id, {farm_expr} AS farm_id, {area_select} "
            f"FROM {table} WHERE {id_column} = %s",
            (int(target_id),),
        )
        if row is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"归属对象不存在（{target_type}#{target_id}）",
                data={"field": "target_id"},
            )
        if not scope.allows_row(row):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED,
                f"归属对象不在当前账号的数据范围内（{target_type}#{target_id}）",
            )
        return {
            "organization_id": int(row["organization_id"]),
            "farm_id": int(row["farm_id"]),
            "area_id": None if row["area_id"] is None else int(row["area_id"]),
        }

    @staticmethod
    def _organization_of_scope(tx: UnitOfWork, scope: Scope, *, action: str) -> int:
        """从当前账号的数据范围解析出它所属的**企业**（分租键里最粗的那一层）。

        ## 为什么需要它（不是"顺手复用"）

        有些对象的租户键是**一层**的，而不是三层：`accounting_periods.organization_id`
        是 NOT NULL，而它**没有** `farm_id` / `area_id`（期间是全局对象，不属于某个区域）。
        这类对象不能用 `ScopePolicy.resource("area_id")` 那种逐区域的谓词去卡，
        但**必须**仍然按企业收窄 —— 否则"不分区域"会被误读成"不分企业"，
        于是跨企业串号（`PeriodOpen` 刚修掉的就是这一族缺陷的孪生形态）。

        ## 解析规则（fail-closed，与 `_tenant_from_scope` 同源同口径）

        | 账号范围 | 企业取自 |
        |---|---|
        | 有 area 型记录 | 该区域所属企业 |
        | 只有 farm 型记录 | 该基地下 id 最小的区域所属企业 |
        | `allow_all`（全场 / 超管） | **拒绝**（解析不出单一企业，见下） |
        | 没有任何范围记录 | **拒绝**（`Scope` 构造期其实已经拒绝了这种账号） |

        `allow_all` 为什么拒绝而不是"随便挑一家"：那正是本次修掉的缺陷形态 ——
        让系统替用户挑一家企业，用户看到的是"关账成功"，而实际被关的可能是别人的账。
        与 `_tenant_from_scope` / `master_data._scope_keys.tenant_keys_for_create`
        对"全场账号"的处理一致：**要关账，请先配一个具体数据范围**（或有显式企业入参）。
        """
        from fpa.kernel.scope import ScopeType

        if scope.allow_all:
            raise DomainError(
                ErrorCode.DATA_SCOPE_UNRESOLVED,
                f"无法确定{action}对象所属企业：当前账号的数据范围是全场。"
                "会计期间按企业隔离，请先为该账号配置具体数据范围"
                "（不要让系统替你挑一家企业 —— 那会关错账）",
                data={"field": "period"},
            )

        area_ids = sorted(
            entry.bound_id for entry in scope.entries if entry.scope_type is ScopeType.AREA
        )
        if area_ids:
            row = tx.query_one("SELECT id, organization_id FROM areas WHERE id = %s", (area_ids[0],))
        else:
            farm_ids = sorted(
                entry.bound_id for entry in scope.entries if entry.scope_type is ScopeType.FARM
            )
            if not farm_ids:
                raise DomainError(
                    ErrorCode.DATA_SCOPE_UNRESOLVED,
                    f"无法确定{action}对象所属企业：当前账号的数据范围不含具体区域或基地",
                    data={"field": "period"},
                )
            row = tx.query_one(
                "SELECT id, organization_id FROM areas WHERE farm_id = %s ORDER BY id LIMIT 1",
                (farm_ids[0],),
            )
        if row is None:
            raise DomainError(
                ErrorCode.DATA_SCOPE_UNRESOLVED,
                f"无法确定{action}对象所属企业：数据范围引用的区域不存在"
                "（换一家企业会让账目落到错误的对象上）",
                data={"field": "period"},
            )
        return int(row["organization_id"])

    @staticmethod
    def _tenant_from_scope(tx: UnitOfWork, scope: Scope) -> dict[str, int]:
        """把数据范围解析成可落库的分租键。**fail-closed。**

        解析不出**具体**的 area 时抛 `DATA_SCOPE_UNRESOLVED`，绝不退化成"填个默认值"。
        早期版本 `common/security/data_scope.py:58` 在同样情形下 `return "1=0", []` ——
        用户看到 0 行数据却不报错，是本项目要根除的形态之一（`ARCHITECTURE.md:183`）。

        两种情形分得很清楚：

        * **范围里有具体区域**（area 型 scope）—— 取最小那个 area_id，
          反查它的 farm / organization。这是最常见的配置，也是唯一能自动确定归属的配置。
        * **全场账号或只有 farm 型范围** —— 没有具体的 area 可归，
          因此要求调用方显式指定归属对象。**不给默认值**：给默认值会让"这笔成本算在
          哪个塘口"变成系统猜的，而成本归属是财务口径，不该由系统猜。
        """
        from fpa.kernel.scope import ScopeType

        if scope.allow_all:
            raise DomainError(
                ErrorCode.DATA_SCOPE_UNRESOLVED,
                "无法确定成本归属：当前账号的数据范围是全场，请显式指定归属对象"
                "（塘口 / 批次 / 区域 / 基地）",
                data={"field": "target_id"},
            )

        area_ids = sorted(
            entry.bound_id for entry in scope.entries if entry.scope_type is ScopeType.AREA
        )
        if not area_ids:
            raise DomainError(
                ErrorCode.DATA_SCOPE_UNRESOLVED,
                "无法确定成本归属：当前账号的数据范围不含具体区域，请显式指定归属对象"
                "（塘口 / 批次 / 区域 / 基地）",
                data={"field": "target_id"},
            )

        area_id = area_ids[0]
        area = tx.query_one(
            "SELECT id, organization_id, farm_id FROM areas WHERE id = %s", (area_id,)
        )
        if area is None:
            # 范围里引用了一个不存在的区域 -> 数据范围本身不可解析，必须报错。
            # 静默换一个区域会让成本落到错误的塘口上，而账面看不出任何异常。
            raise DomainError(
                ErrorCode.DATA_SCOPE_UNRESOLVED,
                f"无法确定成本归属：数据范围引用的区域 {area_id} 不存在",
            )
        return {
            "organization_id": int(area["organization_id"]),
            "farm_id": int(area["farm_id"]),
            "area_id": int(area["id"]),
        }

    # -- 提交 / 归档 ----------------------------------------------------------
    #
    # ★ 这两个方法是**未注册的写路径**，刻意保留但**不可达** —— 请勿当成遗漏。
    #
    # registry §1.9 给 cost 域定的能力是 5 条（list / create / confirm / summary /
    # period.close），**没有** `cost.entry.submit` / `cost.entry.archive`。
    # 但 `COST_ENTRY_WORKFLOW`（照 master_data 的 `RECORD_LIFECYCLE` 抄的）在
    # `draft` 状态上声明了 `edit` / `submit` 动作，于是
    # `Capability.row_actions()` 会把它们报给前端 —— 前端因此会渲染出按钮，
    # 而点击时会 404（没有对应的能力与路由）。
    #
    # 这是一个**真实的、我以前没有明说的缺口**：状态机的 row_actions 是"静态上限"，
    # 它与"已注册的能力集合"之间没有任何机械一致性保证。
    #
    # 本域的处置：
    #   * 实现保留（它们逻辑正确、`_lifecycle()` 抽出了公共部分，将来要开放
    #     `cost.entry.submit` / `cost.entry.archive` 只需加两条能力声明）；
    #   * **不注册**（registry 的 5 条是权威清单，擅自扩到 7 条会改变能力总数，
    #     而"69 条"是 负责人 与全队的验收基线）；
    #   * 在 `capabilities.py` 里显式记录这个缺口（见该文件的说明）。
    #
    # 建议 负责人 把它作为**跨域的共性问题**处置：要么给 `Workflow` 加一个
    # "只声明已实现的动作"的约束（声明期可校验），要么在架构测试里加一条
    # "`row_actions` 的每个动作都能在 REGISTRY 里找到对应能力"的断言。
    # 否则每个域都会渲染出点不动的按钮，而这**在测试里是看不出来的**。

    def submit_entry(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        entry_id: f_int("成本记录 ID", required=True),
        expected_version: _EXPECTED_VERSION,
    ) -> cap.HandlerResult:
        """提交核验：draft -> submitted。"""
        return self._lifecycle(
            tx, ctx, scope,
            entry_id=int(entry_id),
            expected_version=int(expected_version),
            from_states=("draft",),
            to_state="submitted",
            action=RowAction.SUBMIT,
            permission="cost.manage",
            message="成本记录已提交核验",
        )

    def archive_entry(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        entry_id: f_int("成本记录 ID", required=True),
        expected_version: _EXPECTED_VERSION,
    ) -> cap.HandlerResult:
        """归档：draft / verified -> archived。

        归档不是删除——数据保留，只是退出业务视图（与 master_data 的
        `pond.archive` 同一裁决：早期版本的物理删除靠外键报错兜底，属于纯负债）。
        """
        return self._lifecycle(
            tx, ctx, scope,
            entry_id=int(entry_id),
            expected_version=int(expected_version),
            from_states=("draft", "verified"),
            to_state="archived",
            action=RowAction.ARCHIVE,
            permission="cost.manage",
            message="成本记录已归档",
        )

    def _lifecycle(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        *,
        entry_id: int,
        expected_version: int,
        from_states: tuple[str, ...],
        to_state: str,
        action: RowAction,
        permission: str,
        message: str,
    ) -> cap.HandlerResult:
        """生命周期动作的统一实现（与 master_data 的 `PondWriteService._lifecycle` 同构）。

        三个动作形状相同，差别只在"从哪些状态来、到哪个状态去、需要什么权限"。
        抽成一个方法而不是复制三遍——早期版本每个动作各写一遍 UPDATE + WHERE +
        冲突翻译，那是 bug 的温床（改了一处忘了另一处）。
        """
        ctx.require(permission)
        row = self._scope_row(tx, scope, entry_id)
        from .service import COST_ENTRY_WORKFLOW

        COST_ENTRY_WORKFLOW.require_action(str(row["status"]), action)
        if str(row["status"]) not in from_states:
            raise DomainError(
                ErrorCode.CONFLICT,
                "当前状态不允许该操作",
                data={"status": str(row["status"]), "allowed_from": list(from_states)},
            )

        placeholders = ",".join(["%s"] * len(from_states))
        affected = tx.execute(
            f"UPDATE cost_entries SET status=%s, row_version=row_version+1, updated_by=%s "
            f"WHERE id=%s AND row_version=%s AND status IN ({placeholders})",
            (to_state, ctx.actor.user_id, entry_id, expected_version, *from_states),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该成本记录已被他人修改，请刷新后重试",
            )

        updated = self.load_entry(tx, scope=scope, record_id=entry_id)
        assert updated is not None
        return cap.HandlerResult(
            data={"record": decorate(updated, ctx.actor.permissions)},
            resource_id=entry_id,
            message=message,
        )

    # -- 确认成本（本域的核心证明点）------------------------------------------

    def confirm_entry(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        entry_id: f_int("成本记录 ID", required=True),
        expected_version: _EXPECTED_VERSION,
        source_ref: f_str(
            "来源单号", max_length=128,
            help="确认时必须给出：每笔成本都要能追溯到底单（无附件时的替代形态）",
        ),
        reason: text("确认说明", max_length=500),
    ) -> cap.HandlerResult:
        """确认成本入账：pending -> confirmed。**必须由另一人确认。**

        ## 三条校验，任何一个不过都不改数据

        1. **记录仍在 pending**（原子占位：`UPDATE ... WHERE confirm_state='pending'`）
           —— 防止并发双确认，也防止"确认后又被确认一次"产生第二条账目影
        2. **确认人 ≠ 经办人**（`DistinctActors` 不变量 + 这里的显式判断
           + 数据库 CHECK 兜底）
        3. **来源单号非空**（`RequiredField` 不变量 + `source_ref` 列 NOT NULL 兜底）

        ## 为什么合规判断要写两遍（服务里一遍、不变量里一遍）

        不变量是**执行器**的一层。如果将来有别的调用方绕过执行器直接调服务
        （例如后台任务、数据导入），服务里的显式判断仍然拦得住。
        **合规规则的强度应当取决于最弱的那条路径，不是最强的那条。**

        而数据库层的 CHECK 是第三重：它保证连脏数据都写不进去（如手工 SQL 修数据）。
        三层不是重复劳动——它们的失效模式不同，各自挡的是不同的人。

        ## `source_ref` 在确认时再次校验

        registry §4 #15 把"核验必须有凭据"降级为"核验必须有来源单号"（Q5 裁决：
        P0 不做附件）。降级后"每笔成本都可追溯到底单"这个核心价值仍在，
        但**强制点必须是确认这一动作**：登记时允许先记账后补单号（现场常见的做法），
        确认时不允许。
        """
        ctx.require("cost.confirm")
        row = self._scope_row(tx, scope, int(entry_id))

        if str(row["confirm_state"]) == "confirmed":
            raise DomainError(
                ErrorCode.CONFLICT,
                "该成本记录已确认，不能重复确认",
                data={"rule": "ALREADY_CONFIRMED"},
            )
        if int(row["created_by"]) == ctx.actor.user_id:
            raise DomainError(
                ErrorCode.FORBIDDEN,
                "经办人不能确认自己登记的成本，请由他人复核",
                data={"rule": "DISTINCT_ACTORS"},
            )

        # 来源单号：提交值优先，其次回落到记录上已有的值。
        # 为什么允许回落：登记时留空、确认时补填是常见的现场流程。
        effective_ref = str(source_ref or row.get("source_ref") or "").strip()
        if not effective_ref:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "确认成本必须给出来源单号（每笔成本都要能追溯到底单）",
                data={"field": "source_ref", "rule": "SOURCE_REF_REQUIRED"},
            )

        # 期间锁定：确认入账也是一次账目影响，已关账期间不得确认（Q16 语义）。
        occurred_on = row["occurred_on"]
        if not isinstance(occurred_on, date):
            occurred_on = date.fromisoformat(str(occurred_on))
        self._require_open_period(tx, occurred_on, int(row["organization_id"]))

        affected = tx.execute(
            "UPDATE cost_entries SET confirm_state='confirmed', source_ref=%s, "
            "verified_by=%s, verified_at=NOW(), status='verified', "
            "row_version=row_version+1, updated_by=%s "
            "WHERE id=%s AND row_version=%s AND confirm_state='pending' AND created_by <> %s",
            (
                effective_ref,
                ctx.actor.user_id,
                ctx.actor.user_id,
                int(entry_id),
                int(expected_version),
                ctx.actor.user_id,
            ),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "该成本记录状态已变化或已被他人修改，请刷新后重试",
            )

        confirmed = self.load_entry(tx, scope=scope, record_id=int(entry_id))
        assert confirmed is not None
        return cap.HandlerResult(
            data={
                "record": decorate(confirmed, ctx.actor.permissions),
                "_invariant_context": {
                    # DistinctActors 读 `before`，RequiredField 读 payload；
                    # 这里把确认时生效的来源单号放进去，让校验对象与落库值一致。
                    "source_ref": effective_ref,
                    "occurred_on": occurred_on,
                    # `PeriodOpen` 的反查还需要租户键：会计期间每个企业各有一份，
                    # 少了它内核会命中邻家的期间行（误拦本企业的合法确认，
                    # 或静默放行本企业已关账期间的确认）。内核缺它时显式报错，
                    # 所以这里回传记录自己的 `organization_id`（上一步刚校验过范围）。
                    "organization_id": row["organization_id"],
                },
            },
            resource_id=int(entry_id),
            message=f"成本已确认：{confirmed['amount']} 元（来源单号 {effective_ref}）",
        )

    # -- 关账 -----------------------------------------------------------------

    def close_period(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        period: f_str("会计期间", required=True, max_length=7, help="格式 YYYY-MM，例如 2026-09"),
        reason: text("关账说明", required=True, max_length=500),
    ) -> cap.HandlerResult:
        """关闭一个会计期间。**这是 `PeriodOpen` 不变量的开启点。**

        ## 为什么 `period` 的解析必须严格

        `accounting_periods.period` 是 CHAR(7) 且与企业组成唯一键。如果接受
        `2026-1` 这种变体，同一个月份就会出现两个键（`2026-01` 与 `2026-1`），
        于是"这个月关账了吗"会有两个答案。`period_bounds()` 因此只接受
        `YYYY-MM` 的严格形态。

        ## 关账人为什么不需要"≠ 某人"

        `DistinctActors` 不挂在关账上：关账不是一个"提交—审批"的单据流程，
        而是对**整个期间**的封存动作。给它套上双人规则会变成"谁都可以提，谁都不能批"。
        为它把规则放松是在给自己开后门，所以不挂；真正的管控靠
        `cost.close` 权限码 + 审计 + `reason` 必填。

        ## 为什么要 `reason`

        registry §2.11 把 `reason` 列为 `cost.period.close` 的必填字段
        （【新增设计】）。早期版本只记结果不记原因，事后无法回答"这个月为什么提前关了"。

        ## 关账的原子性

        `UPDATE ... WHERE status='open'` —— 两个并发关账请求里只有一个能改到行。
        "先查状态再断言"在并发下两个请求会都通过检查。**约束要放在能真正保证它的地方。**

        ## ★ 关账按 `organization_id` 收窄（与 `PeriodOpen` 同型缺陷的孪生形态）

        本方法的期间反查原先只按日期区间取行、**不带租户键**，而
        `accounting_periods.organization_id` 是 NOT NULL —— **每个企业各有一份自己的期间行**。
        后果与刚修掉的 `PeriodOpen` 缺陷完全同形、而且更严重：`ORDER BY id LIMIT 1`
        会命中**别的企业**的期间行，于是**"A 企业关账"实际关掉的是最先建的那个企业的这个月**
        （`UPDATE ... WHERE id=%s` 用的是查回来的那一行的 id）。这不是"漏拦"，是**改错了对象**。

        修法与内核那条保持同一口径：**把租户键放回谓词**。租户从哪来？
        - 本能力的 `scope` 声明是 `ScopePolicy.none()`（期间是全局对象、不是某个区域的数据行），
          所以**没有** area 级的 DataScope 谓词 —— 但"不分区域"不等于"不分企业"；
        - 因此这里从**当前账号的数据范围**解析出它所属的企业（`_organization_of_scope`，
          与 `_tenant_from_scope` 同一份解析、同一套 fail-closed 规则）：
          范围里的区域 -> 区域所属企业；只有基地 -> 该基地下最小区域所属企业；
        - **解析不出企业时拒绝**（`DATA_SCOPE_UNRESOLVED`），绝不退化成"关掉任意一家"。
          这是刻意的：会计期间是分租户的事实，"这个月关谁家的账"不能由系统猜。

        **产品语义变化（显式记账）**：关账现在按 `organization_id` 收窄 ——
        **A 企业关账不再影响 B 企业**（这正是 R2/§4 #7 要求的方向）；代价是
        **`allow_all`（全场 / 超管）账号不再能关账**（它解析不出单一企业）。
        要恢复"全场关账"，正确做法是给该账号配一个具体数据范围，或以后新增
        "显式指定企业"的入参 —— 而不是让它随便挑一行（那正是本次修掉的缺陷）。
        """
        ctx.require("cost.close")

        start, end = period_bounds(str(period))
        period_text = f"{start.year:04d}-{start.month:02d}"
        organization_id = self._organization_of_scope(tx, scope, action="关账")

        # 期间是全局对象：它的 `scope` 声明是 `none`（registry §1.9），因此这里**不做**
        # `scope.allows_row`（关账的对象是会计期间，不是某个区域的数据行；
        # "同一期间对部分区域已关、部分未关"比不锁更危险）。但"不分区域"不等于"不分企业"：
        # 租户键必须出现在谓词里，否则 `ORDER BY id LIMIT 1` 会命中别的企业的期间行。
        row = lock_period_for_date(
            tx, organization_id=int(organization_id), occurred_on=start
        )
        if row is None:
            raise not_found("会计期间")
        if str(row["period"]) != period_text:
            # 区间匹配到一个"跨界"的期间（例如自定义的季度期间）——报错而不是
            # 按 id 关掉它。声称关了 2026-09 却关掉了 2026-Q3，是对不上的。
            raise DomainError(
                ErrorCode.CONFLICT,
                f"期间 {period_text} 与已有期间记录「{row['period']}」不一致，无法关账",
                data={"period": period_text, "matched": str(row["period"])},
            )
        if str(row["status"]) == "closed":
            raise DomainError(
                ErrorCode.CONFLICT,
                f"{period_text} 已经关账（{row['closed_at']}），不能重复关账",
                data={"rule": "ALREADY_CLOSED", "period": period_text},
            )

        # `AND organization_id=%s` 不是装饰：它是"并发 + 串行都只能关到本企业那一行"的
        # 第二道保证（第一道是上面的 SELECT 已带租户键）。少了它，一旦上面那句被改回
        # 跨企业查询，这里会**静默**关掉别人的期间。
        affected = tx.execute(
            "UPDATE accounting_periods SET status='closed', closed_by=%s, closed_at=NOW(), "
            "close_reason=%s, row_version=row_version+1 "
            "WHERE id=%s AND organization_id=%s AND status='open'",
            (ctx.actor.user_id, reason, int(row["id"]), organization_id),
        )
        if affected == 0:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                f"{period_text} 的关账状态已被他人改变，请刷新后重试",
            )

        closed = self.load_period(tx, scope=scope, record_id=int(row["id"]))
        assert closed is not None
        return cap.HandlerResult(
            data={
                "period": {
                    "id": int(closed["id"]),
                    "period": str(closed["period"]),
                    "status": str(closed["status"]),
                    "status_label": "已关账",
                    "period_start": str(closed["period_start"]),
                    "period_end": str(closed["period_end"]),
                    "closed_at": str(closed["closed_at"]),
                    "close_reason": str(closed["close_reason"]),
                },
                "_invariant_context": {
                    # `PeriodOpen` 按 `occurred_on` 反查所属期间。
                    # 关账动作的"发生日"就是它要关的那个期间的起点——
                    # 用期间起点反查所属期间是**唯一**的（期间不重叠），
                    # 因此这条不变量在关账上的含义精确地是"被关的期间必须仍是开放的"。
                    "occurred_on": closed["period_start"],
                },
            },
            resource_id=int(closed["id"]),
            message=f"{period_text} 已关账：{reason}",
        )


# ---------------------------------------------------------------------------
# 回读函数绑定（`__fpa_load_by_id__`）
#
# `CapabilityRunner` 在**提交前**调这个函数回读，确认"写入真的落了库"
# （`docs/WRITE_CONTRACT.md` 规则 2）。它按 `resource_id` 读：
#
#   * 记录类能力 -> `load_entry`
#   * 关账        -> `load_period`（resource_id 是期间记录的 id）
#
# **忘了绑定的后果是显式的**：执行器会抛
# `INTERNAL_ERROR: 能力 X 声称写入成功但没有提供回读函数，无法确认写入结果`
# ——不会静默降级成成功。这条"忘了就报错"的设计是刻意的：
# 早期版本此刻会返回 {"id": ...} 之类的占位数据，看起来"有数据"但来源不可追溯。
#
# 为什么用模块级赋值而不是装饰器：注册表里挂的是**类上的未绑定函数**
# （`CostWriteService.create_entry`），`__fpa_load_by_id__` 必须挂在同一个函数
# 对象上；装饰器会让"能力声明里的 handler"与"挂载回读函数的对象"变成两个不同的
# 东西，而 `runner._resolve()` 只认前者。
# ---------------------------------------------------------------------------

CostWriteService.create_entry.__fpa_load_by_id__ = CostEntryService.load_entry  # type: ignore[attr-defined]
CostWriteService.confirm_entry.__fpa_load_by_id__ = CostEntryService.load_entry  # type: ignore[attr-defined]
CostWriteService.submit_entry.__fpa_load_by_id__ = CostEntryService.load_entry  # type: ignore[attr-defined]
CostWriteService.archive_entry.__fpa_load_by_id__ = CostEntryService.load_entry  # type: ignore[attr-defined]
CostWriteService.close_period.__fpa_load_by_id__ = CostEntryService.load_period  # type: ignore[attr-defined]


#: 归属对象类型 -> (表名, 主键列名, farm_id 的取法, area_id 的取法 / None)。
#:
#: 写成显式映射而不是拼字符串：`f"{target_type}s"` 这种"猜复数"在不规则复数上必然
#: 出错（`batch` -> `batchs`），而且把表名藏进了推导规则里。白名单同时挡掉了
#: SQL 注入的可能（`target_type` 来自请求体）。
#:
#: ★ **后两列（分租键取法）不能省**。原实现是"每张表都
#:   `SELECT organization_id, farm_id, area_id`"，隐含假设"每张目标表都有这三列"
#:   —— 但事实不是：
#:     * `farms` 只有 `organization_id`（**没有 farm_id，也没有 area_id**）
#:       —— 基地自己就是基地，它不属于任何区域；
#:     * `areas` 只有 `organization_id` / `farm_id`（**没有 area_id**）
#:       —— 区域自己就是区域；
#:   于是 `target_type=farm` / `target_type=area` 一定抛 MySQL 1054「未知列」，
#:   再被 `kernel/uow.py::_translate_db_error` 兜底成
#:   `503 SERVICE_UNAVAILABLE 数据库服务暂时不可用` —— 用户被告知"数据库坏了"，
#:   而真实原因是"这个归属层级在表结构上取不出来"。
#:   实测：4 个 target_type 里 farm / area 提交必 503，pond / batch 正常。
#:   取法写成"该表里哪一列代表这一层"，缺的那一层给 None（落库为 NULL）。
_TARGET_TABLE: dict[str, tuple[str, str, str, str | None]] = {
    # target_type -> (表, 主键列, farm_id 取法, area_id 取法)
    "farm": ("farms", "id", "id", None),
    "area": ("areas", "id", "farm_id", "id"),
    "pond": ("ponds", "id", "farm_id", "area_id"),
    "batch": ("production_batches", "id", "farm_id", "area_id"),
}
