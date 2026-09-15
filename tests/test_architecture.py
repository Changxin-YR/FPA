"""架构约束的可执行形态。

`DEVELOPMENT.md` §6 列了三条"不可违反"，但它在写这份文档时自己标注了
"依赖方向由 tests 约束（**待补**：`test_architecture.py`）"——也就是说
在那之前，这三条只是文档里的句子，没有任何东西**强制**它们。

本文件把它们变成断言：违反会在 `python -m pytest tests` 里失败，
而不是靠下一个人读到 §6 时记得。这是本项目那条纪律的应用——
"任何两处描述同一件事的地方，都要删掉一处"：约束要么在文档里、要么在测试里，
两者并存时必须让它们指向同一个判定。
"""

from __future__ import annotations

import os
from pathlib import Path

from source_index import (
    BACKEND as _BACKEND,
    PACKAGE as _PACKAGE,
    imported as _imported,
    matches as _matches,
    module_name as _module_name,
    python_files as _python_files,
)

import fpa.bootstrap as bootstrap
from fpa.kernel.capability import NO_LOADER, REGISTRY
from fpa.kernel.workflow import RESOURCES

# ---------------------------------------------------------------------------
# 依赖方向
# ---------------------------------------------------------------------------


def test_kernel_has_no_web_or_business_dependency() -> None:
    """内核不得依赖 Web 框架与业务层——否则内核就无法被单独测试。

    这是 `DEVELOPMENT.md` §6.3 的字面条款，实测成立。
    """
    offenders: dict[str, list[str]] = {}
    for path in _python_files(_PACKAGE / "kernel"):
        bad = sorted(
            module
            for module in _imported(path)
            if _matches(module, "flask") or _matches(module, "fpa.domains")
        )
        if bad:
            offenders[_module_name(path)] = bad
    assert offenders == {}, f"内核出现了对 Web/业务层的依赖：{offenders}"


def test_mysql_driver_is_confined_to_one_module() -> None:
    """MySQL 驱动只允许出现在**一个**模块里。

    `DEVELOPMENT.md` §6.3 写的是"内核不 import flask / pymysql / domains"，
    但实测 `kernel/uow.py:26` 确实 `import pymysql` —— 那是有意为之：
    `UnitOfWork` 就是事务边界，它必须持有连接。所以那条约束的**真实意图**
    不是"内核不许碰 pymysql"（那会让 uow 无处安放），而是
    "驱动接入与错误翻译只有一个落点"（早期版本在每个 store 里各写一遍 errno 判断、
    以及用异常消息文本匹配约束类型）。

    本测试按意图断言：静态 import pymysql 的模块集合必须恰好是
    `{fpa.kernel.uow}`。这条比原文更强——它同时禁止了"第二个地方也接驱动"。
    """
    modules = {
        _module_name(path)
        for path in _python_files(_PACKAGE)
        if any(_matches(module, "pymysql") for module in _imported(path))
    }
    assert modules == {"fpa.kernel.uow"}, (
        "MySQL 驱动只能出现在 fpa.kernel.uow；实测出现在："
        f"{sorted(modules)}"
    )


def test_web_and_agent_layers_never_touch_the_database() -> None:
    """`web/` 与 `agent/` 只翻译协议、不写 SQL。

    判据是"它们从不调用事务对象的取数接口"。这比正则匹配 SQL 关键字可靠：
    注释与文档字符串里出现 `SELECT` 是正常的（本项目大量引用早期实现位置），
    而调用 `query_one` / `query_all` / `.execute(` 才是真的在碰库。
    """
    tokens = ("query_one(", "query_all(", ".execute(")
    offenders: dict[str, list[str]] = {}
    for layer in ("web", "agent"):
        for path in _python_files(_PACKAGE / layer):
            text = path.read_text(encoding="utf-8")
            hits = [token for token in tokens if token in text]
            if hits:
                offenders[_module_name(path)] = hits
    assert offenders == {}, f"Web/Agent 层在访问数据库：{offenders}"


