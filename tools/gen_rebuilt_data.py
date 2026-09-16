"""从**当前库 + 当前组合根**现算生成 `docs/CAPABILITY_REGISTRY.md`。

## 为什么是自动生成

交付文档手工维护必然漂移：实测出现过"文档还写 BZ-* 数据集、库里已经是
P-*/PO-2026-*"（review 明确点名）。本工具把"文档 == 当前库"变成一条可复跑的命令。

## 口径

* 与前端列表页完全同源：用 `/api/v1/meta/capabilities` 的 `resources[].list_path`
  与 `resources[].columns`（"页面所见"就是这些列）；
* 每个资源最多列前 10 行，其余只给总数——文档用于核对，不替代导出；
* 全局事实（用户外键数 / 关账期间数）直接查库，属于"文档里最容易写错的那类数字"。

用法::

    $env:MYSQL_PASSWORD='yuxin_dev_password'
    python tools/gen_rebuilt_data.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from yuxin.factory import build_app  # noqa: E402
from yuxin.kernel.uow import UnitOfWork  # noqa: E402
from yuxin.kernel.uow_factory import connection_config  # noqa: E402

OUT = ROOT / ".verify" / "REBUILT_DATA.md"
DEMO_USER = "demo"
DEMO_PASSWORD = "Demo1234!"
MAX_ROWS = 10

#: 需要单列一节的“身份/权限”资源；其余进“业务数据”。
IDENTITY_RESOURCES = {"access_user", "access_role", "access_scope"}


def _cell(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().replace("\n", " ")
    return text or None


def _query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with UnitOfWork(connection_config()).begin() as tx:
        return tx.query_all(sql, params)


def _scalar(sql: str, params: tuple = ()) -> Any:
    with UnitOfWork(connection_config()).begin() as tx:
        return tx.query_scalar(sql, params)


def _resource_section(client, resource: dict[str, Any]) -> list[str]:
    title = str(resource.get("title") or resource.get("name") or "")
    list_path = resource.get("list_path")
    if not list_path:
        return []
    separator = "&" if "?" in str(list_path) else "?"
    response = client.get(f"{list_path}{separator}page=1&page_size=100")
    head = f"### {title}（{list_path}）"
    if response.status_code != 200:
        return [head, "", f"> 跳过：HTTP {response.status_code}", ""]
    payload = (response.get_json() or {}).get("data")
    if not isinstance(payload, dict):
        return [head, "", "> 跳过：响应 data 不是分页对象", ""]
    items = payload.get("items") or []
    total = int(payload.get("total", len(items)) or 0)
    columns = [c for c in (resource.get("columns") or []) if c.get("key")]
    shown = items[:MAX_ROWS]
    lines = [head, "", f"共 {total} 条", ""]
    if not shown:
        lines.append("- （空）")
        lines.append("")
        return lines
    for item in shown:
        if not isinstance(item, dict):
            continue
        parts: list[str] = []
        if "id" in item:
            value = _cell(item["id"])
            if value is not None:
                parts.append(f"id={value}")
        for column in columns:
            key = str(column.get("key"))
            label = str(column.get("label") or key)
            value = _cell(item.get(key))
            if value is not None:
                parts.append(f"{label}={value}")
        lines.append("- " + " | ".join(parts))
    remaining = total - len(shown)
    if remaining > 0:
        lines.append(f"- …余 {remaining} 条")
    lines.append("")
    return lines


def main() -> int:
    app = build_app()
    app.config["TESTING"] = True
    client = app.test_client()
    login = client.post(
        "/api/v1/auth/login", json={"identifier": DEMO_USER, "password": DEMO_PASSWORD}
    )
    if login.status_code != 200:
        print(f"FAIL  登录失败 status={login.status_code}: {login.get_data(as_text=True)[:200]}")
        return 2

    meta_response = client.get("/api/v1/meta/capabilities")
    if meta_response.status_code != 200:
        print(f"FAIL  取元数据失败 status={meta_response.status_code}")
        return 2
    meta = (meta_response.get_json() or {}).get("data") or {}
    resources = meta.get("resources") or []
    if not resources:
        print("FAIL  元数据里没有 resources —— 空输入不是通过")
        return 1

    user_counts = {
        str(row["status"]): int(row["n"])
        for row in _query("SELECT status, COUNT(*) AS n FROM users GROUP BY status")
    }
    fk_count = int(
        _scalar(
            "SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME='users'"
        )
        or 0
    )
    period_total = int(_scalar("SELECT COUNT(*) FROM accounting_periods") or 0)
    period_closed = int(
        _scalar("SELECT COUNT(*) FROM accounting_periods WHERE status<>'open'") or 0
    )

    identity = [r for r in resources if str(r.get("name")) in IDENTITY_RESOURCES]
    business = [r for r in resources if str(r.get("name")) not in IDENTITY_RESOURCES]

    lines: list[str] = [
        "# 交付数据集（由当前库现算生成）",
        "",
        "> 由 `tools/gen_rebuilt_data.py` 在**当前库 + 当前组合根**上现算生成；",
        "> 每个资源用与前端列表页**完全同源**的 `list_path` + `columns`（即页面所见）。",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}（本机时区）",
        "",
        "## 0. 全局事实",
        "",
        f"- 启用账号：{user_counts.get('active', 0)}；已注销：{user_counts.get('retired', 0)}；"
        f"强制改密：{user_counts.get('must_change_password', 0)}",
        f"- 用户行被业务外键引用：{fk_count} 处",
        f"- 会计期间：{period_total} 条，其中已关账 {period_closed} 条",
        "",
        "## 1. 账号 / 角色 / 数据范围",
        "",
    ]
    for resource in sorted(identity, key=lambda r: str(r.get("title") or "")):
        lines.extend(_resource_section(client, resource))
    lines.extend(["## 2. 业务数据（按资源）", ""])
    for resource in sorted(business, key=lambda r: str(r.get("title") or "")):
        lines.extend(_resource_section(client, resource))

    # ★ 必须用 write_bytes：Windows 上 `Path.write_text` 走文本模式，会把 `\n`
    # 翻译成 `os.linesep`（CRLF），而 `tools/check_source_hygiene.py` 要求统一 LF。
    # 这个坑会让"生成文档"这一步自己污染源码卫生门禁。
    OUT.write_bytes(("\n".join(lines).rstrip() + "\n").encode("utf-8"))
    print(
        f"OK  已生成 {OUT.relative_to(ROOT)}（{len(resources)} 个资源；"
        f"账号 {user_counts}；用户外键 {fk_count} 处；关账 {period_closed}/{period_total}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
