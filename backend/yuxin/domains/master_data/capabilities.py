"""主数据域的能力声明。

**这是"声明即派生"的落点**：一个 `Capability(...)` 同时派生

    REST 路由（`web/app.py` 按 `method` + `path` 自动注册）
    权限码（`required_permission`）
    DataScope 谓词（`scope`）
    幂等策略（`idempotent`）
    人工确认闸门（`risk` + `confirmation`）
    审计字段（`audit`）
    Agent Tool schema（`agent_exposure` + 从处理器标注收集的字段）
    前端表单与列（`fields` + 资源声明的 `columns`）

所以这个文件里**不写**任何路由、校验、权限判断的代码——那些都是派生物。

## 一个刻意的省略：写能力的 read 方法

`pond.create` 的 `kind="create"`、`pond.list` 的 `kind="read"`。
`kind` 影响 `required_create_fields()` 的行为（create 强制必填、update 不强制），
所以它必须准确。
"""

from __future__ import annotations

from yuxin.kernel.capability import (
    AgentExposure,
    AuditPolicy,
    Capability,
    Confirmation,
    HttpMethod,
    REGISTRY,
    Risk,
)
from yuxin.kernel.fields import f_enum, f_int, f_ref, f_str, text
from yuxin.kernel.invariants import (
    AtMostOnePending,
    DistinctActors,
    OptimisticLock,
    RequiredField,
    StateTransition,
    StatusAllowsEdit,
    UniqueCode,
)
from yuxin.kernel.scope import ScopePolicy

from .masterdata_write import AreaWriteService, FarmWriteService, MaterialWriteService
from .ponds import PondService
from .ponds import confirmation_labels as pond_confirmation_labels
from .ponds_write import PondWriteService
from .resources_read import AreaService, FarmService, MaterialService, PartnerService
from .partners_write import PartnerWriteService
from .service import EDITABLE_LIFECYCLE, POND_STATUS_WORKFLOW

#: 写能力默认对智能体开放（README 的裁决：不额外阉割）。
#:
#: 需求明确要求"新建塘口、投喂、采购、销售这类普通业务"不能被随意标 human_only。
#: 管控靠 L1 权限 + DataScope + 不变量 + 确认闸门，而不是靠"不让 Agent 碰"。
_EXPOSED = AgentExposure.EXPOSED