# ---------------------------------------------------------------------------
# 装配完整性（组合根）
# ---------------------------------------------------------------------------


def test_every_declared_domain_registers_at_least_one_capability(load_all_status) -> None:
    """声明了 `capabilities.py` 的域，装载后必须真的有注册。

    这条守着 `bootstrap.load_all()` 存在的**全部理由**：在组合根补齐之前，
    `domains/master_data/capabilities.py` 里的 9 条声明没有任何可运行路径
    import，能力因此在真实 HTTP 路径上不存在（404），而七套自检照样全绿。
    """
    bootstrap.load_all()
    domains = bootstrap.discover_domains()
    assert domains, "一个域都没被发现——组合根失效了"
    for domain in domains:
        count = sum(1 for item in REGISTRY.all() if item.domain == domain)
        assert count >= 1, (
            f"域 `{domain}` 有 capabilities.py，但装载后注册了 0 条能力。"
            "最常见的原因是模块底部漏了 `_register_all()` 调用。"
        )


def test_capability_resources_resolve(load_all_status) -> None:
    """能力声明的 `resource` 必须在资源注册表里存在。

    `resource` 是前端按资源聚合渲染列表页/行内动作的键。写错一个字符串不会
    报错，只会让前端那条资源静默渲染不出来——所以在这里挡住。
    """
    bootstrap.load_all()
    missing = sorted(
        {
            item.resource
            for item in REGISTRY.all()
            if item.resource and RESOURCES.find(item.resource) is None
        }
    )
    assert missing == [], f"能力引用了未注册的资源：{missing}"


def test_no_duplicate_capability_names_or_routes(load_all_status) -> None:
    """能力名与 `(方法, 路径)` 都必须唯一。

    重名会让 `web/app.py` 的端点名冲突在启动时抛错（那里有守卫），
    但 `(方法, 路径)` 重复会让 Flask 在**后注册者覆盖先注册者**时静默丢一条能力——
    这类缺陷只在调用方拿到 404/意外行为时才暴露。
    """
    bootstrap.load_all()
    items = REGISTRY.all()

    names = [item.name for item in items]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == [], f"重复的能力名：{duplicates}"

    routes = [(str(item.method), item.path) for item in items]
    collisions = sorted({route for route in routes if routes.count(route) > 1})
    assert collisions == [], f"重复的 (方法, 路径)：{collisions}"


# ---------------------------------------------------------------------------
# 域一致性
# ---------------------------------------------------------------------------


def _domain_directories() -> dict[str, str]:
    """`fpa.domains.<pkg>` -> 声明它的目录（用来核对 `Capability.domain`）。"""
    found: dict[str, str] = {}
    root = _PACKAGE / "domains"
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
        if "capabilities.py" not in filenames:
            continue
        relative = Path(dirpath).relative_to(root)
        package = f"fpa.domains.{str(relative).replace(os.sep, '.')}"
        found[package] = str(relative).replace(os.sep, ".")
    return found


