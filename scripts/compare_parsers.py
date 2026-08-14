#!/usr/bin/env python3
"""Batch parser comparison: vasp_parse vs the gateway regex inspector.

For every case directory under --cases, runs vasp_parse (pymatgen-enhanced
when a suitable interpreter exists, fallback otherwise) and, when the case
contains a saved gateway-inspect.json, diffs the two parsers field by field.
Emits a comparison matrix plus an agreement summary.

Usage:
  py scripts/compare_parsers.py --cases eval/cases --out eval/comparison.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).parent


def best_python() -> str:
    """Prefer an interpreter with pymatgen (3.12); fall back to the default."""
    for exe in ("py -3.12", "py -3", "python"):
        command = f"{exe} -c 'import pymatgen'".split() if " " in exe else [exe, "-c", "import pymatgen"]
        probe = subprocess.run(command, capture_output=True, text=True)
        if probe.returncode == 0:
            return exe
    return sys.executable


def run_parse(case_dir: Path, python_exe: str) -> dict[str, Any]:
    command = python_exe.split() + [str(SCRIPTS / "vasp_parse.py"), str(case_dir), "--workflow", "static"]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout).strip()[:300]}
    try:
        return {"ok": True, "manifest": json.loads(result.stdout)}
    except json.JSONDecodeError:
        return {"ok": False, "error": "vasp_parse returned invalid JSON"}


def compare_one(case_dir: Path, python_exe: str) -> dict[str, Any]:
    parsed = run_parse(case_dir, python_exe)
    if not parsed.get("ok"):
        return {"case": case_dir.name, "parse_ok": False, "error": parsed.get("error", "")}
    manifest = parsed["manifest"]
    record: dict[str, Any] = {
        "case": case_dir.name,
        "parse_ok": True,
        "parser": manifest.get("software", {}).get("parser"),
        "scientific_status": manifest.get("results", {}).get("scientific_status"),
        "errors": sorted({e["code"] for e in manifest.get("results", {}).get("errors", [])}),
        "e0_ev": manifest.get("results", {}).get("energy", {}).get("e0_ev"),
        "n_atoms": manifest.get("structure", {}).get("n_atoms"),
        "formula": manifest.get("structure", {}).get("formula"),
    }
    gateway_file = case_dir / "gateway-inspect.json"
    if gateway_file.is_file():
        try:
            gateway = json.loads(gateway_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            gateway = None
        if gateway:
            differences = compare_with_gateway(manifest, gateway)
            record["gateway_compare"] = {
                "equal": differences["equal"],
                "differences": differences["differences"],
            }
    return record


def _numeric(value: Any) -> float | None:
    import re as _re
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _re.match(r"^\s*([-+0-9.eEdD]+)", str(value))
    if not match:
        return None
    try:
        return float(match.group(1).replace("d", "e").replace("D", "E"))
    except ValueError:
        return None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().upper().split("(")[0].strip()
    if text in (".TRUE.", ".T.", "TRUE", "T", "1", "YES"):
        return True
    if text in (".FALSE.", ".F.", "FALSE", "F", "0", "NO"):
        return False
    return None


def equivalent(left: Any, right: Any) -> bool:
    """Value equality tolerant of str-vs-number ('520' == 520.0), trailing
    annotations ('100 (Max ionic steps)' == 100) and boolean spellings."""
    if left == right:
        return True
    left_bool, right_bool = _boolean(left), _boolean(right)
    if left_bool is not None and right_bool is not None:
        return left_bool == right_bool
    left_num, right_num = _numeric(left), _numeric(right)
    if left_num is not None and right_num is not None:
        return left_num == right_num
    return False


def compare_with_gateway(manifest: dict[str, Any], gateway: dict[str, Any]) -> dict[str, Any]:
    """Field-level diff (mirrors vasp_parse --compare logic, kept local)."""
    diffs: list[str] = []
    gw_files = gateway.get("files", {})
    my_files = manifest.get("results", {}).get("files", {})
    # Only report files the local parser has but the gateway does not.
    # Gateway-only files are usually uncollected large outputs (CHGCAR,
    # WAVECAR, ...) - a sampling difference, not a parsing difference.
    for name in sorted(my_files):
        gw_exists = bool((gw_files.get(name) or {}).get("exists"))
        if not gw_exists:
            diffs.append(f"files.{name}: gateway=False, parser=True")
    gw_incar = {k.lower(): v for k, v in (gateway.get("incar") or {}).items()}
    my_incar = manifest.get("inputs", {}).get("incar", {}).get("key_params", {})
    for key in sorted(set(gw_incar) | set(my_incar)):
        if not equivalent(gw_incar.get(key), my_incar.get(key)):
            diffs.append(f"incar.{key}: gateway={gw_incar.get(key)!r}, parser={my_incar.get(key)!r}")
    gw_st = gateway.get("structure") or {}
    my_st = manifest.get("structure") or {}
    if gw_st.get("species") != my_st.get("species"):
        diffs.append("structure.species mismatch")
    if gw_st.get("counts") != my_st.get("counts"):
        diffs.append("structure.counts mismatch")
    gw_err = {e.get("code") for e in (gateway.get("errors") or [])}
    my_err = {e["code"] for e in manifest.get("results", {}).get("errors", [])}
    if gw_err != my_err:
        diffs.append(f"errors: gateway-only={sorted(gw_err - my_err)}, parser-only={sorted(my_err - gw_err)}")
    return {"equal": not diffs, "differences": diffs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, help="directory of case directories")
    parser.add_argument("--out", default="", help="write the comparison JSON here")
    args = parser.parse_args(argv)

    cases_root = Path(args.cases)
    if not cases_root.is_dir():
        print(f"error: cases directory missing: {cases_root}", file=sys.stderr)
        return 2
    case_dirs = sorted(p for p in cases_root.iterdir() if p.is_dir())
    if not case_dirs:
        print(f"error: no case directories under {cases_root}", file=sys.stderr)
        return 2

    python_exe = best_python()
    print(f"using interpreter: {python_exe}")

    records = []
    for case_dir in case_dirs:
        record = compare_one(case_dir, python_exe)
        records.append(record)
        status = record.get("scientific_status", "parse failed")
        errors = ",".join(record.get("errors", [])) or "-"
        gateway = record.get("gateway_compare", {})
        equal = gateway.get("equal")
        gate = "-" if equal is None else ("agree" if equal else "DIFF")
        print(f"{record['case']:40s} status={status:22s} errors={errors:30s} gateway={gate}")

    summary = {
        "total_cases": len(records),
        "parse_ok": sum(1 for r in records if r.get("parse_ok")),
        "compared_with_gateway": sum(1 for r in records if r.get("gateway_compare") is not None),
        "gateway_agreement": sum(1 for r in records if (r.get("gateway_compare") or {}).get("equal")),
        "records": records,
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + chr(10), encoding="utf-8")
        print(f"comparison written to {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
