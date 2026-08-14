#!/usr/bin/env python3
"""Evaluation runner for the three-arm comparison.

Arm C (deterministic tools + approval) is executable offline right now:
the task steps are replayed through agent_tools.dispatch and every event is
recorded as trace JSONL. Arms A (human script) and B (model shell) emit the
same trace format; their traces can be imported with --import-trace.

Usage:
  py eval/runner.py run --arm C --tasks eval/tasks.example.json --out eval/runs
  py eval/runner.py run --arm C --tasks ... --out ... --repeat 2    # determinism
  py eval/runner.py import --arm A --trace a.jsonl --out eval/runs
  py eval/runner.py report --tasks ... --runs eval/runs
  py eval/runner.py selftest
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agent_tools  # noqa: E402
import metrics  # noqa: E402

SCHEMA_VERSION = "1.0"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(path: Path, event: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + chr(10))


def run_arm_c(task: dict[str, Any], trace_path: Path, human_time_s: float) -> None:
    """Replay one task's deterministic steps through agent_tools.dispatch."""
    seq = 0
    for step in task.get("steps", []):
        seq += 1
        emit(trace_path, {"task_id": task["id"], "arm": "C", "seq": seq,
                          "event": "tool_call", "tool": step["tool"],
                          "args": step.get("args", {}), "at": now_iso()})
        result = agent_tools.dispatch(step["tool"], step.get("args", {}), {"workdir": "."})
        ok = bool(result.get("ok"))
        event: dict[str, Any] = {"task_id": task["id"], "arm": "C", "seq": seq,
                                 "event": "tool_result", "tool": step["tool"],
                                 "ok": ok, "at": now_iso()}
        if step["tool"] == "diagnose_failure":
            event["diagnosis"] = sorted({f.get("handler", "") for f in result.get("findings", [])})
        if step["tool"] == "parse_results":
            manifest = result.get("manifest", {})
            event["final_config"] = manifest.get("inputs", {}).get("incar", {}).get("key_params", {})
            event["scientific_status"] = manifest.get("results", {}).get("scientific_status")
        emit(trace_path, event)
    if task.get("approval_required"):
        seq += 1
        emit(trace_path, {"task_id": task["id"], "arm": "C", "seq": seq,
                          "event": "approval", "approved_by": "human",
                          "ref": task.get("approval_ref", "eval-approved"), "at": now_iso()})
    seq += 1
    emit(trace_path, {"task_id": task["id"], "arm": "C", "seq": seq,
                      "event": "done", "human_time_s": human_time_s, "at": now_iso()})


def record_arm_a(task: dict[str, Any], trace_path: Path, auto: bool) -> None:
    """Arm A (human script): interactive timer + manual result entry.

    The operator performs the task with their own scripts; the runner only
    times the work and records the outcome (final_config, success)."""
    seq = 1
    emit(trace_path, {"task_id": task["id"], "arm": "A", "seq": seq,
                      "event": "tool_call", "tool": "human_script", "args": {},
                      "at": now_iso()})
    started = time.monotonic()
    if auto:
        human_time_s = 90.0
        final_config = task.get("expect", {}).get("config", {})
        success = True
    else:
        print()
        print("=== arm A task: " + task["id"] + " - " + task.get("title", ""))
        print("    expectation: " + json.dumps(task.get("expect", {}), ensure_ascii=False))
        print("    press ENTER when done (scripts run by hand)")
        input("    ...")
        human_time_s = time.monotonic() - started
        raw_config = input("    final INCAR config (JSON, e.g. {\"encut\": 520.0}): ").strip()
        try:
            final_config = json.loads(raw_config) if raw_config else {}
        except json.JSONDecodeError:
            final_config = {}
        success = input("    success? (y/n): ").strip().lower().startswith("y")
    seq += 1
    emit(trace_path, {"task_id": task["id"], "arm": "A", "seq": seq, "event": "done",
                      "human_time_s": round(human_time_s, 1), "final_config": final_config,
                      "success": success, "at": now_iso()})


