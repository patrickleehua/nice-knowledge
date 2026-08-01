"""运行时装配层(MIGRATION-PLAN §5.7):宿主用这里的东西把 SDK 组装成服务。

- :mod:`nicekit.runtime.app_factory` —— ``create_app()``:中间件顺序、lifespan、
  常驻后台循环、``/metrics``;
- :mod:`nicekit.runtime.bootstrap` —— ``install_default_ports()`` /
  ``bootstrap_platform()``:装配期注册与幂等 seed(A2/A3);
- :mod:`nicekit.runtime.dispatch` —— 任务派发注册表(inline / celery 双模式);
- :mod:`nicekit.runtime.reliability` —— ``RecoverableTask`` 框架(孤儿恢复 + stale 清扫);
- :mod:`nicekit.runtime.celery_app` / :mod:`nicekit.runtime.tasks` —— celery 装配与通用任务。

子模块按需 import:``celery_app``/``tasks`` 会拉起 celery 与全部执行链依赖,
本 ``__init__`` 刻意保持空导出(``kb.ingestion`` 延迟 import celery_app 同理,
为的是断开 runtime → operations → kb → runtime 的导入环)。
"""
