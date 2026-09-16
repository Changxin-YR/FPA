"""验证 DeepSeek Harness 运行时能真跑一轮对话。

这一步验证的是**依赖可用性**，不是业务闭环：

    * SDK 能否启动 dsh 子进程
    * sdk profile 能否初始化
    * 凭据能否被 dsh 自己找到（`~/.dsh/.credentials.yaml`）
    * 模型能否返回一轮完整回复

价值在于：如果这一步不通，后面"Agent 说已完成但库里没数据"的排查会失去方向——
分不清是"模型没调工具"还是"工具根本调不通"。先把底层链路证明可用，再往上叠业务。

## 关于运行时载体（本机踩过的坑）

SDK 的 `resolve_bundled_launch_args()` 默认**只找生产用单文件 exe**
（`runtime/deepseek-harness-sdk-runtime-win-x64.exe`），刻意不自动回退到源码构建——
设计意图是"生产部署不能静默依赖源码"。

本机没有那个 exe，但有**完整的 node runtime closure**（193 个包，含 `dsh-tools`）。
于是必须**显式**选择开发载体：`DSH_RUNTIME_MODE=node`。
选它会走 `_node_launch_args()`，使用
`runtime/node/node_modules/@deepseek-ai/dsh/lib/bin.js`。

这个环境变量必须在**子进程可见**的地方设置。本项目通过 SDK 的 `env=` 参数传，
而不是改 `os.environ`——后者会影响同进程内的所有并发请求。早期版本为此专门加了一把
全局锁来做 `os.environ` 的快照/清空，那是个更糟的解法（即使有锁，并发请求也会
看到空的 `os.environ`）。

用法::

    python tools/harness_smoke.py
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

#: node runtime closure 的位置（**含 `node_modules/@deepseek-ai/dsh` 的那一级目录**）。
#:
#: 这份 SDK 源码不随本仓库分发，路径因机器而异，所以优先读环境变量。
#: 变量名与 `agent-runtime/bin/run.cmd` 保持一致（**同一个键，两处都认**）：
#:
#:     $env:DSH_NODE_RUNTIME='<某一级目录>'
#:     # 校验：<某一级目录>/node_modules/@deepseek-ai/dsh/lib/bin.js 必须存在
#:     # 未设置时回落到历史本机布局（脱敏占位符）
_closure_env = os.environ.get("DSH_NODE_RUNTIME", "").strip()
NODE_CLOSURE = (
    Path(_closure_env)
    if _closure_env
    else (
        Path(r"<repo>\deepseek-harness\python\sdk-runtime")
        / "src"
        / "deepseek_harness_runtime"
        / "runtime"
        / "node"
    )
)

#: SDK 的 **Python** 包路径（`deepseek_harness` 所在目录）。用 `pip install -e` 装过就不需要设。
_sdk_env = os.environ.get("DSH_SDK_PYTHONPATH", "").strip()
if _sdk_env:
    sys.path.insert(0, _sdk_env)

#: 传给子进程的环境变量白名单。**只放运行必需的。**
_ENV_ALLOWLIST = {
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "NODE_PATH",
    # 开发载体开关。必须放行——否则 dsh 入口读不到它，
    # 会去要那个不存在的生产 exe。这个坑我自己踩过一次。
    "DSH_RUNTIME_MODE",
    # ★ 凭据必须放行。
    #
    # 我上一版的注释写着"机器级环境变量会被子进程自然继承"——**但下面这个白名单
    # 决定了子进程能看到什么**。注释和代码矛盾时，代码赢了。
    # 症状是 `MISSING_CREDENTIAL`，而前置检查报 PASS（它查的是本进程环境里有值）。
    #
    # 教训：**白名单式的环境构造里，"自然继承"是一个不存在的概念。**
    # 凡是白名单，就要显式列出每一个必需的键——包括那些"显然会有"的。
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    # 载体与源码 checkout 的位置：`agent-runtime/bin/run.cmd` 要用它们找入口。
    # 与上面的道理一样——白名单里的键，少一个子进程就少一个能力。
    "DSH_NODE_RUNTIME",
    "AGENT_HARNESS_ROOT",
}

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES += 1
        print(f"  FAIL  {label}  {detail}")


def _machine_env(name: str) -> str:
    """从 Windows 机器级环境变量读一个值。

    **为什么需要这个**：机器级变量是在 `SetEnvironmentVariable(..., 'Machine')` 时
    写入注册表的，**已经运行的进程看不到它**——本会话的 shell 就是从设置之前启动的
    进程继承的，所以 `os.environ` 里没有 `DEEPSEEK_API_KEY`，但注册表里有。

    这不只是测试的问题：任何"先启动服务、后配环境变量"的部署顺序都会遇到它。
    直接从注册表读，可以让结果与启动时机无关。

    非 Windows 平台返回空串——那些平台上环境变量就是唯一来源，不会出现这个错位。
    """
    if os.name != "nt":
        return ""
    import winreg

    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            r"Environment",
        ):
            try:
                with winreg.OpenKey(root, sub) as handle:
                    value, _kind = winreg.QueryValueEx(handle, name)
                if value:
                    return str(value)
            except OSError:
                continue
    return ""


def resolve_api_key() -> str:
    """取 DEEPSEEK_API_KEY：优先环境变量，回退注册表。"""
    return os.environ.get("DEEPSEEK_API_KEY", "") or _machine_env("DEEPSEEK_API_KEY")


def child_env(dsh_home: str) -> dict[str, str]:
    """构造子进程环境。

    凭据通过 `DEEPSEEK_API_KEY` 传给子进程——但**不是**简单地从 `os.environ` 复制，
    因为本进程可能看不到它（见 `_machine_env` 的说明）。
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _ENV_ALLOWLIST
    }
    env["DSH_HOME"] = dsh_home
    # 开发载体：**显式设置**，不依赖从 os.environ 继承。
    #
    # 它是用户级环境变量，但本进程可能看不到（进程启动早于变量设置，见
    # `_machine_env` 的说明）。依赖"环境里应该有"是这类 bug 的通用形态——
    # 凡是子进程必需的，就在构造时显式写进去。
    env["DSH_RUNTIME_MODE"] = "node"

    api_key = resolve_api_key()
    if api_key:
        env["DEEPSEEK_API_KEY"] = api_key
    return env


