"""运维可观测性子包(MIGRATION-PLAN §5.9 A1)。

- ``runtime``:进程心跳写入 / 心跳年龄诊断(刷新 ``core.metrics.SERVICE_HEARTBEAT_AGE``);
- ``incidents``:``kb/ports.py::IncidentRecorder`` 的默认 SQL 实现;
- ``probes``:四个 AI 能力槽位的周期化有界探测;
- ``schedules``:周期任务目录(beat schedule 构建器 + 运维诊断);
- ``diagnostics``:运维深度诊断聚合(心跳 / 槽位 / 探测 / 技能 / MCP / Agent / KB 健康);
- ``readiness``:依赖就绪探测(``GET /api/v1/ready``);
- ``worker_heartbeat``:Celery worker 侧的非阻塞心跳写线程。

子模块按需 import(``probes``/``diagnostics`` 会拖进 KB 与 agent 依赖链),
本 ``__init__`` 刻意保持空导出。
"""
