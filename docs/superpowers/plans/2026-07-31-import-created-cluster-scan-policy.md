# Import-Created Cluster Scan Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Excel 自动创建的新集群使用首条成功导入记录的采集间隔和定时采集开关。

**Architecture:** 扩展 `create_cluster` 的可选初始化参数，保持所有现有调用默认行为不变。Excel 导入仅在目标集群不存在时传入当前行策略，设备随后继续通过 `cluster_scan_values` 继承集群策略。

**Tech Stack:** Python 3.10、SQLAlchemy 2、openpyxl、pytest

## Global Constraints

- 已存在集群不能被 Excel 导入修改。
- 同一新集群后续行统一继承第一条成功记录建立的策略。
- 未分组设备继续使用 Excel 行设置。
- 不修改 Excel 表头和 API 格式。
- 在当前 `main` 分支完成、测试、提交并推送。

---

### Task 1: Add failing import policy tests

**Files:**
- Modify: `tests/test_imports.py`

**Interfaces:**
- Consumes: `import_devices(session, cipher, filename, content) -> ImportBatch`
- Produces: 新集群初始化和后续继承规则的回归测试

- [ ] **Step 1: Test a newly created cluster**

导入一行所属集群不存在、采集间隔 60、定时采集为“否”的设备，断言新集群与设备的 `scan_interval_minutes == 60` 且 `scheduled_enabled is False`。

- [ ] **Step 2: Test first-row policy precedence**

导入两行属于同一新集群的设备，第一行设置 60/否，第二行设置 10/是；断言集群和两台设备均为 60/否。

- [ ] **Step 3: Run focused tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_imports.py -k "creates_cluster_with_row_scan_policy or first_row_cluster_policy" -q`

Expected: FAIL，当前新集群和设备均保存默认 5/启用。

### Task 2: Initialize new clusters from the import row

**Files:**
- Modify: `app/services/clusters.py`
- Modify: `app/services/imports.py`
- Test: `tests/test_imports.py`

**Interfaces:**
- Consumes: `create_cluster(session, name, description, internal_networks)`
- Produces: `create_cluster(..., scan_interval_minutes: int = 5, scheduled_enabled: bool = True) -> Cluster`

- [ ] **Step 1: Extend cluster creation defaults**

给 `create_cluster` 增加仅限关键字的 `scan_interval_minutes=5` 和 `scheduled_enabled=True` 参数，并在创建 `Cluster` 时赋值；现有 API、设备快捷建集群等调用不传参数，行为保持不变。

- [ ] **Step 2: Pass parsed policy only for absent import clusters**

在 `import_devices` 中，仅当 `find_cluster_by_name` 返回 `None` 时执行：

```python
cluster = create_cluster(
    session,
    parsed["cluster_name"],
    scan_interval_minutes=parsed["scan_interval_minutes"],
    scheduled_enabled=parsed["scheduled_enabled"],
)
```

已有集群不调用更新逻辑。

- [ ] **Step 3: Run cluster and import tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_imports.py tests/test_clusters.py -q`

Expected: PASS。

### Task 3: Clarify template instructions and validate

**Files:**
- Modify: `app/services/imports.py`
- Modify: `tests/test_imports.py`

**Interfaces:**
- Consumes: `build_import_template() -> bytes`
- Produces: 明确集群策略优先级的“填写说明”文本

- [ ] **Step 1: Update instructions**

把“采集间隔（分钟）”说明改为：`未分组设备直接采用；新集群以首条成功记录为准；已有集群继承集群设置`。把“启用定时采集”说明补充相同优先级。

- [ ] **Step 2: Assert instruction text**

读取模板“填写说明”工作表，断言采集间隔说明包含“新集群”和“已有集群”。

- [ ] **Step 3: Run complete validation**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: 全部测试通过。

Run: `.\.venv\Scripts\python.exe -m ruff check app tests`

Expected: `All checks passed!`

### Task 4: Restart, package, and publish

**Files:**
- Create: `connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz`

**Interfaces:**
- Consumes: 通过验证的代码
- Produces: 更新后的本地服务、Linux 部署包和 GitHub `main`

- [ ] **Step 1: Restart the verified local service**

只停止 8000 端口上命令行为 `uvicorn app.main:app` 的进程，使用项目 `.venv` 重新启动并验证 `/devices` 返回 200。

- [ ] **Step 2: Build and inspect the package**

Run: `powershell -ExecutionPolicy Bypass -File .\package-linux.ps1`

Expected: 新时间戳包包含修复代码，不含数据库、日志、虚拟环境或缓存。

- [ ] **Step 3: Commit and push**

```powershell
git add app/services/clusters.py app/services/imports.py tests/test_imports.py docs/superpowers/plans/2026-07-31-import-created-cluster-scan-policy.md
git commit -m "fix: preserve imported policy for new clusters"
git push origin main
```

Expected: 本地 `main` 与 `origin/main` 一致且工作区干净。