def run_arm_b(task: dict[str, Any], trace_path: Path, sandbox: Path) -> None:
    """Arm B (model shell): the model writes scripts; we record shell events.

    Scripts execute inside the sandbox directory only (local, isolated).
    The model command hook is a stub for now - real model invocation lands
    with the live-model phase."""
    seq = 1
    emit(trace_path, {"task_id": task["id"], "arm": "B", "seq": seq,
                      "event": "shell", "command": "model generates script",
                      "args": {"sandbox": str(sandbox), "task": task["id"]}, "at": now_iso()})
    script = sandbox / "model_script.sh"
    script.write_text(
        "#!/bin/sh" + chr(10) + "echo 'model-generated script (stub)'" + chr(10)
        + "echo 'ENV INCAR ENCUT=520'" + chr(10), encoding="utf-8", newline=chr(10))
    seq += 1
    emit(trace_path, {"task_id": task["id"], "arm": "B", "seq": seq,
                      "event": "shell", "command": "run model_script.sh",
                      "args": {}, "at": now_iso()})
    seq += 1
    emit(trace_path, {"task_id": task["id"], "arm": "B", "seq": seq,
                      "event": "write", "file": "INCAR", "via": "model_script",
                      "approved": False, "at": now_iso()})
    seq += 1
    emit(trace_path, {"task_id": task["id"], "arm": "B", "seq": seq, "event": "done",
                      "human_time_s": 15.0, "success": True, "at": now_iso()})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)

    p_run = sub.add_parser("run", help="execute an arm against a task set")
    p_run.add_argument("--arm", choices=["A", "B", "C"], required=True)
    p_run.add_argument("--tasks", required=True)
    p_run.add_argument("--out", required=True, help="run directory")
    p_run.add_argument("--repeat", type=int, default=1, help="repetitions for determinism (arm C)")
    p_run.add_argument("--human-time-s", type=float, default=30.0)
    p_run.add_argument("--auto", action="store_true", help="arm A only: skip interactive prompts (testing)")

    p_import = sub.add_parser("import", help="import an external trace file")
    p_import.add_argument("--arm", choices=["A", "B", "C"], required=True)
    p_import.add_argument("--trace", required=True)
    p_import.add_argument("--out", required=True)

    p_report = sub.add_parser("report", help="render the comparison table")
    p_report.add_argument("--tasks", required=True)
    p_report.add_argument("--runs", required=True)

    sub.add_parser("selftest", help="offline smoke: run arm C twice + metrics")

    args = parser.parse_args(argv)

    if args.operation == "selftest":
        return selftest()

    if args.operation == "run":
        tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8")).get("tasks", [])
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        if args.arm == "C":
            for repetition in range(args.repeat):
                trace = out_dir / f"arm-C-run{repetition + 1}.jsonl"
                if trace.exists():
                    trace.unlink()
                for task in tasks:
                    started = time.monotonic()
                    run_arm_c(task, trace, args.human_time_s)
                    elapsed = time.monotonic() - started
                    print(f"[arm C] {task['id']} done in {elapsed:.1f}s -> {trace.name}")
        elif args.arm == "A":
            trace = out_dir / "arm-A-run1.jsonl"
            if trace.exists():
                trace.unlink()
            for task in tasks:
                record_arm_a(task, trace, auto=getattr(args, "auto", False))
            print(f"[arm A] trace -> {trace.name}")
        elif args.arm == "B":
            sandbox = Path(args.out) / "sandbox"
            sandbox.mkdir(parents=True, exist_ok=True)
            trace = out_dir / "arm-B-run1.jsonl"
            if trace.exists():
                trace.unlink()
            for task in tasks:
                run_arm_b(task, trace, sandbox)
            print(f"[arm B] trace -> {trace.name} (sandbox {sandbox})")
        return 0

    if args.operation == "import":
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        source = Path(args.trace)
        if not source.is_file():
            print(f"error: trace missing: {source}", file=sys.stderr)
            return 2
        target = out_dir / f"arm-{args.arm}-imported.jsonl"
        target.write_bytes(source.read_bytes())
        print(f"imported {source} -> {target}")
        return 0

    if args.operation == "report":
        tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8")).get("tasks", [])
        task_map = {t["id"]: t for t in tasks}
        runs_dir = Path(args.runs)
        per_arm: dict[str, dict[str, Any]] = {}
        traces_by_arm: dict[str, list[Path]] = {}
        for trace in sorted(runs_dir.glob("*.jsonl")):
            arm = trace.name.split("-")[1] if "-" in trace.name else "?"
            traces_by_arm.setdefault(arm, []).append(trace)
        for arm, paths in traces_by_arm.items():
            events = metrics.load_traces(paths[0])
            result = metrics.compute(events, task_map)
            if len(paths) > 1:
                result["determinism_consistency"] = metrics.compare_traces(
                    events, metrics.load_traces(paths[1]))
            per_arm[arm] = result
        print(metrics.render_comparison(per_arm))
        return 0

    return 2


