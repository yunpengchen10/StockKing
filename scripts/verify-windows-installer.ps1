[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Manifest,
    [Parameter(Mandatory = $true)][string]$ExtractedDir
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$releaseRoot = Split-Path $manifestPath -Parent
$metadata = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$extracted = (Resolve-Path -LiteralPath $ExtractedDir).Path
$engineRoot = Join-Path $extracted 'resources\daily-engine\stock_analysis'
function Assert-Hash([string]$Path, [string]$Expected) {
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Expected) { throw "Hash mismatch: $Path" }
}
$installer = Join-Path $releaseRoot $metadata.installer
Assert-Hash $installer $metadata.installerSha256
Assert-Hash (Join-Path $extracted 'Stock King.exe') $metadata.desktopSha256
$allowed = @{'Stock King.exe' = $true}
foreach ($file in $metadata.engineFiles) {
    Assert-Hash (Join-Path $engineRoot $file.path) $file.sha256
    $allowed['resources/daily-engine/stock_analysis/' + $file.path] = $true
}
Assert-Hash (Join-Path $engineRoot 'stock_analysis.exe') $metadata.engineExecutableSha256
$documents = @{
    'docs/README.md' = 'README.md'; 'docs/README.en.md' = 'README.en.md'
    'docs/WINDOWS_INSTALL.md' = 'docs/WINDOWS_INSTALL.md'; 'docs/WINDOWS_INSTALL.en.md' = 'docs/WINDOWS_INSTALL.en.md'
    'resources/register-stock-king-tasks.ps1' = 'scripts/register-stock-king-tasks.ps1'
    'licenses/Stock-King-GPL-3.0.txt' = 'desktop/LICENSE'
    'licenses/Daily-Stock-Analysis-MIT.txt' = 'daily-engine/LICENSE'
    'licenses/MASTER-MIT.txt' = 'licenses/MASTER-MIT.txt'
    'licenses/THIRD-PARTY-NOTICES.md' = 'THIRD_PARTY_NOTICES.md'
    'licenses/THIRD-PARTY-NOTICES.zh-CN.md' = 'THIRD_PARTY_NOTICES.zh-CN.md'
}
foreach ($target in $documents.Keys) {
    Assert-Hash (Join-Path $extracted $target) (Get-FileHash -LiteralPath (Join-Path $repoRoot $documents[$target]) -Algorithm SHA256).Hash
    $allowed[$target] = $true
}
$allFiles = @(Get-ChildItem -LiteralPath $extracted -File -Recurse)
if ($allFiles.Count -ne $allowed.Count) { throw "Unexpected extracted file count: $($allFiles.Count), expected $($allowed.Count)" }
foreach ($file in $allFiles) {
    $relative = $file.FullName.Substring($extracted.Length + 1).Replace('\','/')
    if (-not $allowed.ContainsKey($relative)) { throw "Unexpected extracted file: $relative" }
    if ($file.Name -match '^\.env($|\.)|^settings\.json$|^credentials.*\.json$|^service-account.*\.json$|\.(db|sqlite|sqlite3|p12|pfx)$') {
        throw "Private-data filename in payload: $relative"
    }
    if ($file.Extension -in '.pem', '.key') {
        $pem = Get-Content -LiteralPath $file.FullName -Raw
        if ($pem -match '-----BEGIN [^-]*PRIVATE KEY-----' -or $pem -notmatch '-----BEGIN (CERTIFICATE|PUBLIC KEY|RSA PUBLIC KEY)-----') {
            throw "Unexpected private or unknown key material: $relative"
        }
    }
}
$result = [ordered]@{
    version = $metadata.version; verifiedAtUtc = [DateTime]::UtcNow.ToString('o')
    installer = $metadata.installer; installerBytes = (Get-Item -LiteralPath $installer).Length
    sha256 = $metadata.installerSha256; desktopPayloadVerified = $true
    engineFilesVerified = @($metadata.engineFiles).Count; documentationVerified = $true
    taskScriptVerified = $true; licensesVerified = $true
    unexpectedFiles = 0; privateDataFiles = 0; extractOnly = $true
}
$result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $releaseRoot "verification-v$($metadata.version).json") -Encoding UTF8
$result | ConvertTo-Json -Compress
