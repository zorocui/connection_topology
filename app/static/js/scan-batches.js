(() => {
  const batchList = document.getElementById("scan-batch-list");
  const failurePanel = document.getElementById("scan-failure-panel");
  if (!batchList || !failurePanel) return;

  const batchTypeNames = {
    all: "全部设备",
    cluster: "集群扫描",
    import: "导入首次扫描",
    retry: "失败重试"
  };
  const batchStatusNames = {
    pending: "等待",
    running: "执行中",
    completed: "已完成"
  };
  const failureTitle = document.getElementById("scan-failure-title");
  const failureSummary = document.getElementById("scan-failure-summary");
  const failureSearch = document.getElementById("scan-failure-search");
  const failurePageSize = document.getElementById("scan-failure-page-size");
  const failureBody = document.getElementById("scan-failure-body");
  const failureError = document.getElementById("scan-failure-error");
  const failurePageInput = document.getElementById("scan-failure-page");
  const failurePages = document.getElementById("scan-failure-pages");
  const failurePrev = document.getElementById("scan-failure-prev");
  const failureNext = document.getElementById("scan-failure-next");
  const retryButton = document.getElementById("retry-failed-devices");
  let batchTimer = null;
  let searchTimer = null;
  let failureController = null;

  const failureState = {
    batchId: null,
    page: 1,
    pageSize: 20,
    query: "",
    pages: 1,
    status: null,
    timer: null
  };

  const stopFailurePolling = () => {
    if (failureState.timer) clearTimeout(failureState.timer);
    failureState.timer = null;
  };

  const formatBeijingTime = value => value
    ? new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
        timeZone: "Asia/Shanghai"
      }).format(new Date(value))
    : "—";

  const batchCard = batch => {
    const completed = batch.success_tasks + batch.failed_tasks;
    const progress = batch.total_tasks
      ? Math.round(completed * 100 / batch.total_tasks)
      : 100;
    const failureAction = batch.failed_tasks > 0
      ? `<button class="scan-failure-link" type="button"
                 data-view-failures="${batch.id}">
           查看失败明细 <span>${batch.failed_tasks}</span>
         </button>`
      : "";
    return `<article class="scan-batch-card" data-batch-id="${batch.id}">
      <header>
        <div>
          <small>${batchTypeNames[batch.batch_type] || batch.batch_type}</small>
          <b>#${batch.id} · ${batchStatusNames[batch.status] || batch.status}</b>
        </div>
        <strong>${progress}%</strong>
      </header>
      <div class="scan-progress" aria-label="完成进度 ${progress}%">
        <i style="width:${progress}%"></i>
      </div>
      <div class="scan-counts">
        <span>总数 <b>${batch.total_tasks}</b></span>
        <span>等待 <b>${batch.pending_tasks}</b></span>
        <span>执行中 <b>${batch.running_tasks}</b></span>
        <span>成功 <b>${batch.success_tasks}</b></span>
        <span class="${batch.failed_tasks ? "has-failures" : ""}">
          失败 <b>${batch.failed_tasks}</b>
        </span>
      </div>
      ${failureAction}
    </article>`;
  };

  const loadScanBatches = async () => {
    if (batchTimer) clearTimeout(batchTimer);
    try {
      const response = await fetch("/api/scan-batches");
      if (!response.ok) throw new Error("读取扫描批次失败");
      const batches = await response.json();
      batchList.innerHTML = batches.length
        ? batches.map(batchCard).join("")
        : `<p class="muted">还没有批量扫描记录。</p>`;
      if (batches.some(batch => batch.status !== "completed")) {
        batchTimer = window.setTimeout(loadScanBatches, 1500);
      }
    } catch (error) {
      if (!batchList.querySelector(".scan-batch-card")) {
        batchList.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
      }
    }
  };

  const createScanBatch = async payload => {
    const response = await fetch("/api/scan-batches", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (!response.ok) {
      toast(result.detail || "创建扫描批次失败", "error");
      return;
    }
    toast(`扫描批次 #${result.id} 已进入队列`);
    await loadScanBatches();
  };

  const renderFailureRows = items => {
    failureBody.innerHTML = items.length
      ? items.map(item => `<tr>
          <td><b>${escapeHtml(item.device_name)}</b></td>
          <td><code>${escapeHtml(item.host)}</code></td>
          <td>${escapeHtml(item.cluster_name || "未分配")}</td>
          <td class="scan-failure-reason">${escapeHtml(item.error_message)}</td>
          <td>${formatBeijingTime(item.started_at)}</td>
          <td>${formatBeijingTime(item.finished_at)}</td>
        </tr>`).join("")
      : `<tr><td colspan="6" class="muted">当前条件下没有失败设备。</td></tr>`;
  };

  const loadFailurePage = async () => {
    if (!failureState.batchId || failurePanel.hidden) return;
    stopFailurePolling();
    if (failureController) failureController.abort();
    const controller = new AbortController();
    failureController = controller;
    const requestedBatchId = failureState.batchId;
    const params = new URLSearchParams({
      page: String(failureState.page),
      page_size: String(failureState.pageSize)
    });
    if (failureState.query) params.set("q", failureState.query);
    try {
      const response = await fetch(
        `/api/scan-batches/${requestedBatchId}/failures?${params.toString()}`,
        {signal: controller.signal}
      );
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.detail || "读取失败明细失败");
      }
      if (requestedBatchId !== failureState.batchId) return;
      if (payload.pages > 0 && failureState.page > payload.pages) {
        failureState.page = payload.pages;
        return loadFailurePage();
      }
      failureState.pages = Math.max(payload.pages, 1);
      failureState.status = payload.batch_status;
      failureTitle.textContent = `批次 #${payload.batch_id} · 失败设备`;
      failureSummary.textContent = payload.total
        ? `共 ${payload.total} 台失败设备，当前第 ${payload.page} 页`
        : "当前条件下没有失败设备";
      failurePageInput.value = String(failureState.page);
      failurePageInput.max = String(failureState.pages);
      failurePages.textContent = `共 ${failureState.pages} 页`;
      failurePrev.disabled = failureState.page <= 1;
      failureNext.disabled = failureState.page >= failureState.pages;
      retryButton.disabled = payload.total === 0;
      failureError.hidden = true;
      renderFailureRows(payload.items);
      if (
        payload.batch_status !== "completed"
        && !failurePanel.hidden
        && failureState.batchId === requestedBatchId
      ) {
        failureState.timer = window.setTimeout(loadFailurePage, 1500);
      }
    } catch (error) {
      if (error.name === "AbortError") return;
      failureError.textContent = `${error.message}，可点击“刷新”重试。`;
      failureError.hidden = false;
    } finally {
      if (failureController === controller) failureController = null;
    }
  };

  const openFailurePanel = batchId => {
    stopFailurePolling();
    if (failureController) failureController.abort();
    failureState.batchId = batchId;
    failureState.page = 1;
    failureState.pageSize = 20;
    failureState.query = "";
    failureState.pages = 1;
    failureSearch.value = "";
    failurePageSize.value = "20";
    failurePanel.hidden = false;
    failureBody.innerHTML = `<tr><td colspan="6" class="muted">正在读取失败明细…</td></tr>`;
    failureError.hidden = true;
    loadFailurePage();
    failurePanel.scrollIntoView({behavior: "smooth", block: "nearest"});
  };

  const closeFailurePanel = () => {
    stopFailurePolling();
    if (failureController) failureController.abort();
    failureState.batchId = null;
    failurePanel.hidden = true;
  };

  const goToFailurePage = requestedPage => {
    const numericPage = Number(requestedPage);
    const safePage = Number.isFinite(numericPage)
      ? Math.min(Math.max(Math.trunc(numericPage), 1), failureState.pages)
      : 1;
    if (safePage === failureState.page) {
      failurePageInput.value = String(safePage);
      return;
    }
    failureState.page = safePage;
    loadFailurePage();
  };

  document.addEventListener("click", event => {
    const button = event.target.closest("[data-view-failures]");
    if (button) openFailurePanel(Number(button.dataset.viewFailures));
  });

  document.getElementById("scan-all-devices").addEventListener("click", () => {
    createScanBatch({scope: "all"});
  });
  document.getElementById("scan-selected-cluster").addEventListener("click", () => {
    const clusterId = document.getElementById("scan-cluster-select").value;
    if (!clusterId) return toast("请先选择集群", "error");
    createScanBatch({scope: "cluster", cluster_id: Number(clusterId)});
  });
  document.getElementById("close-scan-failures").addEventListener(
    "click",
    closeFailurePanel
  );
  document.getElementById("refresh-scan-failures").addEventListener(
    "click",
    loadFailurePage
  );

  failureSearch.addEventListener("input", () => {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      failureState.query = failureSearch.value.trim();
      failureState.page = 1;
      loadFailurePage();
    }, 300);
  });
  failurePageSize.addEventListener("change", () => {
    failureState.pageSize = Number(failurePageSize.value);
    failureState.page = 1;
    loadFailurePage();
  });
  failurePrev.addEventListener("click", () => {
    goToFailurePage(failureState.page - 1);
  });
  failureNext.addEventListener("click", () => {
    goToFailurePage(failureState.page + 1);
  });
  failurePageInput.addEventListener("change", () => {
    goToFailurePage(failurePageInput.value);
  });
  failurePageInput.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      goToFailurePage(failurePageInput.value);
    }
  });

  retryButton.addEventListener("click", async () => {
    if (!failureState.batchId || retryButton.disabled) return;
    retryButton.disabled = true;
    const original = retryButton.textContent;
    retryButton.textContent = "正在创建重试批次…";
    try {
      const response = await fetch(
        `/api/scan-batches/${failureState.batchId}/retry-failures`,
        {method: "POST"}
      );
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.detail || "创建重试批次失败");
      }
      toast(`失败设备已加入重试批次 #${result.id}`);
      await loadScanBatches();
      await loadFailurePage();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      retryButton.disabled = false;
      retryButton.textContent = original;
    }
  });

  window.renderImportScanBatch = async (importBatchId, scanBatchId) => {
    const response = await fetch(`/api/scan-batches/${scanBatchId}`);
    if (!response.ok) return;
    const batch = await response.json();
    const target = document.getElementById(`import-scan-${importBatchId}`);
    if (!target) return;
    target.innerHTML = `<h3>首次完整扫描</h3>${batchCard(batch)}`;
    if (batch.status !== "completed") {
      window.setTimeout(
        () => window.renderImportScanBatch(importBatchId, scanBatchId),
        1500
      );
    }
  };

  loadScanBatches();
})();
