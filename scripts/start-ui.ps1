[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$ui = Join-Path $PSScriptRoot "vasp_ui.py"
if (-not (Test-Path -LiteralPath $ui -PathType Leaf)) {
    throw "VASPilot UI program was not found: $ui"
}

# Single-instance behavior: when the UI server is already running on the
# requested port, do NOT start a second server (two instances sharing one
# port make CSRF tokens flaky). Just open a fresh browser window pointing
# at the existing instance.
$existing = netstat -ano | Select-String ":$Port\s" | Select-String "LISTENING"
if ($existing) {
    $pidLine = ($existing | Select-Object -First 1).ToString().Trim()
    $owner = ($pidLine -split '\s+')[-1]
    Write-Host "VASPilot is already running on port $Port (PID $owner). Opening a new browser window instead."
    Start-Process "http://127.0.0.1:$Port/"
    exit 0
}

$python = Get-Command py -ErrorAction SilentlyContinue
if ($python) {
    $arguments = @("-3", $ui, "--port", [string]$Port)
    if ($NoBrowser) { $arguments += "--no-browser" }
    & $python.Source @arguments
    exit $LASTEXITCODE
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw "Python 3 was not found. Install Python first." }
$arguments = @($ui, "--port", [string]$Port)
if ($NoBrowser) { $arguments += "--no-browser" }
& $python.Source @arguments
exit $LASTEXITCODE
