# Connection Atlas 服务器连接拓扑

Connection Atlas 是一个 Python 3.10 Web 系统，通过 SSH 采集 Linux `ss` 输出、通过
WinRM 采集 Windows Server TCP/UDP 连接，并展示设备与集群拓扑、历史快照和扫描批次。

## 运行要求

- Python 3.10
- PostgreSQL 15.x（当前验收版本为 15.18）
- Linux 目标机启用 SSH 并安装 `ss`
- Windows 采集为可选能力，需要 WinRM 和 `pywinrm`

SQLite 已不再受支持。系统从空 PostgreSQL 数据库开始，不迁移旧 SQLite 数据。

## 安装

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

需要 Windows WinRM 采集时安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[windows]"
```

## 配置

复制 `.env.example` 为 `.env`，生成 Fernet 密钥，并填写 PostgreSQL 连接地址：

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```dotenv
APP_SECRET_KEY=粘贴生成的Fernet密钥
DATABASE_URL=postgresql+psycopg://connection_topology_app:替换密码@127.0.0.1:5432/connection_topology
WEB_WORKERS=
DB_POOL_SIZE=3
DB_MAX_OVERFLOW=2
DB_POOL_TIMEOUT_SECONDS=30
DB_POOL_RECYCLE_SECONDS=1800
IMPORT_TEST_MAX_WORKERS=20
SCAN_MAX_WORKERS=30
SCAN_QUEUE_SIZE=2000
SCAN_JITTER_SECONDS=300
SCAN_LEASE_SECONDS=90
TASK_HEARTBEAT_SECONDS=15
```

`DATABASE_URL` 必须使用 `postgresql+psycopg://`。`.env` 和 `APP_SECRET_KEY` 不得提交到版本库；
密钥丢失后，已保存的设备密码无法解密。

`WEB_WORKERS` 留空时使用 `min(CPU 数, 8)`，也可设置为正整数覆盖。数据库连接预算为：

```text
WEB_WORKERS × (DB_POOL_SIZE + DB_MAX_OVERFLOW + 2)
```

其中每个进程额外预留一个通知监听连接和一个调度器候选连接。

## 初始化与启动

```powershell
.\start.ps1
```

启动脚本依次执行：

1. `alembic upgrade head`
2. PostgreSQL 版本、迁移版本和连接预算预检
3. 按配置启动多个 Uvicorn worker

访问：

- 系统页面：<http://127.0.0.1:8000>
- API 文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/api/health>

健康状态必须同时包含 `database=ok` 和 `migration=current`。

## 并发与故障恢复

- 扫描远程并发上限 `SCAN_MAX_WORKERS=30` 是所有 Web 进程共享的全局上限。
- 导入连接测试上限 `IMPORT_TEST_MAX_WORKERS=20` 同样是应用全局上限。
- PostgreSQL advisory lock 与 `FOR UPDATE SKIP LOCKED` 保证任务只被一个 worker 领取。
- 扫描和导入测试使用租约与批量心跳；worker 崩溃后，过期任务可由其他进程接管。
- 旧 worker 的迟到结果无法覆盖新 worker 的结果。
- SSH/WinRM 网络等待期间不持有数据库事务或连接。
- PostgreSQL 只针对死锁 `40P01` 和序列化失败 `40001` 使用新 Session 重试。
- 只有取得 PostgreSQL leader 锁的进程运行 APScheduler；失去连接后自动让位。
- 拓扑变更通过 PostgreSQL `NOTIFY` 使所有进程清缓存，30 秒 TTL 作为兜底。

## 批量导入

- 支持 `.xlsx`，单次最多 1000 行、5 MB。
- 格式异常行会被跳过，批次结果和日志会记录异常原始行信息，便于后续排查。
- 密码不会写入导出报告或错误日志。
- 密码留空时，仅标注该设备及其集群归属，不执行连接测试和采集；集群模式仍显示为集群设备。
- 有密码的新设备进入持久化连接测试队列，成功后进入首次完整扫描。
- 重复设备不会被无密码导入修改。

## 采集说明

Linux 首选执行 `ss -H -tunap`；权限不足时降级为 `ss -H -tuna`，采集仍成功，但部分
PID 和进程名可能为空。无法解析的 `ss` 行会跳过，并在日志中记录异常原始行。

Windows 采集需要目标机启用 WinRM。生产环境建议使用域身份、HTTPS 监听器或网络级访问
控制，不要将管理接口直接暴露到不可信网络。

## 测试

测试只允许使用名为 `connection_topology_test` 的 PostgreSQL 数据库：

```powershell
$env:TEST_DATABASE_URL='postgresql+psycopg://.../connection_topology_test'
.\.venv\Scripts\python.exe -m ruff check app scripts tests migrations
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m alembic check
```

部署、备份、恢复、连接预算及回滚步骤见
[`docs/postgresql-15-deployment.md`](docs/postgresql-15-deployment.md)。
