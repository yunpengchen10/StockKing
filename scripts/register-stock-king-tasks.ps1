[CmdletBinding()]
param(
    [string]$InstallDir,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$taskPrefix = 'Stock King Adaptive'
$legacyTaskNames = @(
    'StockKing-YaoScout-Prefetch',
    'StockKing-YaoScout-Preopen',
    'StockKing-YaoScout-Intraday-AM',
    'StockKing-YaoScout-Intraday-PM',
    'StockKing-YaoScout-Postclose'
)
$slots = @(
    # Keep existing task identities. Initial lead is 10 minutes, not a proven
    # latency SLA. Engine records late_seconds and refuses expired publication.
    @{ Name = '0922 Scan'; Time = '09:10'; Slot = '0920' },
    @{ Name = '0940 Scan'; Time = '09:30'; Slot = '0940' },
    @{ Name = '0955 Scan'; Time = '09:45'; Slot = '0955' },
    @{ Name = '1030 Scan'; Time = '10:20'; Slot = '1030' },
    @{ Name = '1455 NLS'; Time = '14:45'; Slot = '1455' },
    @{ Name = '1530 Review'; Time = '15:30'; Slot = 'review' },
    @{ Name = '1545 Weekly Gate'; Time = '15:45'; Slot = 'weekly' }
)

$backupDir = Join-Path $env:APPDATA ('Stock King\config\task-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$existingTasks = @(Get-ScheduledTask -TaskName "$taskPrefix*" -ErrorAction SilentlyContinue)
if ($existingTasks.Count -gt 0) {
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    foreach ($existing in $existingTasks) {
        Export-ScheduledTask -TaskName $existing.TaskName | Set-Content -LiteralPath (Join-Path $backupDir ($existing.TaskName + '.xml')) -Encoding Unicode
    }
}
# Superseded auction-confirmation time: no extra delivery slot.
Unregister-ScheduledTask -TaskName "$taskPrefix 0925 Confirm" -Confirm:$false -ErrorAction SilentlyContinue

# v2.3.0 and earlier could leave these standalone Python tasks behind. They
# duplicate the adaptive schedule and launch python.exe directly, which creates
# a visible console on Windows despite Task Scheduler's Hidden setting.
foreach ($legacyTaskName in $legacyTaskNames) {
    Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false -ErrorAction SilentlyContinue
}

foreach ($slot in $slots) {
    $taskName = "$taskPrefix $($slot.Name)"
    if ($Uninstall) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        continue
    }

    if ([string]::IsNullOrWhiteSpace($InstallDir)) {
        throw 'InstallDir is required when registering Stock King tasks.'
    }
    $executable = Join-Path $InstallDir 'Stock King.exe'
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "Stock King executable was not found: $executable"
    }

    $action = New-ScheduledTaskAction -Execute $executable -Argument "--stock-king-task=$($slot.Slot)" -WorkingDirectory $InstallDir
    $scheduleDays = if ($slot.Slot -eq 'weekly') { @('Friday') } else { @('Monday','Tuesday','Wednesday','Thursday','Friday') }
    $trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $scheduleDays -At $slot.Time
    $shanghaiDate = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd')
    $trigger.StartBoundary = "${shanghaiDate}T$($slot.Time):00+08:00"
    $settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 2) -ExecutionTimeLimit (New-TimeSpan -Minutes 35)
    $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Stock King V1.1 local engine; Shanghai 09:20 watchlist, 09:40/09:55/10:30/14:55 fresh scans, 15:30 delayed reviews, Friday 15:45 gated learning. No LLM in picks.' -Force | Out-Null
}
