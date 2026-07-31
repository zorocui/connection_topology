from pathlib import Path

CLUSTERS_JS = Path("app/static/js/clusters.js")


def script_text() -> str:
    return CLUSTERS_JS.read_text(encoding="utf-8")


def test_cluster_page_splits_network_input_and_calls_crud_api():
    script = script_text()

    assert '/[\\n,，]+/' in script
    assert 'fetch("/api/clusters")' in script
    assert "method: clusterId ? \"PUT\" : \"POST\"" in script
    assert "internal_networks: parseNetworks()" in script
    assert "scan_interval_minutes: Number(scanIntervalInput.value)" in script
    assert "scheduled_enabled: scheduledInput.checked" in script
    assert 'method: "DELETE"' in script


def test_cluster_page_supports_scan_policy_and_device_hint():
    template = Path("app/templates/clusters.html").read_text(
        encoding="utf-8"
    )
    devices = Path("app/templates/devices.html").read_text(encoding="utf-8")
    script = script_text()

    assert 'id="cluster-scan-interval"' in template
    assert 'min="1" max="10080"' in template
    assert 'id="cluster-scheduled-enabled"' in template
    assert (
        "scanIntervalInput.value = String(cluster.scan_interval_minutes)"
        in script
    )
    assert "scheduledInput.checked = cluster.scheduled_enabled" in script
    assert "由集群统一管理" in devices


def test_cluster_page_renders_network_tags_and_safe_text():
    script = script_text()

    assert "network-tag" in script
    assert "escapeHtml(cluster.name)" in script
    assert "escapeHtml(network)" in script
    assert "删除集群后，所属设备将变为未分组" in script
