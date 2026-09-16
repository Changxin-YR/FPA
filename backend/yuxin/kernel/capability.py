"""Capability：业务能力的唯一声明，其余全部派生。

早期版本的病根：一次业务写操作要改哪些东西被描述了 4 遍，且没有一处权威——
`product/*/routes.py` 的 handler、`features/*_service.py` 顶部的 `FIELDS` 常量、
`agent_tool_policy.py` 的 URL 正则、`tools/build_openapi.py` 的契约。
后果是：Agent 的确认策略靠 URL 里有没有 `approve/status/verify` 这种词决定；
新增一条路由可以不惊动任何安全策略；前端手写第二份字段清单并已漂移。

新系统的公理：**声明一次，机械派生。**

从这个对象派生出：
    REST 路由 · 请求校验模型 · 前端字段元数据 · RBAC 权限码 · DataScope 谓词
    · 幂等策略 · 人工确认闸门 · 审计字段 · Agent Tool schema · OpenAPI 文档
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field as dataclass_field
from enum import StrEnum
from typing import Any, Protocol

from yuxin.kernel.errors import DomainError, ErrorCode, validation
from yuxin.kernel.fields import Field, FieldSet
from yuxin.kernel.scope import Scope, ScopePolicy, ScopePredicate
from yuxin.kernel.uow import RequestContext, UnitOfWork


class Risk(StrEnum):
    """风险等级。``READ`` 只是查询，``NORMAL`` 是普通写，``HIGH`` 是高危写。"""

    READ = "read"
    NORMAL = "normal"
    HIGH = "high"


class Confirmation(StrEnum):
    """人工确认闸门。

    早期版本用 URL 正则猜这件事（`agent_gateway_policy.py:73`）：
    ``DELETE`` 或 ``/api/v1/admin`` 或路径里含 `approve/cancel/verify/status/...`
    或前缀是 cost/purchase/sales 就要确认。给新路由起个别的名字就能绕过。
    """

    NEVER = "never"
    ALWAYS = "always"


class AgentExposure(StrEnum):
    """一个能力是否对智能体开放。

    默认值刻意是 ``HIDDEN``：能力清单是权威的，Tool 清单是它的派生**子集**。
    默认暴露会让"新加的能力自动获得 AI 可调用权"，方向反了。
    """

    EXPOSED = "exposed"
    HIDDEN = "hidden"
    HUMAN_ONLY = "human_only"


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


@dataclass(frozen=True, slots=True)
class AuditPolicy:
    """一条能力**记什么审计**。

    ## 只有两档，而且**没有"不写审计"这一档**

    审计行是**无条件写入**的：`runner._write_audit()` 在成功路径（`runner.py:266`）与
    幂等路径（`runner.py:368`）各调用一次，**两条路径都不判 `AuditPolicy`**。
    这个策略只决定一件事：**要不要另外记 before/after 全量 diff**。

        summary()   记动作：capability / domain / permission / object_type / object_id /
                    object_ref / result / reason / ip / actor —— **不带 diff**
        snapshot()  = summary 的一切，**外加** before_json / after_json

    ## 为什么原名 `none()` 必须删掉（这是本类的核心教训）

    它原来叫 `AuditPolicy.none()`，而 `docs/CAPABILITY_REGISTRY.md` §0.6 有一张表写着

        | none | 不写审计 | 旧读操作 |

    **那个行为在运行时根本不存在**：26 条只读能力用的正是 `none()`，
    实测库里它们每一行都有审计（`before_json` / `after_json` 均为 NULL）——
    也就是说它们实际执行的是文档里叫 `summary` 的那一档。

    一个叫 `none` 的构造器实际做的是 `summary`，后果不只是"名字不好看"：
    读它的人会以为"这条能力不落审计"，进而**在别处补一遍审计写入**；
    实测发生过的定性争论也正是卡在这里（一方定性为"契约标签错误"、
    一方说"文档错"）——**根因是这个命名**。

    删掉 `none()` 而不是留一个别名：留别名等于把这个会撒谎的名字永久留在公共契约上，
    而它现在**只剩两个调用点**（默认工厂 + `sales_order.submit`），改名成本近乎为零。

    **注意与 `ScopePolicy.none()` 区分**：那是另一个类型，语义是"不受数据范围约束"，
    与审计无关，不要一起改（`docs/CAPABILITY_REGISTRY.md` §0.3 的 `none` 就是它）。
    """

    #: 是否额外记 before/after 全量快照。`summary()` 为 False，`snapshot()` 为 True。
    before_after: bool = False

    #: 会被记进审计 `detail_json` 的字段（脱敏后）。
    #:
    #: 与 `before_after` 的分工：`before_after` 记**整行**的前后状态，
    #: `detail_fields` 记"这次调用里哪几个参数值得单独留痕"。
    #: 两者是**正交**维度，所以都可以带。
    detail_fields: tuple[str, ...] = ()

    @classmethod
    def summary(cls) -> AuditPolicy:
        """只记动作，不记 diff。**这是默认档**（`Capability.audit` 的默认工厂）。

        registry §0.6 的 `summary` 就是它："`action_code` + `object_ref` + `reason`，无 diff"。
        """
        return cls()

    @classmethod
    def snapshot(cls) -> AuditPolicy:
        """记动作**并**记 before/after 全量 diff。

        registry §0.6 的 `before_after`："`before_json` + `after_json` + actor 快照"。

        用它的判据：**"改了什么"本身是审理要看的**——例如改角色权限、
        改账号授权、改塘口资料。而"提交/停用"这类动作变化量小、且状态转移本身就
        写在 `reason` 与 `object_ref` 里，记全量 diff 只会让审计表膨胀。
        """
        return cls(before_after=True)


class Invariant(Protocol):
    """业务不变量。

    早期版本把不变量写在仓储里紧贴 SQL（`production_store.py:273-280` 的负存塘、
    `warehouse_ledger_store.py:181-202` 的负库存），`FOR UPDATE` 行锁写法是**正确的**，
    但有两个代价：只能靠真实 MySQL 测，且无法被第二个调用方复用。

    新系统把不变量提取成可组合对象，由内核统一执行。仓储仍然负责行锁。

    执行时机：**同一事务内、业务写入之后、提交之前**（`runner._invoke_once` 的顺序是
    `_maybe_load_before` → `_call_service` → `run_invariants` → `_reload_after` → 审计）。
    失败整体回滚、不产生部分写入；但不变量看到的是**写入之后**的状态 ——
    账本类规则因此直接判后验余额，不要再自己加增量。
    """

    name: str

    def check(
        self,
        *,
        tx: UnitOfWork,
        scope: Scope,
        actor_id: int,
        payload: dict[str, Any],
        before: dict[str, Any] | None,
    ) -> None:
        """不满足时抛 DomainError。"""
        ...


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """业务处理器的返回值。

    ``resource_id`` 用于幂等回放（只存 id，不存整个响应体——早期版本
    `idempotency_keys.response_json` 存了可能 200 KB 的快照）。
    """

    data: Any
    resource_id: int | None = None
    message: str = ""


Handler = Callable[..., HandlerResult]


class NoLoader:
    """``loader=NO_LOADER`` 的哨兵类型。

    为什么用一个**专用哨兵**而不是 ``False`` / ``None``：本字段有三种含义，
    其中两种（"此处不需要回读函数"与"还没接线"）都必须能表达，而 ``None``
    天然只能承担一个。用 ``False`` 代替会立刻遇到第二个问题——``False`` 与
    "没传"在 ``if loader:`` 下无法区分，于是"忘记声明"会被静默当成"不需要"，
    而那正是这个字段要根治的失败形态。

    它也刻意**不是** ``bool``：``loader=True`` 这种笔误必须在**导入期**就炸，
    而不是等执行器 ``callable()`` 判假后报一句"没有提供回读函数"。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只为可读的报错信息
        return "NO_LOADER"

    def __bool__(self) -> bool:
        return False


