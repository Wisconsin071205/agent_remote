[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$IdentityFile,
    [string]$VlabHost = "vlab.ustc.edu.cn",
    [string]$VlabUser = "ubuntu"
)

$ErrorActionPreference = "Stop"
$key = (Resolve-Path -LiteralPath $IdentityFile).Path
$gateway = Join-Path $PSScriptRoot "vasp_gateway.py"
if (-not (Test-Path -LiteralPath $gateway -PathType Leaf)) {
    throw "Gateway helper not found: $gateway"
}

$target = "$VlabUser@$VlabHost"
Write-Host "Installing the restricted gateway helper on $target ..."
& ssh -q -i $key -o StrictHostKeyChecking=ask -o UpdateHostKeys=no $target "mkdir -p ~/bin ~/.config/vasp-remote-agent ~/.cache/vasp-remote-agent && chmod 700 ~/bin ~/.config/vasp-remote-agent ~/.cache/vasp-remote-agent"
if ($LASTEXITCODE -ne 0) { throw "Could not prepare the Vlab directories." }

& scp -q -i $key -o StrictHostKeyChecking=ask -o UpdateHostKeys=no $gateway "${target}:~/bin/vasp-remote-agent"
if ($LASTEXITCODE -ne 0) { throw "Could not copy the gateway helper." }

# Normalize CRLF -> LF on the gateway. Windows scp copies bytes as-is; a
# CRLF shebang makes the kernel look for "python3" and every gateway
# command dies with /usr/bin/env errors.
& ssh -q -i $key -o StrictHostKeyChecking=ask -o UpdateHostKeys=no $target "sed -i 's/\r`$//' ~/bin/vasp-remote-agent && chmod 700 ~/bin/vasp-remote-agent && python3 -m py_compile ~/bin/vasp-remote-agent"
if ($LASTEXITCODE -ne 0) { throw "The gateway helper failed validation on Vlab." }

Write-Host "Installed. Next run: ./scripts/vasp-agent.ps1 connect -IdentityFile `"$key`""
