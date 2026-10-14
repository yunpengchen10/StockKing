[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Executable,
    [string]$WorkingDirectory = 'D:\Stock King',
    [string]$OutputDirectory,
    [Alias('MinimizedSeconds')][ValidateRange(30, 300)][int]$IdleSeconds = 30,
    [ValidateRange(30, 300)][int]$StartupTimeoutSeconds = 120
)

# Deliberately injects failures only into descendants of this script's isolated
# desktop. Keep the profile and receipt for inspection; never alter user tasks.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$appExe = (Resolve-Path -LiteralPath $Executable).Path
$appWorkingDirectory = (Resolve-Path -LiteralPath $WorkingDirectory).Path
if (-not (Test-Path -LiteralPath (Join-Path $appWorkingDirectory 'Resources\daily-engine\stock_analysis\stock_analysis.exe') -PathType Leaf)) {
    throw 'WorkingDirectory must contain the installed Resources\daily-engine\stock_analysis engine.'
}

if (-not ('StockKingBackgroundQA.Windows' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
namespace StockKingBackgroundQA {
    public static class Windows {
        private delegate bool EnumWindow(IntPtr window, IntPtr data);
        [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindow callback, IntPtr data);
        [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
        [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder name, int capacity);
        [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr window);
        [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr window);
        [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr window, int command);
        [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr window, uint message, IntPtr wparam, IntPtr lparam);
        public static IntPtr FindDesktop(uint pid) {
            IntPtr found = IntPtr.Zero;
            EnumWindows(delegate(IntPtr window, IntPtr data) {
                uint owner;
                GetWindowThreadProcessId(window, out owner);
                if (owner != pid) return true;
                var name = new StringBuilder(128);
                GetClassName(window, name, name.Capacity);
                if (name.ToString() != "wailsWindow") return true;
                found = window;
                return false;
            }, IntPtr.Zero);
            return found;
        }
    }
}
'@
}

function Wait-Check([string]$Description, [scriptblock]$Condition, [int]$Seconds = 30) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $value = & $Condition
        if ($value) { return $value }
        Start-Sleep -Milliseconds 300
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Timed out: $Description"
}

function Get-ProcessRecord([uint32]$ProcessId) {
    $record = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $record) { return $null }
    $process = $null
    try {
        $process = [Diagnostics.Process]::GetProcessById([int]$ProcessId)
        $path = $process.MainModule.FileName
        $created = $process.StartTime.ToUniversalTime().Ticks
        # CIM timestamps have microsecond precision; store the handle's full
        # timestamp so later identity checks don't depend on CIM rounding.
        if ($path -ne $record.ExecutablePath -or [Math]::Abs($created - ([DateTime]$record.CreationDate).ToUniversalTime().Ticks) -ge 10000) {
            throw 'Process changed while recording its identity.'
        }
        [pscustomobject]@{
            Id = [uint32]$record.ProcessId
            ParentId = [uint32]$record.ParentProcessId
            Path = [string]$path
            Created = $created
        }
    } catch [ArgumentException] { return $null
    } catch [InvalidOperationException] { return $null
    } finally { if ($null -ne $process) { $process.Dispose() } }
}

function Test-SameProcess($Identity) {
    if ($null -eq $Identity) { return $false }
    $current = Get-ProcessRecord $Identity.Id
    return $null -ne $current -and $current.Path -eq $Identity.Path -and $current.Created -eq $Identity.Created
}

function Stop-ExactProcess($Identity) {
    if (-not (Test-SameProcess $Identity)) { return }
    if (@($script:unrelatedIdentities | Where-Object { $_.Identity.Id -eq $Identity.Id -and $_.Identity.Created -eq $Identity.Created }).Count -gt 0) {
        throw 'Target belongs to an unrelated desktop or scheduled task; refusing termination.'
    }
    # GetProcessById opens the exact process; recheck its creation time/path
    # before using that handle so a recycled PID cannot target another app.
    $process = [Diagnostics.Process]::GetProcessById([int]$Identity.Id)
    try {
        if ($process.StartTime.ToUniversalTime().Ticks -ne $Identity.Created -or $process.MainModule.FileName -ne $Identity.Path) {
            throw 'Process identity changed; refusing failure injection or forced cleanup.'
        }
        $script:terminatedIdentities.Add($Identity)
        $process.Kill()
        [void]$process.WaitForExit(5000)
    } finally { $process.Dispose() }
}

