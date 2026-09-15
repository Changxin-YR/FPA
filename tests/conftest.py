"""pytest 的路径引导。

本仓库没有 `pyproject.toml` / `pytest.ini`，`tools/*.py` 各自用
`sys.path.insert(0, .../backend)` 自举。`tests/` 也在这里显式引导一次——
而不是要求把 `backend/fpa` 安装成包：安装成包会让"我改了源码但跑的是
已安装副本"这种失败成为可能，而本项目所有自检工具走的都是"直接跑当前源码"。

## `load_all_status` fixture：让域导入失败只剩一条清晰失败

组合根按目录自动发现并 import 每个域的 `capabilities.py`。**任何一个域语法错 /
import 失败**都会让 `fpa.bootstrap.load_all()` 整体装不起来，于是几十条依赖注册表的
断言连锁变红（实测：1 条真故障 → 22 failed / 94 passed），真缺陷被噪声淹没。

需要真实注册表的用例请 `from conftest import load_all_status` 并**调用**它：装载失败时
它会 **skip 并注明原因**，失败因此只剩
`test_aaa_bootstrap_smoke.py::test_all_domains_import` 那一条。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _load_all_status_impl():
    """真正干活的那份：装载一次；失败就 skip（并注明真失败在哪）。"""
    from test_aaa_bootstrap_smoke import attempt_load

    state = attempt_load()
    if state.error is not None:
        pytest.skip(
            "组合根装载失败，本用例依赖注册表因此跳过（真失败见 "
            "test_aaa_bootstrap_smoke.py::test_all_domains_import）：" + state.summary()
        )
    return state


#: 给 pytest 参数注入用的 fixture（`def test_x(load_all_status)`）。
@pytest.fixture(scope="session", name="load_all_status")
def _load_all_status_fixture():
    return _load_all_status_impl()


#: 给"直接 import 后当函数调用"的用例用（tests 里两种用法都存在）。
load_all_status = _load_all_status_impl



#: 域装载失败时**不跳过**的文件（它就是负责报这条失败的）。
_SMOKE_FILE = "test_aaa_bootstrap_smoke.py"


def pytest_collection_finish(session) -> None:
    """收集一结束就尝试装载一次（异常不抛，只记状态）。

    放在这里的理由：绝大多数用例是**在测试体里**调用 `bootstrap.load_all()`，
    所以必须在任何用例执行之前把"域导不进来"这个事实抓出来，autouse 门才能短路它们。
    """
    if session.config.getoption("collectonly", default=False):
        return
    from test_aaa_bootstrap_smoke import attempt_load

    attempt_load()


def pytest_collection_modifyitems(config, items) -> None:
    """把"依赖注册表但没声明 fixture"的用例**显式**标出来（而不是靠 autouse 一把抓）。

    为什么保留这段：`autouse` 版会把整个 tests/ 一起 skip（见文件头说明）。这里只做一件事——
    收集完成后如果注册表装不起来，就给**已经声明了 `load_all_status` 的用例**加 skip 标记。
    fixture 自己也会 skip（双保险），但标记在收集阶段就可见，`--collect-only` 也能看到。
    """
    from test_aaa_bootstrap_smoke import bootstrap_state

    state = bootstrap_state()
    if state.attempted and state.error is not None:
        marker = pytest.mark.skip(
            "组合根装载失败，本用例依赖注册表因此跳过（真失败见 "
            f"test_aaa_bootstrap_smoke.py::test_all_domains_import）：{state.summary()}"
        )
        for item in items:
            if "load_all_status" in getattr(item, "fixturenames", ()):
                item.add_marker(marker)
