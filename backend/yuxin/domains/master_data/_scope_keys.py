"""从数据范围解析"新建无归属入参资源"的三个分租键。

## 为什么需要它

registry §0.7 规则 1：`create` **不接收也不返回** `organization_id` / `farm_id` /
`area_id`，由服务端从引用对象解析并写入。而 `partner` 这类资源在 registry §2.5 里
**没有任何归属对象字段**，却声明了 `scope=resource(area_id)`（`area_id` 在
`business_partners` 上是 NOT NULL）—— 于是归属只能来自数据范围本身。

fail-closed（`ARCHITECTURE.md:183`）：解析不出**具体**区域时抛
`DATA_SCOPE_UNRESOLVED`，绝不给默认值。给默认值会让"这条往来单位算在哪个区域"
变成系统猜的，而这是数据权限的落点，不该由系统猜。

## 这一处与 cost 域的口径一致

`cost.entries_write._tenant_from_scope` 在"未指定归属对象"时做的是同一件事
（取范围内 id 最小的区域，反查 farm / organization）。两处口径一致是**刻意的**：
它是 registry §0.3"area 型范围 = 我有这个区域"在写路径上的唯一解释。
"""

from __future__ import annotations

from yuxin.kernel.errors import DomainError, ErrorCode
from yuxin.kernel.scope import Scope, ScopeType
from yuxin.kernel.uow import UnitOfWork

#: 把"我应该问哪一列"这件事显式写出来：`Scope.allows_row` 按 `SCOPE_COLUMN` 从行里
#: 取**同名**列（area 型取 `row["area_id"]`、farm 型取 `row["farm_id"]`），所以判定
#: 一个 `areas` 行时，`area_id` 必须填**区域自身 id**——`areas` 表根本没有 `area_id` 列。
#: 不这么改写会出现真实缺陷：area 型范围的账号建不了塘口（`row.get("area_id")` 得到
#: None → 判定位外 → 403）。


def area_scope_row(area: dict[str, object]) -> dict[str, object]:
    """把 `areas` 行改写成"能对内核判定接口问对列"的行。

    改写的依据：一个区域在数据范围里的身份就是 **它自己的 id**（area 型范围记录
    的 `bound_id` 就是 `areas.id`）。所以 `area_id := areas.id`。
    """
    return {"area_id": int(area["id"]), "farm_id": int(area["farm_id"])}


def _resolve_org_farm(tx: UnitOfWork, scope: Scope) -> dict[str, int]:
    """解析**组织 + 基地**两个键（不含区域）。`tenant_keys_for_create` 与
    `area_keys_for_create` 共用它——两者在上述三种范围配置下取值完全相同，
    差别只在"要不要再落一个区域"。

    把它抽出来的判据不是"代码像"，而是：如果各写一遍，
    **"什么范围算能确定归属"这条规则就有了两处实现**，而它在 farm 型那一支上
    很容易写歪（见 `area_keys_for_create` 的说明）。
    """
    if scope.allow_all:
        row = tx.query_one(
            "SELECT id, organization_id FROM farms ORDER BY id LIMIT 1"
        )
        if row is None:
            raise DomainError(
                ErrorCode.DATA_SCOPE_UNRESOLVED,
                "系统里没有任何基地，无法确定数据归属（请先建立基地）",
            )
        return {"organization_id": int(row["organization_id"]), "farm_id": int(row["id"])}

    farm_ids = sorted(
        entry.bound_id for entry in scope.entries if entry.scope_type is ScopeType.FARM
    )
    if farm_ids:
        row = tx.query_one(
            "SELECT id, organization_id FROM farms WHERE id = %s", (farm_ids[0],)
        )
        if row is None:
            # 范围引用了一个不存在的基地 -> 数据范围本身不可解析，必须报错。
            raise DomainError(
                ErrorCode.DATA_SCOPE_UNRESOLVED,
                f"数据范围里的基地 #{farm_ids[0]} 不存在，无法确定数据归属",
            )
        return {"organization_id": int(row["organization_id"]), "farm_id": int(row["id"])}

    area_ids = sorted(
        entry.bound_id for entry in scope.entries if entry.scope_type is ScopeType.AREA
    )
    for area_id in area_ids:
        row = tx.query_one(
            "SELECT organization_id, farm_id FROM areas WHERE id = %s", (area_id,)
        )
        if row is not None:
            return {
                "organization_id": int(row["organization_id"]),
                "farm_id": int(row["farm_id"]),
            }
    if area_ids:
        raise DomainError(
            ErrorCode.DATA_SCOPE_UNRESOLVED,
            f"数据范围里的区域 #{area_ids[0]} 不存在，无法确定数据归属",
        )

    raise DomainError(
        ErrorCode.DATA_SCOPE_UNRESOLVED,
        "无法确定数据归属：当前账号的数据范围不含具体区域或基地，请让管理员分配范围",
    )