function Get-UnrelatedProcessState {
    foreach ($entry in $script:unrelatedIdentities) {
        [pscustomobject]@{
            Id = $entry.Identity.Id
            Kind = $entry.Kind
            Alive = [bool](Test-SameProcess $entry.Identity)
        }
    }
}

function Get-OwnedDesktop([uint32]$SupervisorId, [uint32]$ExcludeId = 0) {
    $matches = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$SupervisorId" |
        Where-Object { $_.ExecutablePath -eq $appExe -and $_.ProcessId -ne $ExcludeId -and $_.CommandLine -match '(?:^|\s)--stock-king-desktop-child(?:\s|$)' })
    if ($matches.Count -gt 1) { throw 'Supervisor owns more than one desktop child.' }
    if ($matches.Count -eq 1) { return Get-ProcessRecord $matches[0].ProcessId }
    return $null
}

function Get-OwnedEngine($Desktop) {
    $engineExe = Join-Path $appWorkingDirectory 'Resources\daily-engine\stock_analysis\stock_analysis.exe'
    $matches = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($Desktop.Id)" |
        Where-Object { $_.ExecutablePath -eq $engineExe })
    if ($matches.Count -gt 1) { throw 'Desktop owns more than one engine process.' }
    if ($matches.Count -eq 1) { return Get-ProcessRecord $matches[0].ProcessId }
    return $null
}

function Get-OwnedBrowser($Desktop) {
    $matches = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($Desktop.Id) AND Name='msedgewebview2.exe'" |
        Where-Object { $_.CommandLine -notmatch '(?:^|\s)--type(?:=|\s)' })
    if ($matches.Count -gt 1) { throw 'Ambiguous browser ownership; refusing failure injection.' }
    if ($matches.Count -eq 1) { return Get-ProcessRecord $matches[0].ProcessId }
    return $null
}

function Get-OwnedRenderer($Browser) {
    # Chromium renderer processes are children of the browser. Require that
    # exact browser identity to remain alive before selecting a renderer.
    if (-not (Test-SameProcess $Browser)) { return $null }
    $matches = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($Browser.Id) AND Name='msedgewebview2.exe'" |
        Where-Object { $_.CommandLine -match '(?:^|\s)--type=renderer(?:\s|$)' })
    if ($matches.Count -gt 1) { throw 'Multiple renderers are present; refusing an ambiguous failure injection.' }
    if ($matches.Count -eq 1) { return Get-ProcessRecord $matches[0].ProcessId }
    return $null
}

function Read-LiveLog([string]$Path) {
    $stream = $null
    $reader = $null
    try {
        # The engine inherits an open write handle. A reader must explicitly
        # share writes (and rotation/deletion) while the process is alive.
        $sharing = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
        $stream = [IO.FileStream]::new($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $sharing)
        $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::UTF8, $true)
        return $reader.ReadToEnd()
    } catch [IO.FileNotFoundException] { return ''
    } catch [IO.DirectoryNotFoundException] { return ''
    } finally {
        if ($null -ne $reader) { $reader.Dispose() }
        elseif ($null -ne $stream) { $stream.Dispose() }
    }
}

function Get-HealthCount {
    $content = Read-LiveLog $engineLog
    return [Regex]::Matches($content, '"GET /api/v1/health HTTP/1\.[01]" 200').Count
}

function Test-EngineHealthAfterStart($Engine) {
    $content = Read-LiveLog $engineLog
    $started = [Regex]::Match($content, 'Started server process\s+\[' + $Engine.Id + '\]')
    if (-not $started.Success) { return $false }
    return $content.Substring($started.Index + $started.Length) -match '"GET /api/v1/health HTTP/1\.[01]" 200'
}

function Wait-HealthyDesktop([uint32]$SupervisorId, [uint32]$ExcludeId, [int]$PreviousHealthCount) {
    $desktop = Wait-Check 'supervised desktop child' { Get-OwnedDesktop $SupervisorId $ExcludeId } $StartupTimeoutSeconds
    [void](Wait-Check 'Wails window and ready marker' {
        if (-not (Test-SameProcess $desktop)) { throw 'Desktop exited before readiness.' }
        $window = [StockKingBackgroundQA.Windows]::FindDesktop($desktop.Id)
        $ready = @(Get-ChildItem -LiteralPath $logDirectory -Directory -Filter '.desktop-session-*' -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'ready') }).Count -gt 0
        return $window -ne [IntPtr]::Zero -and $ready
    } $StartupTimeoutSeconds)
    $engine = Wait-Check 'owned bundled engine' { Get-OwnedEngine $desktop } $StartupTimeoutSeconds
    [void](Wait-Check 'new engine authenticated health response' {
        if (-not (Test-SameProcess $desktop) -or -not (Test-SameProcess $engine)) { throw 'Desktop or engine exited while awaiting health.' }
        return (Get-HealthCount) -gt $PreviousHealthCount -and (Test-EngineHealthAfterStart $engine)
    } $StartupTimeoutSeconds)
    $script:ownedIdentities.Add($desktop)
    $script:ownedIdentities.Add($engine)
    return [pscustomobject]@{ Desktop = $desktop; Engine = $engine }
}

