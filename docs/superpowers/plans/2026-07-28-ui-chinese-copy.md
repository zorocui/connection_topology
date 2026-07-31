# 页面中文文案实施计划

> **For agentic workers:** 在当前会话内按检查、替换、测试顺序执行。

**目标：** 将页面中除专业术语外的英文描述全部改为中文。

**范围：** Jinja 页面模板、拓扑交互脚本、FastAPI 页面标题及页面测试。

**保留术语：** SSH、WinRM、TCP、UDP、PID、IP、API、Linux、Windows、Fernet、ESTABLISHED、LISTEN、TIME_WAIT。

## 文案映射

- `Connection Atlas`、`CONNECTION / ATLAS` → `连接图谱`
- `LOCAL CONTROL` → `本地控制`
- `NETWORK OPERATIONS` → `网络运维`
- `ASIA / SHANGHAI` → `中国标准时间`
- `LIVE NETWORK PULSE` → `实时网络态势`
- `FLEET STATUS` → `设备运行状态`
- `RECENT RUNS` → `最近采集任务`
- `AUTO` → `自动`
- `RELATIONSHIP MAP` → `连接关系图`
- `CONNECTION DETAIL` → `连接详情`
- `CONNECTIONS` → `连接数`
- `REMOTE INVENTORY` → `远程设备清单`
- `NEW ENDPOINT` → `新增设备`
- `MANAGED FLEET` → `受管设备`
- `SNAPSHOT ARCHIVE` → `快照档案`
- `SCAN LEDGER` → `采集记录`
- `SYSTEM POLICY` → `系统策略`
- `DATA RETENTION` → `数据保留`
- `SECURITY NOTE` → `安全说明`
- `SUCCESS / FAILED / RUNNING` → `成功 / 失败 / 运行中`
- `MANUAL / SCHEDULED` → `手动 / 定时`
- `DEVICE` → `设备`

## 验证

1. 扫描模板和用户可见 JavaScript 字符串，确认仅保留专业术语。
2. 更新页面测试断言。
3. 运行 `ruff check app tests`。
4. 运行 `pytest -q`。