def area_keys_for_create(tx: UnitOfWork, scope: Scope) -> dict[str, int]:
    """`area.create` 要写入的两个分租键（`organization_id` / `farm_id`）。

    ## 为什么不能复用 `tenant_keys_for_create`

    `tenant_keys_for_create` 回答的是"**这条新记录算在哪个区域下**"，
    所以它在 farm 型范围那一支要**再向下找一个区域**（`areas WHERE farm_id=%s`
    取 id 最小的一行）——因为 A 表没有别的办法确定归属。

    而创建一个**区域**时，它的归属是**基地**（`areas` 只有 `organization_id` /
    `farm_id` 两个分租列）。复用那个函数会有一个真实且致命的后果：
    **某个基地下一个区域都还没有时，恰恰建不出第一个区域** —— 而那正是
    "系统里还没有这个基地的区域"的常见状态（README 的种子只种了两个区域）。
    新建**物料** / **仓库** / **塘口** 没有这个问题，因为它们的归属列里
    本来就有 `area_id`。

    ## 三种范围配置下的取值（与 `tenant_keys_for_create` 逐条一致）

    | 账号的范围 | 归属怎么定 | 为什么 |
    |---|---|---|
    | `allow_all`（全场 / 超管） | 系统里 id 最小的基地 | 全场的意思是**不受限**，不是"无处可归" |
    | 有 farm 型记录 | 取 id 最小的那个基地 | 基地是这个范围**本身**给出的具体事实 |
    | 只有 area 型记录 | 取 id 最小的区域，用它的 `farm_id` | area 型范围的 `bound_id` 就是 `areas.id`，反查即得基地 |
    | 一条范围都没有 | 抛 `DATA_SCOPE_UNRESOLVED` | fail-closed，绝不给默认值 |

    `scope=resource(farm_id)` 是 registry §1.4 给 `area.*` 的声明
    （`areas` 表**没有 `area_id` 列**），本函数解析出的 `farm_id` 正是那条声明
    在写路径上的落点：新区域的 `farm_id` 必须落在账号的 farm 型范围内。
    """
    return _resolve_org_farm(tx, scope)


