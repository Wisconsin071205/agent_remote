# Operations

All examples assume `VLAB_IDENTITY_FILE` is set.

Every operation except the catalog ones accepts `-ServerName` to select a server from the gateway catalog; without it the default server is used.

| Operation | Example | Effect |
|---|---|---|
| List servers | `./scripts/vasp-agent.ps1 servers` | Catalog with per-server connection state (JSON) |
| Add server | `./scripts/vasp-agent.ps1 server-add alt -ServerTarget user@example.edu -ServerPort 22 -ServerRoot /home/user` | Adds a catalog entry |
| Set default | `./scripts/vasp-agent.ps1 server-set-default alt` | Default for operations without `-ServerName` |
| Edit server | `./scripts/vasp-agent.ps1 server-edit alt -ServerTarget user@example.edu -ServerPort 2222 -ServerRoot /home/user -ServerPersist 4h` | All fields optional; target/port changes require a disconnect; root/persist may change while connected |
| Remove server | `./scripts/vasp-agent.ps1 server-remove alt` | Refuses the default, a connected, or the last server |
| Connection status | `./scripts/vasp-agent.ps1 status` | Read-only |
| Connect | `./scripts/vasp-agent.ps1 connect -ServerName alt` | Interactive SSH password + TOTP authentication |
| Disconnect | `./scripts/vasp-agent.ps1 disconnect -ServerName alt` | Closes the reusable master connection |
| Identity | `./scripts/vasp-agent.ps1 whoami` | Shows host, user, home, and directory |
| Queue | `./scripts/vasp-agent.ps1 jobs` | Runs `squeue` for the current user |
| Recent history | `./scripts/vasp-agent.ps1 recent` | Runs `sacct` from today |
| Inspect VASP calculation | `./scripts/vasp-agent.ps1 vasp-inspect -RemotePath /public/home/wuhong/x` | Structured inputs, structure, progress, completion, and errors |
| Validate VASP inputs | `./scripts/vasp-agent.ps1 vasp-validate -RemotePath /public/home/wuhong/x` | Checks required inputs and basic consistency |
| VASP progress | `./scripts/vasp-agent.ps1 vasp-progress -RemotePath /public/home/wuhong/x` | Compact convergence/progress summary |
| Read file | `./scripts/vasp-agent.ps1 read -RemotePath /public/home/wuhong/x/OUTCAR` | Prints a text file, capped at 2 MiB |
| Tail file | `./scripts/vasp-agent.ps1 tail -RemotePath /public/home/wuhong/x/OUTCAR -Lines 80` | Prints final lines |
| List directory | `./scripts/vasp-agent.ps1 list -RemotePath /public/home/wuhong/x` | Runs `ls -la` on the remote directory |
| Create directory | `./scripts/vasp-agent.ps1 mkdir -RemotePath /public/home/wuhong/x/new` | Runs `mkdir -p` on the remote directory |
| Copy | `./scripts/vasp-agent.ps1 copy -RemotePath /public/home/wuhong/x -DestinationPath /public/home/wuhong/y` | Recursively copies a remote path |
| Move | `./scripts/vasp-agent.ps1 move -RemotePath /public/home/wuhong/x -DestinationPath /public/home/wuhong/y` | Moves or renames a remote path |
| Remove | `./scripts/vasp-agent.ps1 remove -RemotePath /public/home/wuhong/x` | Permanently deletes a remote file or directory |
| Submit | `./scripts/vasp-agent.ps1 submit -RemotePath /public/home/wuhong/x -JobScript job.slurm` | Runs `sbatch` |
| Cancel | `./scripts/vasp-agent.ps1 cancel -JobId 123 -ConfirmJobId 123` | Runs `scancel` |
| Diagnostic | `./scripts/vasp-agent.ps1 diagnostic -Diagnostic disk` | Approved read-only command |
| Upload | `./scripts/vasp-agent.ps1 upload -LocalPath ./INCAR -RemotePath /public/home/wuhong/x/INCAR` | Copies through Vlab |
| Download | `./scripts/vasp-agent.ps1 download -RemotePath /public/home/wuhong/x/OUTCAR -LocalPath ./OUTCAR` | Copies through Vlab |

For a natural-language DeepSeek session, set `DEEPSEEK_API_KEY` and `VLAB_IDENTITY_FILE`, then run `python ./scripts/deepseek-agent.py`. The DeepSeek adapter exposes the same operations; every remote write (mkdir, copy, move, remove, upload, download, submit, cancel) requires a local approval prompt.

Approved diagnostics are `hostname`, `pwd`, `disk`, `quota`, `partitions`, and `modules`.

Remote paths must be absolute, contain no whitespace or shell metacharacters, and remain below the active server's configured remote root (each server keeps its own root). This intentionally favors predictable VASP directory names. The root itself can never be copied, moved, or removed.

## Troubleshooting

- **Vlab resolves to `198.18.x.x`:** disable proxy TUN/Fake-IP mode, flush DNS, and retry.
- **Vlab authentication fails:** regenerate/download the Vlab PEM key and rerun the installer.
- **A server is disconnected:** run `connect -ServerName <name>` and complete password plus current TOTP.
- **`servers` shows no entries after upgrading:** the old flat `config.json` migrates automatically on first run; verify with `./scripts/vasp-agent.ps1 servers`.
- **Host key changed:** do not delete the warning blindly. Verify the new fingerprint with the administrator.
- **The connect window returns to a PowerShell prompt:** this is normal after `cl9 connected`; the short Vlab control session closes while the reusable cl9 master connection remains active.
- **DeepSeek reports insufficient tool messages:** start a new chat. VASPilot repairs interrupted multi-tool batches before the next API request; update/restart the interface if using an older running instance.
- **`squeue` or `sbatch` not found:** the cluster may use another scheduler or require a login profile; update the gateway only after confirming the correct commands.
