"""master_data 的只读资源：区域、物料、往来单位。

## 为什么三个资源共用一个基类，而塘口单独写

区域 / 物料 / 往来单位在**服务侧**是同一种东西：一个带 `code` / `name` / `status` /
`row_version` 的字典表，读路径只有"分页列表 + 单行详情"。把分页、筛选、排序、
范围谓词各写三遍，就是早期版本 25 处重复分页 SQL 的起点（`.local/recon-backend.md` §2.5）。

而塘口不同：它有双状态机（`status` + `pond_status`）、有 `available_transitions`
派生、有写路径——所以 `ponds.py` 仍然是单独一份。**共用的判据是"读法是否相同"，
不是"资源类型是否相同"。**

## 只有 region.list 的 scope 用 farm_id

`docs/CAPABILITY_REGISTRY.md` §1.4：`area.list` 是 `resource(farm_id)`，其余是
`resource(area_id)`。为什么区域列表按基地受控：`areas` 表**没有 area_id 列**，
它自己就是区域的层级——用 `farm_id` 才是这张表上真实存在的分租列。这条不是实现选择，
是 registry 的声明（`ScopePolicy.resource` 的注释也写明"列名必须存在于资源表"）。

## 列表**默认排除 `archived`**（t18 裁决，写在基类里）

"停用"在本版落地为 `status='archived'`（§0.2：本版 `delete` 能力为 0，删除一律改成
状态迁移）。而这三个资源的列表同时是**两个消费方**：

  * 管理页面（用户要能按 `?status=archived` 把停用的翻出来看 / 复核）；
  * `DynamicForm` 的 `ref` 下拉（`frontend/src/layers/common/ui/DynamicForm.vue`
    请求 `<list_path>?page=1&page_size=100`，**不带任何筛选参数**）。

下拉里出现已停用的区域／物料，就等于"停用"是假的——用户还是能把它选进新建的塘口。
而前端不许改（`ROADMAP.md` §4.3 的元数据驱动边界），所以判据只能落在服务端：

    不带 `status`  → 排除 `archived`
    带 `status=…`  → 按该值等值筛选（`?status=archived` 因此照常能查到停用的）

一条规则、一处实现（`_DEFAULT_EXCLUDED_STATUS` + 本类的 `list_rows`），
不靠前端自觉、也不靠"每个下拉自己记得加参数"。

## 回读函数（`__fpa_load_by_id__`）

只读能力**不需要**回读函数：执行器 `_reload_after` 对 `spec.is_read` 直接返回 None，
`_maybe_load_before` 在没有回读函数时返回 None（而不是报错）。所以这三个服务**刻意不挂**
`__fpa_load_by_id__` —— 挂了也不会被调用，反而会让"哪些能力需要它"这条界线变模糊。
写能力（塘口、往来单位）**必须**挂。界线见 `ROLLOUT_CONTRACT.md` §3。
"""

from __future__ import annotations

from typing import Any

from fpa.kernel.workflow import allowed_actions_for
from fpa.kernel import capability as cap
from fpa.kernel.errors import DomainError, ErrorCode, not_found
from fpa.kernel.money import unit_label
from fpa.kernel.workflow import category_label
from fpa.kernel.scope import Scope, ScopePolicy
from fpa.kernel.uow import UnitOfWork
from fpa.kernel.workflow import RESOURCES, RowAction

from .service import RECORD_LIFECYCLE


class _ReadSpec:
    """一个只读资源的机械参数。**唯一一处**描述这张表怎么读。"""

    __slots__ = ("resource", "title", "view_permission", "table", "alias", "search_columns",
                 "filter_columns", "order_by", "policy", "scope_columns", "farm_column")

    def __init__(
        self,
        *,
        resource: str,
        title: str,
        view_permission: str,
        table: str,
        alias: str,
        search_columns: tuple[str, ...],
        policy: ScopePolicy | None = None,
        scope_columns: tuple[str, ...] = ("area_id", "farm_id", "organization_id"),
        farm_column: str = "",
        filter_columns: tuple[str, ...] = ("status",),
        order_by: str = "{alias}.id DESC",
    ) -> None:
        self.resource = resource
        self.title = title
        self.view_permission = view_permission
        self.table = table
        self.alias = alias
        self.search_columns = search_columns
        # 范围策略必须与能力声明里的 `scope=` **同一个对象**（同一个来源，不可能漂移）。
        # `area` 表没有 `area_id` 列，它的分租列是 `farm_id` —— 这正是由策略表达的事实。
        self.policy = policy or ScopePolicy.resource("area_id")
        # 本表上**真实存在**的分租列（`areas` 没有 area_id）：谓词只渲染这里面的列。
        self.scope_columns = scope_columns
        # 基地表自身以 `id` 承载 farm 范围；不要改全局 SCOPE_COLUMN。
        self.farm_column = farm_column
        self.filter_columns = filter_columns
        self.order_by = order_by


