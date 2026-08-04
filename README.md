# Connection Atlas 服务器连接拓扑

一个 Python 3.10 Web 系统：使用 SSH 采集 Linux 服务器、使用 WinRM 采集 Windows Server 的 TCP/UDP 连接，保存历史快照，并以拓扑图展示服务器与对端的连接关系。

## 已实现功能

- Linux：SSH 登录并执行固定只读命令 `ss -H -tunap`
- Windows：WinRM 登录并执行固定 PowerShell 采集脚本
- 统一记录协议、本地/对端地址与端口、TCP 状态、PID 和进程名
- Fernet 加密保存服务器密码
- 手动采集和每台设备独立的分钟级定时采集
- 完整保存成功快照，记录失败批次与错误摘要
- 拓扑优先页面、设备管理、采集历史和系统设置
- 默认保留 7 天历史，支持系统、集群和设备三级设置
  - 优先级：设备自定义 > 集群自定义 > 系统默认
  - 设备或集群留空时自动继承上一级设置
- 集群统一管理采集间隔和定时采集开关
  - 修改集群会立即同步当前全部成员设备
  - 后续加入或 Excel 导入到该集群的设备自动采用集群策略
  - 未分组设备继续使用自身的采集设置
- 同一设备采集互斥，失败快照不会写入部分连接记录

## 运行要求

- Windows 本机或其他可运行 Python 的管理主机
- Python 3.10
- 目标 Linux 服务器启用 SSH 且安装 `ss`（通常来自 `iproute2`）
- 目标 Windows Server 启用 WinRM，并允许管理主机访问 5985（HTTP）或 5986（HTTPS）

## 本项目环境

项目已经创建 Python 3.10 虚拟环境：

```powershell
.\.venv\Scripts\python.exe --version
```

预期输出为 `Python 3.10.x`。

如需从头重建：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

## 配置

应用从项目根目录的 `.env` 读取配置。本项目已生成本地 `.env`，该文件被 `.gitignore` 排除。

重新生成 Fernet 密钥：

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

配置字段：

```dotenv
APP_SECRET_KEY=粘贴生成的Fernet密钥
DATABASE_URL=sqlite:///./connection_topology.db
HOST=127.0.0.1
PORT=8000
HISTORY_RETENTION_DAYS=7
```

`APP_SECRET_KEY` 丢失或变化后，已保存的设备密码将无法解密。应像备份密码一样妥善备份该密钥，但不要将 `.env` 提交到版本库。

## 启动

在项目根目录执行：

```powershell
.\start.ps1
```

然后访问：

- 系统页面：<http://127.0.0.1:8000>
- API 文档：<http://127.0.0.1:8000/docs>

默认只监听本机。如果改为 `0.0.0.0` 供内网访问，应使用反向代理认证或其他可信网络访问控制；当前版本不包含应用登录。

## Linux SSH 前置条件

目标机需要：

1. 启用 SSH 密码登录。
2. 安装 `ss`。
3. 允许账户执行 `ss -H -tuna`。

系统首先执行包含进程信息的 `ss -H -tunap`。如果普通用户无权查看全部进程，系统会降级执行 `ss -H -tuna`，采集仍成功，但部分 PID 和进程名为空。

## Windows WinRM 前置条件

Windows 扫描是可选能力。只采集 Linux 设备时，使用基础安装即可，未安装
`pywinrm` 不会影响系统启动、页面访问或 Linux SSH 扫描：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

