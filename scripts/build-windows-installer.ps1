[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BuiltExe,
    [Parameter(Mandatory = $true)][string]$BuiltEngineDir,
    [Parameter(Mandatory = $true)][string]$WebViewInstaller,
    [string]$OutputDir,
    [ValidateSet('lzma', 'zlib')][string]$Compression = 'lzma'
)

# Package validated native builds. This does not read the user's installation,
# databases or settings, and does not register tasks or launch the application.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'Build the Windows installer on Windows.' }
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$desktopExe = (Resolve-Path -LiteralPath $BuiltExe).Path
$engineRoot = (Resolve-Path -LiteralPath $BuiltEngineDir).Path
$runtimeInstaller = (Resolve-Path -LiteralPath $WebViewInstaller).Path
$version = (Get-Content -LiteralPath (Join-Path $repoRoot 'desktop/wails.json') -Raw -Encoding UTF8 | ConvertFrom-Json).info.productVersion
if ((Get-Item -LiteralPath $desktopExe).VersionInfo.ProductVersion -ne $version) {
    throw "Desktop executable does not match installer version $version. Rebuild it first."
}
if ((Split-Path $engineRoot -Leaf) -ne 'stock_analysis' -or
    -not (Test-Path -LiteralPath (Join-Path $engineRoot 'stock_analysis.exe'))) {
    throw 'BuiltEngineDir must be a frozen stock_analysis directory.'
}
$engineFiles = @(Get-ChildItem -LiteralPath $engineRoot -Recurse -File)
$privateFiles = @($engineFiles | Where-Object { $_.Name -match '^\.env$|\.(db|sqlite|sqlite3)$|^settings\.json$' })
if ($privateFiles.Count -gt 0) { throw 'Frozen payload contains user data or configuration files; refusing to package it.' }
$signature = Get-AuthenticodeSignature -LiteralPath $runtimeInstaller
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
    throw 'WebView2 installer must have a valid Microsoft signature.'
}
$nsis = Get-Command makensis.exe -ErrorAction SilentlyContinue
$nsisPath = if ($nsis) { $nsis.Source } else {
    @('C:\Program Files (x86)\NSIS\makensis.exe', 'C:\Program Files\NSIS\makensis.exe') |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $nsisPath) { throw 'NSIS 3 (makensis.exe) is required.' }
if (-not $OutputDir) { $OutputDir = Join-Path $repoRoot 'dist/installers' }
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$outputRoot = (Resolve-Path -LiteralPath $OutputDir).Path
$outputFile = Join-Path $outputRoot "Stock-King-Setup-x64-v$version.exe"
$driveLetter = @('Z','Y','X','W','V','U','T','S','R','Q','P') |
    Where-Object { -not (Test-Path -LiteralPath "${_}:\") } | Select-Object -First 1
if (-not $driveLetter) { throw 'No free temporary drive letter for long dependency paths.' }
$mappedDrive = "${driveLetter}:"
$mapped = $false
try {
    & subst.exe $mappedDrive (Split-Path $engineRoot -Parent)
    if ($LASTEXITCODE -ne 0) { throw 'Could not map the frozen engine directory.' }
    $mapped = $true
    Push-Location (Join-Path $repoRoot 'desktop/build/windows/installer')
    try {
        & $nsisPath "/DINFO_PRODUCTVERSION=$version" "/DARG_WAILS_AMD64_BINARY=$desktopExe" `
            "/DARG_STOCKKING_SIDECAR_ROOT=$mappedDrive" "/DARG_STOCKKING_WEBVIEW2_INSTALLER=$runtimeInstaller" `
            "/DARG_STOCKKING_OUTPUT=$outputFile" "/DARG_STOCKKING_COMPRESSOR=$Compression" 'project.nsi'
        if ($LASTEXITCODE -ne 0) { throw 'NSIS installer build failed.' }
    } finally { Pop-Location }
} finally {
    if ($mapped) { & subst.exe $mappedDrive /D }
}
$manifest = [ordered]@{
    version = $version
    compression = $Compression
    createdAtUtc = [DateTime]::UtcNow.ToString('o')
    installer = [IO.Path]::GetFileName($outputFile)
    installerSha256 = (Get-FileHash -LiteralPath $outputFile -Algorithm SHA256).Hash.ToLowerInvariant()
    desktopSha256 = (Get-FileHash -LiteralPath $desktopExe -Algorithm SHA256).Hash.ToLowerInvariant()
    engineExecutableSha256 = (Get-FileHash -LiteralPath (Join-Path $engineRoot 'stock_analysis.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
    engineFileCount = $engineFiles.Count
    webView2Sha256 = (Get-FileHash -LiteralPath $runtimeInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
    engineFiles = @($engineFiles | Sort-Object FullName | ForEach-Object {
        [ordered]@{
            path = $_.FullName.Substring($engineRoot.Length + 1).Replace('\','/')
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $outputRoot "manifest-v$version.json") -Encoding UTF8
"$($manifest.installerSha256)  $($manifest.installer)" | Set-Content -LiteralPath (Join-Path $outputRoot 'SHA256SUMS.txt') -Encoding ASCII
Copy-Item -LiteralPath (Join-Path $repoRoot 'docs/WINDOWS_INSTALL.md') -Destination $outputRoot -Force
Copy-Item -LiteralPath (Join-Path $repoRoot 'docs/WINDOWS_INSTALL.en.md') -Destination $outputRoot -Force
Write-Output "Installer: $outputFile"
Write-Output "SHA256: $($manifest.installerSha256)"
