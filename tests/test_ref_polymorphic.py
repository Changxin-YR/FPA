"""`RefTarget` 的多态形态（`resource_field`）与它的真实消费点。

## 这个文件为什么存在

`cost.entry.create` 的「归属对象」（`target_id`）曾经声明成 ``f_int`` ——
元数据因此告诉前端"这是个整数输入框"，于是用户看到一个要求自己填 id 的数字框，
既不知道填哪个、也无从知道合法值有哪些。**用户报的"登记对象不可输入"就是它。**

修法不是在前端加特例，而是让声明能表达"本字段引用哪类对象，由另一个字段的取值
决定"。这个文件同时钉住三件事：

1. 多态形态的**契约**（二选一、取值为空不是错误）；
2. `cost.entry.create` 的**真实元数据**必须带 `ref.resource_field`；
3. **两处描述同一件事的地方必须一致**：`target_type` 的枚举取值
   （`TARGET_TYPES`）与"取值 → 物理表"的映射（`_TARGET_TABLE`）是两份表，
   而多态引用要求前者每个取值都是 `ResourceRegistry` 里真实存在的资源名。
   少了这条断言，加一个 `target_type` 取值就会得到一个"永远拉不到候选"的下拉。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from fpa.kernel.errors import DomainError
from fpa.kernel.fields import Field, FieldType, RefTarget, f_ref_by
from fpa.kernel.scope import Scope, scope_predicate_for_columns
from fpa.kernel.workflow import RESOURCES

from conftest import load_all_status


# ---------------------------------------------------------------------------
# 1) RefTarget 本身的契约
# ---------------------------------------------------------------------------


def test_静态形态仍然可用并保持既有签名默认值():
    target = RefTarget("area")
    assert target.polymorphic is False
    assert target.resource_for("pond") == "area", "静态形态忽略传入值"


def test_多态形态按取值解析资源名():
    target = RefTarget(resource_field="target_type")
    assert target.polymorphic is True
    assert target.resource_for("pond") == "pond"
    assert target.resource_for(" batch ") == "batch", "取值两端空白要剪掉"


def test_未选类型时返回空串而不是抛错():
    """「还没选」不是错误，只是还没有目标——前端据此禁用下拉并说明原因。"""
    target = RefTarget(resource_field="target_type")
    assert target.resource_for(None) == ""
    assert target.resource_for("") == ""


@pytest.mark.parametrize("kwargs", [{}, {"resource": "area", "resource_field": "target_type"}])
def test_两处真相或没有真相的声明在构造期就报错(kwargs):
    """两个都写就有两处真相，两个都不写就无法解析地址——都不许静默通过。"""
    with pytest.raises(ValueError, match="必须且只能给出"):
        RefTarget(**kwargs)


def test_多态引用的列表地址来自资源注册表_不猜():
    target = RefTarget(resource_field="target_type")
    assert target.resolved_list_path("pond") == "/api/v1/ponds"
    assert target.resolved_list_path("area") == "/api/v1/areas"
    with pytest.raises(ValueError, match="没有可用列表地址"):
        target.resolved_list_path("no-such-resource")
    with pytest.raises(ValueError, match="还没有取值"):
        target.resolved_list_path()


def test_多态形态下自带的_list_path_会被忽略():
    """`list_path` 是"某一个静态资源"的地址；多态下它必然只对其中一类成立。"""
    target = RefTarget(resource_field="target_type", list_path="/api/v1/ponds")
    assert target.resolved_list_path("area") == "/api/v1/areas"


def test_f_ref_by_生成多态_ref_字段():
    annotation = f_ref_by("归属对象", "target_type", minimum=1, help="随类型切换")
    spec = next(item for item in annotation.__metadata__ if isinstance(item, Field))
    assert spec.type is FieldType.REF
    assert spec.ref is not None
    assert spec.ref.resource_field == "target_type"
    assert spec.ref.resource == ""
    assert spec.minimum == 1


def test_多态_ref_的元数据下发_resource_field_并标注_dynamic():
    spec = Field("归属对象", FieldType.REF, ref=RefTarget(resource_field="target_type"))
    meta = spec.to_meta("target_id")
    assert meta["type"] == "ref"
    assert meta["ref"] == {
        "resource": "",
        "label_key": "name",
        "resource_field": "target_type",
    }
    # `dynamic: true` 与 `dynamic_choices` 同一口径：前端据此知道"现在没有精确目标"。
    assert meta["dynamic"] is True


def test_静态_ref_的元数据也带_resource_field_键但为空串():
    spec = Field("所属区域", FieldType.REF, ref=RefTarget("area"))
    meta = spec.to_meta("area_id")
    assert meta["ref"]["resource"] == "area"
    assert meta["ref"]["resource_field"] == ""
    assert "dynamic" not in meta


# ---------------------------------------------------------------------------
# 2) 真实消费点：cost.entry.create
# ---------------------------------------------------------------------------


def test_cost_entry_create_的归属对象声明成多态引用():
    load_all_status()
    from fpa.kernel.capability import REGISTRY

    capability = REGISTRY.find("cost.entry.create")
    assert capability is not None, "成本登记能力必须已注册"

    fields = {item["key"]: item for item in capability.to_meta()["fields"]}

    target_id = fields["target_id"]
    assert target_id["type"] == "ref", "归属对象是引用，不是整数输入框"
    assert target_id["ref"]["resource_field"] == "target_type"
    assert target_id["ref"]["resource"] == "", "多态形态不给静态资源名"

    target_type = fields["target_type"]
    assert target_type["type"] == "enum"
    # 两个都可选：registry §2.10 的 required 列都是空的；非法的是"只给一个"
    # （服务层与 DB CHECK 保证一致性），不是"两个都不给"。
    assert target_type["required"] is False
    assert target_id["required"] is False


#: `target_type` 里没有列表资源的取值；所有多态候选现在都必须可列举。
#:
_TARGET_TYPES_WITHOUT_RESOURCE: frozenset[str] = frozenset()


def test_target_type_除具名缺口外的每个取值都是真实存在的资源():
    """**多态引用的隐含前提**：取值就是资源名，所以每个取值都得是注册过的资源。

    这条断言把"`target_type` 的枚举取值"与"`ResourceRegistry` 的资源名"钉在一起。
    少了它，加一个取值（或改一个资源名）就会静默得到一个**永远拉不到候选**的下拉
    —— 空下拉的成因看起来与"这个基地确实没有数据"一模一样。

    当前所有目标类型都必须有列表资源，缺口集合为空。
    """
    load_all_status()
    from fpa.domains.cost.service import TARGET_TYPES

    missing = {code for code in TARGET_TYPES if RESOURCES.find(code) is None}
    assert missing == set(_TARGET_TYPES_WITHOUT_RESOURCE), (
        "无列表资源的 target_type 取值集合变了："
        f"实测 {sorted(missing)}，登记在案的缺口是 {sorted(_TARGET_TYPES_WITHOUT_RESOURCE)}。"
        "新出现的取值要么补上该资源的 list 能力，要么按同样的方式登记进那个常量。"
    )
    assert TARGET_TYPES


def test_farm资源把基地范围映射到自身的_id列():
    scope = Scope.from_rows(
        [{"scope_type": "farm", "farm_id": 7}], user_id=1
    )
    fragment, params = scope_predicate_for_columns(
        scope, ("id", "organization_id"), "f", farm_column="id"
    )
    assert fragment == "f.id IN (%s)"
    assert params == [7]


def test_归属对象取值到物理表的映射与枚举取值一一对应():
    """`_TARGET_TABLE` 是"取值 → 真实表名"的唯一处，取值集合必须与枚举一致。

    两张表回答的是两个不同问题（引用哪类 API 资源 / 校验哪张物理表），因此都得有；
    但**取值集合**必须是同一套，否则会出现"前端能选、后端不认"或反过来的字段。
    """
    load_all_status()
    from fpa.domains.cost.entries_write import _TARGET_TABLE
    from fpa.domains.cost.service import TARGET_TYPES

    assert set(_TARGET_TABLE) == set(TARGET_TYPES)


def test_归属对象类型与对象必须同时给出():
    """`required=False` **不是**"随便给一个就行"：服务层拒绝半给。

    这条是"保留可选"的前提条件——若半给不会被拒，那 `required=False` 就真的变成
    "两个都能空、等于没选"，那时才该把 `target_type` 改成必填。所以本用例同时守着
    那条"半给必拒"的实现（它一旦被删，必填与否的判断依据就变了）。
    """
    load_all_status()
    from fpa.domains.cost import entries_write

    source = inspect.getsource(entries_write.CostWriteService.create_entry)
    assert "(target_type is None) != (target_id is None)" in source
    assert "必须同时给出" in source
    # 是**抛错**而不是只写注释：同一函数里必须出现该异常的构造点。
    assert DomainError.__name__ in source


def test_多态_ref_不改变_Agent_工具_schema_的参数类型():
    """元数据形态变了，**模型侧契约不变**（同一个 Field 供给三处消费方）。

    多态只影响"前端渲染什么控件、候选从哪拉"；`target_id` 对模型仍是 `integer`
    （`_JSON_TYPE[FieldType.REF] == "integer"`）。若哪天有人为了前端方便把 ref 的
    JSON Schema 类型改成 `object` 之类，模型会开始传错类型——所以这里钉住。
    """
    load_all_status()
    from fpa.kernel.capability import REGISTRY

    capability = REGISTRY.find("cost.entry.create")
    assert capability is not None
    properties = capability.to_tool_schema()["properties"]
    assert properties["target_id"]["type"] == "integer"
    assert properties["target_type"]["type"] == "string"
    assert properties["target_type"]["enum"] == ["farm", "area", "pond", "batch"]
    # 两个都可选 —— 与元数据一致（见上一条用例的说明）
    required = capability.to_tool_schema()["required"]
    assert "target_id" not in required
    assert "target_type" not in required


def _ddl_columns(table: str) -> set[str]:
    """从迁移文件里读出某张表的列名（DDL 是表结构的唯一真相）。

    刻意**不**连库：本文件其余用例都是纯单测，连库会让这条守卫在没 MySQL 的
    环境里变成 skip（skip 不是"通过"）。解析 `CREATE TABLE` 块即可，且这正是
    我们要比对的那一份文本。
    """
    migrations = Path(__file__).resolve().parents[1] / "database" / "migrations"
    pattern = re.compile(
        rf"CREATE TABLE IF NOT EXISTS {table} \((?P<body>.*?)\n\)\s*ENGINE", re.S
    )
    for path in sorted(migrations.glob("*.sql")):
        found = pattern.search(path.read_text(encoding="utf-8"))
        if not found:
            continue
        columns: set[str] = set()
        for raw in found.group("body").splitlines():
            line = raw.strip()
            if not line or line.startswith("--"):
                continue
            if re.match(r"(PRIMARY KEY|UNIQUE KEY|KEY|INDEX|CONSTRAINT|FOREIGN KEY)\b",
                        line, re.IGNORECASE):
                continue
            head = re.match(r"([a-z_][a-z0-9_]*)\s+\S", line)
            if head:
                columns.add(head.group(1))
        return columns
    raise AssertionError(f"迁移文件里找不到 {table} 的 CREATE TABLE 块")


def test_归属对象解析出的租户键仍按归属对象查():
    """多态只改**声明**，不改服务行为：租户键仍按归属对象解析。

    ## 这条用例同时是 D2 的回归守卫（原缺陷：farm/area 提交必 503）

    原来的写法是"每张目标表都 `SELECT organization_id, farm_id, area_id`"，
    隐含假设"每张目标表都有这三列" —— 而 `farms` 只有 `organization_id`、
    `areas` 只有 `organization_id`/`farm_id`。于是 `target_type=farm`/`area`
    必然抛 MySQL 1054，被兜底成 `503 数据库服务暂时不可用`。

    **只钉"表名对"是不够的**（原来那版就钉了表名、却漏了列）：所以要拿
    `_TARGET_TABLE` 里的**取值表达式**去和 DDL 的真实列名比对 —— 表达式引用了
    目标表里不存在的列，这里就红。这条判据与"哪张表缺哪一层"无关，将来加层级
    同样有效。
    """
    load_all_status()
    from fpa.domains.cost.entries_write import _TARGET_TABLE

    assert set(_TARGET_TABLE) == {"farm", "area", "pond", "batch"}
    expected_tables = {
        "farm": "farms",
        "area": "areas",
        "pond": "ponds",
        "batch": "production_batches",
    }
    for target_type, (table, id_column, farm_expr, area_expr) in _TARGET_TABLE.items():
        assert table == expected_tables[target_type], f"{target_type} 的表名不对"
        assert id_column == "id", f"{target_type} 的主键列不对"

        columns = _ddl_columns(table)
        assert columns, f"{table} 的列解析为空集（解析器坏了，不是表坏了）"
        assert "organization_id" in columns, f"{table} 没有 organization_id"

        assert id_column in columns, f"{table} 没有 {id_column}"
        # `farm_id` / `area_id` 的取法必须落在该表**真实存在**的列上；
        # None 表示"这个层级在这张表上不存在"（落库为 NULL，例如基地没有区域）。
        for layer, expr in (("farm_id", farm_expr), ("area_id", area_expr)):
            if expr is None:
                continue
            assert expr in columns, (
                f"{target_type} 的 {layer} 取法引用了 {table} 里不存在的列 {expr!r}"
                f"（该表实际列：{sorted(columns)}）—— 这正是 503 的成因"
            )
