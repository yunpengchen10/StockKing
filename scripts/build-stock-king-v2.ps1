param(
    [switch]$SkipTests,
    [switch]$SkipSidecarBuild,
    [switch]$SkipDesktopBuild,
    [switch]$SkipSourceArchive,
    [string]$PrivateIconPng = $env:STOCK_KING_PRIVATE_ICON_PNG,
    [string]$PrivateIconIco = $env:STOCK_KING_PRIVATE_ICON_ICO
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$desktopRoot = Join-Path $repoRoot 'desktop'
$dailyRoot = Join-Path $repoRoot 'daily-engine'
$pythonBin = Join-Path $repoRoot '.venv\Scripts\python.exe'
$artifactRoot = Join-Path $repoRoot 'artifacts\v2.5.0'
$sourceFolderName = "$([char]0x6E90)$([char]0x4EE3)$([char]0x7801)"
$webViewInstaller = Join-Path $desktopRoot 'build\windows\installer\tmp\MicrosoftEdgeWebView2RuntimeInstallerX64.exe'

if (-not (Test-Path -LiteralPath $pythonBin)) {
    throw "Project Python environment not found: $pythonBin"
}
New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null

Write-Host 'Generating Stock King public brand assets...'
& $pythonBin (Join-Path $repoRoot 'scripts\generate-brand-assets.py')
if ($LASTEXITCODE -ne 0) { throw 'Stock King brand asset generation failed.' }

if (-not $SkipTests) {
    Write-Host 'Running Stock King quant and sidecar tests...'
    Push-Location $dailyRoot
    try {
        & $pythonBin -m pytest tests/test_king_adaptive.py tests/test_king_rate_integrity.py tests/test_yao_scout.py tests/test_stock_king_tier_models.py tests/test_stock_king_quant.py tests/test_quant_training_tasks.py tests/test_stock_king_sidecar_auth.py tests/test_windows_scheduled_tasks.py tests/test_balancedrank_validation.py tests/test_executable_backtest.py tests/test_economy_review.py -q
        if ($LASTEXITCODE -ne 0) { throw 'Stock King engine tests failed.' }
    } finally { Pop-Location }

    Write-Host 'Running Wails Vue production build...'
    Push-Location (Join-Path $desktopRoot 'frontend')
    try {
        npm test
        if ($LASTEXITCODE -ne 0) { throw 'Frontend behavior tests failed.' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Wails Vue production build failed.' }
    } finally { Pop-Location }

    Write-Host 'Running Go tests...'
    Push-Location $desktopRoot
    try {
        go test .
        if ($LASTEXITCODE -ne 0) { throw 'Go tests failed.' }
    } finally { Pop-Location }
}

if (-not $SkipSidecarBuild) {
    Write-Host 'Freezing the Stock King/PyTorch/LightGBM sidecar...'
    Push-Location $dailyRoot
    try {
        $env:PYTHON_BIN = $pythonBin
        & (Join-Path $dailyRoot 'scripts\build-backend.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Stock King sidecar build failed.' }
    } finally {
        Remove-Item Env:PYTHON_BIN -ErrorAction SilentlyContinue
        Pop-Location
    }
}

$sidecarExe = Join-Path $dailyRoot 'dist\backend\stock_analysis\stock_analysis.exe'
if (-not (Test-Path -LiteralPath $sidecarExe)) {
    throw "Frozen sidecar was not found: $sidecarExe"
}

Write-Host 'Preparing the Microsoft-signed offline WebView2 x64 runtime...'
$webViewDirectory = Split-Path -Parent $webViewInstaller
New-Item -ItemType Directory -Force -Path $webViewDirectory | Out-Null
if (-not (Test-Path -LiteralPath $webViewInstaller) -or (Get-Item -LiteralPath $webViewInstaller).Length -lt 50MB) {
    Invoke-WebRequest -UseBasicParsing `
        -Uri 'https://go.microsoft.com/fwlink/?linkid=2124701' `
        -OutFile $webViewInstaller
}
$webViewSignature = Get-AuthenticodeSignature -LiteralPath $webViewInstaller
if ($webViewSignature.Status -ne 'Valid' -or $webViewSignature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
    throw "Offline WebView2 installer signature validation failed: $($webViewSignature.Status) $($webViewSignature.StatusMessage)"
}

$privateIconBackupRoot = $null
$privateIconTargets = @()
$hasPrivatePng = -not [string]::IsNullOrWhiteSpace($PrivateIconPng)
$hasPrivateIco = -not [string]::IsNullOrWhiteSpace($PrivateIconIco)
if ($hasPrivatePng -xor $hasPrivateIco) {
    throw 'Private branding requires both -PrivateIconPng and -PrivateIconIco.'
}
if ($hasPrivatePng -and $hasPrivateIco) {
    $privatePngPath = [System.IO.Path]::GetFullPath($PrivateIconPng)
    $privateIcoPath = [System.IO.Path]::GetFullPath($PrivateIconIco)
    foreach ($privatePath in @($privatePngPath, $privateIcoPath)) {
        if (-not (Test-Path -LiteralPath $privatePath -PathType Leaf)) {
            throw "Private branding asset was not found: $privatePath"
        }
        if ($privatePath.StartsWith($repoRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'Private branding assets must stay outside the Git repository.'
        }
    }
    $privateIconBackupRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("stock-king-public-icons-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $privateIconBackupRoot | Out-Null
    $privateIconTargets = @(
        @{ Target = (Join-Path $desktopRoot 'build\appicon.png'); Source = $privatePngPath },
        @{ Target = (Join-Path $desktopRoot 'build\app.ico'); Source = $privateIcoPath },
        @{ Target = (Join-Path $desktopRoot 'build\windows\icon.ico'); Source = $privateIcoPath }
    )
    foreach ($item in $privateIconTargets) {
        Copy-Item -LiteralPath $item.Target -Destination (Join-Path $privateIconBackupRoot ([System.IO.Path]::GetFileName($item.Target))) -Force
        Copy-Item -LiteralPath $item.Source -Destination $item.Target -Force
    }
    Write-Host 'Private branding injected for the local binary/installer build only.'
}

try {
Write-Host 'Building the Wails x64 application and offline NSIS installer...'
Push-Location $desktopRoot
try {
  if (-not $SkipDesktopBuild) {
    wails generate module
    if ($LASTEXITCODE -ne 0) { throw 'Wails binding generation failed.' }
    wails build -platform windows/amd64 -webview2 embed -clean -trimpath -skipbindings
    if ($LASTEXITCODE -ne 0) { throw 'Wails production build failed.' }
  } else {
    $existingDesktopBinary = Join-Path $desktopRoot 'build\bin\Stock King.exe'
    if (-not (Test-Path -LiteralPath $existingDesktopBinary) -or (Get-Item -LiteralPath $existingDesktopBinary).VersionInfo.ProductVersion -notmatch '^2\.5\.0(?:\.|$)') {
      throw 'Skipping desktop build requires an already validated v2.5.0 executable.'
    }
  }
} finally { Pop-Location }

$makeNsisCommand = Get-Command 'makensis.exe' -ErrorAction SilentlyContinue
if ($makeNsisCommand) {
    $makeNsis = $makeNsisCommand.Source
} else {
    $makeNsis = @(
        'C:\Program Files (x86)\NSIS\makensis.exe',
        'C:\Program Files\NSIS\makensis.exe',
        (Join-Path $env:LOCALAPPDATA 'Programs\NSIS\makensis.exe')
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if ([string]::IsNullOrWhiteSpace($makeNsis)) {
    throw 'NSIS 3 is required to create the installer (makensis.exe was not found).'
}

$sidecarParent = (Resolve-Path -LiteralPath (Split-Path -Parent (Split-Path -Parent $sidecarExe))).Path
$driveLetter = @('Z', 'Y', 'X', 'W', 'V', 'U', 'T', 'S', 'R', 'Q', 'P') |
    Where-Object { -not (Test-Path -LiteralPath ("${_}:\")) } |
    Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($driveLetter)) {
    throw 'No unused drive letter is available for the temporary NSIS sidecar mapping.'
}
$mappedDrive = "${driveLetter}:"
$desktopExe = Join-Path $desktopRoot 'build\bin\Stock King.exe'
$installerScriptRoot = Join-Path $desktopRoot 'build\windows\installer'
try {
    & subst.exe $mappedDrive $sidecarParent
    if ($LASTEXITCODE -ne 0) { throw "Could not map $mappedDrive to the sidecar staging directory." }
    Push-Location $installerScriptRoot
    try {
        & $makeNsis "/DARG_WAILS_AMD64_BINARY=$desktopExe" "/DARG_STOCKKING_SIDECAR_ROOT=$mappedDrive" 'project.nsi'
        if ($LASTEXITCODE -ne 0) { throw 'NSIS installer creation failed.' }
    } finally { Pop-Location }
} finally {
    & subst.exe $mappedDrive /D 2>$null
}

$installer = Join-Path $desktopRoot 'build\bin\Stock-King-Setup-x64-v2.5.0.exe'
if (-not (Test-Path -LiteralPath $installer)) {
    throw "Installer was not generated: $installer"
}
} finally {
    if ($privateIconBackupRoot) {
        foreach ($item in $privateIconTargets) {
            $backupPath = Join-Path $privateIconBackupRoot ([System.IO.Path]::GetFileName($item.Target))
            Copy-Item -LiteralPath $backupPath -Destination $item.Target -Force
        }
        Remove-Item -LiteralPath $privateIconBackupRoot -Recurse -Force
        Write-Host 'Public repository icon placeholders restored; private icon remains only in compiled artifacts.'
    }
}
Copy-Item -LiteralPath $installer -Destination $artifactRoot -Force
Copy-Item -LiteralPath (Join-Path $repoRoot 'THIRD_PARTY_NOTICES.md') -Destination $artifactRoot -Force
Copy-Item -LiteralPath (Join-Path $repoRoot 'THIRD_PARTY_NOTICES.zh-CN.md') -Destination $artifactRoot -Force
Copy-Item -LiteralPath (Join-Path $repoRoot 'README.md') -Destination $artifactRoot -Force
Copy-Item -LiteralPath (Join-Path $repoRoot 'README.en.md') -Destination $artifactRoot -Force

if (-not $SkipSourceArchive) {
    $sourceDirectory = Join-Path $artifactRoot $sourceFolderName
    & (Join-Path $PSScriptRoot 'export-source.ps1') -Destination $sourceDirectory
    $sourceArchive = Join-Path $artifactRoot 'Stock-King-v2.5.0-source.zip'
    if (Test-Path -LiteralPath $sourceArchive) { Remove-Item -LiteralPath $sourceArchive -Force }
    Push-Location $artifactRoot
    try {
        tar.exe -a -cf $sourceArchive $sourceFolderName
        if ($LASTEXITCODE -ne 0) { throw 'Source archive creation failed.' }
    } finally { Pop-Location }
}

$hashTargets = Get-ChildItem -LiteralPath $artifactRoot -File |
    Where-Object { $_.Extension -in '.exe', '.zip' }
$hashLines = foreach ($file in $hashTargets) {
    $hash = Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256
    "$($hash.Hash.ToLowerInvariant())  $($file.Name)"
}
$hashLines | Set-Content -LiteralPath (Join-Path $artifactRoot 'SHA256SUMS.txt') -Encoding ascii

Write-Host "Stock King v2.5.0 artifacts: $artifactRoot"
Get-ChildItem -LiteralPath $artifactRoot -File | Select-Object Name, Length, LastWriteTime