class DictionaryService:
    """字典型只读资源的读写（读 + 回读），供三个资源配置复用。

    子类只需给出 `SPEC`，并可选覆盖 `_decorate_extra` 加派生列。
    服务自己仍然做第三层校验（`ctx.require` + 范围谓词），不信任调用方。
    """

    SPEC: _ReadSpec

    #: 客户端**没有**指定 `?status=` 时被排除的状态码。
    #:
    #: 为什么是"排除一个状态"而不是"只返回 draft/verified 的白名单"：
    #: 白名单会在 `status` 列出现**新状态**时静默把那些行藏起来；而排除写法只影响
    #: `archived` 这一个已裁决的语义（"停用 = 退出业务视图"）。判据是"客户端没说
    #: 要看什么，就别给它看已经停用的"。
    #:
    #: 它不是能力声明，也不进元数据：它是**列表接口的默认语义**，写在这里就是它的
    #: 唯一落点（本文件是区域／物料／往来单位列表的唯一实现）。
    _DEFAULT_EXCLUDED_STATUS: str = "archived"

    # -- 内部工具 -------------------------------------------------------------

    @classmethod
    def _workflow(cls):
        """本资源声明的状态机。**单一来源**：`service.py` 的 `RESOURCES.register`。

        为什么不再硬编码 `RECORD_LIFECYCLE`：只读资源已经改用 `READ_ONLY_LIFECYCLE`
        （只声明 `view`），硬编码会让它们的 `allowed_actions` 与实际声明不一致——
        又是一处"两处描述同一件事"。
        """
        resource = RESOURCES.find(cls.SPEC.resource)
        if resource is not None and resource.workflow is not None:
            return resource.workflow
        return RECORD_LIFECYCLE

    @classmethod
    def _status_label(cls, code: str) -> str:
        for state in cls._workflow().states:
            if state.code == code:
                return state.label
        return code

    @classmethod
    def _select_columns(cls) -> str:
        return f"{cls.SPEC.alias}.*"

    @classmethod
    def _from_clause(cls) -> str:
        """基表 + 别名。**不做任何 JOIN**。

        刻意不 JOIN 区域名：`Resource.columns` 里只有 `area` 声明了 `area_name`
        （见 `service.py` 的 `Resource(name="area", ...)`），而区域行的"所在区域"就是它
        自己。物料与往来单位的列声明里没有 `area_name`，多映一列是"两处描述同一件事"
        的又一处。要区域名时前端按 `area_id` 反查即可（区域列表本来就在菜单里）。
        """
        return f"{cls.SPEC.table} AS {cls.SPEC.alias}"

    @classmethod
    def _decorate(cls, row: dict[str, Any], scope: Scope, permissions: frozenset[str]) -> dict[str, Any]:
        """派生前端需要的三个字段——与 `PondService._decorate` 同一口径。

        * `status_label`：中文文案**只来自状态机**（`State.label`），不在这里翻译第二遍；
        * `allowed_actions`：由状态机算，前端只渲染不推导；
        * `version`：统一叫 `version`（前端在 4 处猜过 `row_version` 这个名字）。
        """
        status = str(row.get("status") or "")
        # 动作从**该资源声明的状态机**派生（不是硬编码 `RECORD_LIFECYCLE`）：
        # 声明处只有一个（`service.py` 的 `RESOURCES.register`），这里读它即可。
        # 只读资源的 `READ_ONLY_LIFECYCLE` 只声明 `view`，所以它们不会渲染出点不动的按钮。
        actions = allowed_actions_for(cls._workflow(), cls.SPEC.resource, status, permissions)
        decorated = dict(row)
        decorated["version"] = int(row.get("row_version") or 1)
        decorated["status_label"] = cls._status_label(status)
        decorated["allowed_actions"] = actions
        # 计量单位的展示标签。`materials.unit` 是无约束的 `VARCHAR(16)`（默认 `kg`），
        # 所以这里对**任何**资源都安全：没有 `unit` 列就不会有这一项。
        # 词表在内核 `UNIT_LABELS`（前端不写映射表）。
        if decorated.get("unit") is not None:
            decorated["unit_label"] = unit_label(str(decorated["unit"]))
        # 物料分类同理：`materials.category` 存的是机器码（`feed`），
        # 直接渲染就是中英文混杂。标签的唯一落点在内核
        # `kernel/workflow.py::CATEGORY_LABELS`（与 `unit_label` / `ACTION_LABELS` 同一条纪律）。
        if decorated.get("category") is not None:
            decorated["category_label"] = category_label(str(decorated["category"]))
        # 往来单位类型同理：`business_partners.partner_type` 存 `supplier`/`customer`，
        # 列表里声明的是 `partner_type_label`（标签「类型」）。
        # **实测缺陷**：这一项原先谁也不算，于是「类型」列整列都是「—」——
        # 用户读作"这列没用，删掉"。词表的唯一落点仍是 `PARTNER_TYPE_LABELS`
        # （延迟 import：`partners_write` 反过来 import 本模块，模块级 import 会成环）。
        if decorated.get("partner_type") is not None:
            from .partners_write import PARTNER_TYPE_LABELS

            code = str(decorated["partner_type"])
            decorated["partner_type_label"] = PARTNER_TYPE_LABELS.get(code, code)
        return decorated

    # -- 读 -------------------------------------------------------------------

    def list_rows(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> cap.HandlerResult:
        """分页列表。**服务自己**做权限与范围校验（第三层防御）。

        `path_params` / `query` 必须**逐字命名**声明：执行器只在处理器签名里真的有这个
        名字时才传（`runner._call_service` 的 `optional in accepts` 裁剪，`runner.py:496`）。
        写成 `**_` 的后果是分页与筛选静默失效 —— 那不是"少传一个参数"，是"页面永远
        只看得到第一页且筛不出东西"，而日志里什么都没有。
        """
        from fpa.domains._base import Page

        ctx.require(self.SPEC.view_permission)
        params = query or {}
        page = Page.parse(params.get("page"), params.get("page_size"))
        alias = self.SPEC.alias

        where = ["1=1"]
        values: list[Any] = []
        # 范围谓词：只渲染**本表真实存在的分租列**对应的范围类型。
        #
        # 为什么不用 `scope.where_clause()` 一把梭：它按账号持有的**所有**类型渲染
        # （area 型 -> `t.area_id IN (...)`），而 `areas` 表没有 `area_id` 列 ——
        # 对 area 型账号查询区域列表会拼出 `Unknown column 'r.area_id'`（实测）。
        # 也不用能力声明的 `ScopePolicy.render()`：它对"账号持有 farm 型范围、策略列是
        # area_id"这个**正常组合**会抛 `DATA_SCOPE_UNRESOLVED`（把正常配置当配置错误）。
        # 所以这里只问一件事：本表的这一列上，这个账号有没有对应的范围记录。
        scope_fragment, scope_values = self._scope_predicate(scope, alias)
        if scope_fragment != "1=1":
            where.append(f"({scope_fragment})")
            values.extend(scope_values)

        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            pieces = [f"{alias}.{column} LIKE %s" for column in self.SPEC.search_columns]
            where.append("(" + " OR ".join(pieces) + ")")
            values.extend([f"%{keyword}%"] * len(self.SPEC.search_columns))

        requested_status = str(params.get("status") or "").strip()
        for column in self.SPEC.filter_columns:
            raw = str(params.get(column) or "").strip()
            if raw:
                where.append(f"{alias}.{column} = %s")
                values.append(raw)
        if (
            self._DEFAULT_EXCLUDED_STATUS
            and not requested_status
            and "status" in self.SPEC.filter_columns
        ):
            # 默认视图排除 `archived`。显式给了 `?status=` 时上面的循环已经写了
            # 等值条件，这里**不再追加**——两条同时生效会得到
            # `status='archived' AND status<>'archived'`，即永远空集。
            where.append(f"{alias}.status <> %s")
            values.append(self._DEFAULT_EXCLUDED_STATUS)

        clause = " AND ".join(where)
        total_row = tx.query_one(
            f"SELECT COUNT(*) AS n FROM {self.SPEC.table} AS {alias} WHERE {clause}", values
        )
        total = int((total_row or {}).get("n", 0))

        rows = tx.query_all(
            f"SELECT {self._select_columns()} FROM {self._from_clause()} "
            f"WHERE {clause} ORDER BY {self.SPEC.order_by.format(alias=alias)} "
            "LIMIT %s OFFSET %s",
            [*values, page.size, page.offset],
        )
        return cap.HandlerResult(
            data=page.to_result(
                [self._decorate(row, scope, ctx.actor.permissions) for row in rows], total
            ),
            message="",
        )

    def get_row_by_id(
        self,
        tx: UnitOfWork,
        ctx,
        scope: Scope,
        path_params: dict[str, Any] | None = None,
    ) -> cap.HandlerResult:
        """单行详情。路径参数名由能力声明的 `{xxx_id}` 决定（如 `{area_id}`）。"""
        ctx.require(self.SPEC.view_permission)
        path_key = f"{self.SPEC.resource}_id"
        raw = (path_params or {}).get(path_key)
        if raw is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"{self.SPEC.title}详情需要 path_params 传 {path_key}",
            )
        row = self._scope_row(tx, scope, int(raw))
        return cap.HandlerResult(
            data={"record": self._decorate(row, scope, ctx.actor.permissions)},
        )

    # -- 回读（写路径用） ------------------------------------------------------

    @classmethod
    def _scope_predicate(cls, scope: Scope, alias: str) -> tuple[str, list[Any]]:
        """按**每条范围记录自己的分租列**渲染谓词；本表不存在的列不参与。

        实现已上提到内核：`kernel/scope.py::scope_predicate_for_columns`。
        **本方法只做一次委托，不再自己拼 SQL**——t18 把 `warehouse.inventory` 也接到
        同一个函数上时发现，两处实现已经分叉了（它走 `Scope.predicate()`，
        对 `areas` / `warehouses` 这类缺列的表的账号会拼出 `Unknown column`）。
        按本仓纪律"两处描述同一件事就删掉一处"：判据留在内核**一处**，
        各域只声明"我这张表有哪些分租列"（`SPEC.scope_columns`）。

        `"1=0"` 是**正常结果**而不是错误：`areas` 表没有 `area_id` 列，所以只持有
        区域型范围的账号在区域列表上确实看不到东西（这与 `area.get` 对它 403 一致）。
        """
        from fpa.kernel.scope import scope_predicate_for_columns

        return scope_predicate_for_columns(
            scope, cls.SPEC.scope_columns, alias, farm_column=cls.SPEC.farm_column
        )

    @classmethod
    def _allows_row(cls, scope: Scope, row: dict[str, Any]) -> bool:
        """单行判定。`allows_row` 按范围类型的**同名列**取值，所以对每一列都问一次：

        * 声明的策略列（如 `area_id`）—— 用该列对应的范围类型；
        * 本行的 `farm_id` / `organization_id` —— 让 farm 型 / 全场范围也能命中。
        三条都不命中才拒绝（fail-closed）。
        """
        for column in (cls.SPEC.policy.column, cls.SPEC.policy.owner_column):
            if column and scope.allows_row({column: row.get(column)}):
                return True
        if cls.SPEC.farm_column and scope.allows_row({"farm_id": row.get(cls.SPEC.farm_column)}):
            return True
        for column in ("farm_id", "organization_id"):
            if row.get(column) is not None and scope.allows_row({column: row.get(column)}):
                return True
        return False

    @classmethod
    def _scope_row(cls, tx: UnitOfWork, scope: Scope, record_id: int) -> dict[str, Any]:
        """按主键取一行**并校验它在本账号范围内**（第三层防御的关键一步）。

        判定的是**这一行数据**属不属于当前用户，而不是"令牌对不对"——
        两种校验的失效模式不同，第二层被绕过时这一层仍然拦得住。
        """
        alias = cls.SPEC.alias
        row = tx.query_one(
            f"SELECT {cls._select_columns()} FROM {cls._from_clause()} "
            f"WHERE {alias}.id = %s",
            (record_id,),
        )
        if row is None:
            raise not_found(cls.SPEC.title)
        if not cls._allows_row(scope, row):
            raise DomainError(
                ErrorCode.DATA_SCOPE_DENIED, f"该{cls.SPEC.title}不在当前账号的数据范围内"
            )
        return row


