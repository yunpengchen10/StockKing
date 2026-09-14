param(
    [string]$Destination
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$artifactRoot = Join-Path $repoRoot 'artifacts\v2.5.0'
$sourceFolderName = "$([char]0x6E90)$([char]0x4EE3)$([char]0x7801)"
if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path $artifactRoot $sourceFolderName
}

$destinationPath = [System.IO.Path]::GetFullPath($Destination)
$artifactPath = [System.IO.Path]::GetFullPath($artifactRoot)
if (-not $destinationPath.StartsWith($artifactPath + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Source export destination must stay inside $artifactPath"
}

if (Test-Path -LiteralPath $destinationPath) {
    $longDestinationPath = if ($destinationPath.StartsWith('\\?\')) { $destinationPath } else { "\\?\$destinationPath" }
    [System.IO.Directory]::Delete($longDestinationPath, $true)
}
New-Item -ItemType Directory -Force -Path $destinationPath | Out-Null

$excludedDirectories = @(
    'node_modules',
    (Join-Path $repoRoot 'tmp'),
    (Join-Path $repoRoot 'output'),
    '__pycache__',
    '.pytest_cache',
    'private-branding',
    (Join-Path $repoRoot '.git'),
    (Join-Path $repoRoot '.venv'),
    (Join-Path $repoRoot 'artifacts'),
    (Join-Path $repoRoot 'data'),
    (Join-Path $repoRoot 'desktop\frontend\node_modules'),
    (Join-Path $repoRoot 'desktop\frontend\dist'),
    (Join-Path $repoRoot 'desktop\build\bin'),
    (Join-Path $repoRoot 'desktop\build\windows\installer\tmp'),
    (Join-Path $repoRoot 'desktop\data'),
    (Join-Path $repoRoot 'desktop\logs'),
    (Join-Path $repoRoot 'daily-engine\build'),
    (Join-Path $repoRoot 'daily-engine\dist'),
    (Join-Path $repoRoot 'daily-engine\data'),
    (Join-Path $repoRoot 'daily-engine\logs'),
    (Join-Path $repoRoot 'daily-engine\reports')
)

$arguments = @(
    $repoRoot,
    $destinationPath,
    '/E',
    '/R:1',
    '/W:1',
    '/NFL',
    '/NDL',
    '/NJH',
    '/NJS',
    '/NP',
    '/XD'
) + $excludedDirectories + @(
    '/XF', '.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx', 'credentials*.json', 'service-account*.json', '*.pyc', '*.pyo', '*.log', '*.exe', '*.zip', '*.db', '*.db-*', '*.sqlite', '*.sqlite-*', '*.sqlite3', '*.sqlite3-*', '*.pt', '*.pth', '*.ckpt', '*.pkl', '*.joblib', 'stock_analysis.spec'
)

& robocopy.exe @arguments | Out-Null
if ($LASTEXITCODE -gt 7) {
    throw "Source export failed with robocopy exit code $LASTEXITCODE"
}

Write-Host "Complete source code exported to: $destinationPath"
Get-ChildItem -LiteralPath $destinationPath | Select-Object Name, Mode, LastWriteTime
