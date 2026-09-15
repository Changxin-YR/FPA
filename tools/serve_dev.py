r"""开发服务器：把**真实组合根**跑成一个可访问的 HTTP 服务（测试链接用）。

## 为什么需要它

`fpa.wsgi` 里已经有一个模块级 `app`，但它是给 WSGI 服务器用的；而本项目文档里写的
`gunicorn -w 4 "fpa.wsgi:app"` 在 **Windows 上不可用** —— gunicorn 依赖 `fcntl`。
本机是 Windows，所以用 `waitress` 起**同一个 app**，不另造装配路径
（装配只有一处：`fpa.factory.build_app()`）。

## 用法

    set PYTHONPATH=<repo>\backend
    python tools\serve_dev.py              # 默认 127.0.0.1:5101
    python tools\serve_dev.py 8080         # 换端口

前端 dev server 的 `/api` 代理默认指向 `127.0.0.1:5101`（见 `frontend/vite.config.ts`），
所以两端默认端口是配套的 —— 起完这个，再起前端即可用浏览器访问。

## 一条边界

本脚本**只做**启动，不做装配、不做路由、不做鉴权。它不 import 任何业务域。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 与 tools/ 下其他脚本一致：把 backend 注入 sys.path（包未安装为 editable）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

DEFAULT_PORT = 5101


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else int(os.environ.get("FPA_DEV_PORT", DEFAULT_PORT))
    host = os.environ.get("FPA_DEV_HOST", "127.0.0.1")

    # Settings 用 PORT 推导 Agent 子进程的回调地址；它必须与 Waitress 实际监听端口一致。
    os.environ["PORT"] = str(port)

    # 延迟导入：让 `--help` 与参数错误不必先连库。
    from waitress import serve

    from fpa.wsgi import app

    print(f"[serve_dev] 服务已启动: http://{host}:{port}/", flush=True)
    # 线程数必须留足余量：智能体一轮对话会**长时间占住一个线程**（实测 1–5 分钟），
    # 而前端每次进入业务页都要打 `/meta/capabilities`。默认 8 个线程在对话期间被占满后，
    # 元数据请求会排队到超时 → 前端拿不到资源清单 → **全站资源页一起变成"页面不存在"**
    # （用户报的"大量页面出现这个问题"就是这条级联）。32 是"够用且不失控"的折中：
    # 每个线程只在请求期间存活，空闲不占资源。
    serve(app, host=host, port=port, threads=32)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
