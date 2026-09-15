"""真实 HTTP 响应上的**显示契约**守卫（t10 的验收形态）。

## 为什么在 `tests/test_wire_format.py` 之外还要这一层

`test_wire_format.py` 测的是 **provider 本身**（`app.json.dumps({"t": datetime(...)})`）。
那证明"这一处换对了"，但**证明不了"真实资源响应里没有漏网的裸串"** —— 因为：

  * 某个服务可能在 `decorate()` 里就把它 `str()` 成了别的形状（绕过 provider）；
  * 某个列可能是 `VARCHAR` 存了已经格式化过的字符串；
  * 新增一个资源/列时，没有人会记得去跑一遍 provider 单测。

所以这一层**从真实路由取真实响应**，按**独立重算**的口径判定：
它拿资源元数据的 `list_path` 去请求，并按**数据库里的真实列类型**判断
"这一列该给日期还是时刻"。**刻意不 import 前端代码，也不复用服务端的 decorate 逻辑**
—— 与被测实现共用同一套判断，就成了同义反复（本仓已归档的教训：
"夹具与实现共享同一错误假设时，测试从验证退化成复述"）。

## 它钉死的三条

1. **不得出现 `GMT` / RFC 1123 串**（`Wed, 02 Sep 2026 00:00:00 GMT`）；
2. **`date` 列只给 `YYYY-MM-DD`、`datetime` 列给 ISO 时刻** —— 一一对应，
   以**数据库列类型**为准（那是唯一能独立取得的权威）；
3. **不得出现 5 位以上小数的字符串**（金额派生值的静默尾数）。

## 依赖与跳过

需要真实 MySQL（与其它 `tests/` 一致，从环境变量取连接）。连不上库时
**显式 skip 并说明原因**，不静默通过 —— 空集报通过是本仓踩过的坑。
"""

from __future__ import annotations

import json
import re

import pytest

#: RFC 1123 / 裸 `GMT` 串 —— 缺陷 1 的指纹。
_GMT = re.compile(r"GMT|^[A-Z][a-z]{2}, ")
#: 5 位以上小数 —— 缺陷 2 的指纹（金额派生值未量化）。
_MANY_DECIMALS = re.compile(r"^-?\d+\.\d{5,}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _connection():
    """真实 MySQL 连接（与 `kernel/uow_factory` 同源的环境变量）。"""
    import pymysql

    from fpa.kernel.uow_factory import connection_config

    return pymysql.connect(**connection_config().as_kwargs())


def _readable_resources() -> list[tuple[str, str]]:
    """`(list_path, resource_name)`，只取**当前账号有读能力**的资源。"""
    from fpa.bootstrap import load_all
    from fpa.kernel.capability import REGISTRY
    from fpa.kernel.workflow import RESOURCES

    load_all()
    readable = {
        cap.resource for cap in REGISTRY.all() if cap.kind == "read" and cap.resource
    }
    result: list[tuple[str, str]] = []
    for resource in RESOURCES.all():
        if resource.name in readable and resource.list_path:
            result.append((resource.list_path, resource.name))
    return result


@pytest.fixture(scope="module")
def client_and_actor():
    """真 Flask test_client + 一个真登录的账号（`client` 会带 Cookie）。"""
    from fpa.factory import build_app

    try:
        connection = _connection()
        connection.close()
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"连不上真实 MySQL，本组断言无法执行（不是通过）：{error}")

    app = build_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # 用一个真实存在、且**有读权限**的账号登录。不存在则 skip 并说明
    # （不造账号：这一组测的是"真实响应"，造出来的账号会引入另一处夹具假设）。
    username = "demo"
    response = client.post(
        "/api/v1/auth/login",
        json={"identifier": username, "password": "Demo1234!"},
    )
    if response.status_code != 200:
        pytest.skip(
            f"演示账号 {username!r} 登录失败（status={response.status_code}），"
            "本组断言需要真实数据才能判定（不是通过）"
        )
    return client


def test_no_rfc1123_or_gmt_in_any_resource_response(client_and_actor) -> None:
    """**任何**可读资源的**任何**字符串字段，都不得是 RFC 1123 / GMT 串。

    这是缺陷 1 的端到端判据，覆盖全部资源而不是截图里那两列。
    """
    client = client_and_actor
    resources = _readable_resources()
    assert resources, "一个可读资源都没有 —— 判据失效（空集不是通过）"

    offenders: list[str] = []
    scanned_rows = 0
    for list_path, name in resources:
        separator = "&" if "?" in list_path else "?"
        response = client.get(f"{list_path}{separator}page=1&page_size=5")
        if response.status_code != 200:
            continue
        items = (response.get_json().get("data") or {}).get("items") or []
        for row in items:
            scanned_rows += 1
            for key, value in row.items():
                if isinstance(value, str) and _GMT.search(value):
                    offenders.append(f"{name}.{key} = {value!r}")

    assert scanned_rows > 0, (
        "一行数据都没扫到 —— 这条断言无法发现任何问题。"
        "（本仓把「空集报通过」列为缺陷，所以这里显式失败而不是放过。）"
    )
    assert offenders == [], f"响应里仍有 GMT/RFC1123 串：{offenders}"