def test_capability_domain_matches_its_declaring_directory(load_all_status) -> None:
    """每条能力的 `domain` 必须等于它**所在声明目录**的域名。

    ## 这条断言守的是什么

    域目录名与 `Capability.domain` 是同一个事实的两种写法。写成
    `domain="master_data"` 的 warehouse 能力**不会**报错：

      * `bootstrap` 的守卫按 `item.domain` 统计，于是它被算进 master_data 的配额；
      * `Registry.by_domain()`、前端的按域菜单、`runner._impact_of()` 的分支、
        `InvocationResult.resource_type` 全部跟着这个字段走；
      * 结果是"能力装上了、路由也在，但它落在错误的域里"——**静默错域**。

    五域并行铺开时，这是最容易发生的复制粘贴错误（新建域的 `capabilities.py`
    常从别的域整段抄来），而它不会让任何既有检查变红。判据取处理器的模块路径
    （`fpa.domains.<pkg>.*`）：处理器永远与声明同域，这是本项目"一个域一个目录"的
    直接推论，不需要额外维护一张映射表。
    """
    bootstrap.load_all()
    packages = _domain_directories()
    assert packages, "没有发现任何域目录——本断言的判据失效了"

    offenders: list[str] = []
    for capability in REGISTRY.all():
        module = str(getattr(capability.handler, "__module__", ""))
        for package, domain in sorted(packages.items(), key=lambda item: -len(item[0])):
            if module == package or module.startswith(package + "."):
                if capability.domain != domain:
                    offenders.append(
                        f"{capability.name}: domain={capability.domain!r} "
                        f"但声明在 fpa.domains.{domain}（handler {module}）"
                    )
                break
        else:
            # 处理器不在任何域目录里（例如内核自检用的假能力、或未来的平台能力）。
            # 这类不该悄悄放过：要么补域目录，要么把它排除在"域能力"之外。
            offenders.append(
                f"{capability.name}: 处理器 {module!r} 不属于任何域目录，无法核对 domain"
            )

    assert offenders == [], "能力声明在错误的域（或不属于任何域）：" + "；".join(offenders)


# ---------------------------------------------------------------------------
# 回读函数的装配（`Capability.loader`）
# ---------------------------------------------------------------------------


def test_every_write_capability_declares_its_loader() -> None:
    """每个写类能力都必须**显式**声明回读函数，或显式声明"不需要回读"。

    ## 这条断言守的是什么

    `runner._reload_after` 要求：任何返回 ``resource_id`` 的写能力，其 handler 必须
    能解析出 ``__fpa_load_by_id__(tx, *, scope, record_id)``，否则抛
    ``INTERNAL_ERROR``（`docs/WRITE_CONTRACT.md` 规则 2：``executed`` 的含义是
    "读回来的行确实是我们要的样子"，不是"我们调用了 INSERT"）。

    在这个属性有主人之前，`backend/fpa/domains/` 下一处都没挂，只有
    `tools/runner_e2e.py` 与 `tools/web_e2e.py` 在夹具里手工补挂 —— 于是**生产路径下
    所有写能力都会 500，而七套自检全绿**。这与"组合根缺失"是同一形态的缺陷：
    **夹具盖住了生产路径**。

    ## 判据为什么是"两种都必须显式"

    只断言"要么有 loader、要么标记了 NO_LOADER"，看起来宽松，其实是**唯一不会产生
    静默漏洞**的判据：

      * 只要求"有 loader" → 无法表达"这条能力确实不产出可回读单据"
        （`cost.period.close` 这类动作），于是声明者会被逼着写一个假 loader；
      * 允许"什么都不写" → "忘记接线"与"不需要接线"变成同一个状态，
        而**忘记**这条路径会一直静默到某个域的 e2e 才暴露 —— 那正是它已经发生过的形态。

    默认值 ``loader=None`` 因此被定义为**尚未接线**，而不是"不需要"。默认值不会让
    任何人免于做决定：这一行断言就是他必须做决定的地方。

    ## ⚠️ 属性名是 `__fpa_load_by_id__`，且必须逐字写全

    本断言读的是 ``__fpa_load_by_id__`` —— 与 `runner._reload_after`、`runner._replay`
    解析的是**同一个字面量**，也与 `Capability.__post_init__` 在声明 `loader=` 时挂载的
    是同一个。

    **这个名字绝不能被简写。** Python 的名字改写（name mangling）会把类体内的
    ``self.__fpa_load_by_id__`` 变成 ``_ClassName__fpa_load_by_id__``；而像
    "``__fpa_write__``"这种简称**在本仓库里不匹配任何属性**。如果有人按简称去写断言
    （例如 `attribute in repr(handler)` 这类子串检查），那条断言会**永远为真**——
    它一次都不可能在真实违规上失败，于是保护不了任何东西。

    这与本仓已记录的其他"门禁假通过"是同一族：**报成功，是因为它什么也没检查**。
    """
    bootstrap.load_all()

    unwired: list[str] = []
    for item in REGISTRY.all():
        if item.is_read:
            continue
        loader = getattr(item.handler, "__fpa_load_by_id__", None)
        if callable(loader):
            continue
        if getattr(item.handler, "__fpa_no_loader__", False):
            continue
        unwired.append(f"{item.name}(domain={item.domain}, kind={item.kind})")

    assert unwired == [], (
        "以下写类能力既没有回读函数，也没有声明 NO_LOADER。"
        "它们在生产路径下会在回读步骤抛 INTERNAL_ERROR（写入已落库但永远返回不了 "
        "executed），而夹具测试不会发现。修法：在能力声明上加 loader=<回读函数>；"
        "确实不产出可回读单据的能力加 loader=NO_LOADER。\n  " + "\n  ".join(unwired)
    )