# ---------------------------------------------------------------------------
# 三个资源的声明（每条一行参数，读法全部继承基类）
# ---------------------------------------------------------------------------

AREA_SPEC = _ReadSpec(
    resource="area",
    title="区域",
    view_permission="area.view",
    table="areas",
    alias="r",
    search_columns=("code", "name"),
    # registry §1.4：`area.list` 是 `resource(farm_id)` —— `areas` 表没有 area_id 列。
    policy=ScopePolicy.resource("farm_id"),
    scope_columns=("farm_id", "organization_id"),
    order_by="r.id ASC",
)

MATERIAL_SPEC = _ReadSpec(
    resource="material",
    title="物料",
    view_permission="material.view",
    table="materials",
    alias="r",
    search_columns=("code", "name", "spec"),
    policy=ScopePolicy.resource("area_id"),
    scope_columns=("area_id", "farm_id", "organization_id"),
    filter_columns=("status", "category"),
    order_by="r.id DESC",
)

PARTNER_SPEC = _ReadSpec(
    resource="partner",
    title="往来单位",
    view_permission="partner.view",
    table="business_partners",
    alias="r",
    search_columns=("code", "name", "contact_name", "phone"),
    policy=ScopePolicy.resource("area_id"),
    scope_columns=("area_id", "farm_id", "organization_id"),
    filter_columns=("status", "partner_type"),
    order_by="r.id DESC",
)

