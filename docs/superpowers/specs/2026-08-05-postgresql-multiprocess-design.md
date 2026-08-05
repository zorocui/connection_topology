# PostgreSQL 15 多进程架构设计

## 背景与目标

当前系统使用 SQLite、单 Uvicorn 进程和进程内写入协调器。该架构已经能保持
30 路 SSH/WinRM 并行采集，但无法安全利用多个 Web 进程。目标是将运行数据库
完全切换到内网 PostgreSQL 15.18，并让多个 Uvicorn 进程共同处理 API、扫描任务
和导入连接测试，同时保证任务不会重复、全局扫描并发不超过 30、故障后可恢复。

本次使用全新 PostgreSQL 空库，不迁移任何 SQLite 数据。完成后不再支持 SQLite
运行模式。

## 范围

### 包含

- Psycopg 3 PostgreSQL 驱动和 PostgreSQL 专用连接配置。
- Alembic 初始迁移及数据库版本检查。
- PostgreSQL 原生多进程扫描任务抢占、租约、心跳和恢复。
- 全应用扫描总并发上限 30。
- 多进程导入连接测试抢占和全局并发上限。
- PostgreSQL advisory lock 调度器领导者选举与故障接管。
- PostgreSQL 跨进程拓扑缓存失效通知。
- PostgreSQL 事务冲突重试、安全错误分类和日志脱敏。
- Windows 本机 PostgreSQL 15 开发环境以及内网 PostgreSQL 15.18 部署文档。
- 基于真实 PostgreSQL 的完整测试和多实例并发验收。

### 不包含

- SQLite 数据迁移。
- SQLite 运行兼容模式。
- Redis、Celery、RabbitMQ 或独立任务 Worker 服务。
- PostgreSQL 高可用、主从复制和备份平台建设；只提供应用所需的备份操作说明。

## 总体架构

数据库连接使用 SQLAlchemy 2 和 Psycopg 3，URL 格式为：

```text
postgresql+psycopg://user:password@host:5432/database
```

应用不再执行 SQLite PRAGMA，不再创建数据库文件进程锁，也不再串行化所有写事务。
现有 `SQLiteWriteCoordinator` 替换为通用 PostgreSQL 事务执行器。执行器负责使用
新会话提交可重放操作，并只对 PostgreSQL 死锁和序列化冲突进行有限退避重试。

Uvicorn 默认进程数按 `min(CPU 逻辑核心数, 8)` 计算，至少为 1。显式设置
`WEB_WORKERS` 时使用配置值。每个进程都能领取扫描任务，PostgreSQL 负责跨进程
协调。

## 数据库结构和 Alembic

引入 Alembic，并创建一个面向全新 PostgreSQL 数据库的初始迁移。部署启动顺序为：

1. 创建数据库和最小权限应用账号。
2. 执行 `alembic upgrade head`。
3. 启动多进程 Uvicorn。
4. 每个应用进程启动时检查数据库是否位于 Alembic 最新版本；版本落后时拒绝启动。

应用进程不执行 `create_all`，也不并发执行迁移。删除自制 `schema_versions` 迁移
路径及 SQLite 专用 `INSERT OR IGNORE` 等语句。

初始迁移明确创建所有表、枚举、外键和索引，并处理以下 PostgreSQL 差异：

- 时间字段使用带时区的 `TIMESTAMPTZ`。
- 布尔服务器默认值使用 `TRUE/FALSE`。
- 活跃扫描任务唯一索引使用 PostgreSQL 部分索引条件，只约束状态为
  `PENDING` 或 `RUNNING` 的任务。
- 主键由 PostgreSQL identity/sequence 生成。
- 保留当前级联删除、扫描历史和拓扑查询索引。

## 扫描任务的分布式抢占

`scan_tasks` 增加以下字段：

- `worker_id`：领取任务的应用进程 UUID。
- `lease_expires_at`：任务租约截止时间。
- `heartbeat_at`：最近续租时间。
- `attempt_count`：成功领取执行的次数。

每个进程拥有本地线程池，但 `SCAN_MAX_WORKERS=30` 表示全应用总并发。领取流程在
一个短事务中完成：

1. 获取固定键的事务级 PostgreSQL advisory lock，串行化“计算名额并领取”步骤。
2. 将租约已经过期的 `RUNNING` 任务恢复为可领取状态，并同步批次计数。
3. 统计仍持有有效租约的运行任务。
4. 计算 `30 - 有效运行任务数`，再与本进程空闲线程数取最小值。
5. 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 按优先级领取任务。
6. 写入 `worker_id`、租约、心跳和 `attempt_count` 后提交。

远程 SSH/WinRM 采集在事务外执行，不占用数据库连接。执行线程定期续租；续租使用
短事务并要求 `worker_id` 匹配。保存扫描结果时同样校验任务仍由当前进程持有，扫描
快照、连接记录、设备状态、任务状态、批次明细及计数仍在一个事务中原子提交。

如果租约已被其他进程接管，旧执行线程丢弃结果并记录 `task_lease_lost`，不得覆盖
新任务状态。进程异常退出时数据库锁立即释放，租约到期后其他进程自动接管。

## 导入连接测试

导入连接测试改为数据库驱动的待处理队列，不依赖上传请求所在进程内存中的 Future。
导入行增加测试执行者、租约、心跳和尝试次数字段。各进程通过与扫描队列相同的
`FOR UPDATE SKIP LOCKED` 模式领取测试任务。