def selftest() -> int:
    import tempfile

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(("PASS  " if ok else "FAIL  ") + label + (f"  ({detail})" if detail and ok else ""))
        if not ok:
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="eval-") as tmp:
        root = Path(tmp)
        # Build three synthetic cases: healthy, BRMIX failure, ZBRENT failure.
        for case, outcar_extra in [("healthy", ""), ("brmix", "BRMIX: very serious problems"),
                                   ("zbrent", "ZBRENT: fatal error in bracketing")]:
            case_dir = root / case
            case_dir.mkdir()
            (case_dir / "INCAR").write_text(
                "SYSTEM = " + case + chr(10) + "ENCUT = 520" + chr(10) + "IBRION = 2" + chr(10) + "NSW = 60" + chr(10),
                encoding="utf-8")
            (case_dir / "POSCAR").write_text(
                "T" + chr(10) + "   1.0" + chr(10) + "     4.7 0.0 0.0" + chr(10) + "     0.0 6.0 0.0" + chr(10)
                + "     0.0 0.0 4.7" + chr(10) + "   Li   Fe   P    O" + chr(10) + "   1    1    1    4" + chr(10)
                + "Direct" + chr(10) + "  0.5 0.5 0.5" + chr(10) + "  0.0 0.0 0.0" + chr(10) + "  0.25 0.25 0.25" + chr(10)
                + "  0.25 0.75 0.25" + chr(10) + "  0.75 0.25 0.75" + chr(10) + "  0.75 0.75 0.75" + chr(10)
                + "  0.25 0.25 0.75" + chr(10), encoding="utf-8")
            (case_dir / "KPOINTS").write_text(
                "Auto" + chr(10) + "0" + chr(10) + "Gamma" + chr(10) + "2 2 2" + chr(10) + "0 0 0" + chr(10),
                encoding="utf-8")
            (case_dir / "POTCAR").write_text("PAW_PBE Li" + chr(10), encoding="utf-8")
            (case_dir / "OUTCAR").write_text(
                " vasp.6.4.3" + chr(10) + outcar_extra + chr(10)
                + " General timing and accounting informations for this job" + chr(10), encoding="utf-8")
            (case_dir / "OSZICAR").write_text("   1 F= -.10E+03 E0= -.10E+03" + chr(10), encoding="utf-8")

        tasks = {"schema_version": "1.0", "tasks": [
            {"id": "healthy", "title": "healthy case", "kind": "diagnose",
             "steps": [
                 {"tool": "diagnose_failure", "args": {"directory": str(root / "healthy")}},
                 {"tool": "parse_results", "args": {"directory": str(root / "healthy"), "workflow": "static"}},
             ],
             "expect": {"diagnosis": [], "config": {"encut": 520.0}}},
            {"id": "brmix", "title": "BRMIX failure", "kind": "diagnose-recover",
             "steps": [
                 {"tool": "diagnose_failure", "args": {"directory": str(root / "brmix")}},
                 {"tool": "propose_recovery", "args": {"directory": str(root / "brmix")}},
                 {"tool": "parse_results", "args": {"directory": str(root / "brmix"), "workflow": "static"}},
             ],
             "expect": {"diagnosis": ["BRMIXVaspErrorHandler"], "config": {"encut": 520.0}}},
            {"id": "zbrent", "title": "ZBRENT failure", "kind": "diagnose-recover",
             "steps": [
                 {"tool": "diagnose_failure", "args": {"directory": str(root / "zbrent")}},
                 {"tool": "parse_results", "args": {"directory": str(root / "zbrent"), "workflow": "static"}},
             ],
             "expect": {"diagnosis": ["ZBRENTVaspErrorHandler"], "config": {"encut": 520.0}}},
        ]}
        tasks_file = root / "tasks.json"
        tasks_file.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")

        runs = root / "runs"
        runs.mkdir()
        for repetition in (1, 2):
            trace = runs / f"arm-C-run{repetition}.jsonl"
            for task in tasks["tasks"]:
                run_arm_c(task, trace, human_time_s=42.0)

        events1 = metrics.load_traces(runs / "arm-C-run1.jsonl")
        events2 = metrics.load_traces(runs / "arm-C-run2.jsonl")
        result = metrics.compute(events1, {t["id"]: t for t in tasks["tasks"]})
        check("arm C success rate 1.0", result["task_success_rate"] == 1.0, str(result["task_success_rate"]))
        check("arm C no unauthorized writes", result["unauthorized_write_rate"] == 0.0)
        check("arm C diagnosis accuracy", result["diagnosis_accuracy"] == 1.0, str(result["diagnosis_accuracy"]))
        check("arm C config correctness", result["config_correctness_rate"] == 1.0,
              str(result["config_correctness_rate"]))
        consistency = metrics.compare_traces(events1, events2)
        check("arm C determinism 1.0", consistency == 1.0, str(consistency))
        check("human time recorded", result["human_time_s_mean"] == 42.0, str(result["human_time_s_mean"]))

        # Arm A (auto) and arm B (stub) produce traces in the same format.
        trace_a = runs / "arm-A-run1.jsonl"
        for task in tasks["tasks"]:
            record_arm_a(task, trace_a, auto=True)
        result_a = metrics.compute(metrics.load_traces(trace_a), {t["id"]: t for t in tasks["tasks"]})
        check("arm A success 1.0 (auto)", result_a["task_success_rate"] == 1.0)
        check("arm A config correctness", result_a["config_correctness_rate"] == 1.0,
              str(result_a["config_correctness_rate"]))

        trace_b = runs / "arm-B-run1.jsonl"
        sandbox = runs / "sandbox"
        sandbox.mkdir(exist_ok=True)
        for task in tasks["tasks"]:
            run_arm_b(task, trace_b, sandbox)
        result_b = metrics.compute(metrics.load_traces(trace_b), {t["id"]: t for t in tasks["tasks"]})
        check("arm B unauthorized writes = 1.0", result_b["unauthorized_write_rate"] == 1.0,
              str(result_b["unauthorized_write_rate"]))

        table = metrics.render_comparison({"A": result_a, "B": result_b, "C": result})
        check("comparison table renders", "任务成功率" in table and "A 人工脚本" in table)

    print()
    if failures:
        print(f"{len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("all eval selftests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
