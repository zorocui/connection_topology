# 持久化并发扫描队列设计

## 目标与范围

系统需要支持一次管理数百至 1000 台服务器，避免逐台串行扫描，同时保证同一
设备不会重复执行、服务重启后任务不会丢失。本阶段使用数据库持久化队列和固定
线程池，不引入 Redis、Celery 或新的服务端组件。

系统继续以单个 Uvicorn 进程运行。扫描属于网络等待型工作，使用线程并发而不是
为每台设备创建进程。

## 总体架构

- Web 接口、Excel 导入流程和 APScheduler 只负责向数据库队列投递任务。
- 扫描协调器使用固定线程池领取任务，每个任务在线程内创建独立数据库会话。
- 默认正式扫描并发数为 30，默认导入连接测试并发数为 20。
- 队列任务和批次状态存入 SQLite，应用重启后继续处理。
- SQLite 启用 WAL、外键约束和忙等待，降低并发写冲突。
- 同一设备最多存在一个未完成任务，重复请求合并。

## 数据模型

### `scan_batches`

批次用于追踪“扫描全部”“扫描集群”和“Excel 导入首次扫描”：

- `id`
- `batch_type`：`all`、`cluster`、`import`
- `cluster_id`：集群批次使用，其他情况为空
- `source_import_batch_id`：导入批次使用，其他情况为空
- `status`：`pending`、`running`、`completed`
- `total_tasks`
- `pending_tasks`
- `running_tasks`
- `success_tasks`
- `failed_tasks`
- `created_at`
- `finished_at`

### `scan_tasks`

- `id`
- `device_id`
- `trigger_type`
- `priority`
- `status`：`pending`、`running`、`success`、`failed`、`cancelled`
- `scan_run_id`
- `error_message`
- `created_at`
- `started_at`
- `finished_at`

数据库约束和队列服务共同保证同一设备最多一个未完成任务。由于 SQLite 不便直接
为枚举子集创建通用唯一约束，迁移将增加针对未完成状态的部分唯一索引。

`ImportBatch` 增加可空 `scan_batch_id`，用于关联连接测试完成后创建的首次扫描
批次。`scan_batches.source_import_batch_id` 使用唯一约束，保证并发完成最后几条
连接测试时也只创建一个首次扫描批次。

## 入队与合并规则

优先级从高到低：

| 来源 | 优先级 |
|---|---:|
| 单台手动扫描 | 100 |
| 扫描全部或扫描集群 | 80 |
| Excel 导入首次扫描 | 60 |
| 定时扫描 | 20 |

同一设备已有等待或执行中任务时：

- 不创建第二个未完成任务。
- 新请求优先级更高时提升原任务优先级。
- 新批次仍需要追踪该设备时，通过批次任务关联记录复用结果；第一阶段为保持模型
  简单，批次创建时跳过已在其他任务中运行的设备，并将其计入批次的等待项，原任务
  完成时同步结算所有关联批次。

为实现一个任务关联多个批次，增加 `scan_batch_items` 关联表：

- `batch_id`
- `task_id`
- `device_id`
- `status`

批次统计以关联表为准，避免一个 `scan_tasks.batch_id` 无法表达任务复用。因而
`scan_tasks` 不再保存单一 `batch_id`。

## Excel 导入流程

1. Excel 正确行立即保存设备。
2. 最多 20 个线程并发测试连接。
3. 所有连接测试结束后，筛选测试成功的设备。
4. 创建“导入首次扫描”批次并将成功设备入队，保存到
   `ImportBatch.scan_batch_id`。
5. 最多 30 个扫描线程执行完整扫描。
6. 导入结果页面同时展示连接测试进度和首次扫描批次进度。

测试失败的设备保留，但不进入首次完整扫描。

## 手动及批量扫描

- `POST /api/devices/{device_id}/scan` 改为异步入队并返回 HTTP 202 和任务信息。
- 页面轮询任务状态；成功后刷新设备状态或拓扑。
- 设备管理页增加“扫描全部设备”和“扫描指定集群”。
- 批量接口创建批次并返回批次编号。
- 页面显示总数、等待、执行、成功、失败及进度百分比。
- 页面关闭不影响后台任务，重新打开可查看最近扫描批次。

## 定时扫描

- APScheduler 保留每台设备的间隔计划，但执行函数只负责入队。
- 每个计划使用 0 至 `SCAN_JITTER_SECONDS` 的随机错峰。
- 队列中已有该设备时进行合并，不重复扫描。
- 队列满时定时任务跳过并记录日志，不阻塞调度线程。

## 扫描协调器

- 启动时将遗留的 `running` 任务恢复为 `pending`。
- 按优先级降序、创建时间升序领取任务。
- 单进程内使用领取锁，确保多个扫描线程不会获得同一任务。
- 领取事务只更新任务状态，不执行网络请求。
- 网络采集结束后使用新短事务保存 `ScanRun`、连接明细、任务结果和批次计数。
- 单台失败不影响其他任务，不自动无限重试。
- Windows 缺少 `pywinrm` 时立即失败并写入明确中文错误。
- 应用关闭时停止领取新任务；正常关闭等待当前任务完成，强制终止后遗留任务在下次
  启动时恢复。

## 配置

`.env` 新增：

```dotenv
IMPORT_TEST_MAX_WORKERS=20
SCAN_MAX_WORKERS=30
SCAN_QUEUE_SIZE=2000
SCAN_JITTER_SECONDS=300
SQLITE_BUSY_TIMEOUT_MS=30000
```

所有配置由 `pydantic-settings` 校验，修改后重启生效。第一阶段不在设置页面动态
修改并发数。

## SQLite 配置

- `PRAGMA journal_mode=WAL`
- `PRAGMA foreign_keys=ON`
- `PRAGMA busy_timeout=SQLITE_BUSY_TIMEOUT_MS`
- 每个工作线程使用独立 SQLAlchemy 会话。
- SSH/WinRM 网络等待期间不保持数据库写事务。
- 队列达到 `SCAN_QUEUE_SIZE` 后拒绝新的非合并任务。

## 错误与容量处理

- 手动或批量请求在队列满时返回 HTTP 429 和中文提示。
- 定时任务在队列满时跳过并记录警告日志。
- 删除设备时取消或级联删除其未完成任务和批次关联项。
- 失败任务保留错误摘要，敏感凭据继续经过现有脱敏逻辑。
- 批次完成条件是全部关联项进入成功、失败或取消状态。

## 部署约束

- 继续只运行一个 Uvicorn 进程，不使用 `--workers`。
- systemd 使用单个应用实例，重启策略保持 `on-failure`。
- 后续如需多应用进程或超过约 2000 至 5000 台设备，再迁移到 PostgreSQL 和独立
  分布式任务队列。

## 测试与验收

- 配置默认值和边界校验。
- 任务创建、同设备合并、优先级提升和容量限制。
- 多批次复用同一任务并正确结算。
- 30 个并发线程领取任务时不重复。
- 重启恢复遗留执行中任务。
- 扫描全部、扫描集群和导入首次扫描。
- 定时任务只入队并配置错峰。
- SQLite WAL、外键和忙等待配置。
- 单台失败不影响批次其他任务。
- 缺少 `pywinrm` 不影响队列和 Linux 扫描。
- 页面单台轮询和批次进度展示。
- 完整 Ruff、pytest、JavaScript 语法检查和浏览器页面验证。