`IMPORT_TEST_MAX_WORKERS=20` 表示全应用总连接测试并发。领取操作使用独立 advisory
lock 键控制全局名额。进程崩溃后租约到期自动恢复，完成回写必须校验执行者身份。
没有密码、只用于集群标注的设备保持 `NOT_APPLICABLE`，不进入连接测试队列。

## 定时调度器领导者

每个 Uvicorn 进程启动一个轻量领导者选举循环，使用专用 PostgreSQL 连接尝试获得
固定会话级 advisory lock。只有持锁进程启动 APScheduler、历史清理和启动恢复工作。
持锁连接断开或进程退出时 PostgreSQL 自动释放锁，其他进程在下一次选举周期接管。

调度器只负责按计划创建持久化扫描任务，不直接执行远程采集。唯一活跃任务部分索引
继续阻止同一设备产生多个等待或运行任务。

## 跨进程缓存失效

扫描成功、设备或集群变化、历史清理后，在事务提交成功后发送 PostgreSQL
`NOTIFY topology_changed`。每个应用进程维护一个专用监听连接，收到通知后清空本地
拓扑缓存。监听连接中断时自动重连；现有 30 秒 TTL 作为通知丢失时的最终一致性兜底。

## 事务重试和错误处理

通用 PostgreSQL 事务执行器不持有进程级全局锁。它只对以下可重放事务重试，并且
每次使用全新会话：

- SQLSTATE `40P01`：死锁。
- SQLSTATE `40001`：序列化失败。

唯一约束、外键、数据校验和编程错误不重试。连接失败、连接池超时和数据库不可用
映射为安全的 HTTP 503 或失败记录。面向用户和批次记录使用：

- `database_unavailable`：数据库当前不可用。
- `transaction_conflict`：事务冲突重试耗尽。
- `task_lease_lost`：任务已由其他进程接管。

日志仅记录操作名、SQLSTATE、尝试次数、任务 ID 和 worker ID，不记录密码、密文、
完整数据库连接串、SQL 参数或原始凭据。

## 连接池和进程数量

默认配置：

```dotenv
WEB_WORKERS=
SCAN_MAX_WORKERS=30
IMPORT_TEST_MAX_WORKERS=20
DB_POOL_SIZE=3
DB_MAX_OVERFLOW=2
DB_POOL_TIMEOUT_SECONDS=30
DB_POOL_RECYCLE_SECONDS=1800
```

`WEB_WORKERS` 为空时使用 `min(CPU 逻辑核心数, 8)`。8 个进程按默认连接池最多占用
约 40 个普通业务连接，另有少量调度器选举和通知监听连接。部署文档要求根据
PostgreSQL `max_connections` 为应用、管理和维护连接预留容量。

数据库引擎启用 `pool_pre_ping`。连接建立设置有限的连接超时，连接池获取超时会被
转换为安全错误；任何远程设备网络等待都不得占用数据库会话。

## 本地开发和内网部署

本机安装 PostgreSQL 15，创建独立开发数据库、测试数据库和低权限账号。本地密码
随机生成并只保存在 Git 忽略的 `.env` 中。安装与配置过程不得把密码输出到日志或
提交到仓库。

内网部署文档包含：

- PostgreSQL 15.18 建库和账号授权。
- `pg_hba.conf` 与监听地址的最小内网访问范围建议。
- 应用 `.env` 示例。
- Alembic 迁移、版本检查和多进程启动命令。
- 连接数估算、备份与恢复命令。
- 升级前迁移、升级后健康检查和失败回退步骤。

`start.ps1` 先检查 PostgreSQL 连通性和 Alembic 版本，再执行迁移，最后按自动或显式
worker 数启动 Uvicorn。密码含特殊字符时使用经过 URL 编码的连接串。

## 测试和验收

本机 PostgreSQL 15 安装完成后，测试默认使用真实 PostgreSQL 测试数据库。测试会
在会话开始时执行 Alembic 初始迁移，并在测试之间清理业务表。由于应用后台线程会
使用独立连接，不依赖单连接事务回滚隔离。

必须覆盖：

- 空库执行 `alembic upgrade head` 并达到最新版本。
- 完整 API、导入、集群、拓扑、历史和扫描测试在 PostgreSQL 上通过。
- 两个队列实例使用不同 worker ID，共享数据库且不重复领取任务。
- 多实例共同执行时全局远程扫描并发峰值不超过 30。
- 租约到期后任务被接管，旧 worker 无法保存扫描结果。
- 两个调度器候选者中只有一个领导者；领导者连接关闭后另一候选者接管。
- 导入连接测试不重复执行且全局并发不超过 20。
- PostgreSQL 死锁与序列化冲突只重试可重放事务，并且不产生重复记录。
- PostgreSQL 通知能清除其他进程的拓扑缓存，TTL 仍可兜底。
- 多 worker Uvicorn 能启动并通过健康检查。
- 日志和 API 响应不泄露密码、密文、连接串或 SQL 参数。

完整测试至少连续执行三轮。静态检查、Alembic一致性检查、迁移幂等性检查和 Git
差异检查必须全部通过。

## 完成标准

- 应用只能使用 PostgreSQL 启动，SQLite URL 会得到明确配置错误。
- 新 PostgreSQL 空库能通过 Alembic 一次建立完整结构。
- 多 Uvicorn 进程安全共享扫描和导入测试任务。
- 全局扫描并发稳定限制为 30，导入测试并发稳定限制为 20。
- 任一进程退出后，任务和调度器能在租约/锁释放后自动恢复。
- 不再出现 SQLite `database is locked` 错误或单进程限制。
- 本地真实 PostgreSQL 测试和完整回归测试均通过。
