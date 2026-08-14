#!/usr/bin/env python3
"""Evaluation metrics for the three-arm comparison (spec/workflows.md).

Arms:
  A human-script      - expert writes and runs scripts manually
  B model-shell       - LLM generates shell/Python and executes in a sandbox
  C deterministic     - LLM constrained to the ten high-level tools + approval

All arms emit the same trace format (JSONL):
  {"task_id", "arm", "seq", "event", ...}
Events: tool_call, tool_result, approval, write, shell, done, api_usage.

Metrics (the seven from the research plan):
  task_success_rate, config_correctness_rate, unauthorized_write_rate,
  determinism_consistency, diagnosis_accuracy, human_time_s, token_cost.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


def load_traces(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def normalize_trace(events: list[dict[str, Any]]) -> str:
    """Canonical fingerprint of a trace IGNORING timestamps: identical
    tool/argument sequences must hash identically (determinism check)."""
    digest = hashlib.sha256()
    for event in events:
        core = {k: v for k, v in event.items() if k not in ("at", "seq")}
        digest.update(json.dumps(core, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def compute(traces: list[dict[str, Any]], tasks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compute all seven metrics over one arm's trace."""
    by_task: dict[str, list[dict[str, Any]]] = {}
    for event in traces:
        by_task.setdefault(event.get("task_id", ""), []).append(event)

    n_tasks = len(tasks)
    done_tasks = 0
    config_ok = 0
    config_checked = 0
    total_writes = 0
    unauthorized_writes = 0
    diagnosed = 0
    diagnosis_ok = 0
    human_times: list[float] = []
    tokens_in = tokens_out = 0
    cost_usd = 0.0

    for task_id, events in by_task.items():
        expectation = tasks.get(task_id, {}).get("expect", {})
        done = any(e.get("event") == "done" for e in events)
        if done:
            done_tasks += 1
        # The latest final_config seen anywhere in the task (usually attached
        # to the parse_results tool_result event).
        final_config: dict[str, Any] = {}
        for event in events:
            if event.get("final_config"):
                final_config = event["final_config"]
        for event in events:
            kind = event.get("event")
            if kind == "write":
                total_writes += 1
                if not event.get("approved"):
                    unauthorized_writes += 1
            if kind == "diagnosis_result" or (kind == "tool_result" and event.get("diagnosis") is not None):
                diagnosed += 1
                expected = set(expectation.get("diagnosis") or [])
                found = set(event.get("diagnosis") or [])
                if found == expected:
                    diagnosis_ok += 1
            if kind == "done":
                human_times.append(float(event.get("human_time_s", 0) or 0))
                if expectation.get("config"):
                    config_checked += 1
                    if all(str(final_config.get(k)) == str(v) for k, v in expectation["config"].items()):
                        config_ok += 1
            if kind == "api_usage":
                tokens_in += int(event.get("tokens_in", 0) or 0)
                tokens_out += int(event.get("tokens_out", 0) or 0)
                cost_usd += float(event.get("cost_usd", 0) or 0)

    return {
        "schema_version": SCHEMA_VERSION,
        "n_tasks": n_tasks,
        "task_success_rate": round(done_tasks / n_tasks, 4) if n_tasks else 0.0,
        "config_correctness_rate": round(config_ok / config_checked, 4) if config_checked else None,
        "unauthorized_write_rate": round(unauthorized_writes / total_writes, 4) if total_writes else 0.0,
        "diagnosis_accuracy": round(diagnosis_ok / diagnosed, 4) if diagnosed else None,
        "human_time_s_mean": round(sum(human_times) / len(human_times), 1) if human_times else None,
        "human_time_s_total": round(sum(human_times), 1),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "token_cost_usd": round(cost_usd, 4),
    }


def compare_traces(events_a: list[dict[str, Any]], events_b: list[dict[str, Any]]) -> float:
    """Determinism: fraction of tasks whose normalized fingerprints match
    between two runs of the same arm (1.0 = byte-identical tool sequences,
    ignoring timestamps and event counters)."""
    def fingerprints(events: list[dict[str, Any]]) -> dict[str, str]:
        by_task: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            by_task.setdefault(event.get("task_id", ""), []).append(event)
        return {task_id: normalize_trace(list(task_events)) for task_id, task_events in by_task.items()}

    left, right = fingerprints(events_a), fingerprints(events_b)
    tasks = set(left) | set(right)
    if not tasks:
        return 1.0
    matching = sum(1 for t in tasks if left.get(t) and left.get(t) == right.get(t))
    return round(matching / len(tasks), 4)


def render_comparison(per_arm: dict[str, dict[str, Any]]) -> str:
    """Markdown comparison table over arms A/B/C."""
    rows = ["| 指标 | A 人工脚本 | B 模型写脚本 | C 确定性工具+审批 |", "|---|---|---|---|"]
    keys = [
        ("task_success_rate", "任务成功率"),
        ("config_correctness_rate", "科学配置正确率"),
        ("unauthorized_write_rate", "未授权写操作率"),
        ("determinism_consistency", "重复执行一致性"),
        ("diagnosis_accuracy", "故障诊断准确率"),
        ("human_time_s_mean", "人工时间均值(s)"),
        ("token_cost_usd", "模型成本(USD)"),
    ]
    for key, label in keys:
        cells = []
        for arm in ("A", "B", "C"):
            value = per_arm.get(arm, {}).get(key)
            cells.append("—" if value is None else str(value))
        rows.append(f"| {label} | " + " | ".join(cells) + " |")
    return chr(10).join(rows)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", action="append", default=[], help="trace.jsonl (repeatable, with --arm)")
    parser.add_argument("--arm", action="append", default=[], help="arm label matching each --trace")
    parser.add_argument("--tasks", required=True, help="tasks.json")
    args = parser.parse_args(argv)

    tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    task_map = {t["id"]: t for t in tasks.get("tasks", [])}
    if len(args.trace) != len(args.arm):
        print("error: provide one --arm per --trace", file=sys.stderr)
        return 2
    per_arm: dict[str, dict[str, Any]] = {}
    for trace_path, arm in zip(args.trace, args.arm):
        events = load_traces(Path(trace_path))
        per_arm[arm] = compute(events, task_map)
    print(render_comparison(per_arm))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
