# 批量导入数据库连接池韧性设计

## 背景

一次导入 150 台设备后，系统在“正在测试连接”阶段出现：

```text
QueuePool limit of size 5 overflow 10 reached,
connection timed out, timeout 30.00
```

当前导入连接测试默认并发为 20，而 SQLAlchemy 默认连接池只有 5 个常驻连接和 10 个溢出连接。`ImportTestService.test_row()` 在读取设备后保持数据库会话，随后在该会话内等待 SSH/WinRM 网络测试完成，导致每个测试线程长期占用一个数据库连接。第 16 个并发线程开始等待连接池，超过 30 秒后超时。

## 目标

- 远程连接测试期间不占用数据库连接。
- 连接池容量、溢出量和等待时间可通过环境变量调整。
- 默认配置能够承受 20 个导入测试线程及正常页面请求。
- 150 台设备导入测试能够最终完成，不出现连接池超时。
- 保持现有成功、失败、脱敏和首次完整扫描行为。

## 不在本次范围

- 不切换到 PostgreSQL、MySQL 或其他数据库。
- 不增加 Uvicorn Web 进程数。
- 不改变导入测试默认并发数 20。
- 不重构完整扫描队列的远程采集流程。
- 不增加新的数据库表或迁移版本。

## 连接池配置

在 `Settings` 中新增：

```python
db_pool_size: int = Field(default=20, ge=1, le=200)
db_max_overflow: int = Field(default=10, ge=0, le=200)
db_pool_timeout_seconds: int = Field(default=60, ge=1, le=300)
```

对应环境变量：

```env
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=10
DB_POOL_TIMEOUT_SECONDS=60
```

`create_database_engine()` 接收这三个参数并传给 SQLAlchemy `create_engine()`：

- `pool_size=db_pool_size`
- `max_overflow=db_max_overflow`
- `pool_timeout=db_pool_timeout_seconds`

默认最大同时连接数为 30。连接池用于承受短时数据库操作、页面请求和批次统计，不用于维持远程网络等待。

SQLite 继续使用：

- `check_same_thread=False`
- `PRAGMA foreign_keys=ON`
- WAL 模式
- 现有 `busy_timeout`、缓存和内存临时表配置

系统仍要求只运行一个 Uvicorn 进程。增加连接池容量不改变 SQLite 单机队列的部署边界。

## 导入测试会话生命周期

`ImportTestService.test_row()` 拆分为三个阶段。

### 阶段一：读取测试目标

打开短数据库会话：

1. 读取 `ImportRowResult`。
2. 行不存在或状态不是 `PENDING` 时直接结束。
3. 保存 `batch_id`。
4. 读取关联设备。
5. 复制不可变的连接参数：
   - 设备 ID
   - 操作系统类型
   - 主机地址
   - 端口
   - 用户名
   - 加密密码
6. 关闭会话。

不得把处于会话绑定状态的 `Device` ORM 对象传入网络阶段。

设备在阶段一已经不存在时，将本次结果设为“导入设备不存在”，随后进入阶段三持久化。

### 阶段二：远程网络测试

此阶段没有活动数据库会话：

1. 解密复制的密码。
2. 根据操作系统选择 Linux 或 Windows collector。
3. 创建 `DeviceConnectionSpec`。
4. 执行 `collector.test_connection(spec, password)`。
5. 产生不可变的测试结果：
   - `SUCCESS / 连接测试成功`
   - `FAILED / 脱敏后的异常信息`

密码只存在于当前工作线程的局部变量中。错误文本继续使用 `safe_error_message()` 脱敏并截断。

### 阶段三：保存测试结果

重新打开短数据库会话：

1. 再次读取导入行。
2. 行已删除时直接结束。
3. 行状态不再是 `PENDING` 时不覆盖现有结果。
4. 写入测试状态和消息。
5. 重新统计批次的待测试、成功和失败数量。
6. 首次从未完成变为完成时记录完成时间。
7. 提交并关闭会话。
8. 会话关闭后才调用批次完成回调。

## 并发与竞态

- 同一导入行正常情况下只提交一个测试任务。
- 即使任务重复提交，阶段三的 `PENDING` 状态检查也能避免重复覆盖。
- 设备在网络测试期间被删除时，测试使用阶段一复制的参数完成；阶段三只更新仍存在的导入行，不恢复设备。
- 导入行在网络测试期间被删除时，阶段三安全结束。
- 多个测试同时结束时，SQLite WAL 和 `busy_timeout` 负责串行化短写入。
- 每次提交前重新统计批次计数，最终提交的任务会观察到全部已提交结果。
- 只有 `_refresh_batch_counts()` 检测到批次首次完成时才调用首次完整扫描回调。

## 错误边界

远程测试错误和数据库基础设施错误必须分开：

- SSH/WinRM 认证失败、超时或 collector 异常保存为该设备的测试失败。
- 解密错误保存为该设备的测试失败，并对密码脱敏。
- 数据库连接、查询或提交错误不得保存成设备连接失败；由后台任务记录异常日志。
- `ThreadPoolExecutor` 中的异常通过完成回调读取并记录，避免静默丢失。
- 连接池参数不在允许范围时由 Pydantic 在系统启动时拒绝配置。

## 测试

### 配置和引擎

- 默认值为 20、10、60。
- 验证下界和上界。
- 验证引擎池的 `size()`、`_max_overflow` 和 `_timeout` 使用配置值。
- 验证现有 SQLite PRAGMA 配置未回退。

### 会话生命周期

使用容量为 2、无溢出、短等待时间的测试连接池和 20 个并发任务：

- collector 使用 barrier 让 20 个线程同时停留在网络阶段。
- 20 个线程全部到达 barrier，证明未被连接池容量限制在阶段一。
- barrier 等待期间，主线程仍可打开会话并执行查询。
- 释放 barrier 后，全部任务完成且无 `QueuePool` 超时。

### 行为回归

- 成功测试写入 `SUCCESS` 和“连接测试成功”。
- 认证失败与连接超时写入脱敏后的 `FAILED` 原因。
- 设备在阶段一前不存在时写入“导入设备不存在”。
- 设备在网络测试期间删除时不恢复设备。
- 导入行在网络测试期间删除时安全结束。
- 状态已经不是 `PENDING` 时不重复覆盖。
- 批次完成回调只触发一次。
- 150 行压力测试最终 `test_pending_rows == 0`，且未出现连接池超时。

## 文档与部署

- `.env.example` 增加三个连接池变量。
- README 解释连接池变量、推荐默认值和调整原则。
- Linux 部署继续使用单 Uvicorn 进程。
- 完成后运行完整 pytest、Ruff 和 JavaScript 语法检查。
- 重启正式本地服务。
- 生成带时间戳 Linux 部署包。
- 不执行任何 Git 操作。