#: 声明"本能力**不产出可回读的业务单据**"。
#:
#: 它的含义是一条契约：该能力的处理器**不得**返回 ``resource_id``。
#: 若它将来开始返回，执行器会在回读步骤抛 ``INTERNAL_ERROR`` —— 那是**对的**，
#: 因为"写入成功但没有可回读的行"本身就是矛盾，必须响。
NO_LOADER = NoLoader()


@dataclass(frozen=True, slots=True)
class Capability:
    """一个业务能力的完整声明。"""

    name: str
    title: str
    domain: str
    handler: Handler

    method: HttpMethod
    path: str
    kind: str  # read | create | update | delete | action

    #: 该能力作用的资源名（对应 `ResourceRegistry` 里的 `Resource.name`）。
    #:
    #: 用途：前端按资源聚合能力来渲染列表页与行内动作；`row_actions` 从资源的状态机
    #: 推导。声明它也让"这个能力属于哪个资源"成为可查询的事实，而不是靠 path 猜。
    resource: str = ""

    fields: FieldSet = dataclass_field(default_factory=FieldSet)
    required_permission: str | None = None
    scope: ScopePolicy = dataclass_field(default_factory=ScopePolicy.none)
    risk: Risk = Risk.NORMAL
    confirmation: Confirmation | None = None
    agent_exposure: AgentExposure = AgentExposure.HIDDEN
    idempotent: bool = False
    #: 审计档位。默认 `summary()`（记动作、不记 diff）——与 registry §0.6 一致，
    #: 也与执行器的真实行为一致：**审计行无条件写**，本策略只管 diff。
    #: 用 `snapshot()` 的场合见 `AuditPolicy` 的 docstring。
    audit: AuditPolicy = dataclass_field(default_factory=AuditPolicy.summary)
    invariants: tuple[Invariant, ...] = ()
    description: str = ""

    #: 路径参数名（如 `{pond_id}`）；从 path 模板解析，供 Web 层生成路由使用。
    path_parameters: tuple[str, ...] = ()

    #: 处理器所属服务的构造方式。
    #:
    #: 为什么需要它：能力处理器通常是**实例方法**（`PondService.create`），直接调用会
    #: 报 `missing 1 required positional argument: 'self'`。有三种处理方式：
    #:   1. 声明 `handler=PondService().create`（绑定实例）——但服务通常需要仓储依赖，
    #:      在模块导入期构造会把依赖注入时机提前到 import 时刻，不好；
    #:   2. 默默 `handler.__qualname__` 拆名字去实例化——隐式且对嵌套类/别名不健壮；
    #:   3. **显式声明工厂**（本设计）。
    #:
    #: 选 3：装配关系写在声明处，一眼能看出这个能力属于哪个服务、那个服务怎么来。
    service_factory: Callable[[], Any] | None = None

    #: 回读函数 —— 该写能力**如何按主键读回自己刚写的那一行**。
    #:
    #: 三种取值，**必须显式选一个**：
    #:
    #:   * ``Callable``       —— 声明回读函数（签名与 `runner._reload_after` 的调用一致：
    #:     ``(tx, *, scope, record_id) -> dict | None``）；
    #:   * :data:`NO_LOADER`  —— 本能力不产出可回读的单据，其处理器不得返回 ``resource_id``；
    #:   * ``None``（默认）    —— **尚未接线**。写能力停在这个状态时，执行器会在回读
    #:     步骤抛 ``INTERNAL_ERROR``，架构测试也会失败。
    #:
    #: ## 它为什么必须存在，而不是靠命名约定派生
    #:
    #: `runner._reload_after` 要求：任何返回 ``resource_id`` 的写能力，其 handler 必须
    #: 能解析出 ``__yuxin_load_by_id__(tx, *, scope, record_id)``，否则**抛错而不是降级成
    #: 成功**（`docs/WRITE_CONTRACT.md` 规则 2：``executed`` 的含义是"读回来的行确实是
    #: 我们要的样子"，不是"我们调用了 INSERT"）。
    #:
    #: 在本参数出现之前，这个属性**没有主人**：`backend/yuxin/domains/` 下一处都没挂，
    #: 只有 `tools/runner_e2e.py` 与 `tools/web_e2e.py` 在夹具里手工补挂。于是生产路径下
    #: 所有写能力都会 500，而七套自检全绿 —— 与组合根缺失是同一形态的缺陷
    #: （**夹具盖住了生产路径**）。
    #:
    #: ## 为什么不做成 ``load_{resource}`` 的口径化派生（曾评估并否决）
    #:
    #: 两个理由，第二个是决定性的：
    #:
    #: 1. **静默退回 ``None``**。名字对不上时派生拿不到函数，回读步骤报"没有提供回读
    #:    函数" —— 正是本次故障的形态；而"多数资源名恰好对得上"会长期掩盖个别错配。
    #: 2. **会挂错对象**（更严重）。`pond_status_change.request` 的 ``resource`` 是
    #:    ``pond``，但它的 ``resource_id`` 是**变更申请单 id**，不是塘口 id。派生会挂上
    #:    ``load_pond``，于是用申请单 id 去查塘口：通常回读为 ``None``，撞上"回读不到该
    #:    记录"；**而如果那个 id 恰好撞上某个真实塘口，它会回读到一个错误但确实存在的行，
    #:    并判定写入成功** —— 一个"成功"的写入配上不属于它的回读结果，比报错严重得多，
    #:    因为它没有任何症状。
    #:
    #: 所以 loader 由**声明者看着写下**，不从任何名字里猜。代价是每条写能力多写一行，
    #: 换来的是"回读哪张表"成为一处可评审的事实。
    loader: Callable[..., dict[str, Any] | None] | NoLoader | None = None

    #: 确认卡片的**可读化解析器**（可选）。形如
    #: ``(tx, payload) -> {"target": "周海霞（seller）", "rows": {"role_ids": "质检核验员"}}``
    #:
    #: ## 为什么需要它（提权卡只有裸 id）
    #:
    #: 确认卡片的 `rows` 是把 payload 里的值**原样**渲染出来的，`target` 由
    #: `runner._target_label` 拼成 `key=value`。于是 `access.user.grants` 的卡片长这样：
    #:
    #:     target: user_id=7
    #:     rows:   角色 = [9]      数据范围 = [2]
    #:     impact: 写入业务数据
    #:
    #: 全是要用户**自己去查**的裸 id。而提权恰恰是最需要人看懂"给谁、加了什么"的场景 ——
    #: HITL 的全部价值就在这一眼，看不到名字等于确认闸门退化成一个"点确定"的仪式。
    #:
    #: ## 为什么由域提供、而不是内核自己查
    #:
    #: 把 id 解析成名字要读 `users` / `roles` / `data_scopes` —— 那是**声明这条能力的域**
    #: 自己的表。内核不认识它们，也不该认识（`ARCHITECTURE.md` 的分层）。所以约定与
    #: `loader=` 完全一致：**域提供可调用、内核在正确的时机调一次**。
    #:
    #: 未声明时卡片按原样渲染（既有能力的行为一个字节都不变）。
    #: 解析器**抛错不阻断**：卡片退回裸 id，但绝不允许因为"美化失败"而签不出卡。
    confirmation_labels: Callable[..., dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path_parameters", _path_parameters(self.path))

        # 未显式传字段时，从处理器的 `Annotated[T, Field(...)]` 标注**自动收集**。
        #
        # 为什么把它做成默认行为而不是"记得传"：处理器签名里已经有完全相同的标注，
        # 要求调用方再传一遍就是"两处描述同一件事"——而任何这样的地方，
        # 都会有人忘记同步。实测后果是字段集为空：校验放行一切、
        # 前端渲染不出表单、Agent 工具 schema 里没有参数，**且全程不报错**。
        #
        # 显式传入仍然支持（用于签名无法表达的场景），但不传是对的默认。
        if not self.fields.fields:
            object.__setattr__(self, "fields", _field_annotations(self.handler))
        if self.refuses_agent:
            # HUMAN_ONLY 属于"需求要求的显式限制"，必须说得出理由。
            if not self.description:
                raise ValueError(
                    f"能力 {self.name} 标记为 HUMAN_ONLY 但没有 description 说明理由"
                )
        # -- 回读函数的接线（见 `loader` 字段的 docstring）--------------------
        #
        # 挂载动作放在这里而不是让每个域自己 `Handler.__yuxin_load_by_id__ = ...`：
        # 属性名是内核与执行器之间的**内部约定**（`runner._reload_after` 按它取），
        # 让五个域各自手写字符串常量，等于把这个约定变成五处副本。
        # 曾经就因为一处写成 `__yuxin_read_by_id__` 而另一处写 `__yuxin_load_by_id__`，
        # 导致回放静默退化成只返回 `{"id": ...}`（见 `runner._replay` 的注释）。
        if isinstance(self.loader, NoLoader):
            setattr(self.handler, "__yuxin_no_loader__", True)
        elif self.loader is not None:
            if not callable(self.loader):
                raise TypeError(
                    f"能力 {self.name} 的 loader 必须是可调用对象或 NO_LOADER，"
                    f"实际收到 {type(self.loader).__name__}。"
                    "（用 loader=True 之类的写法是笔误：它会让执行器读不到回读函数。）"
                )
            setattr(self.handler, "__yuxin_load_by_id__", self.loader)

        if self.risk is Risk.READ:
            if self.method is not HttpMethod.GET:
                raise ValueError(f"能力 {self.name} 声明为 READ 但方法不是 GET")
            if self.confirmation is Confirmation.ALWAYS:
                raise ValueError(f"能力 {self.name} 是只读能力，不需要人工确认")
        else:
            if self.method is HttpMethod.GET:
                raise ValueError(f"能力 {self.name} 方法为 GET 却不是 READ 风险等级")

    # -- 派生属性 -------------------------------------------------------------

    @property
    def is_read(self) -> bool:
        return self.risk is Risk.READ

    @property
    def is_write(self) -> bool:
        return not self.is_read

    @property
    def refuses_agent(self) -> bool:
        return self.agent_exposure is AgentExposure.HUMAN_ONLY

    @property
    def effective_confirmation(self) -> Confirmation:
        """确认闸门的**生效值**。

        需求要求："针对创建、修改、审核、付款、收款及库存变更等高风险业务，
        实现'先准备、后确认、再执行'"。

        因此 ``risk=HIGH`` 默认要求确认（``ALWAYS``）。想让高危写操作免确认，必须
        显式写 ``confirmation=Confirmation.NEVER``——那一刻是一次有意识的决定，
        而不是靠 URL 名字碰巧躲过正则。
        """
        if self.is_read:
            return Confirmation.NEVER
        if self.confirmation is not None:
            return self.confirmation
        return Confirmation.ALWAYS if self.risk is Risk.HIGH else Confirmation.NEVER

    @property
    def requires_idempotency_key(self) -> bool:
        """是否**必须**携带 ``Idempotency-Key``。

        只有显式声明 ``idempotent=True`` 的能力才要求。曾经的实现是
        ``self.idempotent or self.effective_confirmation is ALWAYS``，即"高危能力
        自动要求幂等键"——那个写法是错的：

            * 高危能力走**确认路径**，而确认令牌本身就是一次性的
              （``WHERE status='pending'`` 原子占位）。再加一层幂等键是重复约束；
            * 更糟的是它把负担推给了客户端：调用方得在**确认之后**再自己想一个键，
              而准备阶段签发的键并没有被持久化下来。

        确认路径的幂等性由 **confirmation_id 派生的键**保证
        （见 ``agent/gateway.py::_call_confirmed``）：同一次确认只会执行一次。
        """
        return self.idempotent

    @property
    def exposed_to_agent(self) -> bool:
        return self.agent_exposure is AgentExposure.EXPOSED

    # -- 派生：数据范围 -------------------------------------------------------

    def render_scope(self, scope: Scope, alias: str = "") -> ScopePredicate:
        return self.scope.render(scope, alias)

    # -- 派生：请求校验 -------------------------------------------------------

    def create_fields(self) -> dict[str, Field]:
        """客户端可提交的字段（排除服务端解析的 readonly 字段）。"""
        return {key: spec for key, spec in self.fields.writable().items() if not spec.readonly}

    def update_fields(self) -> dict[str, Field]:
        return {
            key: spec
            for key, spec in self.fields.updatable().items()
            if not spec.readonly
        }

    def scope_fields(self) -> dict[str, Field]:
        """服务端解析写入的分租字段（`farm_id` / `area_id` 等）。

        它们要进数据库、要出现在响应的 `fields` 元数据里（让用户看得见自己的
        数据范围），但不进请求校验、不进 Agent Tool schema。
        """
        return {key: spec for key, spec in self.fields.items() if spec.readonly}

    def required_create_fields(self) -> tuple[str, ...]:
        return tuple(key for key, spec in self.create_fields().items() if spec.required)

    def missing_required(self, payload: dict[str, Any], *, for_update: bool = False) -> list[str]:
        specs = self.update_fields() if for_update else self.create_fields()
        if for_update:
            # 更新请求只需要校验"提交了哪些字段"，不强制补齐全部必填项。
            return []
        return [key for key, spec in specs.items() if spec.required and _is_blank(payload.get(key))]

    # -- 派生：前端元数据 -----------------------------------------------------

    def to_meta(self) -> dict[str, Any]:
        """前端字段元数据。

        注意这里用 ``self.fields`` 全量（含 readonly），而不是 ``create_fields()``：
        readonly 字段必须出现在表单里让用户看见数据范围，只是不可提交。
        ``create_fields()`` 管的是"能否提交"，与"是否展示"是两件事。
        """
        return {
            "name": self.name,
            "title": self.title,
            "domain": self.domain,
            "method": str(self.method),
            "path": self.path,
            "kind": self.kind,
            "risk": str(self.risk),
            "confirmation": str(self.effective_confirmation),
            "agent_exposure": str(self.agent_exposure),
            "required_permission": self.required_permission,
            "scope_required": self.scope.constrained,
            "idempotent": self.requires_idempotency_key,
            "description": self.description,
            "resource": self.resource,
            "fields": [spec.to_meta(key) for key, spec in self.fields.items()],
            "path_parameters": list(self.path_parameters),
            "row_actions": self.row_actions(),
        }

    def row_actions(self) -> list[str]:
        """该资源在当前状态下可能出现的动作集合。

        这是**静态上限**：具体某一行能做什么，仍由服务端按该行实际状态算出的
        ``allowed_actions`` 决定（前端只渲染服务端给的动作，不自己推导）。
        这里给出来是为了让列表页在加载数据之前就能准备好按钮渲染逻辑。

        早期版本的做法是把动作清单硬编码在前端，于是"后端加了动作、前端没加"和
        "后端删了动作、前端还在渲染"都会静默发生。
        """
        if not self.resource:
            return []
        from yuxin.kernel.workflow import RESOURCES

        found = RESOURCES.find(self.resource)
        if found is None or found.workflow is None:
            return []
        seen: list[str] = []
        for state in found.workflow.states:
            for action in state.actions:
                text = str(action)
                if text not in seen:
                    seen.append(text)
        return seen

    # -- 派生：Agent Tool -----------------------------------------------------

    def to_tool_schema(self) -> dict[str, Any]:
        """生成给 DeepSeek Harness 的工具 schema（标准 JSON Schema）。

        早期版本是 `biz_query` / `biz_mutation` 两个元工具，把业务操作名塞进一段
        长 description 字符串里让模型猜（`build_agent_tool_catalog()`）。这里改成
        每个能力一个真工具、真 schema。
        """
        if not self.exposed_to_agent:
            raise ValueError(f"能力 {self.name} 未对智能体开放，不能生成工具 schema")

        properties: dict[str, Any] = {}
        required: list[str] = []

        for param in self.path_parameters:
            properties[param] = {
                "type": "integer",
                "description": f"路径参数 {param}",
            }
            required.append(param)

        if self.is_read:
            properties.update(self.read_query_schema())
        else:
            for key, spec in self.create_fields().items():
                # readonly 字段（分租键）由服务端解析，模型不该去填——
                # 放进 schema 只会诱导模型编造 farm_id / area_id。
                properties[key] = spec.json_schema()
                if spec.required:
                    required.append(key)
            if self.kind == "update":
                properties["expected_version"] = {
                    "type": "integer",
                    "minimum": 1,
                    "description": "乐观锁版本号，必须与当前记录版本一致",
                }
                required.append("expected_version")

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        return schema

    def read_query_schema(self) -> dict[str, Any]:
        """从资源声明派生只读工具的查询参数。"""
        if not self.is_read:
            return {}
        from yuxin.kernel.workflow import RESOURCES

        resource = RESOURCES.find(self.resource)
        properties: dict[str, Any] = {
            "page": {"type": "integer", "minimum": 1, "description": "页码，从 1 开始"},
            "page_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "每页条数",
            },
        }
        if resource is None:
            return properties
        if resource.search:
            properties["keyword"] = {
                "type": "string",
                "description": "按编码或名称模糊搜索",
            }
        for item in resource.filters:
            if item.kind == "boolean":
                schema: dict[str, Any] = {"type": "boolean"}
            elif item.kind == "ref":
                schema = {"type": "integer", "minimum": 1}
            elif item.kind == "date_range":
                schema = {"type": "string", "format": "date"}
            else:
                schema = {"type": "string"}
                if item.choices:
                    schema["enum"] = [choice.value for choice in item.choices]
                elif item.kind == "status" and resource.workflow is not None:
                    # `kind="status"` 的候选值是**运行时**从状态机解析的，声明里没有
                    # `choices`（那个字段只服务 `kind="enum"`）。不下发候选值的后果：
                    # 工具的 JSON Schema 里这个参数是**裸 string**，模型只能猜；
                    # 猜错不报错、只是匹配不到任何行 ——
                    # 实测 2026-09-15：模型填 `batch_status="active"`，接口回
                    # 200 + 空列表，于是它把「养殖中 0 个」当事实报给用户（真实 1 个）。
                    # 这属于「静默给出错误答案」，比报错危险。
                    # 候选值取该资源状态机的全部状态码（文档状态 + 业务状态）。这是
                    # **超集**守卫：挡不住拿 `status` 的值去填 `batch_status`，但能挡住
                    # 完全编造的值。逐参数精确到具体状态机，需要"哪条筛选属于哪个
                    # 状态机"的声明，本仓还没有这一层。
                    schema["enum"] = [state.code for state in resource.workflow.states]
            schema["description"] = item.label
            for name in item.param_names:
                properties[name] = dict(schema)
        return properties

    def tool_name(self) -> str:
        """Agent 侧的扁平工具名，例如 `pond.create` → `pond_create`。

        模型对下划线命名的工具选择更稳定，且多数 Provider 的工具名只允许
        ``[A-Za-z0-9_-]``。
        """
        return self.name.replace(".", "_").replace("-", "_")

    def tool_description(self) -> str:
        base = self.description or self.title
        suffix = f"（需要 {self.required_permission} 权限）" if self.required_permission else ""
        if self.effective_confirmation is Confirmation.ALWAYS:
            suffix += "（高危操作：需用户确认后才会真正执行）"
        return f"{base}{suffix}"


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Registry:
    """全部能力的注册表。系统的权威清单。"""

    _capabilities: dict[str, Capability] = dataclass_field(default_factory=dict)

    def register(self, capability: Capability) -> None:
        if capability.name in self._capabilities:
            raise ValueError(f"能力 {capability.name} 重复注册")
        self._capabilities[capability.name] = capability

    def get(self, name: str) -> Capability:
        try:
            return self._capabilities[name]
        except KeyError as exc:
            raise DomainError(
                ErrorCode.CAPABILITY_NOT_FOUND, "该操作不在允许范围内"
            ) from exc

    def find(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    def all(self) -> tuple[Capability, ...]:
        return tuple(self._capabilities.values())

    def by_domain(self, domain: str) -> tuple[Capability, ...]:
        return tuple(item for item in self._capabilities.values() if item.domain == domain)

    def visible_to(self, permissions: frozenset[str]) -> tuple[Capability, ...]:
        """L1 权限过滤：只返回用户有权调用的能力。

        这是"Tool Filtering"层（需求要求的三层防御第一层）的服务端形态，
        前端菜单与 Agent 工具清单都由它驱动，从而天然一致。
        """
        return tuple(
            item
            for item in self._capabilities.values()
            if item.required_permission is None or item.required_permission in permissions
        )

    def agent_tools(self, permissions: frozenset[str]) -> tuple[Capability, ...]:
        return tuple(item for item in self.visible_to(permissions) if item.exposed_to_agent)

    def __len__(self) -> int:
        return len(self._capabilities)

    def __contains__(self, name: object) -> bool:
        return name in self._capabilities


#: 进程级注册表。能力模块在导入时通过 ``@capability`` 装饰器登记自己。
REGISTRY = Registry()

#: 被 ``@capability`` 装饰过的处理器，用于"导入即注册"与"重复注册检查"。
_DECORATED: dict[str, Capability] = {}


def capability(**kwargs: Any) -> Callable[[Handler], Handler]:
    """把一个业务服务方法登记为能力。

    用法::

        class PondService:
            @capability(
                name="pond.create",
                title="新建塘口",
                domain="master_data",
                method=HttpMethod.POST,
                path="/api/v1/ponds",
                kind="create",
                required_permission="pond.create",
                scope=ScopePolicy.resource("area_id"),
                risk=Risk.NORMAL,
                agent_exposure=AgentExposure.EXPOSED,
                idempotent=True,
                audit=AuditPolicy.snapshot(),
                fields=...,  # 由 annotations 自动收集，通常不用显式传
            )
            def create(...) -> HandlerResult: ...
    """

    def decorate(handler: Handler) -> Handler:
        annotations = _field_annotations(handler)
        declared: FieldSet | None = kwargs.pop("fields", None)
        field_set = declared if declared is not None else annotations

        name = kwargs.get("name")
        if name is None:
            raise TypeError("capability() 必须提供 name")
        if name in _DECORATED:
            raise ValueError(f"能力 {name} 已在 {_DECORATED[name].handler.__qualname__} 中声明")

        spec = Capability(fields=field_set, handler=handler, **kwargs)
        _DECORATED[name] = spec
        setattr(handler, "__yuxin_capability__", spec)
        REGISTRY.register(spec)
        return handler

    return decorate


#: 框架注入的参数，不属于业务字段。
#: 框架注入的参数，不属于业务字段。
#:
#: ---------------------------------------------------------------------------
#: 为什么 `expected_version` **不在**这个集合里（一次真实缺陷的修法）
#: ---------------------------------------------------------------------------
#: 它曾经在这里，后果是**每一条 action 能力都无法声明乐观锁**：
#:
#:     处理器签名声明 expected_version -> `_field_annotations` 把它从收集结果里剔除
#:     -> `Capability.fields` 里没有它 -> `validate_payload` 把它从请求体里丢弃
#:     -> 执行器 `handler(**kwargs)` 少传一个参数
#:     -> `TypeError: xxx() missing 1 required positional argument: 'expected_version'`
#:
#: 这个失败形态极难查：报错是原生 `TypeError` 而不是业务错误，排查方向会被引向
#: "服务签名写错了"，而真实原因是框架把声明悄悄吃掉了。更糟的是它**与文档直接矛盾**
#: —— registry §0.7 规则 3 要求"所有 update 与 action 必带 expected_version"，
#: §2.11 把它列为通用 action 载荷，而 DEVELOPMENT §3 第 4 步明说"**不传 fields=** ——
#: 内核会自动从处理器标注派生"。处理器按文档把它写进签名时，框架必须尊重这个声明。
#:
#: 与 `tx` / `scope` / `ctx` 的区别：那三个是**框架注入**的（执行器自己构造并传入，
#: 客户端永远不会提交它们）；`expected_version` 是**客户端提交的业务参数**
#: （前端自动带上、Agent 工具 schema 里也需要它）。把两者放进同一个集合，
#: 就把"谁提供这个值"这个本质区别抹掉了。
#:
#: 移出黑名单后两条既有路径都仍然正确：
#:   * 签名里没声明它的处理器 —— 收集不到该字段，请求体里出现它会被
#:     `validate_payload` 以"不接受的字段"拒绝（fail closed），与之前行为一致；
#:   * 签名里声明了的处理器 —— 字段进入字段表，**校验、前端元数据、Agent 工具
#:     schema、乐观锁不变量**四处同时得到它（这正是"声明一次、机械派生"）。
_FRAMEWORK_ARGS = frozenset(
    {
        "self",
        "cls",
        "tx",
        "scope",
        "ctx",
        "page",
        "page_size",
        "record_id",
        "resource_id",
    }
)


def _field_annotations(handler: Handler) -> FieldSet:
    """从处理器的类型标注里收集字段声明。

    **必须用 `get_type_hints(include_extras=True)`**，不能直接读
    ``handler.__annotations__``：本项目所有模块都带
    ``from __future__ import annotations``，此时 ``__annotations__`` 里是**字符串**，
    ``get_origin()`` 对字符串返回 None，字段会被静默丢空（第一版就踩了这个坑）。
    """
    import typing

    from yuxin.kernel.fields import collect_fields  # 局部导入避免循环

    try:
        raw: dict[str, Any] = dict(typing.get_type_hints(handler, include_extras=True))
    except Exception:  # noqa: BLE001 - 局部定义的类可能解析不到前向引用
        raw = dict(getattr(handler, "__annotations__", {}))
        if any(isinstance(value, str) for value in raw.values()):
            raise TypeError(
                f"无法解析 {handler.__qualname__} 的类型标注。"
                "能力处理器必须定义在模块级，且其标注中的自定义类型必须可导入——"
                "否则字段声明会退化成字符串而无法收集。"
            ) from None

    for argument in _FRAMEWORK_ARGS:
        raw.pop(argument, None)
    return collect_fields(raw)


def _path_parameters(path: str) -> tuple[str, ...]:
    import re

    return tuple(re.findall(r"\{([a-z_][a-z0-9_]*)\}", path))


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def validate_payload(capability_spec: Capability, payload: dict[str, Any], *, for_update: bool = False) -> dict[str, Any]:
    """按声明校验并清洗请求体。

    早期版本每个 service 顶部各有一份 `_clean()` 做同样的事（`production_service.py`
    的 `FIELDS` + `RESERVED`、`warehouse_service.py` 的同名结构、`master_data_service.py`
    的 `RESERVED_FIELDS`），并且各写各的错误文案。
    """
    if not isinstance(payload, dict):
        raise validation("请求内容必须是对象")

    specs = capability_spec.update_fields() if for_update else capability_spec.create_fields()
    readonly = set(capability_spec.scope_fields())

    # 先单独报 readonly 字段：它们的错误原因和"拼错了字段名"完全不同，
    # 混在"不接受的字段"里会让人以为是笔误。
    submitted_readonly = sorted(readonly & set(payload))
    if submitted_readonly:
        labels = [capability_spec.fields[key].label for key in submitted_readonly]
        raise DomainError(
            ErrorCode.FORBIDDEN,
            f"{'、'.join(labels)}由当前账号的数据范围自动确定，不能手工指定",
            data={"readonly_fields": submitted_readonly},
        )

    # `expected_version` 曾经在这里被无条件放行（`- {"expected_version"}`），
    # 与 `_FRAMEWORK_ARGS` 里把它列为框架参数是同一个误解的两半：
    # 前者说"它不算未知字段"，后者说"它不算字段"——合起来的效果是
    # **它既不被拒绝，也不被收集，还不被传给处理器**。
    #
    # 现在它就是一个普通字段：处理器声明了它，它就进字段表，这里无需特判；
    # 处理器没声明它，提交它会被当成未知字段拒绝（fail closed）。
    unknown = sorted(set(payload) - set(specs))
    if unknown:
        # 只在"确实有处理器想接受它、但忘了声明"时给出可操作的提示。
        # 泛泛地说"不接受的字段"会让下一个踩坑的人重复我这次排查。
        hint = ""
        if "expected_version" in unknown:
            hint = "。expected_version 需要在处理器签名里声明（如 `expected_version: f_int(\"乐观锁版本\", required=True, minimum=1)`）才会被接受"
        raise validation(f"请求包含不接受的字段：{'、'.join(unknown)}{hint}")

    cleaned: dict[str, Any] = {}
    missing: list[str] = []

    for key, spec in specs.items():
        if key not in payload:
            if spec.required and not for_update:
                missing.append(key)
            elif spec.default is not None and not for_update:
                cleaned[key] = spec.default
            continue
        cleaned[key] = _coerce(key, spec, payload[key])

    if missing:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            f"缺少必填字段：{'、'.join(specs[key].label for key in missing)}",
            data={"fields": missing},
        )
    return cleaned


