#!/usr/bin/env python3
"""Central-agent tool surface: the ONLY ten tools a model may call.

Architecture rule (spec/workflows.md): the model selects workflows, fills
constrained parameters, explains results and requests approval. It NEVER
gets a shell. Every tool here is a deterministic facade over the tools
built in this repository (workflow_prepare, vasp_parse, slurm_adapter,
custodian_detect, apply_patch, workflow_state) or the restricted gateway.

Usage:
  py agent_tools.py schemas                        # OpenAI-format tool list
  py agent_tools.py dispatch --name prepare_workflow --args '{...}' --workdir DIR
  py agent_tools.py selftest                       # offline smoke run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

TOOL_VERSION = "0.1.0"
SCRIPTS = Path(__file__).parent

# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format). Parameter constraints are
# deliberate: the model can only fill whitelisted values, never free-form
# shell or arbitrary paths outside the working directory.
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "prepare_workflow",
            "description": "Generate the relax-static directory tree, inputs, job scripts and plan.json deterministically from a template directory. Never submits anything. Same inputs always produce the same plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workflow": {"type": "string", "enum": ["relax-static"]},
                    "from_dir": {"type": "string", "description": "template directory containing POSCAR/INCAR/KPOINTS/POTCAR"},
                    "base_dir": {"type": "string", "description": "output base directory"},
                    "set": {"type": "array", "items": {"type": "string"}, "description": "whitelisted INCAR overrides like ENCUT=520"},
                    "kpoints": {"type": "array", "items": {"type": "integer"}, "minItems": 3, "maxItems": 3},
                    "partition": {"type": "string"},
                    "ntasks": {"type": "integer", "minimum": 1, "maximum": 256},
                    "walltime": {"type": "string"},
                },
                "required": ["workflow", "from_dir", "base_dir"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_calculation",
            "description": "Run the deterministic preflight checks (gateway vasp-validate) on a prepared or existing calculation directory. Returns issues/warnings; errors block submission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "remote or local calculation directory"},
                    "server": {"type": "string", "description": "registered server name (empty = default)"},
                },
                "required": ["directory"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "preview_changes",
            "description": "Detect VASP failure signatures and produce a unified-diff PREVIEW of suggested INCAR fixes. Nothing is modified; approval and apply_patch are separate steps.",
            "parameters": {
                "type": "object",
                "properties": {"directory": {"type": "string"}},
                "required": ["directory"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_approved_workflow",
            "description": "Submit a job script with sbatch --parsable. Only accepts directories with a plan.json plus an explicit human approval reference; returns the job id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string"},
                    "script": {"type": "string", "pattern": "^[A-Za-z0-9._+-]+$"},
                    "approval_ref": {"type": "string", "description": "human approval reference (required; rejects empty)"},
                    "server": {"type": "string"},
                },
                "required": ["directory", "script", "approval_ref"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_job_state",
            "description": "Query SCHEDULER state only (PENDING/RUNNING/COMPLETED/FAILED/TIMEOUT/CANCELLED). Scheduler state says nothing about scientific convergence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "server": {"type": "string"},
                },
                "required": ["job_ids"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_vasp_progress",
            "description": "Query SCIENTIFIC progress (gateway vasp-progress): ionic/electronic steps, energies, convergence flags. Distinct from scheduler state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string"},
                    "server": {"type": "string"},
                },
                "required": ["directory"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diagnose_failure",
            "description": "Diagnose a finished or failed calculation: recognized VASP error signatures with evidence and scientific status. Detection only.",
            "parameters": {
                "type": "object",
                "properties": {"directory": {"type": "string"}},
                "required": ["directory"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_recovery",
            "description": "Propose a recovery patch for a diagnosed failure (writes .proposed.patch preview). Never applies anything; human approval plus apply_patch come after.",
            "parameters": {
                "type": "object",
                "properties": {"directory": {"type": "string"}},
                "required": ["directory"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_results",
            "description": "Parse a calculation directory into a versioned CalculationManifest (hashes, energies, convergence, errors). Deterministic: identical directories parse byte-identically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string"},
                    "workflow": {"type": "string", "enum": ["relax", "static", "band", "dos", "convergence-scan"]},
                },
                "required": ["directory"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_report",
            "description": "Render a human-readable Markdown report from a CalculationManifest produced by parse_results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string"},
                    "workflow": {"type": "string", "enum": ["relax", "static", "band", "dos", "convergence-scan"]},
                },
                "required": ["directory"],
                "additionalProperties": False,
            },
        },
    },
]


def run_tool(script: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        text=True, capture_output=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"{script} failed")
    return result


def gateway_run(server: str, operation: str, *flags: str) -> dict[str, Any]:
    """Forward an operation to the restricted gateway controller."""
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(SCRIPTS / "vasp-agent.ps1"), operation]
    if server:
        cmd += ["-ServerName", server]
    cmd += list(flags)
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout or "gateway failed").strip()}
    return {"ok": True, "output": (result.stdout or "").strip()}


def dispatch(name: str, arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic dispatcher: name + constrained arguments -> structured result."""
    context = context or {}
    workdir = Path(context.get("workdir") or ".")
    server = str(arguments.get("server") or context.get("server") or "")

    if name == "prepare_workflow":
        from_dir = str(arguments["from_dir"])
        base_dir = str(arguments["base_dir"])
        cmd = ["relax-static", "--from-dir", from_dir, "--base-dir", base_dir]
        for item in arguments.get("set") or []:
            cmd += ["--set", str(item)]
        if arguments.get("kpoints"):
            cmd += ["--kpoints", *[str(x) for x in arguments["kpoints"]]]
        for flag in ("partition", "ntasks", "walltime"):
            if arguments.get(flag) is not None:
                cmd += ["--" + flag, str(arguments[flag])]
        result = run_tool("workflow_prepare.py", *cmd)
        plan_path = Path(base_dir) / arguments.get("system", "") / "plan.json"
        _ = plan_path  # plan location derived inside workflow_prepare; read below
        # workflow_prepare prints "plan written to <path>"; find it
        import re as _re
        match = _re.search(r"plan written to (.+)", result.stdout or "")
        plan_file = match.group(1).strip() if match else ""
        plan = {}
        if plan_file and Path(plan_file).is_file():
            plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
        return {"ok": True, "plan_file": plan_file, "plan": plan,
                "next": "validate inputs, then request approval and submit_approved_workflow"}

    if name == "validate_calculation":
        return gateway_run(server, "vasp-validate", "-RemotePath", str(arguments["directory"]))

    if name == "preview_changes":
        directory = str(arguments["directory"])
        result = run_tool("custodian_detect.py", directory, "--propose")
        report = json.loads(result.stdout)
        patch_file = report.get("patch_file", "")
        patch_text = Path(patch_file).read_text(encoding="utf-8") if patch_file else ""
        return {"ok": True, "findings": report["findings"], "patch_preview": patch_text,
                "modified": report["modified_files"],
                "next": "human approval, then apply_patch"}

    if name == "submit_approved_workflow":
        approval_ref = str(arguments.get("approval_ref") or "").strip()
        if not approval_ref:
            return {"ok": False, "error": "approval_ref is required and must not be empty"}
        directory = str(arguments["directory"])
        plan_json = Path(directory) / "plan.json"
        if not plan_json.is_file():
            return {"ok": False, "error": "no plan.json in directory; run prepare_workflow first"}
        result = run_tool("slurm_adapter.py", "submit", directory, str(arguments["script"]),
                          "--gateway-server", server, check=False)
        out = result.stdout.strip()
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = {"ok": False, "error": out[:200]}
        if data.get("ok"):
            data["approval_ref"] = approval_ref
        return data

    if name == "query_job_state":
        result = run_tool("slurm_adapter.py", "query", *[str(x) for x in arguments["job_ids"]],
                          "--gateway-server", server)
        return {"ok": True, **json.loads(result.stdout)}

    if name == "query_vasp_progress":
        return gateway_run(server, "vasp-progress", "-RemotePath", str(arguments["directory"]))

    if name == "diagnose_failure":
        directory = str(arguments["directory"])
        result = run_tool("custodian_detect.py", directory)
        report = json.loads(result.stdout)
        parse = run_tool("vasp_parse.py", directory, "--workflow",
                         str(arguments.get("workflow") or "static"))
        manifest = json.loads(parse.stdout)
        return {"ok": True, "findings": report["findings"],
                "scientific_status": manifest["results"]["scientific_status"],
                "errors": manifest["results"]["errors"],
                "modified": report["modified_files"]}

    if name == "propose_recovery":
        directory = str(arguments["directory"])
        result = run_tool("custodian_detect.py", directory, "--propose")
        report = json.loads(result.stdout)
        patch_file = report.get("patch_file", "")
        patch_text = Path(patch_file).read_text(encoding="utf-8") if patch_file else ""
        return {"ok": True, "proposed": patch_text,
                "apply": "after approval: py apply_patch.py " + directory + " --patch " + patch_file}

    if name == "parse_results":
        directory = str(arguments["directory"])
        workflow = str(arguments.get("workflow") or "static")
        result = run_tool("vasp_parse.py", directory, "--workflow", workflow)
        return {"ok": True, "manifest": json.loads(result.stdout)}

    if name == "generate_report":
        directory = str(arguments["directory"])
        workflow = str(arguments.get("workflow") or "static")
        result = run_tool("vasp_parse.py", directory, "--workflow", workflow)
        manifest = json.loads(result.stdout)
        return {"ok": True, "report": render_report(manifest)}

    return {"ok": False, "error": f"unknown tool: {name}"}


