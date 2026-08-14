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