def _coerce(key: str, spec: Field, value: Any) -> Any:
    """把 JSON 值转换成可安全绑定到 SQL 参数的 Python 值。

    早期版本在 `common/db` 层有 7 份 `_decode`、5 份 `_payload` 做同一件事，
    且判定方式是"看字符串里有没有某个词"（`production_store.py:142-146`）。
    """
    from datetime import date, datetime
    from decimal import Decimal, InvalidOperation

    if value is None:
        if spec.required:
            raise validation(f"{spec.label}不能为空", field=key)
        return None

    try:
        match spec.type:
            case "string" | "text":
                text_value = str(value)
                if spec.max_length is not None and len(text_value) > spec.max_length:
                    raise validation(f"{spec.label}不能超过 {spec.max_length} 个字符", field=key)
                return text_value
            case "integer" | "ref":
                if isinstance(value, bool):
                    raise validation(f"{spec.label}必须是整数", field=key)
                return int(value)
            case "number":
                number = Decimal(str(value))
                if not number.is_finite():
                    raise validation(f"{spec.label}数值无效", field=key)
                if spec.minimum is not None and number < Decimal(str(spec.minimum)):
                    raise validation(f"{spec.label}不能小于 {spec.minimum}", field=key)
                if spec.maximum is not None and number > Decimal(str(spec.maximum)):
                    raise validation(f"{spec.label}不能大于 {spec.maximum}", field=key)
                return number
            case "boolean":
                if not isinstance(value, bool):
                    raise validation(f"{spec.label}必须是布尔值", field=key)
                return value
            case "date":
                return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value))
            case "datetime":
                return value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            case "enum":
                text_value = str(value)
                allowed = {choice.value for choice in spec.choices}
                if allowed and text_value not in allowed:
                    raise validation(
                        f"{spec.label}的取值无效，可选：" + "、".join(c.label for c in spec.choices),
                        field=key,
                    )
                return text_value
            case "array":
                # 逐元素转换 + 校验。**不**在这里判"至少一个"——那是业务规则
                # （registry §2.4 的 `≥1`），由服务层判定；schema 只管类型。
                # 两类判定必须分开：混在一起的后果是"空数组"与"元素类型错"报同一条
                # 文案，而它们的修法完全不同（一个是补选一个角色，一个是改传数字）。
                if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
                    raise validation(f"{spec.label}必须是数组", field=key)
                if spec.max_length is not None and len(value) > spec.max_length:
                    raise validation(f"{spec.label}最多 {spec.max_length} 项", field=key)
                items: list[Any] = []
                for index, item in enumerate(value):
                    if spec.items == "integer":
                        if isinstance(item, bool):
                            raise validation(f"{spec.label}第 {index + 1} 项必须是整数", field=key)
                        try:
                            items.append(int(item))
                        except (TypeError, ValueError) as exc:
                            raise validation(
                                f"{spec.label}第 {index + 1} 项必须是整数", field=key
                            ) from exc
                    else:
                        items.append(str(item))
                return items
    except DomainError:
        raise
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise validation(f"{spec.label}格式无效", field=key) from exc

    raise validation(f"{spec.label}的类型 {spec.type} 尚未支持", field=key)