FARM_SPEC = _ReadSpec(
    resource="farm",
    title="基地",
    view_permission="farm.view",
    table="farms",
    alias="f",
    search_columns=("code", "name"),
    policy=ScopePolicy.none(),
    scope_columns=("id", "organization_id"),
    farm_column="id",
    order_by="f.id ASC",
)


def _label_of(code: str) -> str:  # noqa: D103 - 见下方说明（保留给模块级调用）
    for state in RECORD_LIFECYCLE.states:
        if state.code == code:
            return state.label
    return code


class FarmService(DictionaryService):
    """基地读服务，基地键由 `farms.id` 承载。"""

    SPEC = FARM_SPEC

    def list_farms(
        self, tx, ctx, scope, path_params=None, query=None
    ) -> cap.HandlerResult:
        return self.list_rows(tx, ctx, scope, path_params, query)

    def get_farm_by_id(
        self, tx, ctx, scope, path_params=None
    ) -> cap.HandlerResult:
        return self.get_row_by_id(tx, ctx, scope, path_params)

    @classmethod
    def load_farm(cls, tx: UnitOfWork, *, scope: Scope, record_id: int) -> dict[str, Any] | None:
        row = tx.query_one("SELECT f.* FROM farms AS f WHERE f.id = %s", (record_id,))
        if row is None:
            return None
        return {**row, "version": int(row.get("row_version") or 1)}


