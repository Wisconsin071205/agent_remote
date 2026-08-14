#!/usr/bin/env python3
"""Deterministic Slurm adapter: version probing, sbatch --parsable, JSON with text fallback.

Two transport modes:
- --gateway-server NAME   execute Slurm commands over the gateway's existing
                          SSH ControlMaster socket (same connection as vasp_gateway.py)
- --offline-dir DIR       replay saved command output (offline unit tests)

State mapping is normalized (PENDING/RUNNING/COMPLETED/FAILED/TIMEOUT/CANCELLED/
SUSPENDED/UNKNOWN) and is deliberately scheduler-only: it says NOTHING about
scientific convergence. VASP progress stays a separate concern (vasp_parse.py).

Built-in self-test:  py slurm_adapter.py selftest
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOL_VERSION = "0.1.0"

# Slurm native state -> normalized scheduler state (schedule only, not science).
STATE_MAP: dict[str, str] = {
    "PENDING": "PENDING", "PD": "PENDING", "CONFIGURING": "PENDING", "CF": "PENDING",
    "RUNNING": "RUNNING", "R": "RUNNING", "COMPLETING": "RUNNING", "CG": "RUNNING",
    "REQUEUED": "RUNNING", "RQ": "RUNNING", "REVOKED": "RUNNING", "RV": "RUNNING",
    "STAGE_OUT": "RUNNING", "SO": "RUNNING",
    "COMPLETED": "COMPLETED", "CD": "COMPLETED",
    "FAILED": "FAILED", "F": "FAILED", "NODE_FAIL": "FAILED", "NF": "FAILED",
    "OUT_OF_MEMORY": "FAILED", "OOM": "FAILED", "BOOT_FAIL": "FAILED", "BF": "FAILED",
    "DEADLINE": "FAILED", "DL": "FAILED", "PREEMPTED": "FAILED", "PR": "FAILED",
    "SPECIAL_EXIT": "FAILED", "SE": "FAILED",
    "TIMEOUT": "TIMEOUT", "TO": "TIMEOUT",
    "CANCELLED": "CANCELLED", "CA": "CANCELLED",
    "SUSPENDED": "SUSPENDED", "S": "SUSPENDED", "STOPPED": "SUSPENDED", "ST": "SUSPENDED",
}
TERMINAL_STATES = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"}


def normalize_state(raw: str) -> str:
    key = (raw or "").strip().upper().split("+")[0].split(" ")[0]
    return STATE_MAP.get(key, "UNKNOWN")


@dataclass
class Runner:
    """Executes remote commands; subclasses define transport."""
    name: str = "local"

    def run(self, command: str) -> subprocess.CompletedProcess:
        raise NotImplementedError


@dataclass
class SshRunner(Runner):
    """Run Slurm commands over the gateway's ControlMaster socket."""
    gateway_config_dir: Path = Path.home() / ".config" / "vasp-remote-agent"
    cache_dir: Path = Path.home() / ".cache" / "vasp-remote-agent"
    target: str = ""
    port: int = 22

    def configure(self, name: str) -> None:
        cfg_path = self.gateway_config_dir / "config.json"
        with cfg_path.open(encoding="utf-8-sig") as handle:
            config = json.load(handle)
        servers = config.get("servers") if isinstance(config, dict) else {}
        chosen = name or config.get("default_server", "cl9")
        entry = servers.get(chosen)
        if entry is None:
            raise ValueError(f"unknown server: {chosen}")
        self.name = chosen
        self.target = entry["target"]
        self.port = int(entry.get("port", 22))

    def run(self, command: str) -> subprocess.CompletedProcess:
        socket = self.cache_dir / f"{self.name}.sock"
        if not socket.exists():
            raise RuntimeError(f"{self.name} is disconnected; run the connect operation first")
        return subprocess.run(
            ["ssh", "-p", str(self.port), "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=yes", "-o", f"ControlPath={socket}",
             self.target, command],
            text=True, capture_output=True, timeout=120, check=False,
        )