function Set-DesktopMinimized($Desktop) {
    if (-not (Test-SameProcess $Desktop)) { throw 'Cannot minimize a desktop that no longer exists.' }
    $window = [StockKingBackgroundQA.Windows]::FindDesktop($Desktop.Id)
    if ($window -eq [IntPtr]::Zero) { throw 'Cannot minimize: Wails window missing.' }
    [void][StockKingBackgroundQA.Windows]::ShowWindowAsync($window, 6)
    [void](Wait-Check 'desktop minimized' { [StockKingBackgroundQA.Windows]::IsIconic($window) } 10)
    # The supervisor samples window state every 250 ms.
    Start-Sleep -Milliseconds 700
}

function Start-IsolatedSupervisor {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $appExe
    $start.WorkingDirectory = $appWorkingDirectory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    # This is an intentional interactive desktop validation. The supervisor
    # itself has no UI; its Wails child must create a normal test window.
    $start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Normal
    $start.EnvironmentVariables['APPDATA'] = $testRoaming
    $start.EnvironmentVariables['LOCALAPPDATA'] = $testLocal
    $start.EnvironmentVariables.Remove('STOCK_KING_DESKTOP_SESSION')
    $start.EnvironmentVariables.Remove('STOCK_KING_RECOVERY_MINIMIZED')
    $process = [Diagnostics.Process]::Start($start)
    $script:startedSupervisors.Add($process)
    $identity = Get-ProcessRecord $process.Id
    if ($null -ne $identity) { $script:ownedIdentities.Add($identity) }
    return $process
}

