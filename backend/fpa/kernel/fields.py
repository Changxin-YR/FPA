"""字段声明：一份定义，同时供给五处消费方。

早期版本的教训（frontend-recon 高危结论）：
    后端已经有机器可读的字段白名单（`production_service.py:17-33`、
    `master_data_service.py:11-20`、`agent_field_guide.py:74-93`），但前端不知道它的
    存在，把同一份清单**手写了第二遍**，并且已经漂移——前端用
    `material_name` / `supplier_name` / `customer_name`，后端契约是
    `material_id` / `supplier_id` / `customer_id`。

    而且这件事**不能靠 OpenAPI codegen 解决**：早期版本业务请求体用通用
    `$ref: MutationRequest`，187 个 op 中 160 个只含信封引用，字段级信息不在 schema 内。

新系统的做法：字段只写一次（本文件的 ``Field``），从它派生

    ① Pydantic 校验模型      ② 前端 DynamicForm 渲染
    ③ REST 请求/响应 schema   ④ Agent Tool 的 JSON Schema
    ⑤ OpenAPI 文档

这样"前端硬编码字段名"在物理上不可能——它没有字段名可写。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from decimal import Decimal
from enum import StrEnum
from collections.abc import Callable
from typing import Annotated, Any, Literal, get_args, get_origin


class FieldType(StrEnum):
    """对前端暴露的字段类型。前端按这 10 种各渲染一个控件。"""

    STRING = "string"
    TEXT = "text"          # 多行
    INTEGER = "integer"
    NUMBER = "number"      # 支持小数（含 decimal 金额/数量）
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    ENUM = "enum"
    REF = "ref"            # 外键：前端拉对应资源列表做下拉
    #: 数组（`integer[]` / `string[]`）。
    #:
    #: ## 它为什么必须存在，而不是让调用方传逗号分隔的字符串
    #:
    #: registry 的字段表里**确实有数组字段**：§2.4 的 `role_ids` / `scope_ids` /
    #: `permission_codes`（`integer[]` / `string[]`）。在没有本类型之前，
    #: 这三处只能落在两种更差的形态上：
    #:
    #:   * 声明成 `f_str("角色")`、让客户端传 `"1,2,3"` —— **类型撒谎**。
    #:     schema 说它是字符串，于是前端渲染成单行文本框、Agent 的 tool schema
    #:     也写 `string`，而服务端心里想的是列表。这比"不支持"危险：它看起来能用。
    #:   * 干脆不让内核知道这些字段（处理器写成 `**kwargs`）—— 那正是
    #:     `kernel/fields.py` 模块 docstring 要根除的"字段白名单散落在服务里、
    #:     前端再手写一遍"的起点。
    #:
    #: ## 元素类型由声明者选（`f_int_list` / `f_str_list`），不由内核猜
    #:
    #: 与 `loader=` 同一口径：**能猜错的地方就不要猜**。`Field.items` 由声明侧显式
    #: 给出，并参与 `_coerce` 的逐元素校验。
    ARRAY = "array"


@dataclass(frozen=True, slots=True)
class RefTarget:
    """`FieldType.REF` 的取值来源。

    ## 两种形态，`resource` 与 `resource_field` 二选一

    * **静态**：``resource="area"`` —— 本字段永远指向同一类对象。
    * **多态**：``resource_field="target_type"`` —— 指向哪一类对象，由**那个字段的
      当前取值**决定（取值就是资源名）。

    多态形态不是"多了一种写法"，而是因为**有一种真实字段用静态形态表达不出来**：

        `cost.entry.create` 的 `target_id`（归属对象）语义是"指向 `target_type` 所选
        的那类对象"，而 `target_type ∈ {farm, area, pond, batch}`。

    在加上这一项之前，它只能声明成 ``f_int("归属对象")`` —— 元数据于是告诉前端
    "这是个整数输入框"。前端**正确地**按声明渲染出带上下箭头的数字框，而用户根本
    不知道该填哪个 id。这不是前端缺陷（前端完全照声明渲染），是**声明层把"这是引用"
    这件事丢掉了**，然后由用户承担后果。

    ## 为什么不许两个都写 / 两个都不写

    两个都写就有两处真相（"到底看哪个"），两个都不写就无法解析地址。因此构造期直接
    报错，而不是挑一个用——挑一个会让另一种写法静默失效。

    ``list_path`` 优先于 `ResourceRegistry` 解析：前端要用它去拉下拉选项，不提供就只能
    猜地址，而"猜复数"在不规则复数上必然出错。内核**不自动加 `s`**：`Resource` 声明里
    已经有权威的 `list_path`，再发明一条推导规则就是给同一件事加第二个来源。
    """

    #: 静态形态：永远引用的资源名。多态形态下留空（见 `resource_field`）。
    resource: str = ""
    label_key: str = "name"
    list_path: str = ""
    #: 多态形态：**本能力另一个字段的 key**，它的取值就是资源名。
    #:
    #: 取值必须与 `ResourceRegistry` 里的资源名一致（`area` / `pond` / `batch` …）——
    #: 解析地址靠的就是那张表，而"资源名"全系统只有这一处权威来源。
    resource_field: str = ""

    def __post_init__(self) -> None:
        if bool(self.resource) == bool(self.resource_field):
            raise ValueError(
                "RefTarget 必须且只能给出 resource（静态）或 resource_field（多态）之一："
                f"resource={self.resource!r}, resource_field={self.resource_field!r}"
            )

    @property
    def polymorphic(self) -> bool:
        """目标资源是否由另一个字段的取值决定。"""
        return bool(self.resource_field)

    def resource_for(self, value: object | None = None) -> str:
        """解析出**实际**引用的资源名。

        静态形态忽略 ``value``；多态形态在取值为空时返回空串——"还没选类型"不是错误，
        它只是还没有目标。前端据此把下拉禁用并说明原因，而不是渲染一个空下拉。
        """
        if not self.resource_field:
            return self.resource
        return "" if value is None else str(value).strip()

    def resolved_list_path(self, resource: str = "") -> str:
        """返回可用的列表地址；解析不出来就报错，**不猜**。

        ``resource`` 是多态形态解析出来的资源名；静态形态可以不给。
        """
        if self.list_path and not self.resource_field:
            return self.list_path
        target = resource or self.resource
        if not target:
            raise ValueError(f"多态引用 `{self.resource_field}` 还没有取值，无法解析列表地址。")
        from fpa.kernel.workflow import RESOURCES

        found = RESOURCES.find(target)
        if found is not None:
            return found.list_path
        raise ValueError(
            f"引用类型 {target!r} 没有可用列表地址。"
            "请在字段声明里显式给出 list_path=...，或先用 resource(...) 注册该资源。"
        )


@dataclass(frozen=True, slots=True)
class Choice:
    value: str
    label: str
    tone: str = "neutral"


@dataclass(frozen=True, slots=True)
class Field:
    """一个可写字段的完整声明。

    ``driver_type`` 决定 SQL 参数如何绑定——这是早期版本 `common/db` 层反复出现
    「7 份 `_decode`、5 份 `_payload`」的根因：类型转换散落在每个 store 里。
    """

    label: str
    type: FieldType = FieldType.STRING
    required: bool = False
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    precision: int | None = None          # 小数位，用于 DECIMAL
    choices: tuple[Choice, ...] = ()
    #: **动态**候选值来源：接收当前行的状态码，返回该状态下合法的候选值。
    #:
    #: 为什么需要它：能力声明是静态的，而合法目标状态取决于该行**当前**的状态。
    #: 静态声明只能给全集，用户就能选到非法目标——**让用户选一个必然失败的值，
    #: 是把校验成本转嫁给了用户**。
    #:
    #: 消费点有两个，语义不同：
    #:   * 行级元数据：传入该行的实际状态 -> 得到**精确的**合法目标
    #:   * 资源级元数据：没有"哪一行"，传 None -> 得到全集并标注 dynamic
    dynamic_choices: Callable[[str | None], tuple[Choice, ...]] | None = None
    ref: RefTarget | None = None
    #: `FieldType.ARRAY` 的元素类型（`"integer"` / `"string"`）。
    #:
    #: 递归地用 `FieldType` 而不是另发明一套"元素类型"枚举：元素的合法取值就是
    #: 标量字段类型的一个子集，两套枚举会立刻漂移。`_coerce` 按它逐元素转换与校验，
    #: `json_schema()` 把它翻译成 `items`（Agent tool schema 因此能表达
    #: `{"type":"array","items":{"type":"integer"}}`，而不是一个无约束的数组）。
    items: str = "string"
    placeholder: str = ""
    help: str = ""
    default: Any = None
    #: 是否允许出现在更新请求里（创建后不可改的字段如 `code` 置 False）。
    updatable: bool = True
    #: 是否参与列表列展示。
    list_column: bool = False
    #: 服务端解析后写入、**不接受客户端提交**的字段。
    #:
    #: 典型用途是数据范围字段（`farm_id` / `area_id`）：由 `ScopeResolver` 从当前
    #: 账号的 DataScope 解析。早期版本把这三个字段放在可写清单里（`早期版本 master_data_service.py:15-18`），
    #: 等于让客户端自报家门——DataScope 因此形同虚设。
    #:
    #: 这类字段会出现在前端的表单里（渲染成禁用输入框，让用户**看得见**自己的数据
    #: 范围落在哪里），但**不会**出现在 Agent Tool 的 input_schema 里——模型不该去
    #: 填一个它无法影响的值。
    readonly: bool = False

    # -- 派生：JSON Schema（Agent Tool 用） ----------------------------------

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": _JSON_TYPE[self.type]}
        if self.type is FieldType.TEXT:
            schema["type"] = "string"
        if self.type is FieldType.ARRAY:
            # `items` 必须给出：无约束的数组让模型无法知道该填什么，
            # 而"模型猜错类型"会在 `_coerce` 里变成一条它读不懂的错误。
            schema["items"] = {"type": _JSON_TYPE.get(FieldType(self.items), "string")}
        if self.type is FieldType.ENUM and self.choices:
            schema["enum"] = [choice.value for choice in self.choices]
        if self.max_length is not None:
            schema["maxLength"] = self.max_length
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        description_parts = [part for part in (self.help, self.placeholder) if part]
        if self.type is FieldType.ENUM and self.choices:
            description_parts.append(
                "可选值：" + "、".join(f"{c.value}={c.label}" for c in self.choices)
            )
        if description_parts:
            schema["description"] = "；".join(description_parts)
        return schema

    # -- 派生：前端元数据 ----------------------------------------------------

    def to_meta(self, key: str, current_status: str | None = None) -> dict[str, Any]:
        """字段的元数据。

        ``current_status`` 非空时，`dynamic_choices` 会按它**精确**解析；
        为空时退化成全集，并标注 ``dynamic: true``，让前端知道
        "这是全集，行级渲染时会收到更精确的候选"。
        """
        meta: dict[str, Any] = {
            "key": key,
            "label": self.label,
            "type": str(self.type),
            "required": self.required,
        }
        if self.readonly:
            meta["readonly"] = True
        if self.max_length is not None:
            meta["max_length"] = self.max_length
        if self.precision is not None:
            meta["precision"] = self.precision
        if self.placeholder:
            meta["placeholder"] = self.placeholder
        if self.help:
            meta["help"] = self.help
        if self.default is not None:
            meta["default"] = _jsonable(self.default)
        if self.dynamic_choices is not None:
            resolved = self.dynamic_choices(current_status)
            meta["choices"] = [{"value": c.value, "label": c.label} for c in resolved]
            if current_status is None:
                # 没有具体行可依时给的是全集，显式标注，别让前端以为已经过滤过。
                meta["dynamic"] = True
        elif self.choices:
            meta["choices"] = [{"value": c.value, "label": c.label} for c in self.choices]
        if self.ref is not None:
            # `resource` 与 `resource_field` 都下发：前者是静态目标（多态时为空串），
            # 后者告诉前端"目标资源由这个兄弟字段的取值决定"。前端**不写映射表**——
            # 它只按 `resource_field` 去取值，再用取到的名字查 `ResourceMeta.list_path`。
            meta["ref"] = {
                "resource": self.ref.resource,
                "label_key": self.ref.label_key,
                "resource_field": self.ref.resource_field,
            }
            if self.ref.polymorphic:
                # 标成动态：与 `dynamic_choices` 同一口径——让前端知道"现在还没有精确
                # 目标"，从而禁用下拉并说明原因，而不是渲染一个空下拉。
                meta["dynamic"] = True
        if self.type is FieldType.ARRAY:
            # 前端据此知道该渲染"多选"还是"多选（数字）"。不给 `items` 的话前端只能
            # 渲染一个纯文本数组输入框，用户就得自己记住要填数字。
            meta["items"] = self.items
        return meta


_JSON_TYPE: dict[FieldType, str] = {
    FieldType.STRING: "string",
    FieldType.TEXT: "string",
    FieldType.INTEGER: "integer",
    FieldType.NUMBER: "number",
    FieldType.BOOLEAN: "boolean",
    FieldType.DATE: "string",
    FieldType.DATETIME: "string",
    FieldType.ENUM: "string",
    FieldType.REF: "integer",
    FieldType.ARRAY: "array",
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# Annotated 简写：让能力声明读起来像一句话
# ---------------------------------------------------------------------------

#: 可写字段的类型标注形态：``Annotated[str, Field("塘口编号", required=True)]``
AnnotatedField = Annotated[Any, Field]


def text(label: str, **kwargs: Any) -> Any:
    return Annotated[str, Field(label, FieldType.TEXT, **kwargs)]


def f_str(label: str, **kwargs: Any) -> Any:
    return Annotated[str, Field(label, FieldType.STRING, **kwargs)]


def f_int(label: str, **kwargs: Any) -> Any:
    return Annotated[int, Field(label, FieldType.INTEGER, **kwargs)]


def f_num(label: str, **kwargs: Any) -> Any:
    return Annotated[Decimal, Field(label, FieldType.NUMBER, **kwargs)]


def f_bool(label: str, **kwargs: Any) -> Any:
    return Annotated[bool, Field(label, FieldType.BOOLEAN, **kwargs)]


def f_date(label: str, **kwargs: Any) -> Any:
    from datetime import date

    return Annotated[date, Field(label, FieldType.DATE, **kwargs)]


def f_datetime(label: str, **kwargs: Any) -> Any:
    from datetime import datetime

    return Annotated[datetime, Field(label, FieldType.DATETIME, **kwargs)]


def f_enum(label: str, choices: tuple[Choice, ...], **kwargs: Any) -> Any:
    return Annotated[str, Field(label, FieldType.ENUM, choices=choices, **kwargs)]


def f_ref(label: str, resource: str, *, label_key: str = "name", **kwargs: Any) -> Any:
    return Annotated[int, Field(label, FieldType.REF, ref=RefTarget(resource, label_key), **kwargs)]


def f_ref_by(label: str, resource_field: str, *, label_key: str = "name", **kwargs: Any) -> Any:
    """**多态**引用字段：目标资源由 ``resource_field`` 那个字段的取值决定。

    与 ``f_ref`` 分开而不是加一个可选参数：两者的签名不同（一个给资源名，一个给
    兄弟字段名），合在一起会让"传哪个"变成运行时才发现的错误。分开之后，
    ``f_ref(label, "area")`` 与 ``f_ref_by(label, "target_type")`` 在调用处一眼可辨。

    取值到资源的**约定**是"取值就是资源名"（`target_type="pond"` → 资源 `pond`）。
    这条约定不是新发明的：`ResourceRegistry` 的资源名与 `Capability.resource`、
    `RefTarget.resource` 本来就是同一个命名空间，多态只是把"选择哪个资源"从声明期
    挪到了运行期。
    """
    return Annotated[
        int,
        Field(
            label,
            FieldType.REF,
            ref=RefTarget(label_key=label_key, resource_field=resource_field),
            **kwargs,
        ),
    ]


def f_int_list(label: str, **kwargs: Any) -> Any:
    """``integer[]`` 字段（如 `role_ids` / `scope_ids`）。

    用独立的 helper 而不是 ``Field(..., type=FieldType.ARRAY, items="integer")``
    逐处写：元素类型是这个字段**唯一**的额外信息，而漏写 `items` 会让它退化成
    `string[]`（客户端传 `1` 与传 `"1"` 都被接受），症状是"看起来能用"。
    两个 helper 让"整数数组"在声明处只有一种写法。
    """
    return Annotated[list[int], Field(label, FieldType.ARRAY, items="integer", **kwargs)]


def f_str_list(label: str, **kwargs: Any) -> Any:
    """``string[]`` 字段（如 `permission_codes`）。"""
    return Annotated[list[str], Field(label, FieldType.ARRAY, items="string", **kwargs)]


@dataclass(slots=True)
class FieldSet:
    """一组字段声明，由 ``Capability.fields`` 构造。"""

    fields: dict[str, Field] = dataclass_field(default_factory=dict)

    def add(self, key: str, spec: Field) -> None:
        if key in self.fields:
            raise ValueError(f"字段 {key} 重复声明")
        self.fields[key] = spec

    def writable(self) -> dict[str, Field]:
        return dict(self.fields)

    def updatable(self) -> dict[str, Field]:
        return {key: spec for key, spec in self.fields.items() if spec.updatable}

    def required(self) -> tuple[str, ...]:
        return tuple(key for key, spec in self.fields.items() if spec.required)

    def columns(self) -> list[dict[str, Any]]:
        return [
            {"key": key, "label": spec.label}
            for key, spec in self.fields.items()
            if spec.list_column
        ]

    def items(self):
        return self.fields.items()

    def keys(self):
        return self.fields.keys()

    def values(self):
        return self.fields.values()

    def __len__(self) -> int:
        return len(self.fields)

    def __contains__(self, key: object) -> bool:
        return key in self.fields

    def __getitem__(self, key: str) -> Field:
        return self.fields[key]

    def __iter__(self):
        return iter(self.fields)


def collect_fields(annotations: dict[str, Any]) -> FieldSet:
    """从 ``Annotated[T, Field(...)]`` 标注里收集字段声明。

    这是把"能力声明"变成"可校验模型 + 前端表单 + 工具 schema"的机械步骤，
    没有任何人工映射表——早期版本那些 `FIELDS` / `RESERVED` 常量正是要消灭的东西。
    """
    result = FieldSet()
    for key, annotation in annotations.items():
        if key.startswith("_"):
            continue
        if get_origin(annotation) is not Annotated:
            continue
        args = get_args(annotation)
        spec = next((item for item in args[1:] if isinstance(item, Field)), None)
        if spec is None:
            continue
        result.add(key, spec)
    return result


TRUTHY = Literal[True]
