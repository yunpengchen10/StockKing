[CmdletBinding()]
param(
    [string]$InstallDir = 'D:\Stock King',
    [Parameter(Mandatory = $true)][string]$BuiltExe,
    [Parameter(Mandatory = $true)][string]$BuiltEngineDir,
    [Parameter(Mandatory = $true)][string]$PythonBin,
    [switch]$BackupConfig
)

# Local, reversible installer. It never deletes an installation or a database.
# The caller must finish the Python, Go and frontend acceptance tests first.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'Run this updater on Windows.' }

function Resolve-ExistingPath([string]$Value, [string]$Kind) {
    $resolved = (Resolve-Path -LiteralPath $Value -ErrorAction Stop).ProviderPath
    if (-not (Test-Path -LiteralPath $resolved -PathType $Kind)) { throw "Expected $Kind at $resolved" }
    return [IO.Path]::GetFullPath($resolved).TrimEnd('\')
}

function Assert-NoReparseAncestors([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Installation paths must not traverse a junction or symbolic link: $cursor"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}

$installRoot = Resolve-ExistingPath $InstallDir 'Container'
if ($installRoot -eq [IO.Path]::GetPathRoot($installRoot).TrimEnd('\')) { throw 'A drive root cannot be an installation directory.' }
Assert-NoReparseAncestors $installRoot

function Assert-InstallChild([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($installRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the explicitly selected installation: $full"
    }
    Assert-NoReparseAncestors $full
    return $full
}

function Move-InstallItem([string]$Source, [string]$Destination) {
    # Recheck both resolved targets immediately before every move, including rollback.
    $checkedSource = Assert-InstallChild $Source
    $checkedDestination = Assert-InstallChild $Destination
    if (Test-Path -LiteralPath $checkedDestination) { throw "Move destination already exists: $checkedDestination" }
    Move-Item -LiteralPath $checkedSource -Destination $checkedDestination -ErrorAction Stop
}

function Get-TreeFiles([string]$Directory) {
    $root = Resolve-ExistingPath $Directory 'Container'
    $items = @(Get-ChildItem -LiteralPath $root -Recurse -Force)
    foreach ($item in $items) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Payload contains a link: $($item.FullName)" }
    }
    return @($items | Where-Object { -not $_.PSIsContainer } | ForEach-Object {
        [pscustomobject]@{ relativePath = $_.FullName.Substring($root.Length + 1); bytes = $_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
    } | Sort-Object relativePath)
}

function Get-InstalledProcesses {
    return @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and ($_.ExecutablePath.Equals($installedExe, [StringComparison]::OrdinalIgnoreCase) -or
            $_.ExecutablePath.Equals($installedEngineExe, [StringComparison]::OrdinalIgnoreCase))
    })
}

function Stop-InstalledProcesses {
    foreach ($process in @(Get-InstalledProcesses)) {
        $native = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
        if ($native) { [void]$native.CloseMainWindow() }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(6)
    while (@(Get-InstalledProcesses).Count -gt 0 -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
    foreach ($process in @(Get-InstalledProcesses)) {
        # Re-read the path to avoid stopping an unrelated process after PID reuse.
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ProcessId)"
        if ($current -and $current.ExecutablePath -and ($current.ExecutablePath.Equals($installedExe, [StringComparison]::OrdinalIgnoreCase) -or
            $current.ExecutablePath.Equals($installedEngineExe, [StringComparison]::OrdinalIgnoreCase))) {
            Stop-Process -Id $current.ProcessId -Force -ErrorAction Stop
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while (@(Get-InstalledProcesses).Count -gt 0 -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
    if (@(Get-InstalledProcesses).Count -gt 0) { throw 'An installed Stock King process is still running.' }
}

$builtExePath = Resolve-ExistingPath $BuiltExe 'Leaf'
$builtEnginePath = Resolve-ExistingPath $BuiltEngineDir 'Container'
$pythonCommand = Get-Command $PythonBin -CommandType Application -ErrorAction Stop | Select-Object -First 1
$pythonPath = $pythonCommand.Source
$installedExe = Assert-InstallChild (Join-Path $installRoot 'Stock King.exe')
$engineParent = Assert-InstallChild (Join-Path $installRoot 'Resources\daily-engine')
$installedEngine = Assert-InstallChild (Join-Path $engineParent 'stock_analysis')
$installedEngineExe = Assert-InstallChild (Join-Path $installedEngine 'stock_analysis.exe')
foreach ($file in @($installedExe, $installedEngineExe, (Join-Path $builtEnginePath 'stock_analysis.exe'))) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required executable is missing: $file" }
}
if ($builtExePath.Equals($installedExe, [StringComparison]::OrdinalIgnoreCase) -or $builtEnginePath.Equals($installedEngine, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Build inputs must be separate from the installed programs.'
}
$registerScript = Join-Path $PSScriptRoot 'register-stock-king-tasks.ps1'
if (-not (Test-Path -LiteralPath $registerScript -PathType Leaf)) { throw 'The task registration script is missing.' }
foreach ($command in @('Get-ScheduledTask','Export-ScheduledTask','Register-ScheduledTask','Disable-ScheduledTask','Stop-ScheduledTask')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "Required ScheduledTasks command is unavailable: $command" }
}
if ([string]::IsNullOrWhiteSpace($env:APPDATA)) { throw 'APPDATA is unavailable; refusing to guess the live database directory.' }
$roamingStockKing = Join-Path $env:APPDATA 'Stock King'
$dataRoot = Resolve-ExistingPath (Join-Path $roamingStockKing 'data') 'Container'
$configRoot = Join-Path $roamingStockKing 'config'
$tag = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$backupRoot = Assert-InstallChild (Join-Path $installRoot "backups\update-$tag")
$stagedEngine = Assert-InstallChild (Join-Path $engineParent "stock_analysis.staging-$tag")
$stagedExe = Assert-InstallChild (Join-Path $installRoot "Stock King.staging-$tag.exe")
$oldEngineBackup = Assert-InstallChild (Join-Path $engineParent "stock_analysis.backup-$tag")
$oldExeBackup = Assert-InstallChild (Join-Path $backupRoot 'Stock King.exe')
$databaseBackup = Assert-InstallChild (Join-Path $backupRoot 'databases')
$taskBackup = Assert-InstallChild (Join-Path $backupRoot 'tasks')
$manifestPath = Assert-InstallChild (Join-Path $backupRoot 'manifest.json')
$expectedTasks = @(
    @{ Name = 'Stock King Adaptive 0922 Scan'; Slot = '0920' },
    @{ Name = 'Stock King Adaptive 0940 Scan'; Slot = '0940' },
    @{ Name = 'Stock King Adaptive 0955 Scan'; Slot = '0955' },
    @{ Name = 'Stock King Adaptive 1030 Scan'; Slot = '1030' },
    @{ Name = 'Stock King Adaptive 1455 NLS'; Slot = '1455' },
    @{ Name = 'Stock King Adaptive 1530 Review'; Slot = 'review' },
    @{ Name = 'Stock King Adaptive 1545 Weekly Gate'; Slot = 'weekly' }
)
$existingTasks = @(Get-ScheduledTask -TaskName 'Stock King Adaptive*' -ErrorAction SilentlyContinue)
$oldTaskRecords = @()
$manifest = [ordered]@{
    schemaVersion = 1; startedAtUtc = [DateTime]::UtcNow.ToString('o'); phase = 'preflight'; installDir = $installRoot
    sourceExe = $builtExePath; sourceEngineDir = $builtEnginePath; backupRoot = $backupRoot
    previousExe = $oldExeBackup; previousEngineDir = $oldEngineBackup; databaseBackups = @(); tasks = @()
    previousExeSha256 = (Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash
    previousEngineExeSha256 = (Get-FileHash -LiteralPath $installedEngineExe -Algorithm SHA256).Hash
    newExeSha256 = (Get-FileHash -LiteralPath $builtExePath -Algorithm SHA256).Hash
    newEngineExeSha256 = (Get-FileHash -LiteralPath (Join-Path $builtEnginePath 'stock_analysis.exe') -Algorithm SHA256).Hash
    engineFiles = @(); configBackup = $null
}
function Save-Manifest {
    $manifest['updatedAtUtc'] = [DateTime]::UtcNow.ToString('o')
    $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
}

$oldExeMoved = $false; $oldEngineMoved = $false; $newExeInstalled = $false; $newEngineInstalled = $false
$tasksTouched = $false; $newAppStarted = $false
New-Item -ItemType Directory -Path $backupRoot, $databaseBackup, $taskBackup -Force | Out-Null
try {
    # Fully stage and verify the new sidecar before touching a running installation.
    $sourceFiles = @(Get-TreeFiles $builtEnginePath)
    if ($sourceFiles.Count -eq 0) { throw 'The new sidecar directory is empty.' }
    Copy-Item -LiteralPath $builtEnginePath -Destination $stagedEngine -Recurse -Force
    Copy-Item -LiteralPath $builtExePath -Destination $stagedExe
    $stagedFiles = @(Get-TreeFiles $stagedEngine)
    $sourceDigest = $sourceFiles | ConvertTo-Json -Compress -Depth 4
    $stagedDigest = $stagedFiles | ConvertTo-Json -Compress -Depth 4
    if ($sourceDigest -ne $stagedDigest) { throw 'The staged sidecar differs from the build output.' }
    if ((Get-FileHash -LiteralPath $stagedExe -Algorithm SHA256).Hash -ne $manifest.newExeSha256) { throw 'The staged executable hash differs from the build output.' }
    $manifest.engineFiles = $stagedFiles
    $manifest.phase = 'staged'
    Save-Manifest

    # Disable launches during the short replacement window and save exact task XML.
    foreach ($task in $existingTasks) {
        $xmlPath = Assert-InstallChild (Join-Path $taskBackup ($task.TaskName + '.xml'))
        Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath | Set-Content -LiteralPath $xmlPath -Encoding Unicode
        $oldTaskRecords += [pscustomobject]@{ name = $task.TaskName; path = $task.TaskPath; enabled = [bool]$task.Settings.Enabled; xmlPath = $xmlPath }
        $tasksTouched = $true
        Disable-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath | Out-Null
        $currentTask = Get-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop
        if ($currentTask.State -eq 'Running') {
            Stop-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath
        }
    }
    Stop-InstalledProcesses

    # SQLite backup includes committed WAL content, unlike copying only the .db file.
    $backupCode = @'
import contextlib, hashlib, json, pathlib, sqlite3, sys, time
source_root, backup_root = map(pathlib.Path, sys.argv[1:3])
source_root = source_root.resolve(strict=True)
records = []
for source in sorted(source_root.rglob('*')):
    if not source.is_file() or source.suffix.lower() not in {'.db', '.sqlite', '.sqlite3'}:
        continue
    resolved = source.resolve(strict=True)
    resolved.relative_to(source_root)
    if source.is_symlink():
        raise RuntimeError('Database symbolic links are not supported: ' + str(source))
    target = backup_root / source.relative_to(source_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 180
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError('SQLite consistent backup timed out: ' + str(source))
    with contextlib.closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=30)) as src:
        with contextlib.closing(sqlite3.connect(target, timeout=30)) as dest:
            src.backup(dest, pages=1024, progress=progress, sleep=0.05)
            if dest.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('SQLite backup integrity check failed: ' + str(source))
    digest = hashlib.sha256()
    with target.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    records.append({'source':str(source), 'backup':str(target), 'sha256':digest.hexdigest(), 'bytes':target.stat().st_size})
print(json.dumps(records, ensure_ascii=True))
'@
    $databaseJson = $backupCode | & $pythonPath -I - $dataRoot $databaseBackup
    if ($LASTEXITCODE -ne 0) { throw 'The consistent database backup failed; installed programs have not been replaced.' }
    $manifest.databaseBackups = @($databaseJson | ConvertFrom-Json)
    if ($BackupConfig -and (Test-Path -LiteralPath $configRoot -PathType Container)) {
        $configBackupPath = Assert-InstallChild (Join-Path $backupRoot 'config')
        Copy-Item -LiteralPath $configRoot -Destination $configBackupPath -Recurse -Force
        $manifest.configBackup = $configBackupPath
    }
    $manifest.phase = 'backed_up'
    Save-Manifest

    Move-InstallItem $installedEngine $oldEngineBackup
    $oldEngineMoved = $true
    Move-InstallItem $stagedEngine $installedEngine
    $newEngineInstalled = $true
    Move-InstallItem $installedExe $oldExeBackup
    $oldExeMoved = $true
    Move-InstallItem $stagedExe $installedExe
    $newExeInstalled = $true
    if ((Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash -ne $manifest.newExeSha256) { throw 'Installed executable verification failed.' }
    $manifest.phase = 'installed'
    Save-Manifest

    $tasksTouched = $true
    & $registerScript -InstallDir $installRoot
    $taskChecks = @()
    foreach ($expected in $expectedTasks) {
        $task = Get-ScheduledTask -TaskName $expected.Name -ErrorAction Stop
        $actions = @($task.Actions)
        if ($actions.Count -ne 1) { throw "Expected exactly one action: $($expected.Name)" }
        $action = $actions[0]
        $actionExe = [IO.Path]::GetFullPath($action.Execute.Trim('"'))
        if (-not $actionExe.Equals($installedExe, [StringComparison]::OrdinalIgnoreCase) -or
            $action.Arguments -ne "--stock-king-task=$($expected.Slot)" -or
            -not ([IO.Path]::GetFullPath($action.WorkingDirectory)).Equals($installRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Scheduled task does not point to the new installation: $($expected.Name)"
        }
        $previous = @($oldTaskRecords | Where-Object { $_.name -eq $expected.Name })
        if ($previous.Count -eq 1 -and -not $previous[0].enabled) { Disable-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath | Out-Null }
        $taskChecks += [pscustomobject]@{ name = $expected.Name; executable = $actionExe; arguments = $action.Arguments; workingDirectory = $action.WorkingDirectory; verifiedAtUtc = [DateTime]::UtcNow.ToString('o') }
    }
    $manifest.tasks = $taskChecks
    $manifest.phase = 'tasks_verified'
    Save-Manifest

    # After launching, database migrations may occur. Keep backups and never
    # automatically restore old databases over newly written user state.
    $started = Start-Process -FilePath $installedExe -WorkingDirectory $installRoot -WindowStyle Hidden -PassThru
    $newAppStarted = $true
    Start-Sleep -Seconds 3
    $started.Refresh()
    if ($started.HasExited) { throw 'The new desktop process exited during startup; see application logs and the retained backups.' }
    $manifest.phase = 'completed'
    $manifest['completedAtUtc'] = [DateTime]::UtcNow.ToString('o')
    Save-Manifest
    Write-Output "Updated: $installedExe"
    Write-Output "Backup and verification manifest: $manifestPath"
    Write-Output "Verified scheduled tasks: $($taskChecks.Count)"
} catch {
    $failure = $_
    $manifest.phase = 'failed'
    $manifest['failedAtUtc'] = [DateTime]::UtcNow.ToString('o')
    $manifest['automaticProgramRollback'] = 'not_attempted'
    if (-not $newAppStarted) {
        try {
            if ($newExeInstalled) { Move-InstallItem $installedExe (Join-Path $installRoot "Stock King.failed-$tag.exe") }
            if ($oldExeMoved) { Move-InstallItem $oldExeBackup $installedExe }
            if ($newEngineInstalled) { Move-InstallItem $installedEngine (Join-Path $engineParent "stock_analysis.failed-$tag") }
            if ($oldEngineMoved) { Move-InstallItem $oldEngineBackup $installedEngine }
            if ($tasksTouched) {
                foreach ($record in $oldTaskRecords) {
                    Register-ScheduledTask -TaskName $record.name -TaskPath $record.path -Xml (Get-Content -LiteralPath $record.xmlPath -Raw) -Force | Out-Null
                }
                # Newly registered tasks have no previous XML; keep them disabled on failure.
                foreach ($expected in $expectedTasks) {
                    if (-not @($oldTaskRecords | Where-Object { $_.name -eq $expected.Name }).Count) {
                        $newTask = Get-ScheduledTask -TaskName $expected.Name -ErrorAction SilentlyContinue
                        if ($newTask) { Disable-ScheduledTask -TaskName $newTask.TaskName -TaskPath $newTask.TaskPath | Out-Null }
                    }
                }
            }
            $manifest.automaticProgramRollback = 'completed; databases unchanged'
        } catch { $manifest.automaticProgramRollback = 'incomplete; retained files and task XML require inspection' }
    }
    Save-Manifest
    Write-Warning "Update failed. Retained backup and state manifest: $manifestPath"
    throw $failure
}
