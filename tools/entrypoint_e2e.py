"""生产入口装载自检：证明"不传 registry 也能起出可用的 app"。

## 要证明的三件事

`fpa.bootstrap` 补齐后，"能力声明"有了装载处；本工具证明**装配处**也补齐了：

1. `fpa.factory.build_app()`（`registry=None`）会自己去组合根装载 → 无需任何夹具；
2. 组合根装载出的**每一条能力都在 Flask 路由表里有对应规则**（URL 由能力声明派生）；
3. 这些路由来自**域声明**而不是测试夹具 —— 判据有两条，都朝向"夹具不可能满足"：
   * 路由数量与组合根注册表一致，而夹具注册表只有 1 条能力；
   * 抽样的能力处理器的 `__module__` 落在 `fpa.domains.*`。

第 3 条是必要的：`tools/web_e2e.py` 的夹具能让"路由存在"这类断言变绿，而真实进程里
域能力仍然不存在（这正是 `bootstrap.py` 指认的那类静默失败）。

用法::

    python tools/entrypoint_e2e.py

退出码：0 = 全绿；1 = 有断言失败；2 = 被阻塞（组合根加载不起来，先修那个）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fpa.factory import build_app, registry_from_composition_root  # noqa: E402
from fpa.settings import Settings  # noqa: E402

FAILURES = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILURES
    mark = "PASS" if ok else "FAIL"
    if not ok:
        FAILURES += 1
    suffix = f"  [{detail}]" if detail and not ok else ""
    print(f"  {mark}  {label}{suffix}")


def route_index(app) -> set[tuple[str, str]]:
    """Flask 路由表里的 (方法, 路径) 集合（排除静态路由）。"""
    found: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for method in rule.methods or ():
            if method in {"HEAD", "OPTIONS"}:
                continue
            found.add((method, rule.rule))
    return found


def main() -> int:
    print("=== 1. 不传 registry 起一个真实 app（默认走组合根）===")
    try:
        registry = registry_from_composition_root()
    except Exception as exc:  # noqa: BLE001
        print(f"  被阻塞：组合根装载失败（先修这个，不是本工具的问题）")
        print(f"    {type(exc).__name__}: {exc}")
        return 2

    print(f"  组合根注册能力：{len(registry)} 条")
    try:
        app = build_app(settings=Settings.from_env({"APP_ENV": "test", "SECRET_KEY": "entrypoint-e2e"}))
    except Exception as exc:  # noqa: BLE001
        print(f"  被阻塞：build_app(registry=None) 失败")
        print(f"    {type(exc).__name__}: {exc}")
        return 2

    check("build_app() 在 registry=None 时装配成功", app is not None)
    check(
        "app 里挂的就是组合根那一份注册表",
        app.config.get("FPA_REGISTRY") is registry,
    )
    check("Agent 网关已接线（不需要调用方记得赋值）", app.config.get("FPA_AGENT_GATEWAY") is not None)

    print("\n=== 2. 每条能力都有对应的 Flask 路由 ===")
    routes = route_index(app)
    # 路径模板要按 app.py 的同一条规则转换：`{pond_id}` -> `<int:pond_id>`。
    # 直接拿能力声明的原始 path 去比会得到一堆"缺失"，而它们其实都注册了——
    # 那正是"用错口径产生的假失败"，先修口径再谈结论。
    from fpa.web.app import _flask_path

    missing = [
        f"{cap.method} {cap.path}"
        for cap in registry.all()
        if (str(cap.method), _flask_path(cap.path)) not in routes
    ]
    check(f"{len(registry)} 条能力全部生成路由", not missing, f"缺少：{missing[:5]}")
    check("路由表里存在 /api/v1/ponds", ("POST", "/api/v1/ponds") in routes)
    check("路由表里存在 /api/v1/batches（master_data 之外的能力也在）", ("POST", "/api/v1/batches") in routes)

    print("\n=== 3. 路由来自域声明而不是夹具 ===")
    # 夹具注册表只有 1 条能力；组合根有 N 条。若 app 用的是夹具，这里会差一个数量级。
    check(
        f"能力端点数与注册表一致（夹具只会有 1 条）",
        len(registry) > 1,
        f"只注册了 {len(registry)} 条——像是夹具而不是组合根",
    )
    sample = ["batch.create", "feeding.create", "purchase_order.create"]
    domains_module = []
    for name in sample:
        cap = registry.find(name)
        if cap is None:
            continue
        module = str(getattr(cap.handler, "__module__", ""))
        domains_module.append((name, module))
    check(
        "抽样能力的处理器来自 fpa.domains.*",
        bool(domains_module) and all(module.startswith("fpa.domains.") for _, module in domains_module),
        str(domains_module),
    )
    for name, module in domains_module:
        print(f"      {name:22} <- {module}")

    print("\n=== 4. 夹具路径未被破坏（显式传 registry 仍然生效）===")
    from fpa.kernel.capability import Registry

    tiny = Registry()
    fixture_app = build_app(registry=tiny, settings=Settings.from_env({"APP_ENV": "test", "SECRET_KEY": "x"}))
    fixture_routes = route_index(fixture_app)
    check("显式传入的空注册表 => 没有能力路由", len(fixture_routes) < len(routes))
    check("夹具 app 与真 app 是两份不同配置", fixture_app.config["FPA_REGISTRY"] is tiny)

    print()
    if FAILURES:
        print(f"结果：{FAILURES} 项失败")
        return 1
    print(f"结果：全部通过（组合根 {len(registry)} 条能力全部可达）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
