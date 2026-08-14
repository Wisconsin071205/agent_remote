---
name: vasp-remote-agent
description: Safely operate VASP and Slurm workloads on registered HPC servers through the USTC Vlab Ubuntu gateway. Use when Codex needs to check or establish SSH connections, inspect queues and job history, submit or cancel jobs, read or tail calculation outputs, run approved diagnostics, or transfer calculation files between Windows and remote servers without storing passwords or TOTP secrets.
---

# VASP Remote Agent

Use the bundled Windows controller to operate registered servers through Vlab. Keep passwords and TOTP seeds out of files and prompts. The server catalog (names, targets, roots) lives in `~/.config/vasp-remote-agent/config.json` on Vlab; passwords and TOTP codes are only ever entered interactively at connect time.

## Prerequisites

Read [references/setup.md](references/setup.md) when configuring a new computer or repairing connectivity. Require a Vlab PEM key and a manually authenticated reusable SSH connection from Vlab to cl9.

## Run operations

Invoke `scripts/vasp-agent.ps1` from PowerShell. Pass the Vlab PEM path with `-IdentityFile` or set `VLAB_IDENTITY_FILE`.

```powershell
./scripts/vasp-agent.ps1 servers                 # list catalog + per-server state
./scripts/vasp-agent.ps1 status                  # default server
./scripts/vasp-agent.ps1 status -ServerName alt  # named server
./scripts/vasp-agent.ps1 connect -ServerName alt # interactive password + TOTP
./scripts/vasp-agent.ps1 jobs
./scripts/vasp-agent.ps1 recent
./scripts/vasp-agent.ps1 vasp-validate -RemotePath calc
./scripts/vasp-agent.ps1 vasp-progress -RemotePath calc
./scripts/vasp-agent.ps1 vasp-inspect -RemotePath calc
./scripts/vasp-agent.ps1 read -RemotePath calc/OUTCAR
./scripts/vasp-agent.ps1 tail -RemotePath calc/OUTCAR -Lines 80
./scripts/vasp-agent.ps1 submit -RemotePath calc -JobScript job.slurm
```

Every operation accepts `-ServerName`; without it the default server is used. See [references/operations.md](references/operations.md) for the catalog operations (`server-add`, `server-edit`, `server-remove`, `server-set-default`). `server-edit` changes any subset of target/port/root/persist; changing target or port requires the server to be disconnected first.

Read [references/operations.md](references/operations.md) for all supported operations and troubleshooting.
Read [references/vasp-domain.md](references/vasp-domain.md) before validating, submitting, diagnosing, or interpreting a VASP calculation.
Read [references/training.md](references/training.md) when collecting expert-reviewed examples for later fine-tuning or evaluation. Use `scripts/training-record.py` to redact and serialize records.

## Use DeepSeek

Read the **Optional DeepSeek API** section in [references/setup.md](references/setup.md). Run `scripts/deepseek-agent.py`; it uses official function calling to expose only the same restricted operations. Keep the API key in `DEEPSEEK_API_KEY`, never in a file.

## Use the visual interface

Run `scripts/start-ui.ps1` to open the local VASPilot interface. Configure the Vlab PEM path and DeepSeek API Key in its settings panel. The API Key remains in process memory only. Use the interface for chat, connection status, queues, preflight checks, progress inspection, and explicit approvals.

## Safety policy

- Prefer `status`, `jobs`, `recent`, `read`, `tail`, and approved diagnostics for inspection.
- Prefer `vasp-validate`, `vasp-progress`, and `vasp-inspect` over generic file reads for VASP work.
- Read input files before submitting a calculation.
- Require the user to approve every remote write: `submit`, `cancel`, `mkdir`, `copy`, `move`, `remove`, `upload`, and `download`.
- For cancellation, pass both `-JobId ID` and `-ConfirmJobId ID`.
- Never place a password, TOTP seed, current OTP, or private-key content in a command, project file, log, or chat response.
- Never weaken SSH host-key checking. Stop and report a changed host key.
- Do not bypass the controller to run arbitrary remote shell commands.
- Keep all remote file operations under the active server's `remote_root`; the gateway enforces containment per server and refuses copy/move/remove on the root itself.
- When the UI shows multiple servers, the model acts only on the active server named in the per-turn server context; it has no tool to switch servers.

## Deterministic workflow tools

Four deterministic, offline-first tools sit under `scripts/` and follow `spec/workflows.md`:

| Tool | Purpose | Safety property |
|---|---|---|
| `vasp_parse.py DIR` | Directory -> CalculationManifest JSON (hashes, energies, convergence, errors) | Deterministic: same directory parses byte-identically |
| `slurm_adapter.py submit/query/history` | sbatch --parsable, squeue/sacct JSON with text fallback, normalized scheduler states | `selftest` runs 16 offline checks; scheduler state is never science state |
| `workflow_prepare.py relax-static` | Generate 00_relax + 01_static trees, run.slurm, plan.json | Whitelisted INCAR keys, modification audit, idempotent, refuses to overwrite differing files, `--dry-run` |
| `custodian_detect.py DIR` | Detect BRMIX/ZBRENT/EDDDAV/NELM/walltime/ZPOTRF failures | Never modifies files; `--propose` only writes a .proposed.patch preview |
| `workflow_state.py init/status/advance DIR` | Explicit PREPARED→REVIEWED state machine with append-only audit | Illegal transitions rejected; FAILED/TIMEOUT/REJECTED are terminal |
| `apply_patch.py DIR --patch P` | L2: apply an APPROVED patch to whitelisted files only | Backup + JSONL audit + `--dry-run`; non-whitelist targets rejected |
| `agent_tools.py dispatch --name T --args JSON` | The only ten tools a central model may call | `schemas` prints OpenAI-format tool list; `selftest` runs offline |

Prefer these over writing new shell scripts. Do not apply suggested patches without human approval (L1 detect / L2 propose only; L3 auto-apply whitelist is empty).

## Interpret results

The controller returns the remote tool output and propagates failures. A disconnected master connection is expected after its persistence period; ask the user to run `connect -ServerName <name>` and complete password plus current six-digit OTP interactively.