def run_invariants(
    capability_spec: Capability,
    *,
    tx: UnitOfWork,
    scope: Scope,
    actor_id: int,
    payload: dict[str, Any],
    before: dict[str, Any] | None = None,
) -> None:
    for invariant in capability_spec.invariants:
        invariant.check(tx=tx, scope=scope, actor_id=actor_id, payload=payload, before=before)


def authorized(capability_spec: Capability, permissions: Iterable[str]) -> bool:
    """二层校验的公共判定。

    需求要求"工具白名单、Agent Gateway 权限校验和业务服务二次校验"三层，
    三层用的是**同一个判定函数**——所以三层不可能给出不同结论。
    """
    if capability_spec.required_permission is None:
        return True
    return capability_spec.required_permission in set(permissions)


def assert_authorized(capability_spec: Capability, permissions: Iterable[str]) -> None:
    from yuxin.kernel.errors import forbidden

    if not authorized(capability_spec, permissions):
        raise forbidden(required=capability_spec.required_permission)


def path_parameters(path: str) -> tuple[str, ...]:
    return _path_parameters(path)


def group_by_domain(capabilities: Sequence[Capability]) -> dict[str, list[Capability]]:
    grouped: dict[str, list[Capability]] = {}
    for item in capabilities:
        grouped.setdefault(item.domain, []).append(item)
    return grouped


__all__ = [
    "AgentExposure",
    "AuditPolicy",
    "Capability",
    "Confirmation",
    "Handler",
    "HandlerResult",
    "HttpMethod",
    "Invariant",
    "NO_LOADER",
    "NoLoader",
    "REGISTRY",
    "Registry",
    "RequestContext",
    "Risk",
    "assert_authorized",
    "authorized",
    "capability",
    "group_by_domain",
    "path_parameters",
    "run_invariants",
    "validate_payload",
]