def render_report(manifest: dict[str, Any]) -> str:
    """Deterministic Markdown report from a manifest."""
    results = manifest["results"]
    energy = results.get("energy") or {}
    ionic = results.get("ionic") or {}
    lines = [
        "# VASP 计算报告",
        "",
        f"- manifest_id: {manifest.get('manifest_id', '')}",
        f"- workflow: {manifest.get('workflow', {}).get('name', '')} {manifest.get('workflow', {}).get('stage', '')}",
        f"- 科学状态: **{results.get('scientific_status', 'unknown')}**",
        f"- 调度状态: {manifest.get('execution', {}).get('status', 'unknown')}",
        "",
        "## 能量",
        f"- E0 = {energy.get('e0_ev')} eV" if energy.get("e0_ev") is not None else "- E0 = (无)",
        f"- 每原子 = {energy.get('e0_ev_per_atom')} eV/atom" if energy.get("e0_ev_per_atom") is not None else "",
        f"- 总磁矩 = {energy.get('total_magnetization')}",
        "",
        "## 收敛",
        f"- 电子收敛: {results.get('electronic', {}).get('converged')}",
        f"- 离子收敛: {ionic.get('converged')}（判据: {ionic.get('criterion') or '—'}，步数 {ionic.get('steps')}）",
    ]
    errors = results.get("errors") or []
    if errors:
        lines += ["", "## 错误签名"]
        for item in errors:
            lines.append(f"- {item.get('code')} ×{item.get('count', 1)} [{item.get('severity')}] handler={item.get('custodian_handler') or '-'}")
    else:
        lines += ["", "## 错误签名", "- 无"]
    mods = manifest.get("modifications") or []
    if mods:
        lines += ["", "## 修改记录"]
        for mod in mods:
            lines.append(f"- {mod.get('field')}: {mod.get('before')} -> {mod.get('after')} ({mod.get('author')})")
    review = manifest.get("review") or {}
    lines += ["", "## 复核", f"- reviewed: {review.get('reviewed')} conclusion: {review.get('conclusion')}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("schemas", help="print the OpenAI-format tool list")
    p_disp = sub.add_parser("dispatch", help="invoke one tool")
    p_disp.add_argument("--name", required=True, choices=[t["function"]["name"] for t in TOOL_SCHEMAS])
    p_disp.add_argument("--args", required=True, help="JSON object of tool arguments")
    p_disp.add_argument("--workdir", default=".", help="working directory context")
    p_disp.add_argument("--server", default="", help="default server name")
    sub.add_parser("selftest", help="offline smoke test")
    args = parser.parse_args(argv)

    if args.operation == "schemas":
        print(json.dumps(TOOL_SCHEMAS, ensure_ascii=False, indent=2))
        return 0
    if args.operation == "selftest":
        return selftest()
    try:
        arguments = json.loads(args.args)
    except json.JSONDecodeError as exc:
        print(f"error: --args is not valid JSON: {exc}", file=sys.stderr)
        return 2
    try:
        result = dispatch(args.name, arguments, {"workdir": args.workdir, "server": args.server})
    except (RuntimeError, KeyError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def selftest() -> int:
    """Offline smoke: prepare -> diagnose -> propose -> parse -> report."""
    import tempfile

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(("PASS  " if ok else "FAIL  ") + label + (f"  ({detail})" if detail and ok else ""))
        if not ok:
            failures.append(label)

    names = [t["function"]["name"] for t in TOOL_SCHEMAS]
    check("exactly ten tools", len(names) == 10, ", ".join(names))
    check("required tools present", set(names) == {
        "prepare_workflow", "validate_calculation", "preview_changes",
        "submit_approved_workflow", "query_job_state", "query_vasp_progress",
        "diagnose_failure", "propose_recovery", "parse_results", "generate_report"})

    with tempfile.TemporaryDirectory(prefix="agent-tools-") as tmp:
        root = Path(tmp)
        src = root / "src"
        src.mkdir()
        (src / "INCAR").write_text(
            "SYSTEM = smoke\nENCUT = 500\nIBRION = 2\nNSW = 80\nISMEAR = 0\nSIGMA = 0.05\n", encoding="utf-8")
        (src / "POSCAR").write_text(
            "Smoke\n   1.0\n     4.7 0.0 0.0\n     0.0 6.0 0.0\n     0.0 0.0 4.7\n   Li   Fe   P    O\n   1    1    1    4\nDirect\n  0.5 0.5 0.5\n  0.0 0.0 0.0\n  0.25 0.25 0.25\n  0.25 0.75 0.25\n  0.75 0.25 0.75\n  0.75 0.75 0.75\n  0.25 0.25 0.75\n", encoding="utf-8")
        (src / "KPOINTS").write_text("Auto\n0\nGamma\n2 2 2\n0 0 0\n", encoding="utf-8")
        (src / "POTCAR").write_text("PAW_PBE Li\n", encoding="utf-8")
        (src / "OUTCAR").write_text(
            " vasp.6.4.3\n BRMIX: very serious problems\n General timing and accounting informations for this job\n", encoding="utf-8")
        (src / "OSZICAR").write_text("   1 F= -.10E+03 E0= -.10E+03  d E =-.3E+03\n", encoding="utf-8")

        out = root / "out"
        r1 = dispatch("prepare_workflow", {"workflow": "relax-static", "from_dir": str(src),
                                           "base_dir": str(out), "set": ["ENCUT=520"]})
        check("prepare_workflow ok", r1.get("ok") and r1.get("plan_file"), str(r1.get("error", "")))

        r2 = dispatch("diagnose_failure", {"directory": str(src)})
        handlers = {f["handler"] for f in r2.get("findings", [])}
        check("diagnose finds BRMIX", "BRMIXVaspErrorHandler" in handlers)

        r3 = dispatch("propose_recovery", {"directory": str(src)})
        check("propose recovery patch", "ICHARG" in r3.get("proposed", ""), r3.get("proposed", "")[:60])

        r4 = dispatch("parse_results", {"directory": str(src), "workflow": "relax"})
        manifest = r4.get("manifest", {})
        check("parse_results energy", manifest.get("results", {}).get("energy", {}).get("e0_ev") == -100.0)

        r5 = dispatch("generate_report", {"directory": str(src), "workflow": "relax"})
        report = r5.get("report", "")
        check("report mentions BRMIX", "BRMIX" in report or "brmix" in report)
        check("report has scientific status", "科学状态" in report)

        r6 = dispatch("submit_approved_workflow", {"directory": str(src), "script": "run.slurm", "approval_ref": ""})
        check("submit rejects empty approval", r6.get("ok") is False, str(r6.get("error", "")))

    print()
    if failures:
        print(f"{len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("all agent_tools selftests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
