# SQLite 高并发扫描写入稳定性设计

## 背景

批量导入完成后的首次扫描会并行采集大量设备。当前正式扫描默认使用 30 个线程执行 SSH/WinRM 采集，每个线程在采集完成后独立写入 `scan_runs`、`connection_records`、`devices`、`scan_tasks` 和批次统计。

生产环境当前使用 SQLite。虽然数据库已经启用 WAL 和 30 秒 `busy_timeout`，SQLite 仍然只能同时执行一个写事务。大量设备在相近时间完成采集后集中提交，等待超时会抛出 `sqlite3.OperationalError: database is locked`。现有 `ScanService.run()` 把该数据库异常归入 `internal_error`，导致页面显示“采集发生内部错误”，掩盖了真实原因。

## 目标

- 保持默认 30 台设备并行执行远程采集。
- 仅串行化 SQLite 写入阶段，不把数据库锁覆盖到 SSH/WinRM 网络操作。
- 扫描结果、设备状态、任务状态和批次统计原子提交。
- 对 SQLite `locked/busy` 执行可重放的事务级退避重试。
- 重试耗尽时显示明确的 `database_busy`，不再误报采集内部错误。
- 导入连接测试、扫描队列、定时任务及请求写入共用同一协调机制。
- SQLite 模式运行时强制单应用进程，防止重复调度、重复领取任务和跨进程锁失效。
- 保持现有密码与错误信息脱敏规则。

## 非目标

- 本次不增加 PostgreSQL 或其他数据库支持。
- 本次不支持多个 Uvicorn 进程共同访问同一个 SQLite 文件。
- 不降低默认远程采集线程数。
- 不引入 Redis、外部消息队列或分布式锁服务。
- 不改变设备连接采集协议和解析逻辑。

## 运行模型

SQLite 模式采用：

```text
1 个 Uvicorn 进程
  ├─ 30 个远程扫描线程
  ├─ 导入连接测试线程池
  ├─ 定时调度器
  └─ 1 个共享 SQLiteWriteCoordinator
```

30 个扫描线程可以同时等待网络、执行 SSH/WinRM 命令和解析连接数据。只有需要修改 SQLite 时，写事务才进入统一协调器排队。

`SCAN_MAX_WORKERS=30` 的语义保持为“当前进程的远程采集并发数”，不是数据库并发写入数。

## SQLiteWriteCoordinator

新增独立服务 `app/services/sqlite_writes.py`，负责识别瞬时 SQLite 写入错误并协调事务执行。

### 接口

协调器提供两个边界清晰的接口：

```python
class SQLiteWriteCoordinator:
    def write(self, operation: Callable[[Session], T]) -> T: ...

    @contextmanager
    def write_once(self) -> Iterator[None]: ...
```

`write(operation)`：

- 获取进程内共享的可重入锁。
- 每次尝试都创建一个新 `Session`。
- 调用 `operation(session)` 重建本次事务需要的 ORM 对象。
- 成功后统一提交并返回结果。
- 遇到 SQLite `database is locked` 或 `database is busy` 时回滚、关闭会话并退避重试。
- 非瞬时数据库错误立即抛出。
- 不允许调用方把绑定当前会话的 ORM 对象跨重试返回或复用。

`write_once()`：

- 获取同一个可重入锁，但不自动创建会话或重放操作。
- 用于当前请求已经构造好的事务。
- 调用方在上下文内完成 `flush/commit/rollback`。
- 锁竞争只发生在进入事务之前，防止显式 `flush()` 先取得 SQLite 写锁。

### 重试策略

固定退避间隔为：

```text
0.1 秒、0.3 秒、0.8 秒、1.5 秒、3 秒
```

SQLite 原生 `busy_timeout` 继续保留。协调器重试针对超时后仍出现的瞬时锁异常，以及事务从读取状态升级为写入状态时产生的 `busy`。

重试日志使用警告级别，记录操作名称、当前尝试次数和最大次数，不记录 SQL 参数、密码或密文。

## 扫描数据流重构

当前 `ScanService.run()` 同时负责远程采集、创建扫描运行、保存连接记录和更新设备状态。重构后拆成两个阶段。

### Collect 阶段

```python
ScanService.collect(device_id: int, trigger: ScanTrigger) -> ScanOutcome
```

`ScanOutcome` 是不绑定 SQLAlchemy 会话的不可变数据：

- 设备 ID 与触发类型。
- 开始、结束时间。
- 成功时的标准化连接记录和警告。
- 失败时的错误码和脱敏错误说明。

Collect 阶段：

- 短暂读取设备连接参数和密文。
- 释放数据库会话后执行远程采集。
- 不创建 `ScanRun`，不持有数据库事务。
- `CollectorError` 转换为已有采集错误码。
- 非预期采集异常转换为 `internal_error`，服务器日志记录完整堆栈，结果只保存脱敏说明。

### Persist 阶段

扫描队列取得 `ScanOutcome` 后调用协调器的 `write(operation)`。同一个新事务内：

1. 重新读取处于 `RUNNING` 状态的 `ScanTask`。
2. 创建最终状态的 `ScanRun`。
3. 成功时写入全部 `ConnectionRecord`。
4. 更新设备的 `last_scan_status` 和 `last_scan_at`。
5. 更新 `ScanTask` 的状态、`scan_run_id`、结束时间和错误信息。
6. 更新所有关联 `ScanBatchItem`。
7. 重新计算相关批次计数与完成状态。
8. 一次提交全部变更。

如果写入事务发生 `locked/busy`，整个事务回滚。下一次尝试使用原始 `ScanOutcome` 和新会话重新构建全部 ORM 对象，因此不会产生重复连接记录或使用已回滚对象。

