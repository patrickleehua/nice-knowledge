"""nicekit SDK 的最小宿主示例(既是 demo 也是"怎么用 SDK"的活文档)。

模块划分:
- :mod:`demo_backend.extensions` —— 四个扩展点的示例实现与注册入口;
- :mod:`demo_backend.main` —— ``create_app()`` 装配出的 ASGI app;
- :mod:`demo_backend.seed` —— 平台 bootstrap + demo 组织/管理员用户。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
