[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Executable)

$ErrorActionPreference = 'Stop'
$appExe = (Resolve-Path -LiteralPath $Executable).Path
if (@(Get-Process -Name 'Stock King' -ErrorAction SilentlyContinue).Count -gt 0) {
    throw 'Close Stock King before testing its single-instance behavior.'
}
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('stock-king-launch-' + [Guid]::NewGuid().ToString('N'))
$testRoaming = Join-Path $testRoot 'roaming'
$testLocal = Join-Path $testRoot 'local'
$testConfig = Join-Path $testRoaming 'Stock King/config'
New-Item -ItemType Directory -Path $testConfig, $testLocal -Force | Out-Null
New-Item -ItemType File -Path (Join-Path $testConfig 'auto-recommendations.disabled'), (Join-Path $testConfig 'background-learning.disabled') | Out-Null
$originalRoaming = $env:APPDATA
$originalLocal = $env:LOCALAPPDATA
$env:APPDATA = $testRoaming
$env:LOCALAPPDATA = $testLocal

function Start-TestDesktop([Diagnostics.ProcessWindowStyle]$WindowStyle) {
    return Start-Process -FilePath $appExe -WorkingDirectory (Split-Path $appExe -Parent) -WindowStyle $WindowStyle -PassThru
}

$primary = $null
$second = $null
try {
    # Reproduce the old updater's hidden launch using an isolated profile.
    $primary = Start-TestDesktop Hidden
    Start-Sleep -Seconds 6
    $primary.Refresh()
    if ($primary.HasExited) { throw 'Primary desktop exited before the second launch.' }
    if ($primary.MainWindowHandle -ne 0) { throw 'The hidden-launch fixture did not reproduce the original condition.' }

    $second = Start-TestDesktop Normal
    if (-not $second.WaitForExit(15000)) { throw 'The second launch did not forward to the original process.' }
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 250
        $primary.Refresh()
        if ($primary.HasExited) { throw 'Primary desktop exited during restoration.' }
    } while ($primary.MainWindowHandle -eq 0 -and (Get-Date) -lt $deadline)
    if ($primary.MainWindowHandle -eq 0) { throw 'Second launch did not restore the hidden window.' }

    # The independent bundled engine must also boot with this clean profile.
    $engineLog = Join-Path $testLocal 'Stock King/logs/daily-sidecar.log'
    $engineReady = $false
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline -and -not $engineReady) {
        if (Test-Path -LiteralPath $engineLog) {
            # Health is authenticated. Observe the desktop manager's successful
            # request in this fresh profile instead of exposing its secret token
            # or treating an unauthenticated 401 as a healthy engine.
            $engineReady = [bool](Select-String -LiteralPath $engineLog -Pattern '"GET /api/v1/health HTTP/1\.[01]" 200')
        }
        if (-not $engineReady) { Start-Sleep -Milliseconds 500 }
    }
    if (-not $engineReady) { throw 'The bundled engine did not become healthy with a clean profile.' }
    [ordered]@{
        hiddenLaunchReproduced = $true
        secondLaunchExited = $second.HasExited
        originalWindowRestored = $primary.MainWindowHandle -ne 0
        windowTitle = $primary.MainWindowTitle
        engineHealthy = $engineReady
        profileIsolated = $true
        version = (Get-Item -LiteralPath $appExe).VersionInfo.ProductVersion
    } | ConvertTo-Json
} finally {
    foreach ($process in @($second, $primary)) {
        if ($process) {
            $process.Refresh()
            if (-not $process.HasExited) {
                [void]$process.CloseMainWindow()
                if (-not $process.WaitForExit(8000)) { $process.Kill() }
            }
        }
    }
    # Retain the isolated profile for inspection, never touch the user's profile.
    $env:APPDATA = $originalRoaming
    $env:LOCALAPPDATA = $originalLocal
}
