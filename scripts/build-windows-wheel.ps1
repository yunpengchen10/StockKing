param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'
$buildRepo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildPython = Join-Path $buildRepo '.venv\Scripts\python.exe'
$savedNodeOptions = $env:NODE_OPTIONS
$savedPythonBin = $env:PYTHON_BIN
$savedGoToolchain = $env:GOTOOLCHAIN
$savedPath = $env:PATH

function Invoke-BuildStep {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}

if ($env:OS -ne 'Windows_NT') { throw 'Build on Windows x64.' }
foreach ($command in @($Python, 'go', 'npm')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Install the required local tool first: $command"
    }
}
Push-Location $buildRepo
try {
    # Pin the toolchain before any Go command; Go downloads it when necessary.
    $env:GOTOOLCHAIN = 'go1.26.8+auto'
    $goVersion = & go version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot select Go 1.26.8 automatically. Install Go 1.21+ with network access, or install Go 1.26.8 directly. Details: $goVersion"
    }
    if (($goVersion | Out-String).Trim() -ne 'go version go1.26.8 windows/amd64') {
        throw "Expected Go 1.26.8 on Windows x64; got: $goVersion"
    }
    $goTarget = @(& go env GOOS GOARCH)
    if ($LASTEXITCODE -ne 0 -or $goTarget.Count -ne 2 -or $goTarget[0] -ne 'windows' -or $goTarget[1] -ne 'amd64') {
        throw "Go must target windows/amd64; got GOOS/GOARCH: $($goTarget -join '/')"
    }
    if (-not (Test-Path -LiteralPath $buildPython)) {
        Invoke-BuildStep $Python @('-m', 'venv', '.venv')
    }
    Invoke-BuildStep $buildPython @('-c', "import platform,struct,sys; assert sys.platform == 'win32' and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64','x86_64'), 'Use native Windows x64 Python'")
    $env:NODE_OPTIONS = if ($savedNodeOptions) { $savedNodeOptions } else { '--max-old-space-size=4096' }
    $env:PYTHON_BIN = $buildPython
    Invoke-BuildStep $buildPython @('-m', 'pip', 'install', '.[engine,models]', 'pyinstaller', 'build', 'pytest')
    Invoke-BuildStep 'go' @('install', 'github.com/wailsapp/wails/v2/cmd/wails@v2.11.0')
    [string]$goBinaryDirectory = & go env GOBIN
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read Go binary path.' }
    $goBinaryDirectory = $goBinaryDirectory.Trim()
    if (-not $goBinaryDirectory) {
        $goWorkPath = (& go env GOPATH).Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Unable to read GOPATH.' }
        $goBinaryDirectory = Join-Path (($goWorkPath -split ';')[0]) 'bin'
    }
    $env:PATH = "$goBinaryDirectory;$env:PATH"
    Invoke-BuildStep $buildPython @('-m', 'pytest', 'daily-engine/tests/test_precision_policy.py', 'daily-engine/tests/test_local_quote_integration.py', 'daily-engine/tests/test_local_observation_review.py', 'daily-engine/tests/test_economy_review.py', 'daily-engine/tests/test_public_market_quotes.py', 'daily-engine/tests/test_recommendation_evidence.py', 'daily-engine/tests/test_complete_market_evidence.py', 'daily-engine/tests/test_intraday_evidence.py', 'daily-engine/tests/test_efinance_frozen_cache.py', '-q')
    Push-Location (Join-Path $buildRepo 'daily-engine')
    try { & '.\scripts\build-backend.ps1' -SkipDependencyInstall } finally { Pop-Location }
    Push-Location (Join-Path $buildRepo 'desktop\frontend')
    try {
        Invoke-BuildStep 'npm' @('ci')
        Invoke-BuildStep 'npm' @('test')
    } finally { Pop-Location }
    Push-Location (Join-Path $buildRepo 'desktop')
    try {
        Invoke-BuildStep 'wails' @('build', '-clean', '-platform', 'windows/amd64')
        Invoke-BuildStep 'go' @('test', '.', './internal/windowstoast')
    } finally { Pop-Location }
    Invoke-BuildStep $buildPython @('scripts/build-desktop-wheel.py')
    Invoke-BuildStep $buildPython @('scripts/smoke-wheel.py', '--native')
    Get-ChildItem -LiteralPath (Join-Path $buildRepo 'dist\desktop') -Filter '*win_amd64.whl' |
        Get-FileHash -Algorithm SHA256 | Format-Table Hash, Path -AutoSize
} finally {
    $env:NODE_OPTIONS = $savedNodeOptions
    $env:PYTHON_BIN = $savedPythonBin
    $env:GOTOOLCHAIN = $savedGoToolchain
    $env:PATH = $savedPath
    Pop-Location
}
