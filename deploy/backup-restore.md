# 数据库与对象存储备份/恢复

本流程同时备份 PostgreSQL 逻辑数据和 MinIO bucket 内容，并在隔离的临时数据库与临时 bucket 上完成恢复校验。脚本硬性拒绝将 `niceknowledge` 或源名称作为恢复目标。

## 前置条件

- PowerShell 7+
- Docker Compose 中 `postgres`、`minio` 服务健康
- 默认参数对应仓库开发环境；生产执行时通过参数或环境变量提供管理员账号与 MinIO 凭据
- 运行完整演练前暂停 API/worker 写入。`pg_dump` 本身是一致性快照，但源库逐表计数用于恢复验收，持续写入会使计数基线漂移
- 备份目录包含业务数据，不得提交 Git；默认保存到 `.local/backups/<UTC timestamp>`

## 一键备份并恢复演练

```powershell
pwsh -File deploy/backup-restore.ps1 -Action BackupAndVerify
```

流程执行：

1. 以 PostgreSQL custom format + zstd 创建 `niceknowledge.dump`，另存不含角色密码的 globals 和规范化 schema。
2. 校验 MinIO bucket 未启用版本控制；用 `mc mirror --preserve` 下载全部对象，并导出 bucket metadata ZIP。
3. 生成 dump SHA-256、逐表精确行数、对象逐文件 SHA-256/字节数及整体指纹。
4. 创建唯一命名的临时数据库和 bucket，从备份制品恢复。
5. 比对 schema SHA-256、全部 public table 行数、全部对象路径/长度/SHA-256。
6. 默认删除临时数据库与 bucket，保留备份制品与 `manifest.json`、`verification/` 证据。

MinIO 已启用版本控制时脚本会中止，因为文件镜像只包含当前版本，不能称为全量备份。此时应使用 MinIO site replication 或 `mc mirror --versions` 对应的版本保留方案，并单独演练版本恢复。

## 分步执行

```powershell
# 仅备份
pwsh -File deploy/backup-restore.ps1 -Action Backup -BackupDir D:\secure-backups\niceknowledge\20260713T120000Z

# 从已有制品恢复验证；目标名必须为新名称
pwsh -File deploy/backup-restore.ps1 -Action Verify `
  -BackupDir D:\secure-backups\niceknowledge\20260713T120000Z `
  -RestoreDatabase niceknowledge_restore_20260713 `
  -RestoreBucket niceknowledge-restore-20260713
```

生产环境建议使用受限 ACL 的加密目录，并将 `MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY` 放入当前进程环境，避免写入命令历史。`globals-no-passwords.sql` 只保留角色/授权结构，角色密码应由密钥管理系统恢复。

## 制品与验收

每次运行至少包含：

| 路径 | 内容 |
|---|---|
| `manifest.json` | 来源、制品路径/校验和、DB/MinIO 数量、恢复结果、清理状态 |
| `database/niceknowledge.dump` | 可供 `pg_restore` 使用的数据库备份 |
| `database/globals-no-passwords.sql` | 不含密码的角色与全局授权 |
| `database/schema.sql` | 规范化源 schema，用于恢复后哈希比较 |
| `object-store/objects/` | bucket 全部当前对象 |
| `object-store/source-inventory.json` | 每个对象的路径、字节数、SHA-256 |
| `object-store/source-stats.jsonl` | MinIO 原始对象元数据 |
| `cluster-metadata.zip`（名称由 mc 决定） | MinIO bucket policy/lifecycle/locking 等元数据导出 |
| `verification/` | 恢复后的 DB/object inventory、schema、MinIO stat |

验收仅在 `manifest.json` 的 `verification.status` 为 `passed` 且 `cleanup` 为 `completed` 时通过。还应将整个备份目录复制到独立故障域并按保留策略轮换；同卷备份不具备灾难恢复价值。

## 清理与故障处理

默认自动清理恢复目标。调试时可传 `-RetainRestoreTargets`，确认名称后再手工清理：

```powershell
docker compose exec -T postgres dropdb -U postgres --if-exists --force niceknowledge_restore_<run-id>

docker run --rm --network niceknowledgeai_default `
  -e MC_HOST_store=http://<access-key>:<secret-key>@minio:9000 `
  minio/mc:latest rb --force store/niceknowledge-restore-<run-id>
```

禁止对 `niceknowledge` 执行 `dropdb`、`pg_restore --clean`、`mc mirror --remove` 或 `mc rb --force`。恢复失败时保留原备份制品和 `manifest.json`，删除临时目标后用新的目标名重试。
