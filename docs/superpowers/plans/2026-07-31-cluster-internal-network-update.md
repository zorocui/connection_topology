# Cluster Internal Network Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复编辑集群并增加第三个内部 IPv4 地址段时触发的数据库唯一约束错误。

**Architecture:** 保持现有接口和数据模型不变，把内部地址段关系的“全部清空并重建”改为集合差量更新。未变化的 ORM 对象继续保留，只删除缺失项并插入新增项。

**Tech Stack:** Python 3.10、FastAPI、SQLAlchemy 2、pytest、SQLite

## Global Constraints

- 单个集群仍最多支持 100 个内部 IPv4 CIDR。
- 不修改 API 请求或响应格式。
- 不修改数据库结构或创建迁移。
- 按用户要求不执行 Git、分支、提交或工作树操作。

---

### Task 1: Add update regression coverage

**Files:**
- Modify: `tests/test_clusters.py`

**Interfaces:**
- Consumes: `PUT /api/clusters/{cluster_id}` 与 `ClusterRead.internal_networks`
- Produces: 覆盖保留、增加、重排、删除地址段的接口回归测试

- [ ] **Step 1: Write a failing regression test**

新增测试：先创建包含 `10.0.0.0/16` 和 `10.96.0.0/12` 的集群，再编辑为包含原两项和 `172.16.0.0/12`，断言状态码为 200 且列表包含三项；随后用不同顺序提交相同集合，再删除一项并加入 `192.168.0.0/16`，断言结果始终与标准化后的提交集合一致。

- [ ] **Step 2: Run the focused test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_clusters.py -k internal_network_update -q`

Expected: FAIL，旧实现抛出 `UNIQUE constraint failed`。

### Task 2: Implement differential relationship updates

**Files:**
- Modify: `app/services/clusters.py`
- Test: `tests/test_clusters.py`

**Interfaces:**
- Consumes: `replace_internal_networks(session: Session, cluster: Cluster, cidrs: list[str]) -> None`
- Produces: 相同签名、支持幂等和增删混合的差量更新行为

- [ ] **Step 1: Replace clear-and-rebuild with a diff**

在 `replace_internal_networks` 中创建目标 CIDR 集合，移除 `cidr` 不在目标集合中的已有关系；创建现有 CIDR 集合，只追加目标列表中尚不存在的 `ClusterInternalNetwork`，然后调用 `session.flush()`。

- [ ] **Step 2: Run focused cluster tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_clusters.py -q`

Expected: PASS。

- [ ] **Step 3: Run complete verification**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: 全部测试通过。

Run: `.\.venv\Scripts\python.exe -m ruff check app tests`

Expected: `All checks passed!`

Run: `node --check app/static/js/clusters.js`

Expected: 退出码 0。

### Task 3: Runtime smoke test and package

**Files:**
- Verify: `app/services/clusters.py`
- Create: `connection-topology-linux-YYYYMMDD-HHMMSS.tar.gz`

**Interfaces:**
- Consumes: 正式服务的集群管理 API 与现有打包脚本
- Produces: 可部署的时间戳 Linux 压缩包

- [ ] **Step 1: Restart the local service**

使用项目现有启动方式重启 `127.0.0.1:8000` 服务，确保加载修复后的代码。

- [ ] **Step 2: Verify the cluster page**

打开集群管理页面，确认页面正常加载且浏览器控制台无新增错误；通过隔离测试数据验证从两个内部地址段增加到三个可成功保存。

- [ ] **Step 3: Build a timestamped Linux package**

Run: `powershell -ExecutionPolicy Bypass -File .\package-linux.ps1`

Expected: 生成名称包含当前时间戳的 `.tar.gz` 文件，且不包含虚拟环境、数据库、日志或缓存。
