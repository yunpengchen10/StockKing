from pathlib import Path
import json
import shutil
import subprocess

import pytest


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


def _powershell(script: str):
    executable = shutil.which('powershell.exe')
    if not executable:
        pytest.skip('Windows PowerShell is required for isolated task-cmdlet doubles')
    result = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.mark.parametrize('disabled', [False, True])
def test_task_registrar_passes_disabled_setting_at_registration(tmp_path, disabled):
    # Execute the real registrar against cmdlet doubles: no scheduled tasks or
    # application processes are created, changed or run by this test.
    (tmp_path / 'Stock King.exe').touch()
    registrar = REPO_ROOT / 'scripts' / 'register-stock-king-tasks.ps1'
    script = r'''
$ErrorActionPreference = 'Stop'
$recorded = [Collections.Generic.List[object]]::new()
function Get-ScheduledTask { @() }
function Unregister-ScheduledTask { }
function New-ScheduledTaskAction { [pscustomobject]@{} }
function New-ScheduledTaskTrigger { [pscustomobject]@{StartBoundary=''} }
function New-ScheduledTaskSettingsSet { [pscustomobject]@{Enabled=$true} }
function New-ScheduledTaskPrincipal { [pscustomobject]@{} }
function Register-ScheduledTask {
    param($TaskName,$Action,$Trigger,$Settings,$Principal,$Description,[switch]$Force)
    $recorded.Add([pscustomobject]@{name=$TaskName;enabled=$Settings.Enabled})
}
'''
    script += '\n$env:APPDATA = ' + _ps_string(tmp_path) + '\n'
    script += '& ' + _ps_string(registrar) + ' -InstallDir ' + _ps_string(tmp_path)
    script += ' -Disabled' if disabled else ''
    script += '\nConvertTo-Json -InputObject @($recorded.ToArray()) -Compress\n'
    result = _powershell(script)
    assert len(result) == 7
    assert all(row['enabled'] is (not disabled) for row in result)


@pytest.mark.parametrize('keep_disabled,invalid_action', [(True, False), (False, False), (False, True)])
def test_updater_verifies_all_disabled_tasks_before_restoring_only_prior_enabled(keep_disabled, invalid_action):
    source = _read(REPO_ROOT / 'scripts' / 'update-windows-local.ps1')
    start = source.index('    $tasksTouched = $true\n    & $registerScript')
    end = source.index('    $manifest.tasks = $taskChecks', start)
    # Run the updater's actual registration/verification block with isolated
    # task records and in-memory cmdlet doubles. Other installer code is unused.
    script = r'''
$ErrorActionPreference = 'Stop'
$installRoot = 'C:\stock-king-task-test'
$installedExe = Join-Path $installRoot 'Stock King.exe'
$expectedTasks = @(@{Name='was-enabled';Slot='1030'},@{Name='was-disabled';Slot='1455'},@{Name='new-task';Slot='review'})
$oldTaskRecords = @(@{name='was-enabled';enabled=$true},@{name='was-disabled';enabled=$false})
$records = @{}
$checks = [Collections.Generic.List[string]]::new()
$enabled = [Collections.Generic.List[string]]::new()
$registerScript = {
    param($InstallDir,[switch]$Disabled)
    if (-not $Disabled) { throw 'Non-atomic enabled registration' }
    foreach ($item in $expectedTasks) {
        $slot = if ($invalidAction -and $item.Name -eq 'new-task') {'wrong-slot'} else {$item.Slot}
        $records[$item.Name] = [pscustomobject]@{
            TaskName=$item.Name;TaskPath='\';Settings=[pscustomobject]@{Enabled=$false}
            Actions=@([pscustomobject]@{Execute=$installedExe;Arguments="--stock-king-task=$slot";WorkingDirectory=$InstallDir})
        }
    }
}
function Get-ScheduledTask { param($TaskName) $checks.Add($TaskName); return $records[$TaskName] }
function Enable-ScheduledTask {
    param($TaskName,$TaskPath)
    if ($checks.Count -ne 3) { throw 'Enabled before all tasks were verified' }
    $enabled.Add($TaskName)
}
'''
    script += '\n$KeepTasksDisabled = $' + str(keep_disabled).lower()
    script += '\n$invalidAction = $' + str(invalid_action).lower()
    script += '\n$failed = $false\ntry {\n' + source[start:end]
    script += '\n} catch { $failed = $true }\n'
    script += 'ConvertTo-Json -InputObject @{failed=$failed;enabled=@($enabled.ToArray());tasks=@($taskChecks)} -Depth 5 -Compress'
    result = _powershell(script)
    assert result['failed'] is invalid_action
    assert result['enabled'] == ([] if keep_disabled or invalid_action else ['was-enabled'])
    if not invalid_action:
        assert [row['previousEnabled'] for row in result['tasks']] == [True, False, False]


def test_maintenance_rollback_registers_disabled_xml_before_any_task_can_start():
    script = _read(REPO_ROOT / 'scripts' / 'update-windows-local.ps1')
    rollback = script[script.index('[xml]$restoreXml ='):]
    assert rollback.index("$enabledNode.InnerText = 'false'") < rollback.index('Register-ScheduledTask')
    assert '-Xml $restoreXml.OuterXml' in rollback
