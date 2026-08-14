[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("status", "connect", "disconnect", "whoami", "jobs", "recent", "vasp-inspect", "vasp-validate", "vasp-progress", "read", "tail", "list", "mkdir", "copy", "move", "remove", "submit", "cancel", "diagnostic", "upload", "download", "transfer", "servers", "server-add", "server-remove", "server-set-default", "server-edit")]
    [string]$Operation,
    [string]$IdentityFile = $env:VLAB_IDENTITY_FILE,
    [string]$VlabHost = "vlab.ustc.edu.cn",
    [string]$VlabUser = "ubuntu",
    [string]$RemotePath,
    [string]$DestinationPath,
    [string]$LocalPath,
    [string]$JobScript,
    [string]$JobId,
    [string]$ConfirmJobId,
    [ValidateSet("hostname", "pwd", "disk", "quota", "partitions", "modules")]
    [string]$Diagnostic,
    [ValidateRange(1, 2000)]
    [int]$Lines = 80,
    # Positional so catalog operations read naturally: server-add alt ...
    [Parameter(Position = 1)]
    [string]$ServerName,
    [string]$ServerTarget,
    [ValidateRange(1, 65535)]
    [int]$ServerPort = 22,
    [string]$ServerRoot,
    [string]$ServerPersist,
    [string]$NewName,
    [string]$FromServer,
    [string]$FromPath,
    [string]$ToServer,
    [string]$ToPath
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($IdentityFile)) {
    throw "Provide -IdentityFile or set VLAB_IDENTITY_FILE to the downloaded Vlab PEM key."
}
$key = (Resolve-Path -LiteralPath $IdentityFile).Path
$target = "$VlabUser@$VlabHost"
$sshBase = @("-q", "-i", $key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no", $target)

function Quote-Posix([string]$Value) {
    if ($null -eq $Value) { throw "A required argument is missing." }
    return "'" + $Value.Replace("'", "'`"'`"'") + "'"
}

function Invoke-Gateway([string[]]$Arguments, [switch]$Interactive, [switch]$NoServer) {
    if ($ServerName -and -not $NoServer) { $Arguments += @("--server", $ServerName) }
    $remote = "~/bin/vasp-remote-agent " + (($Arguments | ForEach-Object { Quote-Posix $_ }) -join " ")
    $options = @("-q", "-i", $key, "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no")
    if ($Interactive) { $options += @("-tt") }
    else { $options += @("-o", "BatchMode=yes") }
    & ssh @options $target $remote
    $script:GatewayExitCode = $LASTEXITCODE
}

switch ($Operation) {
    "status" { Invoke-Gateway @("status"); $code = $script:GatewayExitCode }
    "connect" { Invoke-Gateway @("connect") -Interactive; $code = $script:GatewayExitCode }
    "disconnect" { Invoke-Gateway @("disconnect"); $code = $script:GatewayExitCode }
    "whoami" { Invoke-Gateway @("whoami"); $code = $script:GatewayExitCode }
    "jobs" { Invoke-Gateway @("jobs"); $code = $script:GatewayExitCode }
    "recent" { Invoke-Gateway @("recent"); $code = $script:GatewayExitCode }
    "servers" { Invoke-Gateway @("servers") -NoServer; $code = $script:GatewayExitCode }
    "server-add" {
        if (-not $ServerName -or -not $ServerTarget) { throw "server-add requires -ServerName and -ServerTarget." }
        $addArgs = @("server-add", $ServerName, "--target", $ServerTarget, "--port", [string]$ServerPort)
        if ($ServerRoot) { $addArgs += @("--root", $ServerRoot) }
        if ($ServerPersist) { $addArgs += @("--persist", $ServerPersist) }
        Invoke-Gateway $addArgs -NoServer; $code = $script:GatewayExitCode
    }
    "server-remove" {
        if (-not $ServerName) { throw "server-remove requires -ServerName." }
        Invoke-Gateway @("server-remove", $ServerName) -NoServer; $code = $script:GatewayExitCode
    }
    "server-set-default" {
        if (-not $ServerName) { throw "server-set-default requires -ServerName." }
        Invoke-Gateway @("server-set-default", $ServerName) -NoServer; $code = $script:GatewayExitCode
    }
    "server-edit" {
        if (-not $ServerName) { throw "server-edit requires -ServerName." }
        $editArgs = @("server-edit", $ServerName)
        if ($NewName) { $editArgs += @("--new-name", $NewName) }
        if ($ServerTarget) { $editArgs += @("--target", $ServerTarget) }
        if ($PSBoundParameters.ContainsKey("ServerPort")) { $editArgs += @("--port", [string]$ServerPort) }
        if ($PSBoundParameters.ContainsKey("ServerRoot")) { $editArgs += @("--root", $ServerRoot) }
        if ($ServerPersist) { $editArgs += @("--persist", $ServerPersist) }
        if ($editArgs.Count -eq 2) { throw "server-edit requires at least one of -ServerTarget, -ServerPort, -ServerRoot, -ServerPersist." }
        Invoke-Gateway $editArgs -NoServer; $code = $script:GatewayExitCode
    }
    { $_ -in @("vasp-inspect", "vasp-validate", "vasp-progress") } {
        if (-not $RemotePath) { throw "$Operation requires -RemotePath (calculation directory)." }
        Invoke-Gateway @($Operation, $RemotePath); $code = $script:GatewayExitCode
    }
    "read" {
        if (-not $RemotePath) { throw "read requires -RemotePath." }
        Invoke-Gateway @("read", $RemotePath); $code = $script:GatewayExitCode
    }
    "tail" {
        if (-not $RemotePath) { throw "tail requires -RemotePath." }
        Invoke-Gateway @("tail", $RemotePath, "--lines", [string]$Lines); $code = $script:GatewayExitCode
    }
    "list" {
        if (-not $RemotePath) { throw "list requires -RemotePath." }
        Invoke-Gateway @("list", $RemotePath); $code = $script:GatewayExitCode
    }
    "mkdir" {
        if (-not $RemotePath) { throw "mkdir requires -RemotePath." }
        Invoke-Gateway @("mkdir", $RemotePath); $code = $script:GatewayExitCode
    }
    "copy" {
        if (-not $RemotePath -or -not $DestinationPath) { throw "copy requires -RemotePath and -DestinationPath." }
        Invoke-Gateway @("copy", $RemotePath, $DestinationPath); $code = $script:GatewayExitCode
    }
    "move" {
        if (-not $RemotePath -or -not $DestinationPath) { throw "move requires -RemotePath and -DestinationPath." }
        Invoke-Gateway @("move", $RemotePath, $DestinationPath); $code = $script:GatewayExitCode
    }
    "remove" {
        if (-not $RemotePath) { throw "remove requires -RemotePath." }
        Invoke-Gateway @("remove", $RemotePath); $code = $script:GatewayExitCode
    }
    "submit" {
        if (-not $RemotePath -or -not $JobScript) { throw "submit requires -RemotePath (directory) and -JobScript." }
        Invoke-Gateway @("submit", $RemotePath, $JobScript); $code = $script:GatewayExitCode
    }
    "cancel" {
        if (-not $JobId -or -not $ConfirmJobId) { throw "cancel requires -JobId and matching -ConfirmJobId." }
        Invoke-Gateway @("cancel", $JobId, $ConfirmJobId); $code = $script:GatewayExitCode
    }
    "diagnostic" {
        if (-not $Diagnostic) { throw "diagnostic requires -Diagnostic." }
        Invoke-Gateway @("diagnostic", $Diagnostic); $code = $script:GatewayExitCode
    }
    "upload" {
        if (-not $LocalPath -or -not $RemotePath) { throw "upload requires -LocalPath and -RemotePath." }
        $local = (Resolve-Path -LiteralPath $LocalPath).Path
        if (-not (Test-Path -LiteralPath $local -PathType Leaf)) { throw "Only regular-file uploads are supported." }
        $stage = "/tmp/vasp-remote-agent-$([guid]::NewGuid().ToString('N'))"
        try {
            & scp -i $key -o BatchMode=yes -o StrictHostKeyChecking=yes -o UpdateHostKeys=no $local "${target}:$stage"
            if ($LASTEXITCODE -ne 0) { throw "Upload to the Vlab staging area failed." }
            Invoke-Gateway @("upload", $stage, $RemotePath); $code = $script:GatewayExitCode
        } finally {
            & ssh @sshBase "rm -f -- $(Quote-Posix $stage)" | Out-Null
        }
    }
    "download" {
        if (-not $LocalPath -or -not $RemotePath) { throw "download requires -RemotePath and -LocalPath." }
        $stage = "/tmp/vasp-remote-agent-$([guid]::NewGuid().ToString('N'))"
        try {
            Invoke-Gateway @("download", $RemotePath, $stage); $code = $script:GatewayExitCode
            if ($code -ne 0) { exit $code }
            & scp -i $key -o BatchMode=yes -o StrictHostKeyChecking=yes -o UpdateHostKeys=no "${target}:$stage" $LocalPath
            $code = $LASTEXITCODE
        } finally {
            & ssh @sshBase "rm -f -- $(Quote-Posix $stage)" | Out-Null
        }
    }
    "transfer" {
        if (-not $FromServer -or -not $FromPath -or -not $ToServer -or -not $ToPath) {
            throw "transfer requires -FromServer, -FromPath, -ToServer and -ToPath."
        }
        Invoke-Gateway @("transfer", "--from-server", $FromServer, "--from-path", $FromPath, "--to-server", $ToServer, "--to-path", $ToPath) -NoServer
        $code = $script:GatewayExitCode
    }
}

exit $code