def test_no_over_precise_decimal_strings(client_and_actor) -> None:
    """字符串形态的数字不得带 5 位以上小数（金额派生值未量化的指纹）。"""
    client = client_and_actor
    offenders: list[str] = []
    scanned = 0
    for list_path, name in _readable_resources():
        separator = "&" if "?" in list_path else "?"
        response = client.get(f"{list_path}{separator}page=1&page_size=5")
        if response.status_code != 200:
            continue
        for row in (response.get_json().get("data") or {}).get("items") or []:
            for key, value in row.items():
                if isinstance(value, str):
                    scanned += 1
                    if _MANY_DECIMALS.match(value):
                        offenders.append(f"{name}.{key} = {value!r}")
    assert scanned > 0, "一个字符串字段都没扫到 —— 判据失效"
    assert offenders == [], f"仍有 5 位以上小数的字符串：{offenders}"


def test_date_and_datetime_shapes_match_the_column_types(client_and_actor) -> None:
    """`date` 列只给日期、`datetime` 列给 ISO 时刻 —— **以数据库列类型为准**。

    负责人 给的定位线索就是这个差异：同一响应里 `expected_delivery_date` 是
    `2026-09-26`（干净）而 `created_at` 是 RFC 1123 串。修完之后两者的**区分**
    必须与列类型一一对应：
      * 库是 `datetime` 却只给 `YYYY-MM-DD` → 丢了时间（静默）；
      * 库是 `date` 却给了 `T00:00:00` → 补出了不存在的时间（静默）。

    **独立来源**：列类型从 `information_schema` 现查，不复用项目的字段声明 ——
    否则就是"用被测实现去验证被测实现"。
    """
    client = client_and_actor
    connection = _connection()
    cursor = connection.cursor()
    mismatches: list[str] = []
    compared = 0

    for list_path, name in _readable_resources():
        # 资源名 → 真实表名（不猜：从迁就存在的表里挑名字最接近的两种形态）
        table = None
        for candidate in (name, f"{name}s"):
            cursor.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_name=%s",
                (candidate,),
            )
            if cursor.fetchone():
                table = candidate
                break
        if table is None:
            continue
        cursor.execute(f"SHOW COLUMNS FROM `{table}`")
        # 连接配置用的是 `DictCursor`（与项目其余地方同源），
        # 所以这里按**列名**取，不按下标 —— 按下标会拿到键 0 而报 `KeyError: 0`。
        column_types = {
            str(row["Field"]): str(row["Type"]) for row in cursor.fetchall()
        }

        separator = "&" if "?" in list_path else "?"
        response = client.get(f"{list_path}{separator}page=1&page_size=3")
        if response.status_code != 200:
            continue
        for row in (response.get_json().get("data") or {}).get("items") or []:
            for key, value in row.items():
                if not isinstance(value, str) or key not in column_types:
                    continue
                declared = column_types[key]
                is_datetime = declared.startswith("datetime") or declared.startswith("timestamp")
                if _ISO_DATE.match(value) and is_datetime:
                    mismatches.append(f"{name}.{key}: 库是 {declared} 但只给了日期 {value!r}")
                elif _ISO_DATETIME.match(value) and not is_datetime:
                    mismatches.append(f"{name}.{key}: 库是 {declared} 但给了时刻 {value!r}")
                if _ISO_DATE.match(value) or _ISO_DATETIME.match(value):
                    compared += 1

    connection.close()
    assert compared > 0, "一个日期形态的字段都没比对到 —— 判据失效"
    assert mismatches == [], f"日期形态与列类型不符：{mismatches}"


def test_json_dumps_output_is_parseable_back(client_and_actor) -> None:
    """响应里的日期串必须能被 `date.fromisoformat` / `datetime.fromisoformat` 解析。

    这条是"给的是可解析的值、而不是漂亮的文案"的机械判据：前端要拿它填
    `<input type="date">` 的 `value`，所以**必须**能解析回去。
    （中文格式化是前端的事 —— 后端给了 `2026年9月2日` 反而会让前端无法解析。）
    """
    client = client_and_actor
    checked = 0
    for list_path, _name in _readable_resources():
        separator = "&" if "?" in list_path else "?"
        response = client.get(f"{list_path}{separator}page=1&page_size=3")
        if response.status_code != 200:
            continue
        for row in (response.get_json().get("data") or {}).get("items") or []:
            for value in row.values():
                if not isinstance(value, str):
                    continue
                if _ISO_DATE.match(value):
                    __import__("datetime").date.fromisoformat(value)
                    checked += 1
                elif _ISO_DATETIME.match(value):
                    __import__("datetime").datetime.fromisoformat(value)
                    checked += 1
    assert checked > 0, "没有一个日期值可解析 —— 判据失效"