进程在 Collect 阶段意外退出时，只会留下 `RUNNING` 的 `ScanTask`；启动恢复逻辑可将任务恢复为 `PENDING`，不会留下半完成的 `ScanRun`。

## 数据库错误分类

新增内部异常：

```python
class DatabaseBusy(RuntimeError):
    pass
```

错误处理规则：

- 认证、连接超时、命令、解析问题：保留原有采集错误码。
- 采集器自身非预期异常：`internal_error`。
- SQLite `locked/busy` 且最终重试成功：不产生失败记录。
- SQLite `locked/busy` 重试耗尽：任务标记失败，错误码为 `database_busy`，错误信息为“数据库繁忙，扫描结果未能保存，请重试”。
- 数据库结构损坏、约束错误或编程错误不归类为 `database_busy`，记录完整服务器堆栈并按内部错误处理。

重试耗尽时不得把成功采集的结果标为采集器错误，也不得保存不完整的连接记录。

## 协调范围

同一个 `SQLiteWriteCoordinator` 在 `create_app()` 中创建并注入以下组件：

- `ScanQueueService`：入队、批次创建、领取、恢复、完成、失败与取消。
- `ScanService`/扫描持久化操作：扫描运行、连接记录和设备状态。
- `ImportTestService`：领取导入行、保存测试结果、更新批次计数。
- `SchedulerService`：定时入队和历史数据清理。
- 导入服务：创建批次、逐行保存设备和行结果。
- FastAPI 写接口：设备、集群、设置、扫描批次等修改操作。

所有可能在提交前显式调用 `flush()` 的路径必须在 `write()` 或 `write_once()` 内进入事务，不能只在 `session.commit()` 外层加锁。

只读 API、拓扑读取和历史查询不进入协调器。WAL 模式继续允许读取与唯一写事务并发。

## 单进程启动保护

新增 `SQLiteProcessGuard`：

- 仅在 `DATABASE_URL` 为文件型 SQLite 时启用；测试使用的内存数据库不启用。
- 锁文件位于 SQLite 数据库文件旁，名称为 `<database>.app.lock`。
- Windows 使用标准库 `msvcrt.locking`；Linux/macOS 使用标准库 `fcntl.flock`。
- 应用 lifespan 启动时取得非阻塞排他锁，并保持文件句柄直到应用退出。
- 第二个进程无法取得锁时拒绝启动，错误明确说明：SQLite 模式只支持一个应用进程；远程扫描并发由 `SCAN_MAX_WORKERS` 提供。
- 正常退出时主动释放；进程崩溃时由操作系统释放，不依赖删除锁文件。

该保护同时避免多个进程各自启动 APScheduler、扫描分发器和导入恢复逻辑。

README 和启动示例继续明确不得在 SQLite 模式使用 `uvicorn --workers`。

## 配置

保留现有配置：

```text
SCAN_MAX_WORKERS=30
SQLITE_BUSY_TIMEOUT_MS=30000
```

新增：

```text
SQLITE_WRITE_RETRY_DELAYS=0.1,0.3,0.8,1.5,3
```

配置解析后必须是非负秒数列表，至少包含一个值。默认值与本设计一致；普通部署无需修改 `.env`。

## 日志与界面

锁竞争重试日志示例只包含：

```text
SQLite 写入繁忙 operation=persist_scan attempt=2/6
```

不得写出 SQL 参数、设备密码或加密密文。

扫描批次失败详情在重试耗尽时显示：

```text
错误码：database_busy
错误信息：数据库繁忙，扫描结果未能保存，请重试
```

现有“采集发生内部错误，详情已写入脱敏批次记录”仅用于真正的非预期采集或程序异常。

## 测试

### 协调器单元测试

- 普通写操作执行一次并提交。
- 前两次抛出 `database is locked`，第三次成功。
- `database is busy` 同样重试。
- 非 OperationalError 不重试。
- 重试耗尽抛出 `DatabaseBusy`。
- 每次重试使用新 Session，失败事务均回滚关闭。
- `write_once()` 和 `write()` 使用同一个可重入锁。

### 扫描事务测试

- 30 个采集线程通过屏障同时完成，最终全部任务成功。
- 所有连接记录数量正确，无重复记录。
- 扫描运行、设备状态、任务状态和批次计数一致。
- 人工制造前几次锁冲突后仍保存成功。
- 重试耗尽时任务为 `database_busy`，无部分 `ScanRun` 或连接记录。
- 真实 `CollectorError` 仍保持原错误码。
- 真正的采集器异常仍为 `internal_error`。

### 混合写入测试

- 导入连接测试和正式扫描同时保存时不发生锁错误。
- 扫描任务领取与批次状态更新并发时不重复领取。
- 历史清理与扫描保存并发时保持事务一致。

### 进程保护测试

- 第一个保护实例成功持锁。
- 第二个保护实例对同一数据库失败并返回明确说明。
- 第一个释放后第二个能够取得锁。
- 不同 SQLite 文件互不影响。
- 内存 SQLite 不启用文件锁。

### 回归与安全测试

- 完整 pytest 测试通过。
- Ruff 检查通过。
- 锁重试日志不包含密码、密文或 SQL 参数。
- 现有导入、集群标注、手动扫描、批量扫描和拓扑功能行为不变。

## 验收标准

- 默认 30 路批量扫描不再因应用内部并发写入产生 `database is locked`。
- 数据库锁冲突不会被错误标记为采集 `internal_error`。
- 任一成功任务都同时具备完整扫描运行、连接记录、设备状态和批次状态。
- 任一失败任务都不存在半保存的扫描数据。
- 同一 SQLite 文件无法启动第二个应用进程。
- 不通过降低远程采集并发来实现稳定性。
