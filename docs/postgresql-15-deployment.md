# PostgreSQL 15.18 内网部署手册

## 1. 创建角色和空数据库

以下命令由 PostgreSQL 管理员执行。密码通过 `psql` 变量传入，不要写入脚本或版本库。

```sql
CREATE ROLE connection_topology_app LOGIN PASSWORD :'app_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE connection_topology OWNER connection_topology_app;
```

应用从空数据库开始，不迁移 SQLite 文件。建议另建测试库：

```sql
CREATE DATABASE connection_topology_test OWNER connection_topology_app;
```

## 2. 限制网络访问

在 `postgresql.conf` 中仅监听数据库实际需要的内网地址。`pg_hba.conf` 示例：

```conf
host  connection_topology  connection_topology_app  10.20.30.0/24  scram-sha-256
```

部署前必须将 `10.20.30.0/24` 替换为应用服务器的真实 CIDR，禁止照抄示例网段或配置
`0.0.0.0/0`。修改后 reload PostgreSQL，并从非授权主机验证连接被拒绝。

## 3. 应用配置

在应用目录创建不纳入版本控制的 `.env`：

```dotenv
APP_SECRET_KEY=使用Fernet生成的密钥
DATABASE_URL=postgresql+psycopg://connection_topology_app:URL编码后的密码@数据库地址:5432/connection_topology
WEB_WORKERS=
DB_POOL_SIZE=3
DB_MAX_OVERFLOW=2
DB_POOL_TIMEOUT_SECONDS=30
DB_POOL_RECYCLE_SECONDS=1800
SCAN_MAX_WORKERS=30
IMPORT_TEST_MAX_WORKERS=20
SCAN_LEASE_SECONDS=90
TASK_HEARTBEAT_SECONDS=15
```

不要在工单、日志或命令输出中粘贴完整 `DATABASE_URL`。

## 4. 迁移、预检和启动

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m app.preflight --workers 2
.\start.ps1
```

应用 worker 不会自动建表或执行迁移；`start.ps1` 只在启动 worker 前执行一次 Alembic。
验证：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

必须返回数据库正常且迁移为当前版本。

## 5. 连接预算

每个 Uvicorn worker 的最大连接预算为：

```text
DB_POOL_SIZE + DB_MAX_OVERFLOW + 2
```

总预算为上述数值乘以 `WEB_WORKERS`。额外两个连接分别用于 PostgreSQL 通知监听器和调度器
leader 候选。至少为数据库管理和维护预留 10 个连接；preflight 会在预算过高时拒绝启动。

## 6. 备份

同时安全备份数据库和 `APP_SECRET_KEY`。没有原密钥就无法恢复设备凭据。

```powershell
pg_dump -Fc -h 数据库地址 -U connection_topology_app `
  -d connection_topology -f connection_topology.dump
```

备份文件应放入受控加密位置，并定期执行恢复演练。

## 7. 恢复

先创建目标空数据库并确保应用停止，再执行：

```powershell
pg_restore --clean --if-exists -h 数据库地址 `
  -U connection_topology_app -d connection_topology connection_topology.dump
.\.venv\Scripts\python.exe -m alembic upgrade head
```

恢复匹配备份时间点的 `APP_SECRET_KEY`，随后运行 preflight 和健康检查。

## 8. 发布回滚

数据库迁移前先创建 `pg_dump -Fc` 备份。若发布失败：

1. 停止所有新版本应用进程。
2. 恢复上一个应用包。
3. 将数据库恢复到该应用包对应的备份；不要让旧代码连接新结构。
4. 恢复匹配的 `APP_SECRET_KEY` 和 `.env`。
5. 运行旧版本规定的健康检查后再恢复流量。

## 9. 日常检查

- `/api/health` 返回正常。
- Alembic revision 与发布包一致。
- PostgreSQL 15.x 服务、连接数和磁盘空间正常。
- 日志中没有完整数据库 URL、密码、导入凭据或 SQL 参数。
- 扫描全局并发不超过 30，导入测试全局并发不超过 20。
- 定期验证备份可恢复。
