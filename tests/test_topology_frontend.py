from pathlib import Path

TOPOLOGY_JS = Path("app/static/js/topology.js")
TOPOLOGY_TEMPLATE = Path("app/templates/topology.html")


def script_text() -> str:
    return TOPOLOGY_JS.read_text(encoding="utf-8")


def test_cluster_filter_requires_explicit_target_selection():
    template = TOPOLOGY_TEMPLATE.read_text(encoding="utf-8")

    assert "目标集群" in template
    assert '<option value="">选择一个集群</option>' in template
    assert "全部集群和设备" not in template
    assert "topology.js') }}?v=20260807-slim-graph" in template


def test_cluster_mode_waits_for_explicit_selection_before_loading():
    script = script_text()

    assert "const showClusterWaiting" in script
    assert "if (!clusterSelect.value) return showClusterWaiting();" in script
    assert 'clusterSelect.addEventListener("change", load)' in script
    assert 'if (mode === "cluster") {' in script
    assert "showClusterWaiting();" in script
    assert (
        'params.set("cluster_id", clusterSelect.value.replace("cluster-", ""))'
        in script
    )


def test_cluster_default_drawer_uses_summary_instead_of_empty_connection_table():
    script = script_text()

    assert "const renderClusterOverview" in script
    assert "成员设备" in script
    assert "外部连接组" in script
    assert "点击拓扑中的节点或连线查看详细信息" in script
    assert 'renderDrawer("集群拓扑"' not in script


def test_cluster_subgraph_keeps_only_target_and_direct_peers():
    script = script_text()

    assert "const selectedClusterId = clusterSelect.value" in script
    assert "edge.data.source === selectedClusterId" in script
    assert "edge.data.target === selectedClusterId" in script
    expected = "node.data.id === selectedClusterId || peerIds.has(node.data.id)"
    assert expected in script


def test_topology_cancels_stale_requests_and_times_out_history():
    script = script_text()

    assert "new AbortController()" in script
    assert "activeTopologyController?.abort()" in script
    assert "window.setTimeout" in script
    assert "90000" in script
    assert "历史拓扑计算超时，请稍后重试或缩短时间范围。" in script


def test_topology_uses_compact_multiring_layout():
    script = script_text()

    assert "assignLayoutLevels" in script
    assert "8 + ringIndex * 4" in script
    assert 'node.data("layoutLevel")' in script
    assert "minNodeSpacing: 44" in script
    assert "padding: 36" in script


def test_topology_uses_readable_visual_sizes():
    script = script_text()

    assert '"width": 52' in script
    assert '"height": 52' in script
    assert '"width": 82' in script
    assert '"height": 56' in script
    assert '"width": 94' in script
    assert '"height": 64' in script
    assert '"font-size": 12' in script
    assert '"font-size": 11' in script
    assert '"width": "mapData(count, 1, 20, 2, 8)"' in script


def test_topology_preserves_readable_initial_zoom():
    script = script_text()

    assert "maxZoom: 2.5" in script
    assert "if (graph.zoom() < 0.7)" in script
    assert "graph.zoom(0.7)" in script


def test_topology_supports_focus_and_view_controls():
    script = script_text()

    assert "clearFocus" in script
    assert "focusNode" in script
    assert "focusEdge" in script
    assert '"is-dimmed"' in script
    assert '"is-focused"' in script
    assert '"is-neighbor"' in script
    assert 'getElementById("fit-topology-button")' in script
    assert 'getElementById("reset-topology-button")' in script


def test_topology_requests_selected_time_window():
    script = script_text()

    assert 'getElementById("topology-window")' in script
    assert "encodeURIComponent(windowSelect.value)" in script
    assert "?window=${selectedWindow()}" in script
    assert 'windowSelect.addEventListener("change", load)' in script


def test_topology_recomputes_edge_status_after_filters():
    script = script_text()

    assert "const currentCount = connections.filter" in script
    assert "is_current: currentCount > 0 ? 1 : 0" in script
    assert "edge[is_current = 0]" in script
    assert '"line-color": "#667176"' in script


def test_cluster_mode_filters_server_side_and_loads_details_on_demand():
    script = script_text()

    assert 'params.set("protocol", protocolSelect.value)' in script
    assert 'params.set("state", stateSelect.value)' in script
    assert 'params.set("process", process)' in script
    assert 'topologyFilterParams().forEach' in script
    assert "/api/topology/edge-connections" in script
    assert 'params.set("source", sourceId)' in script
    assert 'params.set("target", targetId)' in script
    assert "loadEdgeConnections(peer, data.source, data.target)" in script
    assert "loadNodeConnections" in script
    assert "window.setTimeout(load, 350)" in script


def test_topology_details_show_history_metadata_in_chinese():
    script = script_text()

    assert 'row.is_current ? "当前" : "已断开"' in script
    assert "首次发现" in script
    assert "最后发现" in script
    assert "出现次数" in script
    assert "本地端口" in script


def test_topology_formats_server_timestamps_as_beijing_time():
    script = script_text()

    assert 'const utcValue = /(?:Z|[+-]\\d{2}:\\d{2})$/.test(value)' in script
    assert 'timeZone: "Asia/Shanghai"' in script
