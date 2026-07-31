from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_devices_page_has_failure_detail_panel():
    template = (ROOT / "app/templates/devices.html").read_text(encoding="utf-8")

    assert 'id="scan-failure-panel"' in template
    assert 'id="scan-failure-search"' in template
    assert 'id="scan-failure-page-size"' in template
    assert 'id="retry-failed-devices"' in template
    assert "path='js/scan-batches.js'" in template


def test_batch_script_supports_failures_polling_and_retry():
    script = (ROOT / "app/static/js/scan-batches.js").read_text(
        encoding="utf-8"
    )

    assert "/failures?${params.toString()}" in script
    assert "/retry-failures" in script
    assert "failureState.timer" in script
    assert "clearTimeout(failureState.timer)" in script
    assert 'timeZone: "Asia/Shanghai"' in script
    assert "escapeHtml(item.error_message)" in script
    assert "retryButton.disabled = true" in script
    assert 'retry: "失败重试"' in script


def test_batch_script_stops_polling_and_clamps_invalid_page():
    script = (ROOT / "app/static/js/scan-batches.js").read_text(
        encoding="utf-8"
    )

    assert "stopFailurePolling()" in script
    assert "failureState.page > payload.pages" in script
    assert 'payload.batch_status !== "completed"' in script
    assert "failureState.batchId = null" in script
