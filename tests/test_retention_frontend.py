from pathlib import Path


def test_cluster_form_supports_nullable_retention():
    template = Path("app/templates/clusters.html").read_text(
        encoding="utf-8"
    )
    script = Path("app/static/js/clusters.js").read_text(encoding="utf-8")

    assert 'id="cluster-retention-days"' in template
    assert 'min="1" max="3650"' in template
    assert "const retentionValue = retentionInput.value.trim();" in script
    assert "history_retention_days: retentionValue" in script
    assert 'retentionInput.value = cluster.history_retention_days ?? "";' in script


def test_device_form_and_existing_rows_support_nullable_retention():
    template = Path("app/templates/devices.html").read_text(encoding="utf-8")

    assert 'name="history_retention_days"' in template
    assert 'data-save-device-retention="{{ device.id }}"' in template
    assert "history_retention_days: retentionValue" in template
    assert "生效 {{ effective_days }} 天（{{ retention_source }}）" in template
