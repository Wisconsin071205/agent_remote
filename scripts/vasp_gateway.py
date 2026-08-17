#!/usr/bin/env python3
"""Restricted server operations executed on the trusted Vlab gateway."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone


APP_DIR = Path.home() / ".config" / "vasp-remote-agent"
CACHE_DIR = Path.home() / ".cache" / "vasp-remote-agent"
CONFIG_PATH = APP_DIR / "config.json"
AUDIT_PATH = CACHE_DIR / "audit.jsonl"
LOCK_PATH = CACHE_DIR / "config.lock"
DEFAULTS = {
    "target": "user@192.0.2.1",
    "port": 22,
    "remote_root": "/home/user",
    "persist": "8h",
    "scheduler": "auto",
}
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
TARGET_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$")
PERSIST_RE = re.compile(r"^(yes|no|[0-9]+[smhdw]?)$")
MAX_SERVERS = 32
SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/+@=-]+$")
SAFE_JOB_SCRIPT = re.compile(r"^[A-Za-z0-9._+-]+$")
SAFE_JOB_ID = re.compile(r"^[0-9]+(?:_[0-9]+)?(?:\.[A-Za-z0-9.-]+)?$")


def audit(operation: str, outcome: str, detail: str = "", server: str = "") -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "outcome": outcome,
        "detail": detail[:300],
    }
    if server:
        record["server"] = server
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.chmod(AUDIT_PATH, 0o600)

VASP_INSPECTOR = r'''
import json, os, re, sys
from pathlib import Path

directory = Path(sys.argv[1])
mode = sys.argv[2]
result = {"schema_version": 1, "directory": str(directory), "mode": mode, "issues": [], "warnings": []}

def text(name, limit=4_000_000):
    path = directory / name
    if not path.is_file(): return ""
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        data = data[-limit:]
    return data.decode("utf-8", errors="replace")

def incar_values(raw):
    values = {}
    for line in raw.splitlines():
        line = line.split("!", 1)[0].split("#", 1)[0]
        for item in line.split(";"):
            if "=" in item:
                key, value = item.split("=", 1)
                key = key.strip().upper()
                if re.fullmatch(r"[A-Z][A-Z0-9_]*", key): values[key] = value.strip()
    return values

standard = ["INCAR", "POSCAR", "KPOINTS", "POTCAR", "OUTCAR", "OSZICAR", "CONTCAR", "vasprun.xml", "WAVECAR", "CHGCAR"]
result["files"] = {}
for name in standard:
    path = directory / name
    result["files"][name] = {"exists": path.is_file(), "size": path.stat().st_size if path.is_file() else 0}

incar_raw = text("INCAR", 500_000)
incar = incar_values(incar_raw)
safe_incar_keys = ["SYSTEM", "ENCUT", "EDIFF", "EDIFFG", "IBRION", "ISIF", "NSW", "ISMEAR", "SIGMA", "ISPIN", "MAGMOM", "LREAL", "PREC", "ALGO", "NELM", "ISYM", "LCHARG", "LWAVE", "LASPH", "LDAU", "IVDW", "GGA", "METAGGA", "LORBIT", "ICHARG", "EMIN", "EMAX", "LAECHG", "NEDOS"]
result["incar"] = {key: incar[key] for key in safe_incar_keys if key in incar}

for required in ["INCAR", "POSCAR", "POTCAR"]:
    if not result["files"][required]["exists"]: result["issues"].append({"code": f"missing_{required.lower()}", "severity": "error", "message": f"{required} is missing"})
if not result["files"]["KPOINTS"]["exists"] and "KSPACING" not in incar:
    result["issues"].append({"code": "missing_kpoints", "severity": "error", "message": "KPOINTS is missing and KSPACING is not set"})

poscar = text("POSCAR", 2_000_000).splitlines()
if poscar:
    result["structure"] = {"title": poscar[0].strip()[:200]}
    try:
        species_line = poscar[5].split()
        counts_line = poscar[6].split()
        if species_line and all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]*", x) for x in species_line) and all(x.isdigit() for x in counts_line):
            result["structure"].update({"species": species_line, "counts": [int(x) for x in counts_line], "atoms": sum(int(x) for x in counts_line)})
        elif species_line and all(x.isdigit() for x in species_line):
            result["structure"].update({"counts": [int(x) for x in species_line], "atoms": sum(int(x) for x in species_line)})
    except (IndexError, ValueError):
        result["issues"].append({"code": "poscar_parse", "severity": "warning", "message": "Could not parse POSCAR species/count lines"})

potcar = text("POTCAR", 8_000_000)
titles = re.findall(r"^\s*TITEL\s*=\s*(.+)$", potcar, re.M)
if titles: result["potcar_titles"] = [x.strip()[:160] for x in titles]
if result.get("structure", {}).get("species") and titles and len(result["structure"]["species"]) != len(titles):
    result["issues"].append({"code": "potcar_count_mismatch", "severity": "error", "message": "POSCAR species count differs from POTCAR dataset count"})

kpoints = text("KPOINTS", 100_000).splitlines()
if kpoints:
    result["kpoints_preview"] = [line.strip()[:160] for line in kpoints[:5]]

jobs = []
for pattern in ("*.slurm", "*.sbatch", "*.pbs", "job*", "sub*"):
    for path in directory.glob(pattern):
        if path.is_file() and path.name not in jobs and re.fullmatch(r"[A-Za-z0-9._+-]+", path.name): jobs.append(path.name)
result["job_scripts"] = sorted(jobs)[:30]
if not jobs: result["warnings"].append({"code": "no_job_script", "message": "No common Slurm/PBS job script filename was found"})

oszicar = text("OSZICAR")
ionic = re.findall(r"^\s*(\d+)\s+F=\s*([-+0-9.Ee]+)\s+E0=\s*([-+0-9.Ee]+)\s+d\s*E\s*=\s*([-+0-9.Ee]+)", oszicar, re.M)
electronic = re.findall(r"^\s*(DAV|RMM|CG)\s*:\s*(\d+)\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)", oszicar, re.M)
progress = {"ionic_steps": len(ionic)}
if ionic:
    step, fval, e0, de = ionic[-1]
    progress["last_ionic"] = {"step": int(step), "free_energy_ev": float(fval), "energy_zero_ev": float(e0), "delta_energy_ev": float(de)}
if electronic:
    algo, step, energy, de = electronic[-1]
    progress["last_electronic"] = {"algorithm": algo, "step": int(step), "energy_ev": float(energy), "delta_energy_ev": float(de)}

outcar = text("OUTCAR")
progress["ionic_converged"] = "reached required accuracy - stopping structural energy minimisation" in outcar
progress["completed"] = "Voluntary context switches" in outcar or "General timing and accounting informations for this job" in outcar
elapsed = re.findall(r"Elapsed time \(sec\):\s*([-+0-9.Ee]+)", outcar)
if elapsed:
    try: progress["elapsed_seconds"] = float(elapsed[-1])
    except ValueError: pass
mag = re.findall(r"number of electron\s+[-+0-9.Ee]+\s+magnetization\s+([-+0-9.Ee]+)", outcar)
if mag:
    try: progress["total_magnetization"] = float(mag[-1])
    except ValueError: pass

nelm = 60
try: nelm = int(float(incar.get("NELM", "60").split()[0]))
except (ValueError, IndexError): pass
if electronic:
    progress["electronic_reached_nelm"] = int(electronic[-1][1]) >= nelm
    if progress["electronic_reached_nelm"]:
        result["issues"].append({"code": "electronic_nelm", "severity": "warning", "message": f"Last electronic cycle reached NELM={nelm}"})

patterns = {
    "zbrent": r"ZBRENT:", "brmix": r"BRMIX:", "edddav": r"EDDDAV:", "zhegv": r"ZHEGV",
    "posmap": r"POSMAP", "very_bad_news": r"VERY BAD NEWS", "internal_error": r"internal error",
    "subspace_rotation": r"Sub-Space-Matrix is not hermitian", "charge_mismatch": r"inconsistent charge density",
    "nelm": r"WARNING in EDDRMM",
}
errors = []
combined = outcar + "\n" + oszicar
for code, pattern in patterns.items():
    count = len(re.findall(pattern, combined, re.I))
    if count: errors.append({"code": code, "count": count})
result["errors"] = errors
result["progress"] = progress

if mode == "validate":
    result.pop("progress", None); result.pop("errors", None)
elif mode == "progress":
    keep = {"schema_version", "directory", "mode", "issues", "warnings", "files", "progress", "errors"}
    result = {k: v for k, v in result.items() if k in keep}
print(json.dumps(result, ensure_ascii=False, indent=2))
'''


def validate_server(name: str, entry: dict) -> None:
    if not isinstance(entry, dict):
        raise ValueError(f"server {name}: entry must be an object")
    unknown = set(entry) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"server {name}: unsupported keys: {', '.join(sorted(unknown))}")
    if not SERVER_NAME_RE.fullmatch(name):
        raise ValueError(f"server {name}: name may contain only letters, digits, dot, dash, underscore")
    target = entry.get("target")
    if not isinstance(target, str) or not TARGET_RE.fullmatch(target):
        raise ValueError(f"server {name}: target must look like user@host")
    port = entry.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"server {name}: port must be an integer from 1 to 65535")
    scheduler = entry.get("scheduler", "auto")
    if scheduler not in ("auto", "slurm", "pbs"):
        raise ValueError(f"server {name}: scheduler must be auto, slurm or pbs")
    root = entry.get("remote_root")
    # Empty remote_root means "use the login user's home directory", probed
    # lazily from the live connection; it is never a full-filesystem boundary.
    if not isinstance(root, str):
        raise ValueError(f"server {name}: invalid remote_root")
    if root and (not SAFE_REMOTE_PATH.fullmatch(root) or ".." in PurePosixPath(root).parts or "." in PurePosixPath(root).parts):
        raise ValueError(f"server {name}: invalid remote_root")
    persist = entry.get("persist")
    if not isinstance(persist, str) or not PERSIST_RE.fullmatch(persist):
        raise ValueError(f"server {name}: invalid persist value")


def validate_catalog(config: dict) -> None:
    servers = config.get("servers")
    if not isinstance(servers, dict) or not servers:
        raise ValueError("config must contain a non-empty servers object")
    if len(servers) > MAX_SERVERS:
        raise ValueError(f"too many servers (max {MAX_SERVERS})")
    for name, entry in servers.items():
        validate_server(name, entry)
    default = config.get("default_server")
    if not isinstance(default, str) or default not in servers:
        raise ValueError("default_server must name an existing server")


def config_lock():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w", encoding="utf-8")
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except ImportError:
        pass
    return handle


def atomic_write_config(config: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name("config.json.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"servers": {"cl9": dict(DEFAULTS)}, "default_server": "cl9"}
    with CONFIG_PATH.open(encoding="utf-8-sig") as handle:
        custom = json.load(handle)
    if not isinstance(custom, dict):
        raise ValueError("config must be a JSON object")
    if "servers" in custom or "default_server" in custom:
        unknown = set(custom) - {"servers", "default_server"}
        if unknown:
            raise ValueError(f"Unsupported config keys: {', '.join(sorted(unknown))}")
        validate_catalog(custom)
        return custom
    unknown = set(custom) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"Unsupported config keys: {', '.join(sorted(unknown))}")
    merged = {**DEFAULTS, **custom}
    validate_server("cl9", merged)
    config = {"servers": {"cl9": merged}, "default_server": "cl9"}
    with config_lock():
        atomic_write_config(config)
    audit("config-migrate", "ok", "legacy config migrated to servers schema", "cl9")
    return config


def resolve_server(name: str | None) -> tuple[str, dict]:
    chosen = name or CFG["default_server"]
    entry = CFG["servers"].get(chosen)
    if entry is None:
        raise ValueError(f"unknown server: {chosen}")
    return chosen, entry


def socket_path(name: str) -> Path:
    return CACHE_DIR / f"{name}.sock"


CFG = load_config()


def base_ssh(name: str) -> list[str]:
    _, entry = resolve_server(name)
    return [
        "ssh",
        "-p", str(entry["port"]),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"ControlPath={socket_path(name)}",
    ]


def connected(name: str, timeout: int = 4) -> bool:
    if not socket_path(name).exists():
        return False
    _, entry = resolve_server(name)
    result = subprocess.run(
        base_ssh(name) + ["-O", "check", entry["target"]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    return result.returncode == 0


def require_connection(name: str) -> None:
    if not connected(name):
        raise RuntimeError(f"{name} is disconnected; run the connect operation interactively")


def remote(name: str, command: str, *, capture: bool = False) -> subprocess.CompletedProcess:
    require_connection(name)
    _, entry = resolve_server(name)
    return subprocess.run(
        base_ssh(name) + [entry["target"], command],
        text=True,
        capture_output=capture,
        timeout=180,
        check=False,
    )


# Home directories probed from live connections for servers whose remote_root
# is empty; the login user's home never changes, so the cache outlives disconnects.
HOME_CACHE: dict[str, str] = {}


def effective_root(name: str, entry: dict) -> PurePosixPath:
    root = entry.get("remote_root") or ""
    if root:
        return PurePosixPath(root)
    home = HOME_CACHE.get(name)
    if home is None:
        result = remote(name, "echo $HOME", capture=True)
        out = (result.stdout or "").strip()
        if result.returncode != 0 or not out.startswith("/"):
            raise RuntimeError(f"cannot determine the home directory of {name}; connect it first")
        home = out
        HOME_CACHE[name] = home
    return PurePosixPath(home)


def validated_remote_path(raw: str, entry: dict, name: str) -> str:
    if not SAFE_REMOTE_PATH.fullmatch(raw):
        raise ValueError("remote path contains unsupported characters")
    path = PurePosixPath(raw)
    if ".." in path.parts or "." in path.parts:
        raise ValueError("remote path cannot contain dot traversal segments")
    root = effective_root(name, entry)
    if path != root and root not in path.parents:
        raise ValueError(f"remote path must remain under {root}")
    return str(path)


def validated_stage_path(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    if path.parent != Path("/tmp") or not path.name.startswith("vasp-remote-agent-"):
        raise ValueError("staging path must be a vasp-remote-agent file directly under /tmp")
    return path


def emit(result: subprocess.CompletedProcess) -> int:
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


def do_connect(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    if connected(name):
        print(f"{name} connected")
        return 0
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sock = socket_path(name)
    if sock.exists():
        sock.unlink()
    command = [
        "ssh", "-M", "-S", str(sock),
        "-o", "ControlMaster=yes",
        "-o", f"ControlPersist={entry['persist']}",
        "-o", "StrictHostKeyChecking=ask",
        "-o", "PreferredAuthentications=keyboard-interactive,password",
        "-p", str(entry["port"]), "-fN", entry["target"],
    ]
    result = subprocess.run(command, check=False)
    ok = result.returncode == 0 and connected(name)
    audit("connect", "ok" if ok else "failed", server=name)
    if not ok:
        print(f"{name} connection was not established", file=sys.stderr)
        return result.returncode or 1
    print(f"{name} connected")
    return 0


def do_disconnect(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    if not connected(name):
        print(f"{name} already disconnected")
        return 0
    result = subprocess.run(base_ssh(name) + ["-O", "exit", entry["target"]], check=False)
    audit("disconnect", "ok" if result.returncode == 0 else "failed", server=name)
    return result.returncode


def do_simple(operation: str, command: str, server: str | None) -> int:
    name, _ = resolve_server(server)
    result = remote(name, command)
    audit(operation, "ok" if result.returncode == 0 else "failed", server=name)
    return result.returncode


def do_read(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    command = f"test $(wc -c < {shlex.quote(path)}) -le 2097152 && cat -- {shlex.quote(path)}"
    result = remote(name, command)
    audit("read", "ok" if result.returncode == 0 else "failed", path, name)
    return result.returncode


def do_tail(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    if not 1 <= args.lines <= 2000:
        raise ValueError("lines must be from 1 to 2000")
    result = remote(name, f"tail -n {args.lines} -- {shlex.quote(path)}")
    audit("tail", "ok" if result.returncode == 0 else "failed", path, name)
    return result.returncode


def do_list(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    result = remote(name, f"ls -la -- {shlex.quote(path)}")
    audit("list", "ok" if result.returncode == 0 else "failed", path, name)
    return result.returncode


def do_mkdir(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    result = remote(name, f"mkdir -p -- {shlex.quote(path)}")
    audit("mkdir", "ok" if result.returncode == 0 else "failed", path, name)
    return result.returncode


def require_not_root(name: str, path: str, entry: dict) -> None:
    # Compare against the EFFECTIVE root (which resolves an empty remote_root
    # to the login home via a live connection). Comparing the raw remote_root
    # string leaves the home-directory boundary unprotected: PurePosixPath("")
    # equals "." and never matches an absolute home path.
    if PurePosixPath(path) == effective_root(name, entry):
        raise ValueError("operation is not allowed on the remote root itself")


def do_copy(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    source = validated_remote_path(args.source, entry, name)
    destination = validated_remote_path(args.destination, entry, name)
    require_not_root(name, source, entry)
    require_not_root(name, destination, entry)
    result = remote(name, f"cp -r -- {shlex.quote(source)} {shlex.quote(destination)}")
    audit("copy", "ok" if result.returncode == 0 else "failed", f"{source} -> {destination}", name)
    return result.returncode


def do_move(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    source = validated_remote_path(args.source, entry, name)
    destination = validated_remote_path(args.destination, entry, name)
    require_not_root(name, source, entry)
    require_not_root(name, destination, entry)
    result = remote(name, f"mv -- {shlex.quote(source)} {shlex.quote(destination)}")
    audit("move", "ok" if result.returncode == 0 else "failed", f"{source} -> {destination}", name)
    return result.returncode


TRASH_DIR_NAME = ".vaspilot-trash"


def trash_root_for(name: str, entry: dict) -> str:
    """Quarantine area living directly under the server allowed root."""
    return str(effective_root(name, entry) / TRASH_DIR_NAME)


def do_remove(args: argparse.Namespace) -> int:
    """Move a remote path into the timestamped quarantine area (recoverable).

    Nothing is deleted here; permanent deletion is a separate "purge"
    command that only accepts paths already inside the quarantine area.
    """
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    require_not_root(name, path, entry)
    trash_root = trash_root_for(name, entry)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    destination = f"{trash_root}/{stamp}_{PurePosixPath(path).name}"
    command = f"mkdir -p -- {shlex.quote(trash_root)} && mv -- {shlex.quote(path)} {shlex.quote(destination)}"
    result = remote(name, command)
    audit("trash", "ok" if result.returncode == 0 else "failed", f"{path} -> {destination}", name)
    if result.returncode == 0:
        print(f"moved to quarantine: {destination}")
        print("permanent deletion requires a separate purge command")
    return result.returncode


def do_purge(args: argparse.Namespace) -> int:
    """Permanently delete an entry that already lives inside the quarantine area.

    Requires the path to be typed twice (path + confirm_path) and rejects
    anything outside the quarantine area, including the area itself.
    """
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    if args.confirm_path != args.path:
        raise ValueError("confirmation path must exactly match the requested path")
    trash_root = PurePosixPath(trash_root_for(name, entry))
    target = PurePosixPath(path)
    if target == trash_root or trash_root not in target.parents:
        raise ValueError("purge only accepts paths inside the quarantine area")
    result = remote(name, f"rm -r -- {shlex.quote(path)}")
    audit("purge", "ok" if result.returncode == 0 else "failed", path, name)
    if result.returncode == 0:
        print(f"permanently deleted: {path}")
    return result.returncode


def do_trash_list(args: argparse.Namespace) -> int:
    """List everything currently held in the quarantine area."""
    name, entry = resolve_server(args.server)
    trash_root = trash_root_for(name, entry)
    result = remote(name, f"ls -la -- {shlex.quote(trash_root)}")
    audit("trash-list", "ok" if result.returncode == 0 else "failed", trash_root, name)
    return result.returncode


# ---------------------------------------------------------------------------
# Scheduler abstraction: Slurm and PBS are both supported. The catalog may pin
# a scheduler per server ("slurm" / "pbs"); the default "auto" probes the
# login shell once and caches the answer.
# ---------------------------------------------------------------------------
SCHEDULER_CACHE: dict[str, str] = {}


def scheduler_for(name: str, entry: dict) -> str:
    pinned = entry.get("scheduler", "auto")
    if pinned in ("slurm", "pbs"):
        return pinned
    cached = SCHEDULER_CACHE.get(name)
    if cached:
        return cached
    result = remote(
        name,
        "if command -v qsub >/dev/null 2>&1; then echo pbs; "
        "elif command -v sbatch >/dev/null 2>&1; then echo slurm; else echo unknown; fi",
        capture=True,
    )
    detected = (result.stdout or "").strip()
    if detected not in ("slurm", "pbs"):
        raise RuntimeError(f"cannot detect the scheduler on {name} (got {detected!r})")
    SCHEDULER_CACHE[name] = detected
    return detected


def do_jobs(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    scheduler = scheduler_for(name, entry)
    if scheduler == "pbs":
        command = 'qstat -u "$(id -un)"'
    else:
        command = 'squeue -u "$(id -un)" -o "%.18i %.9P %.30j %.8u %.2t %.10M %.6D %R"'
    result = remote(name, command)
    audit("jobs", "ok" if result.returncode == 0 else "failed", scheduler, name)
    return result.returncode


def do_recent(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    scheduler = scheduler_for(name, entry)
    if scheduler == "pbs":
        command = 'qstat -x -u "$(id -un)"'
    else:
        command = 'sacct -u "$(id -un)" --starttime today -X -o JobID,JobName%30,Partition,State,Elapsed,ExitCode'
    result = remote(name, command)
    audit("recent", "ok" if result.returncode == 0 else "failed", scheduler, name)
    return result.returncode


def do_submit(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    directory = validated_remote_path(args.directory, entry, name)
    if not SAFE_JOB_SCRIPT.fullmatch(args.script):
        raise ValueError("job script must be a simple filename")
    scheduler = scheduler_for(name, entry)
    if scheduler == "pbs":
        command = f"cd -- {shlex.quote(directory)} && qsub -- {shlex.quote(args.script)}"
    else:
        command = f"cd -- {shlex.quote(directory)} && sbatch -- {shlex.quote(args.script)}"
    result = remote(name, command)
    audit("submit", "ok" if result.returncode == 0 else "failed", f"{directory}/{args.script}", name)
    return result.returncode


def do_cancel(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    if not SAFE_JOB_ID.fullmatch(args.job_id):
        raise ValueError("invalid job id")
    if args.confirm_job_id != args.job_id:
        raise ValueError("confirmation job id must exactly match the requested job id")
    scheduler = scheduler_for(name, entry)
    command = f"qdel -- {shlex.quote(args.job_id)}" if scheduler == "pbs" else f"scancel -- {shlex.quote(args.job_id)}"
    result = remote(name, command)
    audit("cancel", "ok" if result.returncode == 0 else "failed", args.job_id, name)
    return result.returncode


# Analysis commands the gateway may execute inside a calculation directory.
# Only these PREFIXES are accepted; anything else is rejected outright. This
# gives the agent real post-processing power (plot DOS with python/gnuplot,
# quick math with awk/bc) without granting a general-purpose shell.
RUN_ALLOWED_PREFIXES = (
    "python3", "python", "gnuplot", "bash", "sh", "awk", "bc", "cat", "grep",
    "tail", "head", "wc", "sort", "uniq", "paste", "module",
)
RUN_MAX_SECONDS = 300


def do_run(args: argparse.Namespace) -> int:
    """Execute one whitelisted analysis command inside a remote directory."""
    name, entry = resolve_server(args.server)
    directory = validated_remote_path(args.directory, entry, name)
    raw = (args.command or "").replace("\r", "").strip()
    if not raw:
        raise ValueError("run requires -Command")
    if len(raw) > 2000:
        raise ValueError("command too long (max 2000 characters); for longer work upload a script file and run it with a short command")
    if any(ch in raw for ch in "\n;|&`$<>"):
        raise ValueError(
            "command chaining / shell metacharacters (; | & newline, $, backtick, < >) are not allowed; "
            "upload a script file and run it with a short command")
    first = raw.split()[0].split("/")[-1] if raw.split() else ""
    if first not in RUN_ALLOWED_PREFIXES:
        raise ValueError(
            f"command prefix {first!r} is not allowed; allowed prefixes: "
            + ", ".join(RUN_ALLOWED_PREFIXES))
    # Path escape via cd is blocked by the validation above (no ; & | quoting
    # games survive the whitelist check on the first token).
    command = f"cd -- {shlex.quote(directory)} && timeout {RUN_MAX_SECONDS} {raw}"
    result = remote(name, command, capture=True)
    emit(result)
    audit("run", "ok" if result.returncode == 0 else "failed",
          f"{directory}: {raw[:160]}", name)
    return result.returncode


def do_diagnostic(args: argparse.Namespace) -> int:
    if args.name == "scheduler":
        name, entry = resolve_server(args.server)
        scheduler = scheduler_for(name, entry)
        print(scheduler)
        audit("diagnostic", "ok", f"scheduler={scheduler}", name)
        return 0
    commands = {
        "hostname": "hostname -f",
        "pwd": "pwd",
        "disk": "df -h -- $HOME",
        "quota": "quota -s 2>/dev/null || true",
        "partitions": "sinfo -o '%P %a %l %D %t'",
        "modules": "bash -lc 'module -t avail 2>&1 | head -n 200'",
    }
    return do_simple("diagnostic", commands[args.name], args.server)


def do_vasp(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    directory = validated_remote_path(args.directory, entry, name)
    payload = base64.b64encode(VASP_INSPECTOR.encode("utf-8")).decode("ascii")
    bootstrap = "import base64;exec(base64.b64decode(" + repr(payload) + ").decode())"
    command = "python3 -c " + shlex.quote(bootstrap) + " " + shlex.quote(directory) + " " + shlex.quote(args.operation.replace("vasp-", ""))
    result = remote(name, command)
    audit(args.operation, "ok" if result.returncode == 0 else "failed", directory, name)
    return result.returncode


def transfer_args(name: str) -> list[str]:
    _, entry = resolve_server(name)
    return [
        "scp", "-q", "-P", str(entry["port"]),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"ControlPath={socket_path(name)}",
    ]


def do_upload(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    require_connection(name)
    stage = validated_stage_path(args.stage)
    destination = validated_remote_path(args.destination, entry, name)
    if not stage.is_file():
        raise ValueError("staged upload is not a regular file")
    result = subprocess.run(transfer_args(name) + [str(stage), f"{entry['target']}:{destination}"], check=False)
    audit("upload", "ok" if result.returncode == 0 else "failed", destination, name)
    return result.returncode


def do_download(args: argparse.Namespace) -> int:
    name, entry = resolve_server(args.server)
    require_connection(name)
    source = validated_remote_path(args.source, entry, name)
    stage = validated_stage_path(args.stage)
    result = subprocess.run(transfer_args(name) + [f"{entry['target']}:{source}", str(stage)], check=False)
    audit("download", "ok" if result.returncode == 0 else "failed", source, name)
    return result.returncode


# Server-to-server copies go through the gateway host's /tmp staging area:
# the gateway can reach every server via its ControlMaster socket, but the
# servers have no credentials for each other. Both legs reuse the exact
# transfer_args() connection profile, and the stage path obeys the same
# /tmp/vasp-remote-agent-* contract as upload/download.
TRANSFER_TIMEOUT = 1800  # generous for large calculations


def do_transfer(args: argparse.Namespace) -> int:
    from_name, from_entry = resolve_server(args.from_server)
    to_name, to_entry = resolve_server(args.to_server)
    if from_name == to_name:
        raise ValueError("source and destination must be different servers")
    require_connection(from_name)
    require_connection(to_name)
    from_path = validated_remote_path(args.from_path, from_entry, from_name)
    to_path = validated_remote_path(args.to_path, to_entry, to_name)
    require_not_root(from_name, from_path, from_entry)
    require_not_root(to_name, to_path, to_entry)
    stage = Path("/tmp") / f"vasp-remote-agent-transfer-{secrets.token_hex(6)}"
    detail = f"{from_path} ({from_name}) -> {to_path} ({to_name})"
    try:
        pull = subprocess.run(
            transfer_args(from_name) + ["-r", f"{from_entry['target']}:{from_path}", str(stage)],
            check=False, timeout=TRANSFER_TIMEOUT,
        )
        if pull.returncode != 0:
            raise RuntimeError(f"could not pull {from_path} from {from_name}")
        push = subprocess.run(
            transfer_args(to_name) + ["-r", str(stage), f"{to_entry['target']}:{to_path}"],
            check=False, timeout=TRANSFER_TIMEOUT,
        )
        if push.returncode != 0:
            raise RuntimeError(f"could not push to {to_path} on {to_name}")
    except (subprocess.TimeoutExpired, RuntimeError) as exc:
        audit("transfer", "failed", detail)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    audit("transfer", "ok", detail)
    print(f"transferred {from_path} from {from_name} to {to_path} on {to_name}")
    return 0


def do_servers(_: argparse.Namespace) -> int:
    def check(name: str) -> tuple[str, bool]:
        try:
            return name, connected(name, timeout=4)
        except (ValueError, subprocess.TimeoutExpired, OSError):
            return name, False
    names = list(CFG["servers"])
    with ThreadPoolExecutor(max_workers=min(16, len(names) or 1)) as pool:
        results = dict(pool.map(check, names))
    payload = {
        "default": CFG["default_server"],
        "servers": [
            {
                "name": name,
                "target": entry["target"],
                "port": entry["port"],
                "root": entry["remote_root"],
                "persist": entry["persist"],
                "scheduler": entry.get("scheduler", "auto"),
                "connected": results.get(name, False),
            }
            for name, entry in CFG["servers"].items()
        ],
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def do_server_add(args: argparse.Namespace) -> int:
    name = args.name
    if not SERVER_NAME_RE.fullmatch(name):
        raise ValueError("invalid server name")
    if name in CFG["servers"]:
        raise ValueError(f"server already exists: {name}")
    if socket_path(name).exists():
        probe = subprocess.run(
            ["ssh", "-p", str(args.port), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
             "-o", f"ControlPath={socket_path(name)}", "-O", "check", args.target],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=4, check=False,
        )
        if probe.returncode == 0:
            raise ValueError(f"disconnect {name} before re-pointing it to another host")
        socket_path(name).unlink()
    entry = {
        "target": args.target,
        "port": args.port,
        "remote_root": args.remote_root or "",
        "scheduler": getattr(args, "scheduler", "auto"),
        "persist": args.persist,
    }
    validate_server(name, entry)
    with config_lock():
        config = {"servers": {**CFG["servers"], name: entry}, "default_server": CFG["default_server"]}
        validate_catalog(config)
        atomic_write_config(config)
    audit("server-add", "ok", f"{name} {entry['target']} {entry['remote_root']}", name)
    print(f"added server {name}")
    return 0


def do_server_remove(args: argparse.Namespace) -> int:
    name = args.name
    if name not in CFG["servers"]:
        raise ValueError(f"unknown server: {name}")
    if name == CFG["default_server"]:
        raise ValueError("cannot remove the default server")
    if len(CFG["servers"]) == 1:
        raise ValueError("cannot remove the last server")
    if connected(name):
        raise ValueError(f"disconnect {name} before removing it")
    with config_lock():
        config = {"servers": {k: v for k, v in CFG["servers"].items() if k != name},
                  "default_server": CFG["default_server"]}
        validate_catalog(config)
        atomic_write_config(config)
    if socket_path(name).exists():
        socket_path(name).unlink()
    audit("server-remove", "ok", name, name)
    print(f"removed server {name}")
    return 0


def do_server_set_default(args: argparse.Namespace) -> int:
    name = args.name
    if name not in CFG["servers"]:
        raise ValueError(f"unknown server: {name}")
    with config_lock():
        config = {"servers": CFG["servers"], "default_server": name}
        validate_catalog(config)
        atomic_write_config(config)
    audit("server-set-default", "ok", name, name)
    print(f"default server is now {name}")
    return 0


def do_server_edit(args: argparse.Namespace) -> int:
    name = args.name
    if name not in CFG["servers"]:
        raise ValueError(f"unknown server: {name}")
    new_name = (args.new_name or "").strip() or name
    if new_name != name:
        if not SERVER_NAME_RE.fullmatch(new_name):
            raise ValueError("invalid server name")
        if new_name in CFG["servers"]:
            raise ValueError(f"server already exists: {new_name}")
        # The master socket is keyed by server name; renaming a live one would
        # strand the connection, so require a fresh connect.
        if connected(name):
            raise ValueError(f"disconnect {name} before renaming it")
    entry = dict(CFG["servers"][name])
    changed = []
    if args.target is not None:
        entry["target"] = args.target
        changed.append("target")
    if args.port is not None:
        entry["port"] = args.port
        changed.append("port")
    if args.remote_root is not None:
        entry["remote_root"] = args.remote_root
        changed.append("root")
    if args.persist is not None:
        entry["persist"] = args.persist
        changed.append("persist")
    if getattr(args, "scheduler", None) is not None:
        if args.scheduler not in ("auto", "slurm", "pbs"):
            raise ValueError("scheduler must be auto, slurm or pbs")
        entry["scheduler"] = args.scheduler
        changed.append("scheduler")
        SCHEDULER_CACHE.pop(name, None)
    if not changed and new_name == name:
        raise ValueError("nothing to change")
    # A live master socket routes every command to the old target; re-pointing
    # it must go through a fresh connection.
    if ("target" in changed or "port" in changed) and connected(name):
        raise ValueError(f"disconnect {name} before changing its target or port")
    validate_server(name, entry)
    with config_lock():
        servers = dict(CFG["servers"])
        if new_name != name:
            del servers[name]
        servers[new_name] = entry
        default_server = CFG["default_server"]
        if default_server == name:
            default_server = new_name
        config = {"servers": servers, "default_server": default_server}
        validate_catalog(config)
        atomic_write_config(config)
    audit("server-edit", "ok",
          f"{name}" + (f"->{new_name}" if new_name != name else "") + " " + " ".join(changed),
          new_name)
    label = f"{name}->{new_name}" if new_name != name else name
    print(f"updated server {label}: " + ", ".join(changed) or "(renamed)")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="vasp-remote-agent")
    sub = root.add_subparsers(dest="operation", required=True)
    server = argparse.ArgumentParser(add_help=False)
    server.add_argument("--server", default=None)

    sub.add_parser("status", parents=[server])
    sub.add_parser("connect", parents=[server]).set_defaults(handler=do_connect)
    sub.add_parser("disconnect", parents=[server]).set_defaults(handler=do_disconnect)
    sub.add_parser("whoami", parents=[server]).set_defaults(handler=lambda a: do_simple("whoami", "printf 'host=%s\\nuser=%s\\nhome=%s\\npwd=%s\\n' \"$(hostname -f)\" \"$(id -un)\" \"$HOME\" \"$PWD\"", a.server))
    sub.add_parser("jobs", parents=[server]).set_defaults(handler=do_jobs)
    sub.add_parser("recent", parents=[server]).set_defaults(handler=do_recent)
    sub.add_parser("servers").set_defaults(handler=do_servers)

    add = sub.add_parser("server-add")
    add.add_argument("name")
    add.add_argument("--target", required=True)
    add.add_argument("--port", type=int, required=True)
    add.add_argument("--root", default=None, dest="remote_root",
                     help="optional; when omitted the login user's home directory is the boundary")
    add.add_argument("--persist", default=DEFAULTS["persist"])
    add.add_argument("--scheduler", choices=["auto", "slurm", "pbs"], default="auto",
                     help="job scheduler on this server (auto = probe on first use)")
    add.set_defaults(handler=do_server_add)
    remove_cat = sub.add_parser("server-remove")
    remove_cat.add_argument("name")
    remove_cat.set_defaults(handler=do_server_remove)
    set_default = sub.add_parser("server-set-default")
    set_default.add_argument("name")
    set_default.set_defaults(handler=do_server_set_default)
    edit_cat = sub.add_parser("server-edit")
    edit_cat.add_argument("name")
    edit_cat.add_argument("--new-name", default=None)
    edit_cat.add_argument("--target", default=None)
    edit_cat.add_argument("--port", type=int, default=None)
    edit_cat.add_argument("--root", default=None, dest="remote_root")
    edit_cat.add_argument("--persist", default=None)
    edit_cat.add_argument("--scheduler", default=None, choices=["auto", "slurm", "pbs"])
    edit_cat.set_defaults(handler=do_server_edit)

    read = sub.add_parser("read", parents=[server])
    read.add_argument("path")
    read.set_defaults(handler=do_read)
    tail = sub.add_parser("tail", parents=[server])
    tail.add_argument("path")
    tail.add_argument("--lines", type=int, default=80)
    tail.set_defaults(handler=do_tail)
    listing = sub.add_parser("list", parents=[server])
    listing.add_argument("path")
    listing.set_defaults(handler=do_list)
    mkdir = sub.add_parser("mkdir", parents=[server])
    mkdir.add_argument("path")
    mkdir.set_defaults(handler=do_mkdir)
    copy = sub.add_parser("copy", parents=[server])
    copy.add_argument("source")
    copy.add_argument("destination")
    copy.set_defaults(handler=do_copy)
    move = sub.add_parser("move", parents=[server])
    move.add_argument("source")
    move.add_argument("destination")
    move.set_defaults(handler=do_move)
    remove_op = sub.add_parser("remove", parents=[server])
    remove_op.add_argument("path")
    remove_op.set_defaults(handler=do_remove)
    purge = sub.add_parser("purge", parents=[server])
    purge.add_argument("path")
    purge.add_argument("confirm_path")
    purge.set_defaults(handler=do_purge)
    trash_list = sub.add_parser("trash-list", parents=[server])
    trash_list.set_defaults(handler=do_trash_list)
    submit = sub.add_parser("submit", parents=[server])
    submit.add_argument("directory")
    submit.add_argument("script")
    submit.set_defaults(handler=do_submit)
    cancel = sub.add_parser("cancel", parents=[server])
    cancel.add_argument("job_id")
    cancel.add_argument("confirm_job_id")
    cancel.set_defaults(handler=do_cancel)
    run = sub.add_parser("run", parents=[server])
    run.add_argument("directory")
    run.add_argument("command")
    run.set_defaults(handler=do_run)
    diag = sub.add_parser("diagnostic", parents=[server])
    diag.add_argument("name", choices=["hostname", "pwd", "disk", "quota", "partitions", "modules", "scheduler"])
    diag.set_defaults(handler=do_diagnostic)
    for operation in ("vasp-inspect", "vasp-validate", "vasp-progress"):
        vasp = sub.add_parser(operation, parents=[server])
        vasp.add_argument("directory")
        vasp.set_defaults(handler=do_vasp)
    upload = sub.add_parser("upload", parents=[server])
    upload.add_argument("stage")
    upload.add_argument("destination")
    upload.set_defaults(handler=do_upload)
    download = sub.add_parser("download", parents=[server])
    download.add_argument("source")
    download.add_argument("stage")
    download.set_defaults(handler=do_download)
    transfer = sub.add_parser("transfer")
    transfer.add_argument("--from-server", required=True)
    transfer.add_argument("--from-path", required=True)
    transfer.add_argument("--to-server", required=True)
    transfer.add_argument("--to-path", required=True)
    transfer.set_defaults(handler=do_transfer)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.operation == "status":
        try:
            name, _ = resolve_server(args.server)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if connected(name):
            print("connected")
            return 0
        print("disconnected")
        return 3
    try:
        return args.handler(args)
    except (ValueError, RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        audit(args.operation, "error", str(exc), getattr(args, "server", None) or "")
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
