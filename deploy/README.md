# NiceKnowledge 本地容器开发栈(基础设施)

> `docker-compose.yml` 使用开发密码、可变 tag 和本地基础设施,只用于开发与
> 集成测试,不是生产配置。当前仅包含基础三件套(postgres / redis / minio)
> 与 bucket 初始化;应用服务(API / worker / beat)由 `apps/demo` 在 P4 阶段
> 自行组装。

## 1. PostgreSQL 定制镜像

PostgreSQL 使用仓库内 `deploy/postgres/Dockerfile` 构建,基础镜像固定为 pgvector
`0.8.5` + PostgreSQL 17 bookworm 的 manifest digest `sha256:d2ef61f4...bad0`;
SCWS 固定为 `1.2.3`(下载包校验 SHA-256),zhparser 固定到 commit
`2e995c4df672563992b4d7a147b8fa2d0d4cda6c`。`pg_trgm` 来自 PostgreSQL contrib。

新数据目录初始化时,官方 entrypoint 先以 `postgres` 执行 `00-extensions.sql`
安装 `vector` / `pg_trgm` / `zhparser`,随后才创建非 superuser 的
`niceknowledge_migrator` 与 `niceknowledge_app`(双账号:migrator 跑 Alembic,
app 为应用连接、受 RLS 约束)。

构建并在无宿主端口、无持久卷的临时容器中验证三个扩展:

```powershell
docker compose build postgres
./deploy/smoke-postgres-extensions.ps1
```

脚本会验证 PostgreSQL major version、扩展版本、中文 `tsvector`、trigram
相似度、vector 距离,以及 migrator 仍为非 superuser;完成后删除临时容器和
匿名卷,不读取或修改 compose 的 `pgdata`。

存量库升级时,镜像替换并重启数据库后,必须由运维使用现有 `postgres`
superuser 执行 preflight/smoke,不能把扩展创建下放给 Alembic 账号:

```powershell
./deploy/smoke-postgres-extensions.ps1 -ExistingContainer <postgres-container> -Database niceknowledge
```

该命令会幂等安装缺失扩展并运行 smoke。执行前仍应按
`deploy/backup-restore.md` 完成备份;失败时保持迁移未运行并回退旧镜像。不要
为了通过 `CREATE EXTENSION zhparser` 而赋予 `niceknowledge_migrator` superuser。

后续迁移添加 STORED `tsvector` 生成列和普通 GIN 索引时,会重写/扫描存量表并
持有影响写入的锁,必须在维护窗口或影子库执行,不能按零停机迁移宣称。
zhparser text search configuration、SCWS 词典或 `zhparser.extra_dicts` 发生
变化时,现有 STORED tsvector 不会自动重算:必须强制回填 tsvector、重建相应
GIN 索引,并更新检索配置 fingerprint 后构建新快照。superuser
bootstrap/preflight 会在数据库级启用 zhparser 官方 `multi_short` 模式,使
"酒店推荐"等复合词同时保留"酒店"短词召回;修改该选项同样按词典变更处理。

## 2. 启动与停止

```bash
docker compose up -d      # postgres / redis / minio / createbucket
docker compose down       # 停止(保留数据卷)
```

## 3. 开发账号与变量

| 变量(对应 `nicekit/core/config.py` 字段) | 开发取值 |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://niceknowledge_app:app-dev-secret@localhost:5432/niceknowledge` |
| `MIGRATION_DATABASE_URL` | `postgresql+psycopg://niceknowledge_migrator:migrator-dev-secret@localhost:5432/niceknowledge` |
| `REDIS_URL` | `redis://localhost:6379/0` |
| `MINIO_ENDPOINT` | `localhost:9000` |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `niceknowledge` / `niceknowledge-dev-secret` |
| `MINIO_BUCKET` | `niceknowledge` |

真正上生产时,把上表中的开发密码整体换掉(postgres-init.sql、minio 环境、
应用侧 env 三处同步改),并另行注入 `JWT_SECRET` 与 `NICEKIT_SECRET_KEY`
(密钥加密 master key)。
