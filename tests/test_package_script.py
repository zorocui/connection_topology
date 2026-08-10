from pathlib import Path

PACKAGE_SCRIPT = Path("package-linux.ps1")


def test_linux_package_name_contains_beijing_timestamp():
    script = PACKAGE_SCRIPT.read_text(encoding="utf-8-sig")

    assert '[DateTimeOffset]::UtcNow.ToOffset([TimeSpan]::FromHours(8))' in script
    assert '"connection-topology-linux-$timestamp.tar.gz"' in script
    assert '$timestamp = $beijingNow.ToString("yyyyMMdd-HHmmss")' in script


def test_linux_package_never_bundles_wheels():
    script = PACKAGE_SCRIPT.read_text(encoding="utf-8-sig")

    assert '"wheelhouse"' not in script
    assert "$optionalPath" not in script
    assert "IncludesWheelhouse" not in script