class AreaService(DictionaryService):
    """区域：只读（区域由种子/迁移建立，本版没有 area.create，见 registry §1.4）。"""

    SPEC = AREA_SPEC

    def list_areas(
        self, tx, ctx, scope, path_params=None, query=None
    ) -> cap.HandlerResult:
        return self.list_rows(tx, ctx, scope, path_params, query)

    def get_area_by_id(
        self, tx, ctx, scope, path_params=None
    ) -> cap.HandlerResult:
        return self.get_row_by_id(tx, ctx, scope, path_params)

    @classmethod
    def load_area(cls, tx: UnitOfWork, *, scope: Scope, record_id: int) -> dict[str, Any] | None:
        """回读函数（本域暂时没有 area 的写能力；留着是为了"区域成为可写资源"时不必再改形状）。"""
        row = tx.query_one(
            "SELECT r.* FROM areas AS r WHERE r.id = %s", (record_id,)
        )
        if row is None:
            return None
        return {**row, "version": int(row.get("row_version") or 1)}


class MaterialService(DictionaryService):
    """物料：只读（registry §1.4 明确裁到只读：物料是投喂/出入库的引用对象）。"""

    SPEC = MATERIAL_SPEC

    def list_materials(
        self, tx, ctx, scope, path_params=None, query=None
    ) -> cap.HandlerResult:
        return self.list_rows(tx, ctx, scope, path_params, query)

    def get_material_by_id(
        self, tx, ctx, scope, path_params=None
    ) -> cap.HandlerResult:
        return self.get_row_by_id(tx, ctx, scope, path_params)

    @classmethod
    def load_material(cls, tx: UnitOfWork, *, scope: Scope, record_id: int) -> dict[str, Any] | None:
        row = tx.query_one("SELECT r.* FROM materials AS r WHERE r.id = %s", (record_id,))
        if row is None:
            return None
        return {**row, "version": int(row.get("row_version") or 1)}


class PartnerService(DictionaryService):
    """往来单位：读 + 回读。写路径在 `partners_write.py`。"""

    SPEC = PARTNER_SPEC

    def list_partners(
        self, tx, ctx, scope, path_params=None, query=None
    ) -> cap.HandlerResult:
        return self.list_rows(tx, ctx, scope, path_params, query)

    def get_partner_by_id(
        self, tx, ctx, scope, path_params=None
    ) -> cap.HandlerResult:
        return self.get_row_by_id(tx, ctx, scope, path_params)

    @classmethod
    def load_partner(cls, tx: UnitOfWork, *, scope: Scope, record_id: int) -> dict[str, Any] | None:
        """回读函数：执行器在提交前调它确认"写入真的落了库"。"""
        row = tx.query_one(
            "SELECT r.* FROM business_partners AS r WHERE r.id = %s", (record_id,)
        )
        if row is None:
            return None
        return {**row, "version": int(row.get("row_version") or 1)}