def tenant_keys_for_create(tx: UnitOfWork, scope: Scope) -> dict[str, int]:
    """解析 create 要写入的三个分租键。

    按范围**逐级**取，取不到具体区域就拒绝（fail-closed，绝不给默认值）：

    | 账号的范围 | 归属怎么定 | 为什么 |
    |---|---|---|
    | 有 area 型记录 | 取 id 最小的那个区域（稳定、可复现），反查 farm / organization | 这是最常见的配置，也是唯一能**确定**归属的配置 |
    | 只有 farm 型记录 | 取 id 最小的那个基地，再取该基地下 id 最小的区域 | "这个基地是我的"，其下区域自然是我的；这是 farm 级账号能建数据的必要条件 |
    | `allow_all`（全场 / 超管） | 取系统里 id 最小的区域 | 全场的意思是**不受限**，不是"无处可归" |
    | 一条范围都没有 | 抛 `DATA_SCOPE_UNRESOLVED` | 这种账号本来就在 `Scope` 构造期被拒（不返回空集） |

    ## 两条"过严"的口径已修正（实测踩到）

    初版只认 area 型记录，另外两种一律 `DATA_SCOPE_UNRESOLVED`。后果是可验证的：

      * **全场账号（`allow_all`）建不了往来单位** —— 而全场账号是最该能建的；
      * **farm 型账号建不了往来单位** —— 同一个账号却能建塘口（`create_pond` 按区域判定），
        两处口径不一致本身就是缺陷。

    为什么可以落到"id 最小的区域"而不是随便挑：区域是四层分租键里**最细**的一层，
    选定它同时确定了 organization / farm / area 三个键，且它是**从数据范围里读出来的**，
    客户端无法影响（§0.7 规则 1：create 不接收三个分租键）。这与
    `cost.entries_write._tenant_from_scope` 的口径一致。
    """
    resolved = _resolve_org_farm(tx, scope)

    # 前两档（全场 / farm 型）到这里已经确定了 org + farm，只剩"区域"要落。
    #
    # 为什么是**两级查询**而不是一次 JOIN：`get_by_id` 那一支要报出"这条范围记录
    # 指向的区域不存在"，那需要知道**是哪一条范围记录**（不是"join 出来是空"）。
    # 二级查询让两条支路的报错各自具体——早期版本这类失败统一的症状就是
    # "查不出数据，也没有任何提示"。
    area_id: int | None = None
    if scope.allow_all:
        # 全场：拿系统里第一个区域。取不到区域说明库是空的——那时也建不出任何业务数据，
        # 报 DATA_SCOPE_UNRESOLVED 是准确的（"无从确定归属"）。
        row = tx.query_one("SELECT id FROM areas ORDER BY id LIMIT 1")
        if row is None:
            raise DomainError(
                ErrorCode.DATA_SCOPE_UNRESOLVED,
                "系统里没有任何区域，无法确定数据归属（请先建立区域）",
            )
        area_id = int(row["id"])
    else:
        farm_ids = sorted(
            entry.bound_id for entry in scope.entries if entry.scope_type is ScopeType.FARM
        )
        if farm_ids:
            row = tx.query_one(
                # 注意：`areas` 表**没有 area_id 列**，能选的只有 id / organization_id / farm_id。
                "SELECT id FROM areas WHERE farm_id = %s ORDER BY id LIMIT 1",
                (farm_ids[0],),
            )
            if row is None:
                raise DomainError(
                    ErrorCode.DATA_SCOPE_UNRESOLVED,
                    f"数据范围里的基地 #{farm_ids[0]} 下一个区域都没有，无法确定数据归属",
                )
            area_id = int(row["id"])

    if area_id is None:
        # area 型范围：`bound_id` 就是 `areas.id`（见 `area_scope_row` 的说明），
        # 所以"哪个区域"就是范围记录自己给的那个。
        area_ids = sorted(
            entry.bound_id for entry in scope.entries if entry.scope_type is ScopeType.AREA
        )
        if area_ids:
            row = tx.query_one("SELECT id FROM areas WHERE id = %s", (area_ids[0],))
            if row is None:
                # 范围里引用了一个不存在的区域 -> 数据范围本身不可解析，必须报错。
                raise DomainError(
                    ErrorCode.DATA_SCOPE_UNRESOLVED,
                    f"数据范围里的区域 #{area_ids[0]} 不存在，无法确定数据归属",
                )
            area_id = int(row["id"])
        else:
            # `Scope` 构造期已保证 entries 非空，所以走到这里只剩 personal 型范围
            # （它没有 farm/area 分租键）。报错比给默认值准确。
            raise DomainError(
                ErrorCode.DATA_SCOPE_UNRESOLVED,
                "无法确定数据归属：当前账号的数据范围不含具体区域或基地，请让管理员分配范围",
            )

    return {**resolved, "area_id": area_id}
