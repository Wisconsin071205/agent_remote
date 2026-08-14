# Setup

## Connection design

```text
Windows/Codex --Vlab PEM--> vlab.ustc.edu.cn --manual password+TOTP--> cl9
```

The cl9 master connection lives on Vlab and is reused by later operations. Secrets are never stored by this skill.

## 1. Obtain the Vlab private key

In the Vlab VM page, open **SSH 密钥管理**, generate a key pair, and download the `.pem` private key. Store it under `%USERPROFILE%\.ssh`, allow only the current Windows user to read it, and never share it.

## 2. Install the gateway helper on Vlab

From PowerShell in this skill directory:

```powershell
./scripts/install-vlab.ps1 -IdentityFile "$HOME/.ssh/vlab-vm13926.pem"
```

The installer copies only `scripts/vasp_gateway.py` to `~/bin/vasp-remote-agent` on Vlab.

## 3. Establish the cl9 master connection

```powershell
./scripts/vasp-agent.ps1 connect -IdentityFile "$HOME/.ssh/vlab-vm13926.pem"
```

Confirm the cl9 host fingerprint on first use, then enter the cl9 password and current six-digit TOTP. Input is handled directly by SSH and is not logged.

## 4. Verify

```powershell
./scripts/vasp-agent.ps1 status -IdentityFile "$HOME/.ssh/vlab-vm13926.pem"
./scripts/vasp-agent.ps1 whoami -IdentityFile "$HOME/.ssh/vlab-vm13926.pem"
./scripts/vasp-agent.ps1 jobs -IdentityFile "$HOME/.ssh/vlab-vm13926.pem"
```

Set `VLAB_IDENTITY_FILE` to avoid repeating the key path:

```powershell
$env:VLAB_IDENTITY_FILE = "$HOME/.ssh/vlab-vm13926.pem"
```

## Gateway configuration and multiple servers

The gateway keeps a server catalog in `~/.config/vasp-remote-agent/config.json` on Vlab. Every entry is non-secret directory data only:

```json
{
  "servers": {
    "cl9": {"target": "wuhong@114.214.201.44", "port": 22, "remote_root": "/public/home/wuhong", "persist": "8h"},
    "alt": {"target": "user@example.edu", "port": 22, "remote_root": "/home/user", "persist": "8h"}
  },
  "default_server": "cl9"
}
```

- Server names match `^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$`; targets are `user@host`; roots are absolute paths without `.`/`..`; up to 32 servers.
- An old flat file such as `{"remote_root": "/public/home/wuhong"}` is migrated automatically to `servers.cl9` (merged over the defaults) and written back atomically on first run.
- Passwords and TOTP seeds must never be added to this file; they are only entered interactively at connect time.

The default entry (without `--server`) is:

- target: `wuhong@114.214.201.44`
- port: `22`
- remote root: `/public/home/wuhong`
- connection persistence: `8h`

Manage the catalog through the controller (each SSH connection is a per-server master socket):

```powershell
./scripts/vasp-agent.ps1 servers
./scripts/vasp-agent.ps1 server-add alt -ServerTarget user@example.edu -ServerPort 22 -ServerRoot /home/user -ServerPersist 8h
./scripts/vasp-agent.ps1 server-set-default alt
./scripts/vasp-agent.ps1 status -ServerName alt
./scripts/vasp-agent.ps1 connect -ServerName alt   # interactive password + TOTP
./scripts/vasp-agent.ps1 server-remove alt         # refuses default/connected/last servers
```

## Optional DeepSeek API

DeepSeek can choose among the restricted tools, while this computer performs SSH operations. Set secrets only in the current PowerShell session:

```powershell
$env:DEEPSEEK_API_KEY = "your-api-key"
$env:VLAB_IDENTITY_FILE = "$HOME/.ssh/vlab-vm13926.pem"
python ./scripts/deepseek-agent.py
```

For a single request:

```powershell
python ./scripts/deepseek-agent.py "检查我的任务队列"
```

The key is read from `DEEPSEEK_API_KEY` and is never written by the agent. DeepSeek receives the user's prompts, tool schemas, and tool results. File content is sent only after a local confirmation prompt. Job submission and cancellation also require local confirmation.

## Visual VASPilot interface

Start the local interface from the skill directory:

```powershell
./scripts/start-ui.ps1
```

On Windows, double-click `启动 VASPilot.cmd` for one-click startup.

It opens `http://127.0.0.1:8765` in the default browser. Use **设置** to enter the Vlab PEM path and DeepSeek API Key. The interface binds only to localhost, does not persist the API Key, and opens a separate terminal for cl9 password plus TOTP authentication.