#: 读能力用 common 权限码。
#:
#: 为什么读用一个码、写用细分的码：读操作的权限粒度太细会让角色配置变得无法管理
#: （几十个 `*.view`），而写操作需要细分（谁能核验、谁不能）。
#: 这是早期版本 `master_data.view` / `production.view` 的做法，继承。
def _register_all() -> None:
    """登记主数据域的全部能力。

    用函数包裹而不是模块级裸语句：重复导入这个模块（例如测试里 reload）
    不会因为"能力重复注册"而炸——`REGISTRY.register` 对重名是显式抛错的。
    """
    if REGISTRY.find("pond.create") is not None:
        return  # 已注册（重复导入）

    # ---------------------------------------------------------------- 塘口读

    REGISTRY.register(
        Capability(
            name="farm.list",
            title="基地列表",
            domain="master_data",
            resource="farm",
            handler=FarmService.list_farms,
            service_factory=FarmService,
            method=HttpMethod.GET,
            path="/api/v1/farms",
            kind="read",
            required_permission="farm.view",
            scope=ScopePolicy.resource("farm_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词与状态筛选当前数据范围内的基地",
        )
    )

    REGISTRY.register(
        Capability(
            name="farm.get",
            title="基地详情",
            domain="master_data",
            resource="farm",
            handler=FarmService.get_farm_by_id,
            service_factory=FarmService,
            method=HttpMethod.GET,
            path="/api/v1/farms/{farm_id}",
            kind="read",
            required_permission="farm.view",
            scope=ScopePolicy.resource("farm_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="查询单个基地的完整信息",
        )
    )

    REGISTRY.register(
        Capability(
            name="farm.create",
            title="新建基地",
            domain="master_data",
            resource="farm",
            loader=FarmService.load_farm,
            handler=FarmWriteService.create_farm,
            service_factory=FarmWriteService,
            method=HttpMethod.POST,
            path="/api/v1/farms",
            kind="create",
            required_permission="farm.create",
            scope=ScopePolicy.none(),
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                UniqueCode(
                    fields=("code",),
                    scope=("organization_id",),
                    table="farms",
                    labels=("基地编号",),
                ),
            ),
            description="在当前启用企业下新建基地；仅全数据范围账号可执行",
        )
    )

    REGISTRY.register(
        Capability(
            name="pond.list",
            title="塘口列表",
            domain="master_data",
            resource="pond",
            handler=PondService.list_ponds,
            service_factory=PondService,
            method=HttpMethod.GET,
            path="/api/v1/ponds",
            kind="read",
            required_permission="pond.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、记录状态、塘口业务状态筛选塘口列表",
        )
    )

    REGISTRY.register(
        Capability(
            name="pond.get",
            title="塘口详情",
            domain="master_data",
            resource="pond",
            handler=PondService.get_pond_by_id,
            service_factory=PondService,
            method=HttpMethod.GET,
            path="/api/v1/ponds/{pond_id}",
            kind="read",
            required_permission="pond.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="查询单个塘口的完整信息，含当前状态与可用的状态变更目标",
        )
    )

    # ------------------------------------------------- 区域 / 物料 / 往来单位
    # 这三条读能力对应 registry §1.4 里此前"文档有、代码无"的缺口。
    # 表在 003 迁移里，`service.py` 的 RESOURCES 也已声明三个资源——只缺服务与声明。

    REGISTRY.register(
        Capability(
            name="area.list",
            title="区域列表",
            domain="master_data",
            resource="area",
            handler=AreaService.list_areas,
            service_factory=AreaService,
            method=HttpMethod.GET,
            path="/api/v1/areas",
            kind="read",
            required_permission="area.view",
            # registry §1.4 给的是 resource(farm_id)：`areas` 表**没有 area_id 列**，
            # farm_id 才是这张表上真实存在的分租列。
            scope=ScopePolicy.resource("farm_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词与记录状态筛选区域列表（数据范围按基地）",
        )
    )

    REGISTRY.register(
        Capability(
            name="area.get",
            title="区域详情",
            domain="master_data",
            resource="area",
            handler=AreaService.get_area_by_id,
            service_factory=AreaService,
            method=HttpMethod.GET,
            path="/api/v1/areas/{area_id}",
            kind="read",
            required_permission="area.view",
            scope=ScopePolicy.resource("farm_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="查询单个区域的完整信息",
        )
    )

    # ------------------------------------------------------ 区域写（t18 新增）
    #
    # registry §1.4 原来只给区域 list / get，理由是"区域由种子/迁移建立"。
    # t18 推翻该口径：本版**没有任何可用的种子/导入管线**（仓库里只有 003 迁移的
    # 两条演示区域，`database/seeds/` 下没有区域种子），而用户在验收里报"缺少"
    # 的正是"建不了区域"。裁决与理由见 `docs/DECISIONS.md` Q21。

    REGISTRY.register(
        Capability(
            name="area.create",
            title="新建区域",
            domain="master_data",
            resource="area",
            loader=AreaService.load_area,
            handler=AreaWriteService.create_area,
            service_factory=AreaWriteService,
            method=HttpMethod.POST,
            path="/api/v1/areas",
            kind="create",
            required_permission="area.create",
            # registry §0.3：`areas` 表**没有 area_id 列**，`farm_id` 才是这张表上
            # 真实存在的分租列（与 `area.list` / `area.get` 同一口径）。
            scope=ScopePolicy.resource("farm_id"),
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            # §0.5：`kind=create` ⇒ 强制携带 `Idempotency-Key`（机械派生，不手写）。
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # DB 唯一键 `uq_areas_farm_code(farm_id, code)` 是并发下的正确性底线；
                # 本不变量负责给出**可读的** 409 而不是数据库报错。两层都要有
                # （registry #19 / `INVARIANT_TYPES.md` §4）。
                UniqueCode(
                    fields=("code",),
                    scope=("farm_id",),
                    table="areas",
                    labels=("区域编号",),
                ),
            ),
            description="在指定基地下新建区域。编号在基地内唯一；数据归属由服务端按数据范围解析",
        )
    )

    REGISTRY.register(
        Capability(
            name="area.update",
            title="编辑区域",
            domain="master_data",
            resource="area",
            loader=AreaService.load_area,
            handler=AreaWriteService.update_area,
            service_factory=AreaWriteService,
            method=HttpMethod.PATCH,
            path="/api/v1/areas/{area_id}",
            kind="update",
            required_permission="area.update",
            scope=ScopePolicy.resource("farm_id"),
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            audit=AuditPolicy.snapshot(),
            description="修改区域的名称。区域编号不可修改；已核验/已归档的区域不能改",
            invariants=(
                # #13 乐观锁：`UPDATE ... WHERE row_version=%s` 的实际写入在服务里，
                # 这里负责在**写库之前**给出可读的 409（"该区域已被他人修改"）。
                OptimisticLock(label="该区域"),
                # #12 核验后记录只读（默认可编辑集合 = draft / submitted，
                # `INVARIANT_TYPES.md`）。**这一条不是装饰**：即使状态机的
                # `EDIT` 动作被绕过，它也会拦住"改一条已核验的区域"。
                StatusAllowsEdit(label="区域"),
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="area.archive",
            title="停用区域",
            domain="master_data",
            resource="area",
            loader=AreaService.load_area,
            handler=AreaWriteService.archive_area,
            service_factory=AreaWriteService,
            method=HttpMethod.POST,
            path="/api/v1/areas/{area_id}/archive",
            kind="action",
            required_permission="area.archive",
            scope=ScopePolicy.resource("farm_id"),
            # ## 为什么 `risk=HIGH`
            #
            # §0.4 的判据只有三条：不可逆 / 涉及金额 / 涉及权限或身份。
            # 停用主数据命中第①条，而且是**结构性**的不可逆：
            #   * 归档后该区域从默认列表与所有 `ref` 下拉里消失，
            #     于是"新建塘口时选它"这条路径**在界面上不复存在**；
            #   * 本版没有"取消归档"能力（`archived` 是终态，`EDITABLE_LIFECYCLE`
            #     的转移表里没有任何出边），要恢复只能改库；
            #   * 它**锁掉**引用它的业务（新塘口建不进这个区域）。
            # 而 §0.4 的机械约束 `high ∧ action ⇒ always` 于是自动给出
            # `confirmation=always`（Agent 每次调用都要一次性确认令牌）。
            risk=Risk.HIGH,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            description="停用（归档）一个区域：它退出默认列表与所有下拉候选，但不能被物理删除",
            invariants=(
                OptimisticLock(label="该区域"),
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="material.list",
            title="物料列表",
            domain="master_data",
            resource="material",
            handler=MaterialService.list_materials,
            service_factory=MaterialService,
            method=HttpMethod.GET,
            path="/api/v1/materials",
            kind="read",
            required_permission="material.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、分类与记录状态筛选物料列表",
        )
    )

    REGISTRY.register(
        Capability(
            name="material.get",
            title="物料详情",
            domain="master_data",
            resource="material",
            handler=MaterialService.get_material_by_id,
            service_factory=MaterialService,
            method=HttpMethod.GET,
            path="/api/v1/materials/{material_id}",
            kind="read",
            required_permission="material.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="查询单个物料的完整信息",
        )
    )

    # ------------------------------------------------------ 物料写（t18 新增）
    #
    # ⚠️ **这一批推翻了 registry §1.4 原来的一处明确裁决**，原文是：
    #     "`materials` 保留**只读**：物料是投喂/出入库的引用对象，由种子或导入建立；
    #      `material.verify` 砍掉（见 §6）。"
    # 推翻的理由（三条都经核实，不是转述）记在 `docs/DECISIONS.md` Q21。
    # 摘要：本版**没有可用的种子/导入管线**（核实：`database/seeds/` 下没有物料种子，
    # 003 迁移只种 2 条演示饲料且只有 `INSERT ... SELECT` 一种建立方式）；
    # 用户报"缺少"；而"能新增物料"**不削弱**原裁决的只读意图——原裁决要的是
    # "物料的**业务状态**不该被随意推动"（所以 `material.verify` 仍然砍掉），
    # 要的不是"台账字段不可维护"。

    REGISTRY.register(
        Capability(
            name="material.create",
            title="新建物料",
            domain="master_data",
            resource="material",
            loader=MaterialService.load_material,
            handler=MaterialWriteService.create_material,
            service_factory=MaterialWriteService,
            method=HttpMethod.POST,
            path="/api/v1/materials",
            kind="create",
            required_permission="material.create",
            # `materials` 表有 `area_id` 列（003 迁移），所以策略列就是它。
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # DB 唯一键是 `uq_materials_org_code(organization_id, code)` ——
                # **企业内**唯一，不按基地/区域切分。声明必须与那个键**逐字一致**：
                # 只写 `area_id` 会漏判"同企业另一个区域下已有同编号"，
                # 那时用户拿到的是数据库报错而不是可读的 409。
                UniqueCode(
                    fields=("code",),
                    scope=("organization_id",),
                    table="materials",
                    labels=("物料编号",),
                ),
            ),
            description="新建物料。编号在企业内唯一；数据归属由服务端按数据范围解析",
        )
    )

    REGISTRY.register(
        Capability(
            name="material.update",
            title="编辑物料",
            domain="master_data",
            resource="material",
            loader=MaterialService.load_material,
            handler=MaterialWriteService.update_material,
            service_factory=MaterialWriteService,
            method=HttpMethod.PATCH,
            path="/api/v1/materials/{material_id}",
            kind="update",
            required_permission="material.update",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            audit=AuditPolicy.snapshot(),
            description="修改物料的名称、分类、规格、单位、单价等。物料编号不可修改；已归档的物料不能改",
            invariants=(
                OptimisticLock(label="该物料"),
                StatusAllowsEdit(label="物料"),
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="material.archive",
            title="停用物料",
            domain="master_data",
            resource="material",
            loader=MaterialService.load_material,
            handler=MaterialWriteService.archive_material,
            service_factory=MaterialWriteService,
            method=HttpMethod.POST,
            path="/api/v1/materials/{material_id}/archive",
            kind="action",
            required_permission="material.archive",
            scope=ScopePolicy.resource("area_id"),
            # 与 `area.archive` 同一判据（不可逆 + 退出候选），见那一处的长注释。
            risk=Risk.HIGH,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            description=(
                "停用（归档）一个物料：它退出默认列表与所有下拉候选，"
                "新的领用/到货选不到它；已核验的历史单据不受影响"
            ),
            invariants=(
                OptimisticLock(label="该物料"),
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="material.submit",
            title="提交物料核验",
            domain="master_data",
            resource="material",
            loader=MaterialService.load_material,
            handler=MaterialWriteService.submit_material,
            service_factory=MaterialWriteService,
            method=HttpMethod.POST,
            path="/api/v1/materials/{material_id}/submit",
            kind="action",
            required_permission="material.submit",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.summary(),
            invariants=(
                OptimisticLock(label="该物料"),
                StateTransition(machine="material"),
            ),
            description="把草稿物料提交核验",
        )
    )

    REGISTRY.register(
        Capability(
            name="material.verify",
            title="核验物料",
            domain="master_data",
            resource="material",
            loader=MaterialService.load_material,
            handler=MaterialWriteService.verify_material,
            service_factory=MaterialWriteService,
            method=HttpMethod.POST,
            path="/api/v1/materials/{material_id}/verify",
            kind="action",
            required_permission="material.verify",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.HIGH,
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                OptimisticLock(label="该物料"),
                StateTransition(machine="material"),
                # #5 经办人 ≠ 核验人。**实测缺陷**：物料原只挂了前两条，于是建物料
                # 的人可以自己「核验」自己 —— 一个能点、能成功的静默越权
                # （其余 verify/approve 都挂了本条）。判据在内核，人工页面与 Agent
                # 走的是同一份（Q8）。
                DistinctActors(),
            ),
            description="核验已提交的物料；执行前必须由当前登录用户确认",
        )
    )

    REGISTRY.register(
        Capability(
            name="partner.list",
            title="往来单位列表",
            domain="master_data",
            resource="partner",
            handler=PartnerService.list_partners,
            service_factory=PartnerService,
            method=HttpMethod.GET,
            path="/api/v1/partners",
            kind="read",
            required_permission="partner.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="按关键词、类型与记录状态筛选往来单位（供应商与客户合并为一张表）",
        )
    )

    REGISTRY.register(
        Capability(
            name="partner.get",
            title="往来单位详情",
            domain="master_data",
            resource="partner",
            handler=PartnerService.get_partner_by_id,
            service_factory=PartnerService,
            method=HttpMethod.GET,
            path="/api/v1/partners/{partner_id}",
            kind="read",
            required_permission="partner.view",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.READ,
            agent_exposure=_EXPOSED,
            description="查询单个往来单位的完整信息",
        )
    )

    REGISTRY.register(
        Capability(
            name="partner.create",
            title="新建往来单位",
            domain="master_data",
            resource="partner",
            loader=PartnerService.load_partner,
            handler=PartnerWriteService.create_partner,
            service_factory=PartnerWriteService,
            method=HttpMethod.POST,
            path="/api/v1/partners",
            kind="create",
            required_permission="partner.create",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            invariants=(
                # DB 唯一键 `uq_partners_org_type_code`(organization_id, partner_type, code)
                # 是并发下的正确性底线；本不变量负责给出**可读的** 409 而不是数据库报错。
                # 两层都要有（registry #19 / INVARIANT_TYPES §4）。
                UniqueCode(
                    fields=("code",),
                    scope=("organization_id", "partner_type"),
                    table="business_partners",
                    labels=("单位编号",),
                ),
            ),
            description="新建供应商或客户。编号在同一企业、同一类型内唯一",
        )
    )

    # ---------------------------------------------------------------- 塘口写

    REGISTRY.register(
        Capability(
            name="pond.create",
            title="新建塘口",
            domain="master_data",
            resource="pond",
            loader=PondService.load_pond,
            handler=PondWriteService.create_pond,
            service_factory=PondWriteService,
            method=HttpMethod.POST,
            path="/api/v1/ponds",
            kind="create",
            required_permission="pond.create",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            description="在指定区域下新建一个塘口，初始业务状态只能是待建设或已放苗",
            invariants=(
                # registry #19：编码在范围内唯一。DB 唯一键 `uq_ponds_farm_code(farm_id, code)`
                # 是并发下的正确性底线（单靠应用层"先查再插"做不到），本不变量负责给出
                # 可读的 409 而不是让用户看到数据库报错。两层都要有。
                UniqueCode(fields=("code",), scope=("farm_id",), labels=("塘口编号",)),
                # registry #4 #21「同一塘口只能有一个待核验状态变更」的**不变量层**。
                #
                # ★ 它挂在这里，而不是挂 `pond_status_change.request` —— 后者**结构上挂不了**：
                #   该能力的 `pond_id` 只在**路径**里、不在字段 schema 里，而
                #   `AtMostOnePending.check()` 的触发条件是"payload 里带齐 `dims`"，
                #   于是那条声明永不触发（且即使触发也不可达：状态机每个状态的出边只有
                #   1–2 条，同塘口第二次申请会先被 `require_transition` 拒）。
                #   详见 §4 #21 的「替代强制」段与 `[B-声明]`。
                #
                # 为什么 `pond.create` 是**真实**的挂载点（不是为凑数）：
                #   * `pond_id` 是它本次写入的行的主键（`resolver_id` = 路径参数或
                #     `resource_id`），所以 `dims=("pond_id",)` 能取到值、判定会真的执行；
                #   * 它建的是**同一层级**的行（塘口），与 #21 约束的归属对象一致；
                #   * 正常路径（新塘口）必然放行 —— 新建的塘口不可能有 pending 申请，
                #     所以它不会拦住任何合法创建；一旦"塘口上已经有待核验变更申请"，
                #     它给出一条可读的 CONFLICT，而不是让后续流程踩到半截状态。
                AtMostOnePending(
                    dims=("pond_id",),
                    table="pond_status_change_requests",
                    status_field="status",
                    pending_states=("submitted",),
                    label="待核验的塘口状态变更申请",
                ),
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="pond.update",
            title="修改塘口",
            domain="master_data",
            resource="pond",
            loader=PondService.load_pond,
            handler=PondWriteService.update_pond,
            service_factory=PondWriteService,
            method=HttpMethod.PATCH,
            path="/api/v1/ponds/{pond_id}",
            kind="update",
            required_permission="pond.update",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            # 修改是高风险的（需求点名"创建、修改、审核"），所以显式要求确认。
            # 不写 confirmation 的话 risk=NORMAL 默认为 NEVER——那与需求不符。
            confirmation=Confirmation.ALWAYS,
            agent_exposure=_EXPOSED,
            idempotent=True,
            confirmation_labels=pond_confirmation_labels,
            audit=AuditPolicy.snapshot(),
            description="修改塘口的台账信息（名称、区域、品种、面积等）。塘口业务状态不能在这里改，需走两步审批",
            invariants=(
                StatusAllowsEdit(label="塘口"),                 # #12 核验后记录只读
                OptimisticLock(label="该塘口"),                 # #13 乐观锁
                StateTransition(machine="pond"),                # #14 记录生命周期转移合法
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="pond.submit",
            title="提交塘口核验",
            domain="master_data",
            resource="pond",
            loader=PondService.load_pond,
            handler=PondWriteService.submit_pond,
            service_factory=PondWriteService,
            method=HttpMethod.POST,
            path="/api/v1/ponds/{pond_id}/submit",
            kind="action",
            required_permission="pond.submit",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            idempotent=True,
            # registry 给本行的 `audit` 列是 **summary**（提交是轻量动作，不记 before/after 全量 diff）。
            # 原写 `snapshot()`，与文档不一致：`submit` 只把 status 从 draft 推到 submitted，
            # 那次状态转移本身就写在审计的 `reason` / `object_ref` 里，再记一份整行 diff
            # 只会让审计表膨胀。四领域的同族动作（含 `sales_order.submit`，registry §1.8）统一为 summary——
            # 让三个 `*.submit` 记全量 diff 而第四个不记，是「同一件事两处不一样」的典型。
            audit=AuditPolicy.summary(),
            description="把草稿状态的塘口提交给他人核验",
            invariants=(
                OptimisticLock(label="该塘口"),
                StateTransition(machine="pond"),
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="pond.verify",
            title="核验塘口",
            domain="master_data",
            resource="pond",
            loader=PondService.load_pond,
            handler=PondWriteService.verify_pond,
            service_factory=PondWriteService,
            method=HttpMethod.POST,
            path="/api/v1/ponds/{pond_id}/verify",
            kind="action",
            required_permission="pond.verify",
            scope=ScopePolicy.resource("area_id"),
            # 审核是高风险的（需求点名），所以 HIGH -> 默认 ALWAYS
            risk=Risk.HIGH,
            agent_exposure=_EXPOSED,
            idempotent=True,
            confirmation_labels=pond_confirmation_labels,
            audit=AuditPolicy.snapshot(),
            description="核验他人提交的塘口。经办人不能核验自己提交的",
            invariants=(
                OptimisticLock(label="该塘口"),
                StateTransition(machine="pond"),
                # #5 经办人 ≠ 审批人。**这一条以前是服务里手写的
                # `if int(row["created_by"]) == ctx.actor.user_id: raise FORBIDDEN`**，
                # 现在由内核统一执行 —— 人工页面与 Agent 走的是同一份判断（Q8）。
                DistinctActors(),
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="pond.archive",
            title="归档塘口",
            domain="master_data",
            resource="pond",
            loader=PondService.load_pond,
            handler=PondWriteService.archive_pond,
            service_factory=PondWriteService,
            method=HttpMethod.POST,
            path="/api/v1/ponds/{pond_id}/archive",
            kind="action",
            required_permission="pond.archive",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.HIGH,
            agent_exposure=_EXPOSED,
            idempotent=True,
            confirmation_labels=pond_confirmation_labels,
            audit=AuditPolicy.snapshot(),
            description="归档草稿或已核验的塘口。归档不是删除——数据保留，只是退出业务视图",
            invariants=(
                OptimisticLock(label="该塘口"),
                StateTransition(machine="pond"),
            ),
        )
    )

    # -------------------------------------------------------- 塘口业务状态两步审批

    REGISTRY.register(
        Capability(
            name="pond_status_change.request",
            title="发起塘口状态变更",
            domain="master_data",
            resource="pond",
            loader=PondWriteService.load_pond_status_change_request,
            handler=PondWriteService.request_pond_status_change,
            service_factory=PondWriteService,
            method=HttpMethod.POST,
            path="/api/v1/ponds/{pond_id}/status-changes",
            kind="action",
            required_permission="pond.status.request",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.NORMAL,
            agent_exposure=_EXPOSED,
            idempotent=True,
            audit=AuditPolicy.snapshot(),
            description="为塘口发起业务状态变更申请。这一步不改状态，只建申请，等他人核验",
            invariants=(
                OptimisticLock(label="该塘口", expect="expected_pond_version"),
                RequiredField(fields=("reason",), labels=("变更原因",)),   # #20 原因必填
                # #21「同一塘口只能有一个待核验申请」**刻意不声明为不变量**，
                # 因为它的强制点**结构上不是不变量**，而且内核声明在这里**不可达**：
                #
                # 1. 本能力的 `AtMostOnePending` 触发条件是 payload 里带齐 `dims`
                #    （`pond_id`）。可 `pond_id` 只在**路径**里，不在字段 schema 里，
                #    所以这条声明即使挂上也永不触发（与 StateTransition 同一形态）。
                # 2. 更根本的是：它**不需要**。`POND_STATUS_WORKFLOW` 里每个状态的出边
                #    只有 1–2 条，同一塘口的第二次申请会被 `require_transition` 先拒；
                #    要绕过状态机只能直接 INSERT，而那时拦它的是 DB 唯一键。
                #    于是"不变量"这一层在真实路径上永远轮不到说话。
                #
                # 所以本规则按 registry §4 #21 的"施工要求：两层都要有"由**两层**强制
                # （见 registry §4 #21 的「替代强制」段，对账工具 [B-声明] 会把它列出来供复核）：
                #   a. DB 唯一键 `uq_pond_status_active_request`（生成列 `active_pond_id`）
                #      —— 并发下唯一可信的判定；
                #   b. 服务的显式预检 + 状态机 `require_transition` —— 给出可读的 409 文案。
                # e2e 对这两层分别有断言（`tools/master_data_e2e.py` §7）。
            ),
        )
    )

    REGISTRY.register(
        Capability(
            name="pond_status_change.verify",
            title="核验塘口状态变更",
            domain="master_data",
            resource="pond",
            loader=PondService.load_pond,
            handler=PondWriteService.verify_pond_status_change,
            service_factory=PondWriteService,
            method=HttpMethod.POST,
            path="/api/v1/ponds/{pond_id}/status-changes/{request_id}/verify",
            kind="action",
            required_permission="pond.status.verify",
            scope=ScopePolicy.resource("area_id"),
            risk=Risk.HIGH,
            agent_exposure=_EXPOSED,
            idempotent=True,
            confirmation_labels=pond_confirmation_labels,
            audit=AuditPolicy.snapshot(),
            description="核验塘口状态变更申请并真正变更状态。申请人不能核验自己的申请",
            invariants=(
                # 比的是**塘口**的 row_version：本能力的 `before` 来自回读函数（塘口行），
                # 而申请行没有 `row_version` 列。`expected_version` 是全局乐观锁入参
                # （registry §2.5 明确本能力"无额外字段，只带全局 expected_version"）。
                # 申请单与塘口的并发一致性由服务里的 `pond_version` 校验承担（§3.1-D），
                # 并发本身由那两个带 WHERE 的 UPDATE 承担——三层不要互相顶替。
                OptimisticLock(label="该塘口"),
                # #20 的**状态转移判定**（`StateTransition` 的形态二）。它就是这一步真正
                # 在做的业务事实：把塘口的 `pond_status` 从当前状态迁到申请行的 `to_status`。
                #
                # ★ 为什么用 `from_context_fields=` 而不是普通的字段触发：
                #   迁移的**两端都不在请求体里，也不在同一份快照里** ——
                #   当前状态在**塘口行**（`before` 快照，但列名是 `pond_status`，
                #   不是 `status`），目标状态在**申请行**的 `to_status` 列。
                #   服务经 `_invariant_context` 把这两个值回传，判定因此**真的会执行**
                #   （普通形态下 `field_name in payload` 恒假 → 永不触发，那正是本处
                #   原先的缺口：声明看起来挂了、运行期必定跳过）。
                #
                # ★ 这里**同时**有服务层判定与内核声明式判定 —— 刻意的两层，不是"两处
                #   描述同一件事"。两层的**作用对象与时机不同**，删掉任何一层都留下真实缺口：
                #   * 服务层那次（`request_pond_status_change` 里，
                #     `POND_STATUS_WORKFLOW.require_transition`）在**申请步、INSERT 之前** ——
                #     它挡的是"非法迁移的申请行被写进库"。谁想删掉它请先看后果：申请行一旦
                #     落库就**长期占着** `uq_pond_status_active_request`（生成列
                #     `active_pond_id`），于是那个塘口在它被处理掉之前**无法再发一次正确的
                #     申请** —— 一次非法请求会把塘口卡住，这比"报错晚一步"严重得多。
                #   * 内核这次在**核验步、写入之后** —— 它挡的是"状态真的被改成非法状态"。
                #     它读的是**申请行落库时的** `from_status` / `to_status`（服务经
                #     `_invariant_context` 回传），判的是"这张申请单本身合法吗"。
                # 两者**不是两套逻辑**：都调同一个 `POND_STATUS_WORKFLOW.require_transition`
                # （转移表只有一份）。结论：两层都留，职责各自写在注释里。
                StateTransition(
                    machine=POND_STATUS_WORKFLOW,
                    field_name="pond_status",
                    from_context_fields={
                        "from_field": "status_change_from",
                        "to_field": "status_change_to",
                    },
                    label="塘口状态变更",
                ),
            ),
        )
    )


_register_all()

# ---------------------------------------------------------------------------
# 回读函数的守卫（`WRITE_CONTRACT.md` 规则 2 / `ROLLOUT_CONTRACT.md` §3）
# ---------------------------------------------------------------------------
#
# 守卫放在**声明模块**里而不是写模块里，理由是可验证的：回读函数由内核的
# `Capability.loader=` 在 `Capability.__post_init__` 里挂到处理器上，也就是说
# **只有本模块被执行过**，那些属性才存在。写在写模块里会让守卫依赖 import 顺序。
#
# 断言的对象是**注册表里的写能力**，而不是"服务类里名字不像下划线的方法"——
# 后者会把回读方法本身（如 `load_pond_status_change_request`）误判成漏登记，
# 于是规则变成负担、下一个人就会加白名单，而白名单会让守卫失效。


def assert_reload_wired() -> None:
    """master_data 的写能力必须全部声明了回读函数。

    由 `tests/test_write_reload_contract.py` 与 `tools/master_data_e2e.py` 调用；
    本模块末尾也直接调一次，让"漏了"在 import 期就炸。
    """
    unwired = sorted(
        item.name
        for item in REGISTRY.all()
        if item.domain == "master_data"
        and not item.is_read
        and getattr(item.handler, "__yuxin_load_by_id__", None) is None
    )
    if unwired:
        raise RuntimeError(
            "master_data 这些写能力没有回读函数："
            f"{unwired}。执行器 `_reload_after` 会抛 INTERNAL_ERROR（生产路径 500）——"
            "因为 `executed` 的含义是「读回来的行确实是我们想要的样子」，"
            "而不是「我们调用了 INSERT」。"
            "修法是在能力声明里写 `loader=<回读函数>`。"
        )


assert_reload_wired()