需要启用 Windows WinRM 扫描时，安装 `windows` 可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[windows]"
```

缺少该组件时仍可保存 Windows 设备，但连接测试和采集会明确提示
“当前环境未安装 Windows 采集组件，请安装 pywinrm”。

在目标 Windows Server 的管理员 PowerShell 中按组织安全策略配置 WinRM。测试环境可使用：

```powershell
Enable-PSRemoting -Force
Set-Service WinRM -StartupType Automatic
```

系统使用 NTLM。端口 5985 使用 HTTP，5986 使用 HTTPS。跨工作组环境可能还需要在管理主机配置 WinRM TrustedHosts；生产环境应优先使用域身份、HTTPS 监听器或网络级访问限制。

采集账户需要执行以下只读命令的权限：

- `Get-NetTCPConnection`
- `Get-NetUDPEndpoint`
- `Get-Process`

## 使用流程

1. 打开“设备管理”。
2. 选择 Linux/SSH 或 Windows/WinRM。
3. 输入设备名称、地址、端口、用户名和密码。
4. 点击“测试连接”，或直接点击“测试并保存”。
5. 点击设备行的刷新按钮执行首次采集。
6. 打开“连接拓扑”，选择设备并查看对端节点。
7. 点击拓扑边或节点，查看端口、状态、PID 和进程明细。

## 集群与 Excel 批量导入

- 设备可以归属一个集群，也可以保持“未分组”。设备管理页支持新建、重命名和删除集群；删除集群只会解除设备归属，不会删除设备。
- 手动添加设备时，可以选择现有集群，或直接填写一个新集群名称。
- 设备管理页提供 `.xlsx` 模板下载和批量导入。模板字段依次为：设备名称、主机地址、操作系统、端口、用户名、密码、所属集群、采集间隔（分钟）、启用定时采集。
- 单次最多导入 1000 行、文件不超过 5 MB。重复的主机地址、端口和用户名组合会跳过；错误行不会影响其他正确行。
- 导入成功的设备会自动执行后台连接测试，最多同时测试 3 台。页面会持续更新批次状态，并可下载不含密码的逐行结果报告。
- Excel 中的密码以明文填写，系统接收后立即加密保存。导入完成后，请删除源文件或将其存放在受控加密位置。

拓扑页顶部可以在“设备模式”和“集群模式”之间切换。集群模式会把同一集群的设备合并成一个节点，隐藏集群内部连接，仅聚合显示跨集群或连接外部对端的关系；未分组设备仍作为独立节点显示。

系统会把 IPv4 映射 IPv6 地址（例如 `::ffff:10.160.79.21`）统一显示为普通 IPv4 地址（`10.160.79.21`）。该规则同时适用于新采集记录、已有历史记录、设备拓扑、集群拓扑和扫描差异比较；原生 IPv6 地址保持不变。

设备模式和集群模式会隐藏标准环回连接，包括 IPv4 的整个 `127.0.0.0/8`、IPv6 的 `::1` 及其 IPv4 映射形式。原始连接仍完整保存在数据库和历史快照中。

对端较多时，拓扑会使用紧凑多圈布局并保持节点可读尺寸。点击节点或连线可突出直接相关关系，点击画布空白处恢复；“适配全图”用于查看全部节点，“重置视图”用于返回推荐缩放和中心位置。

## 并发扫描队列

扫描任务使用 SQLite 持久化队列和固定线程池执行。同一设备最多存在一个等待中或
执行中的任务；重复请求会复用任务，手动扫描会提升优先级。服务重启后，未完成
任务会自动恢复。

可在 `.env` 中配置：

```dotenv
IMPORT_TEST_MAX_WORKERS=20
SCAN_MAX_WORKERS=30
SCAN_QUEUE_SIZE=2000
SCAN_JITTER_SECONDS=300
SQLITE_BUSY_TIMEOUT_MS=30000
SQLITE_WRITE_RETRY_DELAYS=0.1,0.3,0.8,1.5,3
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=10
DB_POOL_TIMEOUT_SECONDS=60
```

- `IMPORT_TEST_MAX_WORKERS`：Excel 导入后的并发连接测试数。
- `SCAN_MAX_WORKERS`：完整连接扫描的最大并发线程数。
- `SCAN_QUEUE_SIZE`：等待或执行中的不同设备任务总上限。
- `SCAN_JITTER_SECONDS`：定时任务的最大随机错峰秒数。
- `SQLITE_BUSY_TIMEOUT_MS`：SQLite 并发写入等待时间。
- `SQLITE_WRITE_RETRY_DELAYS`：SQLite 写冲突后的退避重试秒数。
- `DB_POOL_SIZE`：数据库常驻连接数，默认 20。
- `DB_MAX_OVERFLOW`：连接池繁忙时允许的临时额外连接数，默认 10。
- `DB_POOL_TIMEOUT_SECONDS`：获取数据库连接的最长等待秒数，默认 60。

导入连接测试和设备扫描读取参数后会释放数据库会话，SSH/WinRM 网络等待不会占用
连接池。`SCAN_MAX_WORKERS=30` 提供 30 路远程并行采集；采集结果的 SQLite 写入
只会短暂排队，不会降低远程连接并发。

SQLite 模式必须只运行一个 Uvicorn 进程，应用会在启动时对数据库文件加进程锁并
强制检查。不要使用 `uvicorn --workers`；增加连接池或 Web 进程不会加快远程采集，
反而会重复启动调度器。写入发生短暂锁冲突时，会按照
`SQLITE_WRITE_RETRY_DELAYS=0.1,0.3,0.8,1.5,3` 使用新事务重试：

```bash
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

设备管理页支持扫描全部设备或指定集群，并持续显示等待、执行中、成功和失败数量。
失败数大于 0 时，可打开独立失败明细面板，按设备名称、IP、所属集群或失败
原因搜索并分页查看；执行中的批次会自动刷新，完成后仍可回看。点击“重新采集
失败设备”会创建新的失败重试批次，原批次历史保持不变。
Excel 导入连接测试全部结束后，测试成功的设备会自动进入首次完整扫描批次。

## 数据与备份

默认数据库为项目根目录的 `connection_topology.db`。备份时应同时安全保存：

- `connection_topology.db`
- `.env` 中的 `APP_SECRET_KEY`

仅有数据库而没有原加密密钥，无法恢复远程登录密码。

## 测试与代码检查

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing
```

自动化测试使用模拟 SSH/WinRM 采集器，不需要真实服务器。

## 目录

```text
app/
  collectors/   Linux SSH 与 Windows WinRM 采集器
  routes/       页面与 JSON API
  services/     采集事务、拓扑、调度和清理
  static/       页面样式与拓扑交互
  templates/    Jinja2 页面
tests/          自动化测试与采集输出样本
docs/           设计与实施计划
```
