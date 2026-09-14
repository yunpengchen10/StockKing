from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_TASK_NAMES = (
    "StockKing-YaoScout-Prefetch",
    "StockKing-YaoScout-Preopen",
    "StockKing-YaoScout-Intraday-AM",
    "StockKing-YaoScout-Intraday-PM",
    "StockKing-YaoScout-Postclose",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_adaptive_task_installer_removes_all_legacy_python_tasks() -> None:
    script = _read(REPO_ROOT / "scripts" / "register-stock-king-tasks.ps1")

    for task_name in LEGACY_TASK_NAMES:
        assert task_name in script
    assert "Unregister-ScheduledTask -TaskName $legacyTaskName" in script
    assert "New-ScheduledTaskAction -Execute $executable" in script
    assert "New-ScheduledTaskAction -Execute $ScheduledPython" not in script


def test_legacy_task_script_is_cleanup_only() -> None:
    script = _read(
        REPO_ROOT / "daily-engine" / "scripts" / "install-yao-scout-tasks.ps1"
    )

    for task_name in LEGACY_TASK_NAMES:
        assert task_name in script
    assert "Unregister-ScheduledTask" in script
    assert "New-ScheduledTaskAction" not in script
    assert "Register-ScheduledTask" not in script
    assert "python.exe" not in script.lower()
    assert "pythonw.exe" not in script.lower()


def test_nsis_installer_runs_the_migrating_task_registrar() -> None:
    installer = _read(
        REPO_ROOT
        / "desktop"
        / "build"
        / "windows"
        / "installer"
        / "project.nsi"
    )

    assert "register-stock-king-tasks.ps1" in installer
    assert '-InstallDir "$INSTDIR"' in installer