def test_loader_declaration_is_not_ambiguous() -> None:
    """回读函数的声明不得同时给出两种互相矛盾的说法。

    这一条挡住三种真实的笔误：

    1. **属性手工挂载**（`handler.__fpa_load_by_id__ = ...`）与 `loader=` 并存，
       且指向**不同的函数** —— 声明的是 A、执行器用的是 B，哪个生效取决于注册顺序。
       在 `loader=` 出现之前，手工挂载是唯一做法（夹具至今这么写），所以这里给出
       可操作的报错而不是直接禁止。
    2. `loader=NO_LOADER` **同时**又被手工挂了回读函数 —— "不需要回读"与
       "此处回读这张表"是互相否定的两个陈述。
    3. 同一条能力被两条能力声明复用 handler 时，一个声明 loader、另一个声明
       NO_LOADER —— 函数对象上的属性只有一份，后者会覆盖前者。这条靠"两种标记
       同时存在"来发现。
    """
    bootstrap.load_all()

    problems: list[str] = []
    for item in REGISTRY.all():
        if item.is_read:
            continue
        attached = getattr(item.handler, "__fpa_load_by_id__", None)
        marked_no_loader = getattr(item.handler, "__fpa_no_loader__", False)

        if marked_no_loader and callable(attached):
            problems.append(
                f"{item.name}: 同时声明了 NO_LOADER 与回读函数 "
                f"({getattr(attached, '__qualname__', attached)})"
            )
        if item.loader is NO_LOADER and callable(attached):
            problems.append(
                f"{item.name}: loader=NO_LOADER，但 handler 上仍有回读函数 "
                f"({getattr(attached, '__qualname__', attached)}) —— 请二选一"
            )
        if callable(item.loader) and callable(attached) and item.loader is not attached:
            problems.append(
                f"{item.name}: loader= 声明的是 "
                f"{getattr(item.loader, '__qualname__', item.loader)}，"
                f"而 handler 上挂的是 {getattr(attached, '__qualname__', attached)}"
                " —— 两者不一致，执行器用的是后者"
            )

    assert problems == [], "回读函数的声明有歧义：\n  " + "\n  ".join(problems)


def test_loader_is_only_declared_where_it_can_be_used() -> None:
    """`loader` 只对写类能力有意义；读能力声明它是自相矛盾的。

    读能力**不写库**，没有任何"刚写的那一行"需要回读；`runner._reload_after`
    在读能力上直接返回 `None`（`if spec.is_read: return None`）。
    声明一个永远不会被使用的 loader 会让"这个能力写哪张表"变成一个假事实。
    """
    bootstrap.load_all()

    offenders = sorted(
        item.name
        for item in REGISTRY.all()
        if item.is_read and (item.loader is not None or getattr(item.handler, "__fpa_no_loader__", False))
    )
    assert offenders == [], (
        "以下读能力声明了 loader / NO_LOADER，但读能力不写库、没有可回读的行："
        f"{offenders}"
    )
