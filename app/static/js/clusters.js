(() => {
  const form = document.getElementById("cluster-form");
  const idInput = document.getElementById("cluster-id");
  const nameInput = document.getElementById("cluster-name");
  const descriptionInput = document.getElementById("cluster-description");
  const retentionInput = document.getElementById("cluster-retention-days");
  const scanIntervalInput = document.getElementById("cluster-scan-interval");
  const scheduledInput = document.getElementById("cluster-scheduled-enabled");
  const networksInput = document.getElementById("cluster-networks");
  const list = document.getElementById("cluster-list");
  const count = document.getElementById("cluster-count");
  const cancel = document.getElementById("cancel-cluster-edit");
  let clusters = [];

  const parseNetworks = () => networksInput.value
    .split(/[\n,，]+/)
    .map(value => value.trim())
    .filter(Boolean);

  const resetForm = () => {
    form.reset();
    idInput.value = "";
    cancel.hidden = true;
    document.getElementById("cluster-form-title").textContent = "新建集群";
  };

  const render = () => {
    count.textContent = String(clusters.length);
    list.innerHTML = clusters.length ? clusters.map(cluster => `
      <article class="cluster-card">
        <header>
          <div>
            <b>${escapeHtml(cluster.name)}</b>
            <small>${escapeHtml(cluster.description || "暂无描述")}</small>
          </div>
          <span>${cluster.device_count} 台设备</span>
        </header>
        <p class="retention-summary">
          历史保留 ${cluster.effective_history_retention_days} 天
          · ${cluster.history_retention_days === null ? "继承系统" : "集群自定义"}
        </p>
        <p class="retention-summary">
          每 ${cluster.scan_interval_minutes} 分钟采集
          · 定时采集已${cluster.scheduled_enabled ? "启用" : "关闭"}
        </p>
        <div class="network-tags">
          ${cluster.internal_networks.length
            ? cluster.internal_networks.map(network =>
                `<span class="network-tag">${escapeHtml(network)}</span>`
              ).join("")
            : '<span class="muted">未配置内部地址段</span>'}
        </div>
        <footer>
          <button class="button" type="button" data-edit-cluster="${cluster.id}">编辑</button>
          <button class="button danger" type="button" data-delete-cluster="${cluster.id}">删除</button>
        </footer>
      </article>
    `).join("") : '<p class="muted">尚未创建集群。</p>';
  };

  const load = async () => {
    const response = await fetch("/api/clusters");
    if (!response.ok) return toast("读取集群失败", "error");
    clusters = await response.json();
    render();
  };

  list.addEventListener("click", async event => {
    const edit = event.target.closest("[data-edit-cluster]");
    if (edit) {
      const cluster = clusters.find(item => item.id === Number(edit.dataset.editCluster));
      if (!cluster) return;
      idInput.value = String(cluster.id);
      nameInput.value = cluster.name;
      descriptionInput.value = cluster.description || "";
      retentionInput.value = cluster.history_retention_days ?? "";
      scanIntervalInput.value = String(cluster.scan_interval_minutes);
      scheduledInput.checked = cluster.scheduled_enabled;
      networksInput.value = cluster.internal_networks.join("\n");
      cancel.hidden = false;
      document.getElementById("cluster-form-title").textContent = "编辑集群";
      nameInput.focus();
      return;
    }
    const remove = event.target.closest("[data-delete-cluster]");
    if (!remove) return;
    if (!confirm("删除集群后，所属设备将变为未分组，内部地址段也会删除。确定继续吗？")) return;
    const response = await fetch(`/api/clusters/${remove.dataset.deleteCluster}`, {
      method: "DELETE"
    });
    if (!response.ok) return toast("删除集群失败", "error");
    toast("集群已删除");
    resetForm();
    await load();
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    const clusterId = idInput.value;
    const retentionValue = retentionInput.value.trim();
    const response = await fetch(
      clusterId ? `/api/clusters/${clusterId}` : "/api/clusters",
      {
        method: clusterId ? "PUT" : "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          name: nameInput.value,
          description: descriptionInput.value || null,
          internal_networks: parseNetworks(),
          history_retention_days: retentionValue
            ? Number(retentionValue)
            : null,
          scan_interval_minutes: Number(scanIntervalInput.value),
          scheduled_enabled: scheduledInput.checked
        })
      }
    );
    const result = await response.json();
    if (!response.ok) return toast(result.detail || "保存集群失败", "error");
    toast(clusterId ? "集群已更新" : "集群已创建");
    resetForm();
    await load();
  });

  cancel.addEventListener("click", resetForm);
  load();
})();