# This regression requires the profile-scoped single-instance lock. Existing
# user windows and scheduled scans remain running in their own profiles/jobs.
# Record their identities as protected targets, without reading or saving their
# arguments (which may contain private data).
$script:unrelatedIdentities = New-Object 'Collections.Generic.List[object]'
$script:terminatedIdentities = New-Object 'Collections.Generic.List[object]'
$appNames = @([IO.Path]::GetFileName($appExe), 'Stock King.exe') | Select-Object -Unique
foreach ($candidate in @(Get-CimInstance Win32_Process | Where-Object { $_.Name -in $appNames })) {
    $identity = Get-ProcessRecord $candidate.ProcessId
    if ($null -eq $identity) { continue }
    $kind = 'desktop-or-supervisor'
    if ($candidate.CommandLine -match '(?i)(?:^|\s)--stock-king-task=') { $kind = 'scheduled-headless' }
    $script:unrelatedIdentities.Add([pscustomobject]@{ Identity = $identity; Kind = $kind })
    foreach ($engine in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($identity.Id) AND Name='stock_analysis.exe'")) {
        $engineIdentity = Get-ProcessRecord $engine.ProcessId
        if ($null -ne $engineIdentity) {
            $script:unrelatedIdentities.Add([pscustomobject]@{ Identity = $engineIdentity; Kind = $kind + '-engine' })
        }
    }
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path (Split-Path $PSScriptRoot -Parent) 'output\validation'
}
[void](New-Item -ItemType Directory -Path $OutputDirectory -Force)
$receiptDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$runName = 'background-recovery-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$testRoot = Join-Path $receiptDirectory $runName
$testRoaming = Join-Path $testRoot 'roaming'
$testLocal = Join-Path $testRoot 'local'
$testConfig = Join-Path $testRoaming 'Stock King\config'
$logDirectory = Join-Path $testLocal 'Stock King\logs'
$engineLog = Join-Path $logDirectory 'daily-sidecar.log'
$lifecycleLog = Join-Path $logDirectory 'desktop-lifecycle.log'
$receiptPath = Join-Path $testRoot 'receipt.json'
[void](New-Item -ItemType Directory -Path $testConfig, $logDirectory -Force)
foreach ($flag in @('auto-recommendations.disabled', 'background-learning.disabled')) {
    [IO.File]::WriteAllText((Join-Path $testConfig $flag), "isolated regression fixture`n")
}
$script:ownedIdentities = New-Object 'Collections.Generic.List[object]'
$script:startedSupervisors = New-Object 'Collections.Generic.List[object]'
$receipt = [ordered]@{
    success = $false
    startedAt = [DateTime]::UtcNow.ToString('o')
    executable = $appExe
    executableSha256 = (Get-FileHash -LiteralPath $appExe -Algorithm SHA256).Hash.ToLowerInvariant()
    workingDirectory = $appWorkingDirectory
    profile = $testRoot
    profileIsolated = $true
    requiresProfileScopedSingleInstance = $true
    scheduledTasksModified = $false
    unrelatedProcessesAtStart = @($script:unrelatedIdentities.ToArray())
    minimizedSeconds = $IdleSeconds
    checks = [ordered]@{}
    failure = $null
}
$testFailure = $null
$current = $null
try {
    $primary = Start-IsolatedSupervisor
    $current = Wait-HealthyDesktop $primary.Id 0 0
    $receipt.checks.initial = @{ supervisor = (Get-ProcessRecord $primary.Id); desktop = $current.Desktop; engine = $current.Engine; ready = $true; engineHealthy = $true }
    Set-DesktopMinimized $current.Desktop
    $idleUntil = [DateTime]::UtcNow.AddSeconds($IdleSeconds)
    while ([DateTime]::UtcNow -lt $idleUntil) {
        if (-not (Test-SameProcess $current.Desktop) -or -not (Test-SameProcess $current.Engine)) { throw 'Desktop or engine exited during minimized idle check.' }
        $window = [StockKingBackgroundQA.Windows]::FindDesktop($current.Desktop.Id)
        if (-not [StockKingBackgroundQA.Windows]::IsIconic($window)) { throw 'Desktop unexpectedly restored during idle check.' }
        Start-Sleep -Seconds 1
    }
    $receipt.checks.minimizedIdle = $true

    $second = Start-IsolatedSupervisor
    [void](Wait-Check 'forwarded second supervisor exited' { $second.Refresh(); $second.HasExited } 30)
    if ($second.ExitCode -ne 0) { throw "Second launch exited with code $($second.ExitCode)." }
    [void](Wait-Check 'second launch restores original desktop' {
        if (-not (Test-SameProcess $current.Desktop)) { throw 'Second launch replaced the original desktop.' }
        $window = [StockKingBackgroundQA.Windows]::FindDesktop($current.Desktop.Id)
        return $window -ne [IntPtr]::Zero -and [StockKingBackgroundQA.Windows]::IsWindowVisible($window) -and -not [StockKingBackgroundQA.Windows]::IsIconic($window)
    } 15)
    $receipt.checks.secondLaunch = @{ forwardedSupervisorPid = $second.Id; exitCode = $second.ExitCode; originalDesktopRestored = $true }

    foreach ($failureKind in @('browser', 'renderer')) {
        Set-DesktopMinimized $current.Desktop
        $old = $current
        $browser = Wait-Check 'owned WebView2 browser' { Get-OwnedBrowser $old.Desktop } 30
        $victim = $browser
        if ($failureKind -eq 'renderer') { $victim = Wait-Check 'owned WebView2 renderer' { Get-OwnedRenderer $browser } 30 }
        if (-not (Test-SameProcess $old.Desktop) -or -not (Test-SameProcess $browser)) { throw 'Ownership changed before failure injection.' }
        $previousHealth = Get-HealthCount
        $unrelatedBefore = @(Get-UnrelatedProcessState)
        Stop-ExactProcess $victim
        [void](Wait-Check 'failed desktop exits' { -not (Test-SameProcess $old.Desktop) } 30)
        [void](Wait-Check 'old owned engine exits' { -not (Test-SameProcess $old.Engine) } 15)
        $current = Wait-HealthyDesktop $primary.Id $old.Desktop.Id $previousHealth
        $window = [StockKingBackgroundQA.Windows]::FindDesktop($current.Desktop.Id)
        if (-not [StockKingBackgroundQA.Windows]::IsIconic($window)) { throw "$failureKind recovery stole foreground instead of retaining minimized state." }
        $expectedStop = 'desktop stopped pid=' + $old.Desktop.Id + ' exit=75 ready=true closing=false'
        if (-not (Read-LiveLog $lifecycleLog).Contains($expectedStop)) { throw "$failureKind failure did not take the expected recovery path." }
        $unrelatedAfter = @(Get-UnrelatedProcessState)
        $unrelatedExited = @($unrelatedAfter | Where-Object {
            $observed = $_
            -not $observed.Alive -and @($unrelatedBefore | Where-Object { $_.Id -eq $observed.Id -and $_.Alive }).Count -gt 0
        } | ForEach-Object { $_.Id })
        $receipt.checks[$failureKind + 'Failure'] = @{
            injectedProcessPid = $victim.Id
            oldDesktopPid = $old.Desktop.Id
            oldEnginePid = $old.Engine.Id
            oldEngineStopped = $true
            replacementDesktopPid = $current.Desktop.Id
            replacementEnginePid = $current.Engine.Id
            engineHealthy = $true
            remainedMinimized = $true
            unrelatedBefore = $unrelatedBefore
            unrelatedAfter = $unrelatedAfter
            unrelatedStillAlive = ($unrelatedExited.Count -eq 0)
            unrelatedExitedDuringObservation = $unrelatedExited
            unrelatedWasTerminationTarget = $false
        }
        # Existing headless jobs may finish naturally and users may close their
        # own windows while this test runs. Preserve that observation separately
        # instead of attributing every unrelated exit to this injected failure.
    }

    # Normal close while minimized must be intentional, without a recovery.
    $window = [StockKingBackgroundQA.Windows]::FindDesktop($current.Desktop.Id)
    if (-not [StockKingBackgroundQA.Windows]::PostMessage($window, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)) { throw 'Could not request normal desktop close.' }
    [void](Wait-Check 'supervisor finishes after deliberate close' { $primary.Refresh(); $primary.HasExited } 30)
    if ($primary.ExitCode -ne 0) { throw "Deliberate close exited with code $($primary.ExitCode)." }
    [void](Wait-Check 'closed desktop and owned engine stop' { -not (Test-SameProcess $current.Desktop) -and -not (Test-SameProcess $current.Engine) } 15)
    Start-Sleep -Seconds 5
    if ($null -ne (Get-OwnedDesktop $primary.Id)) { throw 'Desktop restarted after deliberate close.' }
    $expectedClose = 'desktop stopped pid=' + $current.Desktop.Id + ' exit=0 ready=true closing=true'
    if (-not (Read-LiveLog $lifecycleLog).Contains($expectedClose)) { throw 'Lifecycle log did not record the deliberate close marker.' }
    $receipt.checks.normalClose = @{ supervisorExitCode = $primary.ExitCode; noRestart = $true; ownedEngineStopped = $true; closingMarkerRecorded = $true }
    $receipt.success = $true
} catch {
    $testFailure = $_
    $receipt.failure = $_.Exception.Message
} finally {
    # Close only exact children of supervisors launched here. Parent shutdown
    # closes its own job, collecting its engine and WebView descendants.
    foreach ($supervisor in $script:startedSupervisors) {
        try {
            $supervisor.Refresh()
            if (-not $supervisor.HasExited) {
                $child = Get-OwnedDesktop $supervisor.Id
                if ($null -ne $child) {
                    $window = [StockKingBackgroundQA.Windows]::FindDesktop($child.Id)
                    if ($window -ne [IntPtr]::Zero) { [void][StockKingBackgroundQA.Windows]::PostMessage($window, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) }
                }
                if (-not $supervisor.WaitForExit(10000)) {
                    $identity = $script:ownedIdentities | Where-Object { $_.Id -eq $supervisor.Id -and $_.Path -eq $appExe } | Select-Object -First 1
                    if ($null -ne $identity) { Stop-ExactProcess $identity }
                }
            }
        } catch { $receipt['cleanupFailure'] = $_.Exception.Message }
    }
    $remaining = @($script:ownedIdentities | Where-Object { Test-SameProcess $_ } | ForEach-Object { $_.Id })
    $receipt['remainingOwnedProcessIds'] = $remaining
    $receipt['unrelatedProcessesAtFinish'] = @(Get-UnrelatedProcessState)
    $receipt['terminationTargets'] = @($script:terminatedIdentities.ToArray())
    if ($remaining.Count -gt 0) { $receipt.success = $false }
    $receipt['finishedAt'] = [DateTime]::UtcNow.ToString('o')
    $receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $receiptPath -Encoding UTF8
    Write-Output "Regression receipt: $receiptPath"
    foreach ($supervisor in $script:startedSupervisors) { $supervisor.Dispose() }
}
if ($null -ne $testFailure) { throw $testFailure }
if (-not $receipt.success) { throw 'Isolated regression left an owned process running; inspect its retained receipt.' }
$receipt | ConvertTo-Json -Depth 8
