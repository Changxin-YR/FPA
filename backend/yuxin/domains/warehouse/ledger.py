"""库存账本：**全系统唯一能改库存的入口**。

## 为什么需要"唯一入口"

库存是四个域的公共事实源：

    warehouse   receipt.verify  入库存（+ 生成应付）
    warehouse   issue.verify    出库存（领用）
    production  feeding.verify  出库存（投喂用料，registry §4 #17 的降级复用）
    sales       delivery.verify 不写库存，但读库存判断交付可行性（P0 只读）

如果每个域各写一份 `INSERT INTO inventory_ledger`，就会出现本项目最反对的形态：
**同一段逻辑的两份副本**。两份副本在各自的单域 e2e 里都能过，只在集成时炸——
因为"负库存怎么判、批次怎么建、唯一键撞了怎么办"三个问题会有三套答案。

所以：账本的**写**只有一个函数（`apply_movement`），其余域调用它，不碰表。

## 本模块的两个公开入口

    resolve_issue_lot(...)   只读：解析"从哪个仓、哪个批次出库"
    apply_movement(...)      写：幂等建批 + 写账本 + 负向增量锁行判定

## 关于"不得负库存"的判定归属（评审结论后定稿）

**判定不在本模块，由声明式不变量 `NoNegativeStock` 强制**（`capabilities.py` 里挂在
`issue.verify` 上）。本模块的职责被收窄为三件事：

    ① 按 `lot_no` 幂等建批
    ② 对 `(warehouse_id, material_id, inventory_lot_id)` 组加行锁
    ③ 写账本行

这样"不得负库存"只有一处实现（能力声明上那条不变量），不会出现"这里手写一遍、
声明上再写一遍"的两处形态。

### 本模块**不再**提供第二份判定实现（这是有意识的一步）

初版我在本模块写了 `check_negative_stock()`，理由是"让全仓共用一份判定"。但
`kernel/invariants.py::NoNegativeStock` 本身就是那条规则的**活实现**（它查写入后的
余额、抛 `NO_NEGATIVE_STOCK`），于是我的那份变成了**第二个真相来源**——
只有 e2e 在调它，生产路径一次都不会走到。

"同一件事写在两处"正是本项目最反对的形态，所以它以两种方式都不可接受：
留着当死代码（下一个人会以为它才是权威），或者留着当"备用实现"（两处文案迟早漂移）。
**已删除**，判定只剩内核那一处。

### ⚠️ 用户可读的错误信息从哪来

不变量在执行器里跑，抛出的 `DomainError` 会直达用户。`issue.verify` 走能力入口时
由**不变量**给出文案；`apply_movement` 的直接调用方（各域内部的跨域路径）则会在
提交前由同一条不变量拦住。两条路径的错误口径一致，因为规则本身只有一份。
## 与早期实现的继承关系

    * 行锁 + 判定的写法    继承 `早期版本 warehouse_ledger_store.py:181-202` `_validate_negative()`
    * 幂等建批             继承 `早期版本 warehouse_ledger_store.py:30-34`
    * 唯一键               `早期版本 010_warehouse.sql` 的 `uq_inventory_ledger_source_line`

早期实现把这三件事写在仓储里紧贴 SQL，且**只服务于仓储自己**——所以生产域的投喂
扣库走的是另一条路径（`早期版本 production_material_control.py`）。本模块把它提出来，
让四个域打同一扇门。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from yuxin.kernel.errors import DomainError, ErrorCode, not_found, scope_denied
from yuxin.kernel.money import money
from yuxin.kernel.scope import Scope, scope_predicate_for_columns
from yuxin.kernel.uow import UnitOfWork, translate_mysql_error

from .service import WAREHOUSE_SCOPE_COLUMNS

#: 账本允许的来源类型。registry §3.3 行 1116 只给这两个值
#: （`correction` 明确不做）。**`feeding.verify` 也用 `issue`**——
#: 它是"生产领用出库"，不是一个新单据类型；新增第三个枚举值会让 §3.3 的
#: 权威表失效，而"库存流水只有两种来源单据"是本版刻意的收敛。
SOURCE_RECEIPT = "receipt"
SOURCE_ISSUE = "issue"
SOURCE_TYPES = (SOURCE_RECEIPT, SOURCE_ISSUE)

#: source_ref 的来源域前缀。见 006 迁移里 `uq_inventory_ledger_source_line` 的注释：
#: 用"域:单据号"而不是自增主键，因为不同单据表的主键各自从 1 开始会撞。
PREFIX_WAREHOUSE = "warehouse"
PREFIX_FEEDING = "feeding"

#: 单次移动允许的最大数量（与 DECIMAL(18,4) 的容量留出安全余量）。
MAX_QUANTITY = Decimal("1000000000")


#: MySQL 的唯一键冲突错误号（1062 ER_DUP_ENTRY / 1586）。
#: 检测 1062 之外还看消息文本，是因为 PyMySQL 在少数路径上把它包成
#: OperationalError（本项目"禁止靠异常消息判断约束类型"的纪律针对的是
#: **区分约束类型**，这里只需要判"是不是重复键"这一个事实，因此把两种信号
#: 都当作命中——宁可多认一个，也不要把重复入账漏成 500）。
_DUPLICATE_ERRNOS = frozenset({1062, 1586})


def _is_duplicate_key(exc: BaseException) -> bool:
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int) and args[0] in _DUPLICATE_ERRNOS:
        return True
    text = str(exc)
    return "Duplicate entry" in text or "duplicate key" in text.lower()


def _invalid(message: str, *, field: str) -> DomainError:
    """账本入口的**调用参数**非法（枚举值 / 标识形态 / 范围）。

    与 `errors.validation()` 的区别：后者按项目约定返回 `FIELD_INVALID`
    （面向"请求体里这个字段的值不对"），而这里说的是"账本接口被用错了"。
    两者都是 400；区分它们是因为 `FIELD_INVALID` 的 `data.field` 是给前端
    高亮输入框用的，而账本入口是**跨域内部契约**，没有输入框可高亮。
    """
    return DomainError(ErrorCode.VALIDATION_ERROR, message, data={"field": field})


def ledger_source_ref(domain: str, code: str) -> str:
    """拼出账本的来源标识：``warehouse:RCV-001`` / ``feeding:FEED-001``。

    刻意做成函数而不是让调用方自己 f-string：这是唯一键的一半，
    两处各自拼一次就会出现 ``warehouse:RCV-001`` 与 ``Warehouse:RCV-001``
    这种"看起来一样、唯一键认为不同"的静默重复入账。
    """
    domain = (domain or "").strip()
    code = (code or "").strip()
    if not domain or not code:
        raise _invalid("来源单据编号不能为空", field="source_ref")
    if ":" in code:
        raise _invalid(f"来源单据编号不能含冒号：{code!r}", field="source_ref")
    return f"{domain}:{code}"


@dataclass(frozen=True, slots=True)
class Movement:
    """一次账本移动的结果。

    为什么返回对象而不是一个 int：调用方要拿它渲染 `message`（WRITE_CONTRACT
    规则 3：`message` 由服务端从**真实数据**渲染）。`balance_after` 是这一句
    "库存剩 420kg" 里的那个 420——它是本次在**持锁状态下**读出来的数，
    不允许调用方事后另查一次（那是第二个真相来源）。
    """

    lot_id: int
    ledger_id: int
    warehouse_id: int
    warehouse_name: str
    lot_no: str
    quantity_delta: Decimal
    balance_before: Decimal
    balance_after: Decimal
    material_id: int
    unit: str = ""

    @property
    def movement_id(self) -> int:
        """账本行 id 的别名（t12 任务说明里的叫法）。"""
        return self.ledger_id


# ---------------------------------------------------------------------------
# 内部：仓库与批次的解析
# ---------------------------------------------------------------------------


def _load_warehouse(tx: UnitOfWork, scope: Scope, warehouse_id: int) -> dict[str, Any]:
    """取仓库行并校验它在数据范围内（第三层防御：看的是**数据本身**）。"""
    row = tx.query_one(
        "SELECT id, organization_id, farm_id, area_id, code, name, is_default, status "
        "FROM warehouses WHERE id = %s",
        (int(warehouse_id),),
    )
    if row is None:
        raise not_found("仓库")
    if not scope.allows_row(row):
        raise scope_denied("该仓库")
    return row


def _load_lot_by_id(tx: UnitOfWork, lot_id: int) -> dict[str, Any]:
    row = tx.query_one(
        "SELECT id, organization_id, farm_id, area_id, warehouse_id, material_id, "
        "       lot_no, status, expiry_date FROM inventory_lots WHERE id = %s",
        (int(lot_id),),
    )
    if row is None:
        raise not_found("物料批次")
    return row


def _find_lot_id(
    tx: UnitOfWork, *, warehouse_id: int, material_id: int, lot_no: str
) -> int | None:
    """按 (仓库, 物料, 批次号) 找批次——对应 006 迁移里那个唯一键。"""
    row = tx.query_one(
        "SELECT id FROM inventory_lots "
        "WHERE warehouse_id = %s AND material_id = %s AND lot_no = %s",
        (int(warehouse_id), int(material_id), lot_no),
    )
    return None if row is None else int(row["id"])


def _lock_lot(tx: UnitOfWork, lot_id: int) -> dict[str, Any]:
    """按**主键**锁定批次行，返回该行。

    这是全模块唯一持有库存并发锁的地方。后续的余额计算与账本写入都在持锁期间
    完成，因此 `SELECT SUM(...)` 与 `INSERT` 之间不存在窗口。

    为什么锁 `inventory_lots` 这一行而不是账本上的聚合：
    按主键定位的锁边界是确定的（一个批次一行）。账本在持续追加，没有稳定的
    "行"可以锁，而 `SELECT SUM(..) ... FOR UPDATE` 锁的是什么取决于 MySQL 的
    执行计划——把并发正确性建立在执行计划上不是一个可以评审的设计。
    """
    row = tx.query_one(
        "SELECT id, organization_id, farm_id, area_id, warehouse_id, material_id, "
        "       lot_no, status, expiry_date FROM inventory_lots WHERE id = %s FOR UPDATE",
        (int(lot_id),),
    )
    if row is None:
        raise not_found("物料批次")
    return row


def _lot_balance(tx: UnitOfWork, lot_id: int) -> Decimal:
    """批次当前余额。**必须在持有 lot 行锁之后调用**。

    ## 这里的 `FOR UPDATE` 不是多余的（这是一处真实缺陷的修复）

    第一版写的是普通 `SELECT SUM(...)`。并发实测（两个真连接，批次余额 100，
    两个请求各出 60）**双双通过**，余额变成 -20——负库存。

    原因：MySQL 默认隔离级别是 REPEATABLE-READ。普通 SELECT 是**快照读**，
    它读的是事务**第一个一致性读**建立的 read view，而不是最新已提交数据。
    时间线实测如下（`t=` 为相对秒）：

        t=0.14s [A] 拿到 lot 行锁 lot_id=10
        t=0.14s [A] 读余额 -> 100          ← 快照建立在这里
        t=0.14s [A] 账本行已写 -60
        t=1.64s [A] 已提交
        t=1.64s [B] 拿到 lot 行锁          ← 锁**确实**等到了 A 提交
        t=1.64s [B] 读余额 -> 100          ← 但快照是 1.64s 之前的，看不到 -60
        t=1.64s [B] 账本行已写 -60 → 合计 -20

    B 的锁等待是正确的，问题在于**读到的是陈旧快照**。行锁保证"互斥"，
    但保证不了"可见性"——两件事，缺一不可。

    修法：余额读也做成**锁定读**。锁定读永远读**最新已提交版本**，且会等待
    其他事务持有的行锁。于是 B 在拿到锁之后必然读到 A 提交的 -60，
    判定 40 + (-60) < 0 并拒绝。项目里 `PeriodOpen` / `OptimisticLock` 这些
    不变量同样依赖"读到的是最新事实"，这条纪律适用于全部读账本的路径。

    代价：锁定读会锁住 `idx_inventory_ledger_balance` 范围内的账本行，
    但该范围只覆盖**同一个批次**（`inventory_lot_id = %s`），
    而同一个批次的写本来就要靠 lot 行锁串行化——没有引入新的锁粒度。
    """
    row = tx.query_one(
        "SELECT COALESCE(SUM(quantity_delta), 0) AS balance "
        "FROM inventory_ledger WHERE inventory_lot_id = %s FOR UPDATE",
        (int(lot_id),),
    )
    return Decimal(str((row or {}).get("balance", 0)))


def _coerce_decimal(value: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise _invalid(f"{field}必须是数字", field=field) from exc


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    raise _invalid("发生时间格式无效，应为 YYYY-MM-DD HH:MM:SS", field="happened_at")


# ---------------------------------------------------------------------------
# 公开入口 1：解析出库的仓库与批次
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssueLot:
    """"这批物料该从哪出"的解析结果。"""

    warehouse_id: int
    warehouse_name: str
    lot_id: int
    lot_no: str
    available: Decimal
    expiry_date: date | None
    production_date: date | None


def resolve_issue_lot(
    tx: UnitOfWork,
    *,
    scope: Scope,
    material_id: int,
    warehouse_id: int | None = None,
    lot_no: str = "",
) -> IssueLot:
    """解析"从哪个仓、哪个批次出库"——**只读**，不写任何行。

    这是给 `feeding.verify` 用的（registry §2.6 的 `feeding.create` 字段表里
    **没有** `warehouse_id` / `lot_no`，所以生产域必须有个地方问"该扣哪个批"）。

    解析规则（按优先级）：

    1. 显式给了 ``lot_no``：精确取该批次（`issue.create` 走这条——它的表单里
       有 `lot_no`，用户已经选好了）。
    2. 显式给了 ``warehouse_id``：在该仓内按 **FEFO**（先到期先出）选。
    3. 都没给：在**默认仓**（`warehouses.is_default=1`）内按 FEFO 选；
       没有默认仓时，若当前数据范围内只有一个可用仓就用它。

    ## FEFO 的排序规则（含 NULL 的处理）

        ORDER BY (expiry_date IS NULL), expiry_date, id

    没有有效期的批次排在最后——它的"到期风险"无法判断，不该挤掉有明确保质期的
    库存。第三排序键 `id` 保证**确定性**：同样条件的两次调用必须选中同一行，
    否则"解析出来的批次"会随执行计划漂移。

    ## 不做跨仓分摊（刻意的取舍）

    若默认仓不够，**不**自动去别的仓凑。原因：一次出库跨两个仓就是两行账本，
    而调用方的 `total_quantity` 只有一个数；更根本的是"库存是分仓记账的",
    自动跨仓出库会让"这个物料的库存去哪了"无法按仓解释。够不到就报错，
    并把**确实有货的仓**列出来，让用户自己决定。
    """
    material_id = int(material_id)
    delta = Decimal(0)  # 只读解析，不涉及增量

    if lot_no:
        if warehouse_id is None:
            raise _invalid(
                "指定了物料批次号时必须同时指定仓库：同一批次号可以出现在不同仓库，"
                "而两个仓各自记账",
                field="warehouse_id",
            )
        lot_id = _find_lot_id(
            tx, warehouse_id=int(warehouse_id), material_id=material_id, lot_no=lot_no
        )
        if lot_id is None:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"物料批次「{lot_no}」在该仓库中不存在，请先在到货核验时登记该批次",
                data={"field": "lot_no"},
            )
        lot = _load_lot_by_id(tx, lot_id)
    else:
        warehouse_id = int(warehouse_id) if warehouse_id is not None else _default_warehouse_id(
            tx, scope=scope
        )
        lot = _pick_lot_fefo(tx, scope=scope, warehouse_id=warehouse_id, material_id=material_id)

    warehouse = _load_warehouse(tx, scope, int(lot["warehouse_id"]))
    if str(lot["status"]) != "available":
        raise DomainError(
            ErrorCode.CONFLICT,
            f"物料批次「{lot['lot_no']}」已关闭，不能出库",
            data={"lot_no": lot["lot_no"], "status": str(lot["status"])},
        )
    if lot["material_id"] != material_id:
        raise DomainError(
            ErrorCode.FIELD_INVALID,
            "物料批次与所选物料不一致",
            data={"field": "lot_no"},
        )

    available = _lot_balance(tx, int(lot["id"]))
    return IssueLot(
        warehouse_id=int(warehouse["id"]),
        warehouse_name=str(warehouse["name"]),
        lot_id=int(lot["id"]),
        lot_no=str(lot["lot_no"]),
        available=available,
        expiry_date=lot.get("expiry_date"),
        production_date=lot.get("production_date"),
    )


def _default_warehouse_id(tx: UnitOfWork, *, scope: Scope) -> int:
    """默认出库仓。见 006 迁移里 `is_default` 的注释（为什么不用"id 最小的仓"）。"""
    # 与 `inventory.list_warehouses` **同一条**范围判据（内核的
    # `scope_predicate_for_columns` 是唯一实现）。这里曾经用
    # `scope.where_clause("w")`：持有 `personal` / `pond` 型范围的账号会拼出
    # `w.created_by` / `w.pond_id`，而 `warehouses` 没有这两列 —— MySQL 报
    # `Unknown column`。那条路径是 `feeding.verify` 扣库存时走的，
    # 所以它的症状是"某个范围的账号投喂核验 500"，而不是列表页少一行。
    fragment, params = scope_predicate_for_columns(
        scope, WAREHOUSE_SCOPE_COLUMNS, "w"
    )
    # `status = 'verified'` 这一条**同时**就是"已停用（archived）的仓库不会被选为
    # 默认出库仓"的强制点——归档令 `status` 变成 `archived`，它自动退出候选。
    candidates = tx.query_all(
        "SELECT w.id, w.name FROM warehouses AS w "
        f"WHERE ({fragment}) AND w.status = 'verified' "
        "ORDER BY w.is_default DESC, w.id ASC",
        list(params),
    )
    if not candidates:
        raise DomainError(
            ErrorCode.FIELD_INVALID,
            "当前账号的数据范围内没有可用的仓库，无法出库。请先建立并核验仓库",
            data={"field": "warehouse_id"},
        )
    return int(candidates[0]["id"])


def _pick_lot_fefo(
    tx: UnitOfWork, *, scope: Scope, warehouse_id: int, material_id: int
) -> dict[str, Any]:
    """在某仓内按 FEFO 选出优先级最高的**有余额**批次。"""
    _load_warehouse(tx, scope, warehouse_id)  # 校验仓库在范围内
    rows = tx.query_all(
        """
        SELECT l.id, l.organization_id, l.farm_id, l.area_id, l.warehouse_id,
               l.material_id, l.lot_no, l.status, l.expiry_date, l.production_date,
               COALESCE(SUM(g.quantity_delta), 0) AS balance
        FROM inventory_lots AS l
        LEFT JOIN inventory_ledger AS g ON g.inventory_lot_id = l.id
        WHERE l.warehouse_id = %s AND l.material_id = %s AND l.status = 'available'
        GROUP BY l.id, l.organization_id, l.farm_id, l.area_id, l.warehouse_id,
                 l.material_id, l.lot_no, l.status, l.expiry_date, l.production_date
        HAVING balance > 0
        ORDER BY (l.expiry_date IS NULL), l.expiry_date, l.id
        """,
        (int(warehouse_id), int(material_id)),
    )
    if not rows:
        raise DomainError(
            ErrorCode.CONFLICT,
            f"仓库「{warehouse_id}」内没有该物料的可用库存批次",
            data={"rule": "NO_AVAILABLE_LOT", "material_id": int(material_id)},
        )
    return rows[0]


# ---------------------------------------------------------------------------
# 公开入口 2：写账本（唯一入口）
# ---------------------------------------------------------------------------


def apply_movement(
    tx: UnitOfWork,
    *,
    scope: Scope,
    actor_id: int,
    source_type: str,
    source_ref: str,
    source_line_no: int,
    warehouse_id: int,
    material_id: int,
    quantity_delta: Decimal | int | str,
    happened_at: datetime | str,
    lot_no: str = "",
    create_lot: bool = False,
    production_date: date | None = None,
    expiry_date: date | None = None,
    unit_cost: Decimal | int | str | None = None,
    pond_id: int | None = None,
    batch_id: int | None = None,
) -> Movement:
    """**全系统唯一的库存写入**：幂等建批 → 写账本 → 负向增量锁行判定。

    调用方（warehouse / production，以及将来的 sales）**不得**自己
    `INSERT INTO inventory_ledger`，也不得自己更新批次余额——余额是账本的
    求和结果，不是一个可以 UPDATE 的列。

    :param source_type: ``receipt`` 或 ``issue``（见模块头注释）。
    :param source_ref: ``ledger_source_ref("warehouse", code)`` 的产物。
        它必须来自**单据号**而不是自增主键——不同单据表的主键会撞。
        把原样传入的字符串做一次形态校验，避免"看起来像是单号"的自由格式
        破坏唯一键的可读性。
    :param quantity_delta: **带符号**的增量：入库为正、出库为负。
        不接收 0（迁移里有 CHECK，这里先给出更可读的报错）。
    :param lot_no: 入库时必填（registry §2.7 的 `WAREHOUSE_LOT_REQUIRED`
        继承）。出库时若留空，调用方应先调 `resolve_issue_lot()` 拿到批次；
        这里仍会在空值时按 FEFO 兜底解析一次，但不推荐——**解析与判定分开，
        调用方才能在扣减之前把"这个批次够不够"讲清楚**。
    :param create_lot: 是否允许新建批次（入库为 ``True``）。
        出库传 ``False``：出库不能凭一个批次号造出库存，那等于凭空补货。

    :returns: `Movement`——含 `balance_after`，供调用方渲染真实数字的 message。
    """
    if source_type not in SOURCE_TYPES:
        raise _invalid(
            f"库存来源类型只能是 {' / '.join(SOURCE_TYPES)}，收到 {source_type!r}",
            field="source_type",
        )
    if not source_ref or ":" not in source_ref:
        raise _invalid(
            "来源标识必须形如 `<域>:<单据号>`（例如 feeding:FEED-001）；"
            "它会与来源行号一起构成账本唯一键",
            field="source_ref",
        )
    try:
        line_no = int(source_line_no)
    except (TypeError, ValueError) as exc:
        raise _invalid("来源行号必须是正整数", field="source_line_no") from exc
    if line_no < 1:
        raise _invalid("来源行号从 1 开始", field="source_line_no")

    delta = _coerce_decimal(quantity_delta, field="quantity")
    if delta == 0:
        raise _invalid("库存增量不能为 0", field="quantity")
    if abs(delta) > MAX_QUANTITY:
        raise _invalid(f"库存增量超出允许范围（{MAX_QUANTITY}）", field="quantity")
    moment = _coerce_datetime(happened_at)
    material_id = int(material_id)

    # ---- ① 定位仓库与批次 -------------------------------------------------
    warehouse = _load_warehouse(tx, scope, int(warehouse_id))

    explicit_lot = bool(lot_no)
    if explicit_lot:
        lot_id = _find_lot_id(
            tx, warehouse_id=int(warehouse["id"]), material_id=material_id, lot_no=lot_no
        )
    else:
        lot_id = None

    if lot_id is None:
        if not explicit_lot:
            # 出库兜底：按 FEFO 解析（推荐调用方自己先调 resolve_issue_lot）
            lot_id = int(
                _pick_lot_fefo(
                    tx,
                    scope=scope,
                    warehouse_id=int(warehouse["id"]),
                    material_id=material_id,
                )["id"]
            )
        elif create_lot:
            # 幂等建批：ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
            # 让"插入或取回"用一条语句完成，且并发安全（继承旧写法）。
            tx.execute(
                "INSERT INTO inventory_lots "
                "(organization_id, farm_id, area_id, warehouse_id, material_id, lot_no, "
                " production_date, expiry_date, status, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'available',%s) "
                "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)",
                (
                    warehouse["organization_id"],
                    warehouse["farm_id"],
                    warehouse["area_id"],
                    int(warehouse["id"]),
                    material_id,
                    lot_no,
                    production_date,
                    expiry_date,
                    int(actor_id),
                ),
            )
            lot_id = tx.last_insert_id()
        else:
            raise DomainError(
                ErrorCode.FIELD_INVALID,
                f"物料批次「{lot_no}」在该仓库中不存在；出库不能凭空创建批次，"
                "请先通过到货核验入库该批次",
                data={"field": "lot_no"},
            )

    # ---- ② 锁定批次行（并发临界区从这里开始） ------------------------------
    lot = _lock_lot(tx, int(lot_id))

    if lot["material_id"] != material_id:
        raise DomainError(
            ErrorCode.FIELD_INVALID,
            "物料批次与所选物料不一致",
            data={"field": "lot_no"},
        )
    if int(lot["warehouse_id"]) != int(warehouse["id"]):
        raise DomainError(
            ErrorCode.FIELD_INVALID,
            "物料批次不属于所选仓库：库存是分仓记账的，跨仓出库会让账无法按仓解释",
            data={"field": "warehouse_id"},
        )
    if delta < 0 and str(lot["status"]) != "available":
        raise DomainError(
            ErrorCode.CONFLICT,
            f"物料批次「{lot['lot_no']}」已关闭，不能出库",
            data={"lot_no": str(lot["lot_no"]), "status": str(lot["status"])},
        )

    balance_before = _lot_balance(tx, int(lot["id"]))

    # ---- 余额不由本函数判定（评审结论 ④，见模块头"判定归属"一节） ----
    #
    # 「不得负库存」（registry §4 #1）由**声明式不变量 `NoNegativeStock`** 强制：
    # 它读**写入后**的账本余额，`SUM(quantity_delta) < 0` 即违规，失败整体回滚。
    # 本函数只负责：**幂等建批 + 加锁 + 写账本行**。
    #
    # 为什么把判定挪出去：否则"同一件事"会存在两处实现（此处一遍、能力声明上再一遍），
    # 正是本项目要根除的形态。判定语义务必与 `capabilities.py` 里
    # `NoNegativeStock(table="inventory_ledger", columns=("quantity_delta",),
    # group_by=_STOCK_GROUP_BY)` 保持一致——两者是同一份规则的两个视图。
    #
    # ## ⚠️ 两个前提（改动这里之前必须先读）
    #
    # 1. **批次行锁不能去掉。** 实测（两个真连接，余额 100，两个请求各出 60）：
    #    锁定读 `SUM(...) FOR UPDATE` 在 REPEATABLE-READ 下，**快照是在加锁之前取的**——
    #    所以它自己并不足以串行化。真正保证"第二个事务看到第一个的扣减"的是
    #    `_lock_lot()` 抢到的**批次行锁**：它让同批次的移动严格排队，之后的
    #    锁定读才必然读到最新已提交值。去掉行锁 → 余额会变成 −20（实测过）。
    # 2. **直接调用本函数的路径必须自己声明 `NoNegativeStock`。**
    #    `issue.verify` 已声明；`production` 的 `feeding.verify` 走的是同一函数，
    #    它是否声明由 production 域决定（见 `docs/ROLLOUT_CONTRACT.md` §2 的入口表）。
    #    这是"判定集中在一处"的代价，已向 负责人 报备。
    # ★ 这一行看起来像内核明确警告的"自己算 available + delta"——**它不是**。
    # 区别在**用途**，不在写法：
    #   * 内核禁止的是**拿它做判定**（`if available + delta < 0: raise`）。那样会与
    #     不变量叠加成 `available_before + 2×delta`，对负增量误拒合法操作；
    #   * 这里算出的 `balance_after` **只进 `Movement` 的返回值**，供调用方渲染
    #     `message`（WRITE_CONTRACT 规则 3：message 用真实数字，例如
    #     "投喂 50kg，1 号料还剩 420kg"）。
    # 本文件里**没有任何 `raise` 依赖这个数**：判定一律由 `NoNegativeStock` 做，
    # 我回传给它的 `_ledger_lines` 只有分组列、不含算术。
    balance_after = balance_before + delta

    # ---- ④ 写账本（唯一键保证"同一来源行只追加一次"） ----------------------
    amount = None
    unit_cost_value = None
    if unit_cost is not None:
        unit_cost_value = _coerce_decimal(unit_cost, field="unit_cost")
        if unit_cost_value < 0:
            raise _invalid("单价不能为负", field="unit_cost")
        # 金额 = 单价 × 数量。**按内核的金额口径量化**（`kernel/money.py`）。
        #
        # 原写 `.quantize(Decimal("0.0001"))` —— 那是**单价**的精度（`DECIMAL(14,4)`），
        # 而这里算的是**金额**。两者碰巧都能写进本表的
        # `amount DECIMAL(18,4)`，所以错了也不会报错 —— 但它让"金额几位小数"这件事
        # 在三个域里有三个答案。
        amount = money(unit_cost_value * abs(delta))

    try:
        tx.execute(
            "INSERT INTO inventory_ledger "
            "(organization_id, farm_id, area_id, warehouse_id, material_id, inventory_lot_id, "
            " lot_no, source_type, source_ref, source_line_no, quantity_delta, unit_cost, amount, "
            " pond_id, batch_id, happened_at, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                # 分租列取自**批次行**（与仓库一致，且批次是账本的直接父行）——
                # 这样账本的 area_id 与批次/仓库永不漂移。
                lot["organization_id"],
                lot["farm_id"],
                lot["area_id"],
                int(warehouse["id"]),
                material_id,
                int(lot["id"]),
                str(lot["lot_no"]),
                source_type,
                source_ref,
                line_no,
                delta,
                unit_cost_value,
                amount,
                pond_id,
                batch_id,
                moment,
                int(actor_id),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 下面按类型判定，非数据库错误原样抛出
        # 唯一键 `uq_inventory_ledger_source_line` 撞了 = "这张单据的这一行已经
        # 入过账"。这是**业务冲突**（重复核验），不是 500：
        # WRITE_CONTRACT 要求给用户一句可展示的中文文案。
        #
        # 为什么在这里翻译而不是让上层兜底：唯一键的**语义只有本模块知道**——
        # 它由本模块的三列（source_type/source_ref/source_line_no）定义，
        # 也只有本模块能说出"是哪张单据的哪一行重复了"。
        # 另外 kernel 的 `translate_mysql_error()` 虽然存在，但 `UnitOfWork`
        # 的取数方法并不调用它，所以裸的 IntegrityError 会一路冒到 500。
        if isinstance(exc, DomainError):
            raise
        if _is_duplicate_key(exc):
            raise DomainError(
                ErrorCode.CONFLICT,
                f"该单据的第 {line_no} 行已经入过库存账，不能重复核验"
                f"（来源 {source_ref}）",
                data={
                    "rule": "LEDGER_SOURCE_LINE_DUPLICATED",
                    "source_ref": source_ref,
                    "source_line_no": line_no,
                },
            ) from exc
        translated = translate_mysql_error(exc) if isinstance(exc, Exception) else None
        if translated is not None:
            raise translated from exc
        raise
    ledger_id = tx.last_insert_id()

    return Movement(
        lot_id=int(lot["id"]),
        ledger_id=int(ledger_id),
        warehouse_id=int(warehouse["id"]),
        warehouse_name=str(warehouse["name"]),
        lot_no=str(lot["lot_no"]),
        quantity_delta=delta,
        balance_before=balance_before,
        balance_after=balance_after,
        material_id=material_id,
    )


__all__ = [
    "IssueLot",
    "MAX_QUANTITY",
    "Movement",
    "PREFIX_FEEDING",
    "PREFIX_WAREHOUSE",
    "SOURCE_ISSUE",
    "SOURCE_RECEIPT",
    "SOURCE_TYPES",
    "apply_movement",
    "ledger_source_ref",
    "resolve_issue_lot",
]
