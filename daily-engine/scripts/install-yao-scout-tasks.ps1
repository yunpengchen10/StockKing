[CmdletBinding()]
param(
    [string]$PythonPath = "",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskNames = @(
    "StockKing-YaoScout-Prefetch",
    "StockKing-YaoScout-Preopen",
    "StockKing-YaoScout-Postclose",
    "StockKing-YaoScout-Intraday-AM",
    "StockKing-YaoScout-Intraday-PM"
)

foreach ($TaskName in $TaskNames) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

if ($Uninstall) {
    Write-Output "Removed legacy Stock King yao-scout scheduled tasks."
} else {
    Write-Output "Legacy standalone Python tasks are retired. Stock King v2.3+ uses the GUI-subsystem adaptive task runner instead."
}