@dataclass
class OfflineRunner(Runner):
    """Replay saved Slurm command output from a sample directory.

    Sample files (name = command signature):
      version.txt          -> "sbatch --version"
      submit.txt           -> sbatch --parsable output
      squeue.json / squeue.txt
      sacct.json / sacct.txt
    """
    directory: Path = Path(".")

    def run(self, command: str) -> subprocess.CompletedProcess:
        if "sbatch --version" in command or "squeue --version" in command:
            path = self.directory / "version.txt"
            text = path.read_text(encoding="utf-8") if path.is_file() else "slurm 23.02.7"
            return subprocess.CompletedProcess(command, 0, text, "")
        if "sbatch" in command:
            path = self.directory / "submit.txt"
            if not path.is_file():
                raise RuntimeError(f"offline sample missing: {path}")
            return subprocess.CompletedProcess(command, 0, path.read_text(encoding="utf-8"), "")
        if "squeue" in command:
            for name in ("squeue.json", "squeue.txt"):
                path = self.directory / name
                if path.is_file():
                    return subprocess.CompletedProcess(command, 0, path.read_text(encoding="utf-8"), "")
            raise RuntimeError(f"offline sample missing: squeue.json or squeue.txt")
        if "sacct" in command:
            for name in ("sacct.json", "sacct.txt"):
                path = self.directory / name
                if path.is_file():
                    return subprocess.CompletedProcess(command, 0, path.read_text(encoding="utf-8"), "")
            raise RuntimeError(f"offline sample missing: sacct.json or sacct.txt")
        raise RuntimeError(f"no offline sample for command: {command[:80]}")


@dataclass
class JobState:
    job_id: str = ""
    state_raw: str = ""
    state: str = "UNKNOWN"
    partition: str = ""
    nodes: str = ""
    reason: str = ""
    time_used: str = ""
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state_raw": self.state_raw,
            "state": self.state,
            "partition": self.partition,
            "nodes": self.nodes,
            "reason": self.reason,
            "time_used": self.time_used,
            "name": self.name,
        }


def detect_version(runner: Runner) -> dict[str, Any]:
    """Probe the Slurm version and decide whether --json querying is usable."""
    result = runner.run("sbatch --version 2>/dev/null || squeue --version 2>/dev/null")
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout or result.stderr or "")
    if not m:
        return {"slurm_version": "", "json_support": False, "note": "version probe failed"}
    major, minor, patch = (int(x) for x in m.groups())
    # squeue/sacct --json landed in Slurm 20.02; require >= 20.11 for stability.
    json_support = (major, minor) >= (20, 11)
    return {
        "slurm_version": f"{major}.{minor}.{patch}",
        "json_support": json_support,
        "query_mode": "json" if json_support else "text",
    }


def submit(runner: Runner, directory: str, script: str) -> dict[str, Any]:
    """Submit via sbatch --parsable and return exactly the job id."""
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", script):
        raise ValueError("job script must be a simple filename")
    result = runner.run(f"cd -- {shlex_quote(directory)} && sbatch --parsable -- {shlex_quote(script)}")
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout or "sbatch failed").strip()}
    job_id = (result.stdout or "").strip().splitlines()[0].strip()
    if not re.fullmatch(r"[0-9]+", job_id):
        return {"ok": False, "error": f"sbatch returned unexpected output: {job_id[:80]}"}
    return {"ok": True, "job_id": job_id}


