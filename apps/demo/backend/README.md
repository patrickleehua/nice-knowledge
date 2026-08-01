# nicekit demo 后端

用 `nicekit.runtime.app_factory.create_app()` 组装出来的最小宿主。既是可运行的
demo,也是"怎么用这个 SDK"的活文档 —— 四个扩展点各有一个可读的最小实现。

## 目录

```
apps/demo/backend/
├── run.py                       # 开发启动入口(必须走它,见下)
├── .env.example                 # 环境变量样例(含 JWT_SECRET / NICEKIT_SECRET_KEY)
└── src/demo_backend/
    ├── main.py                  # create_app 装配(约 20 行有效代码)
    ├── extensions.py            # 四个扩展点示例
    └── seed.py                  # bootstrap_platform + demo 组织/管理员
```

## 启动步骤

```bash
# 0) 仓库根:起三件套(postgres 5433 / redis 6380 / minio 9010)
docker compose up -d

# 1) 建表(alembic 配置在 packages/nicekit)
cd packages/nicekit && uv run --package nicekit alembic upgrade head && cd ../..

# 2) 配置环境变量
cp apps/demo/backend/.env.example apps/demo/backend/.env   # 按需修改

# 3) seed:平台基线 + demo 组织/管理员(幂等)
cd apps/demo/backend && uv run --package nicekit-demo-backend python -m demo_backend.seed

# 4) 启动后端(同目录)
uv run --package nicekit-demo-backend python run.py
```

> **必须用 `uv run python run.py` 启动。**
> 直接 `uvicorn demo_backend.main:app` 会让 Windows 上的事件循环策略在 uvicorn
> 建好 loop 之后才生效,结果是 HTTP 照常响应、但 lifespan 里的所有数据库后台任务
> (心跳 / KB 出箱 / 各类扫描)**静默失败**。这是 TravelFlow 现场踩过的坑。

seed 输出里带默认账号(`admin@demo.example.com` / `demo-admin-2026`)。同一账号
在平台组织里挂 `platform_admin`、在 demo 组织里挂 `org_admin`,登录时用
`org_slug` 切换视角(`platform` / `demo`)。

## 冒烟验证

```bash
curl -s localhost:8020/api/v1/health                       # {"status":"ok"}
curl -s localhost:8020/metrics | head                      # Prometheus 文本
TOKEN=$(curl -s -X POST localhost:8020/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.example.com","password":"demo-admin-2026","org_slug":"platform"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s -H "authorization: Bearer $TOKEN" localhost:8020/api/v1/kb/bases
curl -s -H "authorization: Bearer $TOKEN" localhost:8020/api/v1/chat/sessions
curl -s -H "authorization: Bearer $TOKEN" localhost:8020/api/v1/admin/agent-cards
```

`/api/v1/ready` 会逐依赖探测(DB / 活跃快照 / redis / 对象存储 / 配置),
不就绪返回 503 并给出稳定错误码,可直接用作 k8s readinessProbe。

## celery 模式

`.env` 里把 `TASK_DISPATCH_MODE=celery`,然后另起两个进程:

```bash
uv run celery -A nicekit.runtime.celery_app worker --pool=solo -l info   # Windows 必须 solo
uv run celery -A nicekit.runtime.celery_app beat -l info
```

beat schedule 由 `nicekit.operations.schedules` 的注册表动态构建;宿主自己的
周期任务 `register_system_schedule(...)` 后即自动进 beat 与运维诊断面板。
inline 模式下这些周期任务由 API 进程的 lifespan 常驻循环兜底,两种模式不会双跑。

## 演示的四个扩展点

| 扩展点 | 示例 | 位置 |
|---|---|---|
| 自定义工具 | `echo_note`(宿主 `ToolRegistry` → `create_app(tool_registry=...)`) | `extensions.py` |
| 自定义实体类型 | `product`(带 `filterable_fields`,直接可被 KB 结构化召回过滤) | `extensions.py` |
| `ContextProvider` | `demo_workspace`(每轮注入"此刻工作区状态",超预算整段丢弃) | `extensions.py` |
| `ResourceResolver` | `product_scope`(把 `product_id` 参数解析成作用域根供权限层判定) | `extensions.py` |

还有更多扩展点没在 demo 里展开,协议都在各子包的 `ports.py` / 对应模块:
`ReferenceScanner`(KB purge 前的外部引用计数)、`TraceSink`/`UsageSink`
(LLM 计量落库)、`Notifier`(通知)、`RecoverableTask`(任务恢复)、
`IncidentRecorder`(运维事件)、prompt 资源多 root 叠加、能力槽位注册、
`register_system_schedule` / `register_task`(周期任务与派发任务)。
