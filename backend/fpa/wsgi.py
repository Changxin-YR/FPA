"""WSGI 入口：真实服务进程从这里拿 app。

    gunicorn -w 4 --bind 0.0.0.0:8000 "fpa.wsgi:app"

**为什么单独一个文件**：`fpa.factory.build_app()` 已经能装出 app，但 WSGI 服务器需要一个
**模块级可导入对象**（`module:callable`）。把 `app = build_app()` 写在 `fpa/web/app.py`
里会让 `import fpa.web.app` 产生副作用（导入即连库、即遍历全部域），而 `web/app.py`
是纯函数模块、被大量测试直接 import。

配置全部来自环境变量（见 `fpa.settings` 与 `fpa.kernel.uow_factory` 的默认值），
因此本文件**不需要**任何参数化逻辑——"怎么装"只有一处（`fpa.factory`）。
"""

from __future__ import annotations

from fpa.factory import build_app

#: WSGI 服务器导入的对象。默认走组合根装载全部域能力（`fpa.bootstrap.load_all()`）。
app = build_app()

__all__ = ["app"]