def shlex_quote(text: str) -> str:
    """Minimal POSIX quoting (single quotes with '\'' escaping)."""
    if re.fullmatch(r"[A-Za-z0-9._/+=-]+", text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"


def parse_squeue_json(data: dict) -> list[JobState]:
    jobs: list[JobState] = []
    for item in data.get("jobs", []):
        raw_state = (item.get("job_state") or [""])
        state = raw_state[0] if isinstance(raw_state, list) else str(raw_state)
        jobs.append(JobState(
            job_id=str(item.get("job_id", "")),
            state_raw=state,
            state=normalize_state(state),
            partition=(item.get("partition") or [""])[0] if isinstance(item.get("partition"), list) else str(item.get("partition", "")),
            nodes=(item.get("nodes") or ""),
            reason=(item.get("reason") or ""),
            name=(item.get("name") or ""),
        ))
    return jobs


def parse_squeue_text(text: str) -> list[JobState]:
    """Parse 'squeue -h -o %i|%T|%R|%P|%N|%M|%j' pipe-separated rows."""
    jobs: list[JobState] = []
    for line in text.splitlines():
        fields = line.strip().split("|")
        if len(fields) < 2 or not fields[0].strip().isdigit():
            continue
        job_id = fields[0].strip()
        raw_state = fields[1].strip()
        jobs.append(JobState(
            job_id=job_id,
            state_raw=raw_state,
            state=normalize_state(raw_state),
            reason=fields[2].strip() if len(fields) > 2 else "",
            partition=fields[3].strip() if len(fields) > 3 else "",
            nodes=fields[4].strip() if len(fields) > 4 else "",
            time_used=fields[5].strip() if len(fields) > 5 else "",
            name=fields[6].strip() if len(fields) > 6 else "",
        ))
    return jobs


def parse_sacct_json(data: dict) -> list[JobState]:
    jobs: list[JobState] = []
    for item in data.get("jobs", []):
        raw_state = (item.get("state") or {})
        current = (raw_state.get("current") or [""]) if isinstance(raw_state, dict) else [""]
        state = current[0] if isinstance(current, list) else str(current)
        jobs.append(JobState(
            job_id=str(item.get("job_id", "")),
            state_raw=state,
            state=normalize_state(state),
            partition=(item.get("partition") or ""),
            nodes=(item.get("nodes") or ""),
            reason=(item.get("reason") or ""),
            name=(item.get("job_name") or ""),
        ))
    return jobs


def parse_sacct_text(text: str) -> list[JobState]:
    jobs: list[JobState] = []
    for line in text.splitlines():
        fields = line.strip().split("|")
        if len(fields) < 2 or not fields[0].strip().isdigit():
            continue
        raw_state = fields[1].strip()
        jobs.append(JobState(
            job_id=fields[0].strip(),
            state_raw=raw_state,
            state=normalize_state(raw_state),
            partition=fields[2].strip() if len(fields) > 2 else "",
            nodes=fields[3].strip() if len(fields) > 3 else "",
            name=fields[4].strip() if len(fields) > 4 else "",
        ))
    return jobs


def query(runner: Runner, job_ids: list[str]) -> dict[str, Any]:
    """Live queue state via squeue --json (or fixed-field text fallback)."""
    version = detect_version(runner)
    if version["json_support"]:
        result = runner.run("squeue --json" + (f" --jobs={','.join(job_ids)}" if job_ids else ""))
        try:
            jobs = parse_squeue_json(json.loads(result.stdout or "{}"))
            return {"source": "squeue", "fetched_via": "json", **version, "jobs": [j.to_dict() for j in jobs]}
        except json.JSONDecodeError:
            pass  # fall through to text on malformed JSON
    fmt = "%i|%T|%R|%P|%N|%M|%j"
    result = runner.run(f"squeue -h -o '{fmt}'" + (f" -j {','.join(job_ids)}" if job_ids else ""))
    return {"source": "squeue", "fetched_via": "text", **version,
            "jobs": [j.to_dict() for j in parse_squeue_text(result.stdout or "")]}


def history(runner: Runner, job_ids: list[str]) -> dict[str, Any]:
    """Finished-job records via sacct --json (or text fallback)."""
    version = detect_version(runner)
    if version["json_support"]:
        result = runner.run(f"sacct --json -j {','.join(job_ids)}")
        try:
            jobs = parse_sacct_json(json.loads(result.stdout or "{}"))
            return {"source": "sacct", "fetched_via": "json", **version, "jobs": [j.to_dict() for j in jobs]}
        except json.JSONDecodeError:
            pass
    result = runner.run(f"sacct -n -X -o JobID%20,State%12,Partition,NodeList,JobName%30 -P -j {','.join(job_ids)}")
    return {"source": "sacct", "fetched_via": "text", **version,
            "jobs": [j.to_dict() for j in parse_sacct_text(result.stdout or "")]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)

    def add_transport(p: argparse.ArgumentParser) -> None:
        p.add_argument("--gateway-server", default="", help="server name from the gateway catalog")
        p.add_argument("--offline-dir", default="", help="replay saved output samples from this directory")

    def build_runner(args: argparse.Namespace) -> Runner:
        if args.offline_dir:
            return OfflineRunner(directory=Path(args.offline_dir))
        runner = SshRunner()
        runner.configure(args.gateway_server)
        return runner

    p_probe = sub.add_parser("probe", help="detect Slurm version and JSON support")
    add_transport(p_probe)
    p_submit = sub.add_parser("submit", help="sbatch --parsable")
    p_submit.add_argument("directory")
    p_submit.add_argument("script")
    add_transport(p_submit)
    p_query = sub.add_parser("query", help="squeue state (JSON or text fallback)")
    p_query.add_argument("job_ids", nargs="*")
    add_transport(p_query)
    p_hist = sub.add_parser("history", help="sacct records (JSON or text fallback)")
    p_hist.add_argument("job_ids", nargs="+")
    add_transport(p_hist)
    sub.add_parser("selftest", help="run offline parser tests with built-in samples")

    args = parser.parse_args(argv)

    if args.operation == "selftest":
        return selftest()

    runner = build_runner(args)
    if args.operation == "probe":
        print(json.dumps(detect_version(runner), ensure_ascii=False, indent=2))
    elif args.operation == "submit":
        print(json.dumps(submit(runner, args.directory, args.script), ensure_ascii=False, indent=2))
    elif args.operation == "query":
        print(json.dumps(query(runner, args.job_ids), ensure_ascii=False, indent=2))
    elif args.operation == "history":
        print(json.dumps(history(runner, args.job_ids), ensure_ascii=False, indent=2))
    return 0


def selftest() -> int:
    """Offline parser tests with built-in samples (no server needed)."""
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(("PASS  " if condition else "FAIL  ") + label + (f"  ({detail})" if detail and condition else ""))
        if not condition:
            failures.append(label)

    # 1. state normalization
    check("normalize long states", normalize_state("COMPLETED") == "COMPLETED")
    check("normalize short states", normalize_state("CD") == "COMPLETED")
    check("normalize cancelled+", normalize_state("CANCELLED by 123") == "CANCELLED")
    check("normalize timeout", normalize_state("TIMEOUT") == "TIMEOUT")
    check("normalize unknown", normalize_state("WEIRD") == "UNKNOWN")

    # 2. text parsing (old Slurm fallback)
    squeue_text = "12345|RUNNING|None|gpu|node07|1-02:03:04|relax\n67890|PENDING|Priority|gpu|n/a|0:00|static\n"
    jobs = parse_squeue_text(squeue_text)
    check("squeue text rows", len(jobs) == 2, f"got {len(jobs)}")
    check("squeue text state", jobs[0].state == "RUNNING" and jobs[1].state == "PENDING")

    sacct_text = "12345|COMPLETED|gpu|node07|relax\n67890|FAILED|gpu|node07|static\n"
    hist = parse_sacct_text(sacct_text)
    check("sacct text state", hist[0].state == "COMPLETED" and hist[1].state == "FAILED")

    # 3. JSON parsing (modern Slurm)
    squeue_json = {"jobs": [{"job_id": 12345, "job_state": ["RUNNING"], "partition": ["gpu"], "nodes": "node07", "name": "relax"}]}
    check("squeue json", parse_squeue_json(squeue_json)[0].state == "RUNNING")
    sacct_json = {"jobs": [{"job_id": 67890, "state": {"current": ["TIMEOUT"]}, "partition": "gpu"}]}
    check("sacct json", parse_sacct_json(sacct_json)[0].state == "TIMEOUT")

    # 4. offline runner end-to-end with a sample directory
    sample_dir = Path(__file__).parent / "_selftest_samples"
    sample_dir.mkdir(exist_ok=True)
    (sample_dir / "version.txt").write_text("slurm 19.05.2\n", encoding="utf-8")
    (sample_dir / "submit.txt").write_text("54321\n", encoding="utf-8")
    (sample_dir / "squeue.txt").write_text(squeue_text, encoding="utf-8")
    (sample_dir / "sacct.txt").write_text(sacct_text, encoding="utf-8")
    runner = OfflineRunner(directory=sample_dir)
    probe = detect_version(runner)
    check("offline probe version", probe["slurm_version"] == "19.5.2")
    check("offline probe json_support false", probe["json_support"] is False)
    submitted = submit(runner, "/work/test", "run.slurm")
    check("offline submit parsable", submitted == {"ok": True, "job_id": "54321"}, str(submitted))
    q = query(runner, [])
    check("offline query text fallback", q["fetched_via"] == "text" and len(q["jobs"]) == 2)
    h = history(runner, ["12345"])
    check("offline history text fallback", h["fetched_via"] == "text" and h["jobs"][0]["state"] == "COMPLETED")

    # 5. determinism: same sample parsed twice yields identical JSON
    one = json.dumps(query(runner, []), sort_keys=True, ensure_ascii=False)
    two = json.dumps(query(runner, []), sort_keys=True, ensure_ascii=False)
    check("deterministic output", one == two)

    print()
    if failures:
        print(f"{len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("all selftests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
