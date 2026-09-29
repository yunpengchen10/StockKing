param([switch]$SkipDependencyInstall)
$ErrorActionPreference = 'Stop'

$backendBuildRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ((Get-Location).Path.TrimEnd('\') -ne $backendBuildRoot.TrimEnd('\')) {
  throw "Run this build from $backendBuildRoot"
}

$pythonBin = $env:PYTHON_BIN
if ([string]::IsNullOrWhiteSpace($pythonBin)) {
  $pythonBin = 'python'
}

Write-Host "Using Python: $pythonBin"

# A venv created from Anaconda/Miniconda keeps several Python runtime DLLs in
# <base_prefix>\Library\bin instead of next to python.exe. PyInstaller does not
# always discover that directory when the venv itself is outside the Conda
# root, which can produce an executable that builds successfully but cannot
# import ssl/sqlite/compression modules. Add the owning runtime directories to
# this build process before PyInstaller performs binary dependency analysis.
$pythonBasePrefix = (& $pythonBin -c "import sys; print(sys.base_prefix)").Trim()
$pythonBaseLibraryBin = Join-Path $pythonBasePrefix 'Library\bin'
if (Test-Path $pythonBaseLibraryBin) {
  $pythonBaseDlls = Join-Path $pythonBasePrefix 'DLLs'
  $pythonRuntimePaths = @($pythonBasePrefix, $pythonBaseLibraryBin)
  if (Test-Path $pythonBaseDlls) {
    $pythonRuntimePaths += $pythonBaseDlls
  }
  $env:PATH = (($pythonRuntimePaths + @($env:PATH)) -join ';')
  Write-Host "Added Conda runtime DLL directories: $($pythonRuntimePaths -join ', ')"
}

function Test-PythonCode {
  param(
    [string]$Python,
    [string]$Code
  )

  try {
    & $Python -c $Code *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

Write-Host 'Building backend executable...'
if (-not (Test-PythonCode -Python $pythonBin -Code "import PyInstaller")) {
  & $pythonBin -m pip install pyinstaller
}

if (-not $SkipDependencyInstall) {
Write-Host 'Installing backend dependencies...'
& $pythonBin -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
  throw "pip install -r requirements.txt failed with exit code $LASTEXITCODE."
}

}
Write-Host 'Checking python-multipart availability...'
if (-not (Test-PythonCode -Python $pythonBin -Code "import multipart, multipart.multipart")) {
  throw 'python-multipart is not importable in the selected Python environment.'
}

Write-Host 'Checking built-in screening engine availability...'
if (-not (Test-PythonCode -Python $pythonBin -Code "import src.services.screening.pipeline")) {
  throw 'src.services.screening.pipeline is not importable.'
}

Write-Host 'Checking Futu SDK availability...'
if (-not (Test-PythonCode -Python $pythonBin -Code "import futu")) {
  throw 'futu is not importable after installing requirements.'
}

Write-Host 'Checking orjson availability...'
if (-not (Test-PythonCode -Python $pythonBin -Code "import orjson")) {
  throw 'orjson is not importable after installing requirements.'
}

foreach ($relativeBuildTarget in @('dist\backend', 'dist\stock_analysis', 'build\stock_analysis')) {
  $absoluteBuildTarget = [System.IO.Path]::GetFullPath((Join-Path $backendBuildRoot $relativeBuildTarget))
  if (-not $absoluteBuildTarget.StartsWith($backendBuildRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Build cleanup target escapes backend directory: $absoluteBuildTarget"
  }
  if (Test-Path -LiteralPath $absoluteBuildTarget) {
    Remove-Item -LiteralPath $absoluteBuildTarget -Recurse -Force
  }
}
New-Item -ItemType Directory -Path (Join-Path $backendBuildRoot 'dist\backend') | Out-Null

$hiddenImports = @(
  'src.services.economy_review',
  'src.services.yao_scout.local_opportunities',
  'src.services.yao_scout.signal_learning',
  'src.services.yao_scout.v11_factors',
  'src.services.software_market',
  'multipart',
  'multipart.multipart',
  'json_repair',
  'orjson',
  'tiktoken',
  'tiktoken_ext',
  'tiktoken_ext.openai_public',
  'api',
  'api.app',
  'api.deps',
  'api.v1',
  'api.v1.router',
  'api.v1.endpoints',
  'api.v1.endpoints.analysis',
  'api.v1.endpoints.history',
  'api.v1.endpoints.stocks',
  'api.v1.endpoints.health',
  'api.v1.endpoints.screening',
  'api.v1.endpoints.quant',
  'api.v1.endpoints.stock_king',
  'src.services.screening',
  'src.services.screening.pipeline',
  'api.v1.schemas',
  'api.v1.schemas.analysis',
  'api.v1.schemas.history',
  'api.v1.schemas.stocks',
  'api.v1.schemas.common',
  'api.middlewares',
  'api.middlewares.error_handler',
  'src.services',
  'src.services.task_queue',
  'src.services.analysis_service',
  'src.services.history_service',
  'src.services.screening_service',
  'src.services.stock_king_service',
  'src.services.stock_king_board',
  'src.quant',
  'src.quant.features',
  'src.quant.master',
  'src.quant.service',
  'src.quant.tier_models',
  'sklearn.linear_model',
  'sklearn.metrics',
  'lightgbm',
  'torch',
  'uvicorn.logging',
  'uvicorn.loops',
  'uvicorn.loops.auto',
  'uvicorn.protocols',
  'uvicorn.protocols.http',
  'uvicorn.protocols.http.auto',
  'uvicorn.protocols.websockets',
  'uvicorn.protocols.websockets.auto',
  'uvicorn.lifespan',
  'uvicorn.lifespan.on'
)
$hiddenImportArgs = $hiddenImports | ForEach-Object { "--hidden-import=$_" }
$runtimeHook = Join-Path $PSScriptRoot 'pyinstaller_runtime_compat.py'

$pyInstallerArgs = @(
  '-m', 'PyInstaller',
  '--name', 'stock_analysis',
  '--onedir',
  '--noconfirm',
  '--noconsole',
  '--runtime-hook', $runtimeHook,
  '--add-data', 'resources;resources',
  '--add-data', 'strategies;strategies',
  '--add-data', 'src/assets/share_image;src/assets/share_image',
  '--collect-data', 'litellm',
  '--collect-data', 'tiktoken',
  '--collect-data', 'akshare',
  '--collect-all', 'src.services.screening',
  '--collect-all', 'futu',
  '--collect-all', 'lightgbm'
)
$excludedBuildOnlyModules = @(
  'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
  'IPython', 'sphinx', 'pytest', 'docutils', 'black', 'yapf',
  'matplotlib', 'tkinter', '_tkinter',
  'dask', 'distributed', 'pyarrow', 'tables', 'h5py',
  'bokeh', 'notebook', 'jupyter', 'jupyterlab',
  'xarray', 'patsy', 'statsmodels',
  'panel', 'pyviz_comms', 'plotly', 'skimage', 'intake', 'altair', 'xyzservices',
  'nbformat', 'nbconvert', 'astropy', 'astropy_iers_data',
  'imageio', 'pywt', 'tensorflow', 'holoviews', 'hvplot', 'datashader', 'geoviews'
)
$pyInstallerArgs += $excludedBuildOnlyModules | ForEach-Object { "--exclude-module=$_" }
$pyInstallerArgs += $hiddenImportArgs
$pyInstallerArgs += 'main.py'

Write-Host "Running: $pythonBin $($pyInstallerArgs -join ' ')"
& $pythonBin @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed with exit code $LASTEXITCODE."
}

if (!(Test-Path 'dist\stock_analysis')) {
  throw 'PyInstaller finished but dist\stock_analysis was not generated.'
}

Copy-Item -Path 'dist\stock_analysis' -Destination 'dist\backend\stock_analysis' -Recurse -Force

Write-Host 'Verifying packaged runtime imports...'
$packagedEntry = Join-Path 'dist\backend\stock_analysis' 'stock_analysis.exe'
if (-not (Test-Path $packagedEntry)) {
  throw "Packaged backend entrypoint not found: $packagedEntry"
}
$previousProbe = $env:DSA_PACKAGED_IMPORT_PROBE
try {
  foreach ($module in @('src.services.economy_review', 'src.services.yao_scout.local_opportunities', 'src.services.yao_scout.signal_learning', 'src.services.yao_scout.v11_factors', 'src.services.software_market', 'src.services.screening.pipeline', 'src.quant.service', 'src.quant.executable_backtest', 'api.v1.endpoints.stock_king', 'api.v1.endpoints.quant', 'api.v1.endpoints.portfolio', 'src.services.yao_scout.adaptive', 'lightgbm', 'torch', 'futu', 'orjson')) {
    $env:DSA_PACKAGED_IMPORT_PROBE = $module
    $probeProcess = Start-Process -FilePath $packagedEntry -WindowStyle Hidden -Wait -PassThru
    if ($probeProcess.ExitCode -ne 0) {
      throw "Packaged backend cannot import $module; probe exited with code $($probeProcess.ExitCode)."
    }
  }
} finally {
  if ($null -eq $previousProbe) {
    Remove-Item Env:DSA_PACKAGED_IMPORT_PROBE -ErrorAction SilentlyContinue
  } else {
    $env:DSA_PACKAGED_IMPORT_PROBE = $previousProbe
  }
}

Write-Host 'Verifying packaged AkShare calendar data...'
$packagedAkshareCalendar = Join-Path 'dist\backend\stock_analysis' '_internal\akshare\file_fold\calendar.json'
if (-not (Test-Path $packagedAkshareCalendar)) {
  $packagedAkshareCalendar = Join-Path 'dist\backend\stock_analysis' 'akshare\file_fold\calendar.json'
}
if (-not (Test-Path $packagedAkshareCalendar)) {
  throw 'Packaged AkShare calendar data not found under dist\backend\stock_analysis.'
}

Write-Host 'Verifying packaged Stock King resources...'
$packagedResources = Join-Path 'dist\backend\stock_analysis' '_internal\resources'
if (-not (Test-Path $packagedResources)) {
  $packagedResources = Join-Path 'dist\backend\stock_analysis' 'resources'
}
if (-not (Test-Path (Join-Path $packagedResources 'stocks.index.json'))) {
  throw 'Packaged Stock King stock index was not found.'
}

Write-Host 'Verifying packaged built-in strategies...'
$sourceStrategyCount = @(Get-ChildItem -Path 'strategies' -Filter '*.yaml' -File).Count
$packagedStrategies = Join-Path 'dist\backend\stock_analysis' '_internal\strategies'
if (-not (Test-Path $packagedStrategies)) {
  $packagedStrategies = Join-Path 'dist\backend\stock_analysis' 'strategies'
}
if (-not (Test-Path $packagedStrategies)) {
  throw 'Packaged strategies directory not found under dist\backend\stock_analysis.'
}
$packagedStrategyCount = @(Get-ChildItem -Path $packagedStrategies -Filter '*.yaml' -File).Count
if ($packagedStrategyCount -ne $sourceStrategyCount) {
  throw "Packaged strategies count mismatch: expected $sourceStrategyCount, got $packagedStrategyCount."
}

Write-Host 'Verifying packaged screening strategies...'
$sourceScreeningStrategies = Join-Path 'src\services\screening' 'strategies'
$sourceScreeningStrategyCount = @(Get-ChildItem -Path $sourceScreeningStrategies -Filter '*.yaml' -File).Count
$packagedScreeningStrategies = Join-Path 'dist\backend\stock_analysis' '_internal\src\services\screening\strategies'
if (-not (Test-Path $packagedScreeningStrategies)) {
  $packagedScreeningStrategies = Join-Path 'dist\backend\stock_analysis' 'src\services\screening\strategies'
}
if (-not (Test-Path $packagedScreeningStrategies)) {
  throw 'Packaged screening strategies directory not found under dist\backend\stock_analysis.'
}
$packagedScreeningStrategyCount = @(Get-ChildItem -Path $packagedScreeningStrategies -Filter '*.yaml' -File).Count
if ($packagedScreeningStrategyCount -ne $sourceScreeningStrategyCount) {
  throw "Packaged screening strategies count mismatch: expected $sourceScreeningStrategyCount, got $packagedScreeningStrategyCount."
}

Write-Host 'Backend build completed.'
