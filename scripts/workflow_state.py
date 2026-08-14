#!/usr/bin/env python3
"""Explicit workflow state machine: PREPARED -> ... -> REVIEWED.

Every transition is validated against a fixed table and appended to an
append-only audit trail (state file). Abnormal terminal states (FAILED /
TIMEOUT / REJECTED) enter diagnosis and recovery instead of pretending
the calculation is fine.

Determinism note: plan.json (generated inputs) stays byte-identical for
identical inputs; the state file intentionally contains wall-clock
timestamps because transitions are temporal EVENTS, not generated content.

Usage:
  py workflow_state.py init DIR --workflow relax-static [--plan-sha H]
  py workflow_state.py status DIR
  py workflow_state.py advance DIR --to VALIDATED --by tool --note "..."
  py workflow_state.py selftest
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOL_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"
STATE_FILE = ".vaspilot-state.json"

# Forward transitions allowed from each state. Abnormal terminals are
# reachable from every active state; REVIEWED is final.
TRANSITIONS: dict[str, list[str]] = {
    "PREPARED": ["VALIDATED", "FAILED", "REJECTED", "TIMEOUT"],
    "VALIDATED": ["APPROVED", "FAILED", "REJECTED"],
    "APPROVED": ["SUBMITTED", "FAILED", "REJECTED"],
    "SUBMITTED": ["RUNNING", "FAILED", "TIMEOUT"],
    "RUNNING": ["FINISHED", "FAILED", "TIMEOUT"],
    "FINISHED": ["PARSED", "FAILED"],
    "PARSED": ["REVIEWED", "FAILED"],
    "REVIEWED": [],
    "FAILED": [],      # terminal: recovery creates a NEW state file (fresh run)
    "TIMEOUT": [],
    "REJECTED": [],
}
ACTIVE_STATES = {"PREPARED", "VALIDATED", "APPROVED", "SUBMITTED", "RUNNING", "FINISHED", "PARSED"}
ABNORMAL_TERMINAL = {"FAILED", "TIMEOUT", "REJECTED"}

BY_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
NOTE_RE = re.compile(r"^.{0,300}$", re.S)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_path(directory: Path) -> Path:
    return directory / STATE_FILE


def load_state(directory: Path) -> dict[str, Any]:
    path = state_path(directory)
    if not path.is_file():
        raise RuntimeError(f"no state file in {directory}; run init first")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"state file is corrupt: {exc}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(f"unsupported state schema {data.get('schema_version')}")
    return data


def save_state(directory: Path, data: dict[str, Any]) -> None:
    path = state_path(directory)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8", newline="\n")
    tmp.replace(path)


def init(directory: Path, workflow: str, plan_sha: str, by: str, note: str) -> dict[str, Any]:
    path = state_path(directory)
    if path.is_file():
        raise RuntimeError(f"state file already exists in {directory}")
    data = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "directory": str(directory),
        "workflow": workflow,
        "plan_sha256": plan_sha,
        "state": "PREPARED",
        "history": [{
            "from": None, "to": "PREPARED", "at": now_iso(),
            "by": by, "note": note,
        }],
    }
    save_state(directory, data)
    return data


def advance(directory: Path, to: str, by: str, note: str) -> dict[str, Any]:
    if to not in TRANSITIONS:
        raise ValueError(f"unknown state: {to}")
    if not BY_RE.fullmatch(by):
        raise ValueError("--by must match [A-Za-z0-9._-]{1,40}")
    data = load_state(directory)
    current = data["state"]
    if to not in TRANSITIONS.get(current, []):
        raise ValueError(f"illegal transition {current} -> {to}")
    data["state"] = to
    data["history"].append({
        "from": current, "to": to, "at": now_iso(), "by": by, "note": note,
    })
    save_state(directory, data)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)

    p_init = sub.add_parser("init", help="create a state file in PREPARED")
    p_init.add_argument("directory")
    p_init.add_argument("--workflow", required=True, choices=["relax-static", "static-band-dos", "convergence-scan"])
    p_init.add_argument("--plan-sha", default="", help="plan_sha256 from plan.json")
    p_init.add_argument("--by", default="workflow_prepare")
    p_init.add_argument("--note", default="")

    p_status = sub.add_parser("status", help="print current state and history")
    p_status.add_argument("directory")

    p_adv = sub.add_parser("advance", help="attempt a state transition")
    p_adv.add_argument("directory")
    p_adv.add_argument("--to", required=True,
                       choices=sorted(set(TRANSITIONS) | ABNORMAL_TERMINAL))
    p_adv.add_argument("--by", required=True, help="who/what performs the transition (tool or human)")
    p_adv.add_argument("--note", default="")

    sub.add_parser("selftest", help="validate the transition table offline")

    args = parser.parse_args(argv)

    if args.operation == "selftest":
        return selftest()

    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 2
    try:
        if args.operation == "init":
            data = init(directory, args.workflow, args.plan_sha, args.by, args.note)
            print(f"initialized {directory} at PREPARED")
            print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        elif args.operation == "status":
            data = load_state(directory)
            print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        elif args.operation == "advance":
            data = advance(directory, args.to, args.by, args.note)
            print(f"{data['history'][-1]['from']} -> {args.to} (by {args.by})")
            print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def selftest() -> int:
    """Offline checks of the transition table (temp directory)."""
    import tempfile

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(("PASS  " if ok else "FAIL  ") + label + (f"  ({detail})" if detail and ok else ""))
        if not ok:
            failures.append(label)

    check("happy path transitions", all(to in TRANSITIONS.get(fr, [])
          for fr, to in [("PREPARED", "VALIDATED"), ("VALIDATED", "APPROVED"),
                         ("APPROVED", "SUBMITTED"), ("SUBMITTED", "RUNNING"),
                         ("RUNNING", "FINISHED"), ("FINISHED", "PARSED"),
                         ("PARSED", "REVIEWED")]))
    check("REVIEWED is final", TRANSITIONS["REVIEWED"] == [])
    check("abnormal states are terminal", all(TRANSITIONS[s] == [] for s in ABNORMAL_TERMINAL))
    check("FAILED reachable from RUNNING", "FAILED" in TRANSITIONS["RUNNING"])

    with tempfile.TemporaryDirectory(prefix="wf-state-") as tmp:
        directory = Path(tmp)
        data = init(directory, "relax-static", "abc123", "selftest", "smoke")
        check("init at PREPARED", data["state"] == "PREPARED" and len(data["history"]) == 1)
        try:
            advance(directory, "APPROVED", "selftest", "skip validation")
            check("illegal PREPARED->APPROVED rejected", False)
        except ValueError:
            check("illegal PREPARED->APPROVED rejected", True)
        data = advance(directory, "VALIDATED", "vasp_validate", "no error issues")
        check("legal advance", data["state"] == "VALIDATED" and len(data["history"]) == 2)
        # audit trail immutability: advancing again appends, never rewrites
        data = advance(directory, "FAILED", "selftest", "injected failure")
        check("FAILED transition recorded", data["state"] == "FAILED" and len(data["history"]) == 3)
        check("history append-only", data["history"][0]["to"] == "PREPARED"
              and data["history"][1]["to"] == "VALIDATED" and data["history"][2]["to"] == "FAILED")
        try:
            advance(directory, "RUNNING", "selftest", "resurrect from FAILED")
            check("advance from FAILED rejected", False)
        except ValueError:
            check("advance from FAILED rejected", True)
        # corrupt state file is detected
        state_path(directory).write_text("{not json", encoding="utf-8")
        try:
            load_state(directory)
            check("corrupt state detected", False)
        except RuntimeError:
            check("corrupt state detected", True)

    print()
    if failures:
        print(f"{len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("all workflow_state selftests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
