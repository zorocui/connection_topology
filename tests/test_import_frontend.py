from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_import_summary_shows_active_counts_and_progress():
    template = (ROOT / "app/templates/devices.html").read_text(encoding="utf-8")

    assert "batch.test_pending_rows" in template
    assert "batch.test_running_rows" in template
    assert "等待测试" in template
    assert "正在测试" in template
    assert "导入测试进度" in template


def test_import_polling_preserves_expanded_rows():
    template = (ROOT / "app/templates/devices.html").read_text(encoding="utf-8")

    assert "expandedImportBatchId" in template
    assert "expandedImportRowsHtml" in template
    assert "expandedImportBatchId === batch.id" in template
    assert "${preservedRows}</div>" in template
    assert "expandedImportRowsHtml = rowsHtml" in template
    assert 'running: "正在测试"' in template