def main() -> int:
    global FAILURES

    dsh_home = str(ROOT / ".dsh-home")
    #: 子进程的工作目录（SDK 用它当 `cwd=`，**必须是真实存在的目录**）。
    #: 优先级：AGENT_HARNESS_ROOT（harness 源码 checkout，run.cmd 也认这个键）
    #: → DSH_NODE_RUNTIME（只给了闭包目录时够用）→ 本仓库根目录。
    #: 早期版本这里写的是脱敏占位符 `<harness-runtime>`，本机不存在 → WinError 267。
    harness_root = (
        os.environ.get("AGENT_HARNESS_ROOT", "").strip()
        or os.environ.get("DSH_NODE_RUNTIME", "").strip()
        or str(ROOT)
    )

    print("=== 0. 前置条件 ===")
    check("DSH_HOME 存在", Path(dsh_home).is_dir(), dsh_home)
    check(
        "sdk profile 已引导",
        (Path(dsh_home) / "profiles" / "sdk" / "cordis.yml").is_file(),
        "跑一次 `node <harness>/apps/cli/lib/bin.js --profile sdk --dump-default-config` 即可生成",
    )
    closure_bin = NODE_CLOSURE / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    check(
        "node runtime closure 完整",
        closure_bin.is_file(),
        f"缺少 {closure_bin}。SDK 默认只找生产 exe，本机没有，所以必须用 node 载体；"
        "可用环境变量 DSH_NODE_RUNTIME 指向含 node_modules/@deepseek-ai/dsh 的那一级目录",
    )
    # 凭据判断从"某个文件存在"改为"能真正取到 key"——因为真正生效的是后者。
    # 之前那条检查报 PASS 而模型报 401，正是因为查的是一个已失效的旧文件。
    #
    # **检查项要对应真正生效的机制**，否则会给出"通过"的假信心。
    # 这里用 `resolve_api_key()` 而不是 `os.environ.get()`：后者看不到
    # "在进程启动之后才设置的"机器级变量（见 `_machine_env` 的说明）。
    api_key = resolve_api_key()
    check(
        "DEEPSEEK_API_KEY 可获取（环境变量或注册表）",
        bool(api_key),
        "两处都没有找到；dsh 需要它来调用模型",
    )
    if api_key:
        print(f"        key 前缀 {api_key[:10]}…  长度 {len(api_key)}")

    print("\n=== 1. 导入 SDK ===")
    try:
        from deepseek_harness import DeepSeekHarness
    except ImportError as exc:
        print(f"  FAIL  无法导入 deepseek_harness：{exc}")
        print("        未 `pip install -e` 时，可用 DSH_SDK_PYTHONPATH 指向该包的父目录")
        return 1
    print("  PASS  已导入 deepseek_harness")

    print("\n=== 2. 启动运行时 ===")
    started = time.perf_counter()
    try:
        harness = DeepSeekHarness(
            dsh_home=dsh_home,
            cwd=harness_root,
            profile="sdk",
            provider="deepseek-official",
            model="deepseek-v4-flash",
            max_tokens=2048,
            request_timeout_seconds=90.0,
            # 不传 api_key：DEEPSEEK_API_KEY 是**机器级环境变量**，
            # 子进程自然继承。让本进程不经手凭据，比"读了再传"更干净。
            # 也正因为如此，它不会出现在本进程的任何日志或异常栈里。
            # dsh_bin 必须指向**可执行文件**，不能是 .js。
            # 本机那个 dsh.exe（Python 入口）会崩溃（访问违例 0xC0000005），
            # 所以用 agent-runtime/bin/dsh.cmd 包装器转发到 node bin.js。
            dsh_bin=str(ROOT / "agent-runtime" / "bin" / "run.cmd"),
            env=child_env(dsh_home),
        )
        harness.start()
        print(f"  PASS  运行时已启动（{time.perf_counter() - started:.1f}s）")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  启动失败：{type(exc).__name__}: {exc}")
        print("\n排查建议：")
        print("  1. DSH_HOME 指向本项目目录（不是早期版本的）")
        print("  2. 确认 node runtime closure 完整（见上）")
        print("  3. 确认 ~/.dsh/.credentials.yaml 里有 DEEPSEEK_API_KEY")
        return 1

    print("\n=== 3. 跑一轮真实对话 ===")
    try:
        result = harness.run(
            "请只回复四个字：链路正常。不要调用任何工具。",
            # 会话 id 每次都变：上一轮的会话会持久化在 DSH_HOME 里，
            # 复用同一个 id 会拿到 "session already exists"。
            # 这条对生产代码也是个提示：会话 id 必须与「用户 + 对话」绑定且唯一。
            session_id=f"harness-smoke-{int(time.time())}",
        )
        reply = str(getattr(result, "final_response", "") or "")
        finish = getattr(result, "finish_reason", None)
        print(f"  finish_reason={finish!r}")
        print(f"  模型回复：{reply[:150]!r}")
        if not reply.strip():
            # 失败时要能看到原因，否则只能看到一句"回复为空"。
            events = list(getattr(result, "events", []) or [])
            print(f"  事件数：{len(events)}")
            for event in events[-6:]:
                text = str(event)
                print(f"    {text[:400]}")
        check("返回了非空回复", bool(reply.strip()), "回复为空")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  对话失败：{type(exc).__name__}: {exc}")
        FAILURES += 1
    finally:
        try:
            harness.close()
        except Exception:  # noqa: BLE001
            pass

    print()
    if FAILURES:
        print(f"结果：{FAILURES} 项失败")
        return 1
    print("结果：全部通过 —— Harness 链路可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
