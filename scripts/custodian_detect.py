#!/usr/bin/env python3
"""VASP failure detector: detection and suggestions only, never modifies files.

Three-level repair policy (spec/workflows.md):
  L1 detect (default)   - report handler, evidence, suggested correction
  L2 propose            - --propose additionally writes a unified-diff preview
                          (.proposed.patch); a human approves before anything
                          is applied by a SEPARATE tool
  L3 auto-apply         - deliberately not implemented here; the whitelist is
                          empty by design

Engine selection: custodian is used when importable (its handlers carry the
reference rules); otherwise the builtin rules below take over. Both engines
share the same output schema, so historical-case comparison stays valid.

Usage:
  py custodian_detect.py DIR
  py custodian_detect.py DIR --propose
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any

TOOL_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"


# Builtin rules mirroring custodian's VASP handlers. Each rule declares the
# regex evidence, the handler name it corresponds to, and a suggested
# correction. Corrections are suggestions ONLY - no file is ever touched.
BUILTIN_RULES: list[dict[str, Any]] = [
    {
        "handler": "BRMIXVaspErrorHandler",
        "pattern": r"BRMIX",
        "severity": "error",
        "summary": "charge density mixer fails (BRMIX)",
        "suggestion": "restart from CHGCAR (ICHARG=1) and/or increase AMIX/BMIX in small steps (e.g. AMIX 0.2->0.1, BMIX 1.0->0.001); do not lower both aggressively at once",
        "incar_changes": {"ICHARG": "1"},
        "evidence_limit": 3,
    },
    {
        "handler": "ZBRENTVaspErrorHandler",
        "pattern": r"ZBRENT",
        "severity": "error",
        "summary": "band-energy interpolation fails (ZBRENT)",
        "suggestion": "increase NELM, switch ALGO (VeryFast->Normal or All), or use ISMEAR=0 with a larger SIGMA; check whether the system is metallic before changing ISMEAR",
        "incar_changes": {"NELM": "100"},
        "evidence_limit": 3,
    },
    {
        "handler": "EddDavErrorHandler",
        "pattern": r"EDDDAV|Error EDDDAV",
        "severity": "error",
        "summary": "EDDDAV subspace rotation fails",
        "suggestion": "try ALGO=Normal; if it persists check for NaN in POSCAR or bad POTCAR ordering",
        "incar_changes": {"ALGO": "Normal"},
        "evidence_limit": 3,
    },
    {
        "handler": "NELMVaspErrorHandler",
        "pattern": r"WARNING in EDDRMM",
        "severity": "warning",
        "summary": "electronic steps hit NELM",
        "suggestion": "increase NELM, check ISMEAR/SIGMA adequacy, or restart from WAVECAR",
        "incar_changes": {},
        "evidence_limit": 3,
    },
    {
        "handler": "WalltimeHandler",
        "pattern": r"wall time|DUE TO TIME LIMIT",
        "severity": "warning",
        "summary": "job ran out of wall time",
        "suggestion": "restart from CONTCAR/WAVECAR with a longer walltime or fewer NSW",
        "incar_changes": {},
        "evidence_limit": 3,
    },
    {
        "handler": "ZpotrfVaspErrorHandler",
        "pattern": r"ZPOTRF|LAPACK: Routine ZPOTRF",
        "severity": "error",
        "summary": "Cholesky decomposition fails (ZPOTRF)",
        "suggestion": "switch ALGO (Normal->Fast or All); often a numerical instability in the mixer",
        "incar_changes": {"ALGO": "Normal"},
        "evidence_limit": 3,
    },
    {
        "handler": "PssyevxVaspErrorHandler",
        "pattern": r"PSSYEVX",
        "severity": "error",
        "summary": "ScaLAPACK PSSYEVX fails",
        "suggestion": "switch ALGO to Normal (LAPACK) or check ScaLAPACK/NPROC setup",
        "incar_changes": {"ALGO": "Normal"},
        "evidence_limit": 3,
    },
    {
        "handler": "ZhegvVaspErrorHandler",
        "pattern": r"ZHEGV",
        "severity": "error",
        "summary": "ZHEGV eigensolver fails",
        "suggestion": "switch ALGO to Normal or check the basis set / parallel layout",
        "incar_changes": {"ALGO": "Normal"},
        "evidence_limit": 3,
    },
]
# Note: "reached required accuracy - stopping structural energy minimisation"
# is deliberately NOT a rule - it is VASP's NORMAL relaxation termination
# marker (reported through vasp_parse's ionic.converged), not a failure.


def read_tail(path: Path, limit: int = 8_000_000) -> str:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        data = data[-limit:]
    return data.decode("utf-8", errors="replace")


def detect_builtin(directory: Path) -> list[dict[str, Any]]:
    outcar = read_tail(directory / "OUTCAR") if (directory / "OUTCAR").is_file() else ""
    oszicar = read_tail(directory / "OSZICAR", 2_000_000) if (directory / "OSZICAR").is_file() else ""
    haystack = outcar + "\n" + oszicar
    findings: list[dict[str, Any]] = []
    for rule in BUILTIN_RULES:
        matches = list(re.finditer(rule["pattern"], haystack))
        if not matches:
            continue
        evidence = []
        for m in matches[:rule.get("evidence_limit", 3)]:
            start = max(0, m.start() - 80)
            end = min(len(haystack), m.end() + 120)
            evidence.append(haystack[start:end].replace("\n", " ").strip()[:200])
        findings.append({
            "handler": rule["handler"],
            "severity": rule["severity"],
            "summary": rule["summary"],
            "count": len(matches),
            "evidence": evidence,
            "suggested_correction": rule["suggestion"],
            "suggested_incar_changes": rule["incar_changes"],
            "auto_apply": False,
        })
    return findings


def detect_custodian(directory: Path) -> tuple[list[dict[str, Any]], str]:
    """Use custodian's real handlers when available; returns (findings, engine_label)."""
    try:
        import custodian
        from custodian.vasp.handlers import (
            BRMIXVaspErrorHandler, ZBRENTVaspErrorHandler, EddDavErrorHandler,
            NELMVaspErrorHandler, WalltimeHandler, ZpotrfVaspErrorHandler,
            PssyevxVaspErrorHandler,
        )
    except Exception as exc:
        return [], f"builtin (custodian unavailable: {exc})"

    outcar = read_tail(directory / "OUTCAR") if (directory / "OUTCAR").is_file() else ""
    findings: list[dict[str, Any]] = []
    handlers = [
        BRMIXVaspErrorHandler(), ZBRENTVaspErrorHandler(), EddDavErrorHandler(),
        NELMVaspErrorHandler(), WalltimeHandler(), ZpotrfVaspErrorHandler(),
        PssyevxVaspErrorHandler(),
    ]
    for handler in handlers:
        try:
            matched = bool(handler.check(0, 0, [], outcar, ""))
        except Exception:
            matched = False
        if not matched:
            continue
        msgs = getattr(handler, "error_msgs", [])
        findings.append({
            "handler": type(handler).__name__,
            "severity": "error",
            "summary": msgs[0] if msgs else "",
            "count": 1,
            "evidence": [msgs[0][:200]] if msgs else [],
            "suggested_correction": "see custodian handler.correct() logic; review before applying",
            "suggested_incar_changes": {},
            "auto_apply": False,
        })
    return findings, "custodian " + getattr(custodian, "__version__", "unknown")


def propose_patch(directory: Path, findings: list[dict[str, Any]]) -> str:
    """Write a unified-diff PREVIEW of suggested INCAR changes; original untouched."""
    incar = directory / "INCAR"
    if not incar.is_file() or not any(f["suggested_incar_changes"] for f in findings):
        return ""
    original = incar.read_text(encoding="utf-8")
    changes: dict[str, str] = {}
    for finding in findings:
        changes.update(finding["suggested_incar_changes"])
    modified_lines = original.splitlines()
    for key, value in sorted(changes.items()):
        found = False
        for i, line in enumerate(modified_lines):
            if re.match(rf"(?i)\b{re.escape(key)}\s*=", line):
                modified_lines[i] = f"{key} = {value}"
                found = True
                break
        if not found:
            modified_lines.append(f"{key} = {value}")
    hypothetical = "\n".join(modified_lines) + "\n"
    diff = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        hypothetical.splitlines(keepends=True),
        fromfile=str(incar) + " (original)",
        tofile=str(incar) + " (proposed)",
    ))
    patch_path = directory / "INCAR.proposed.patch"
    patch_path.write_text("".join(diff), encoding="utf-8")
    return str(patch_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", help="VASP calculation directory")
    parser.add_argument("--propose", action="store_true",
                        help="additionally write INCAR.proposed.patch (preview only)")
    parser.add_argument("--out", "-o", help="write JSON report here (default stdout)")
    args = parser.parse_args(argv)

    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 2

    findings, engine = detect_custodian(directory)
    if not findings and engine.startswith("builtin"):
        findings = detect_builtin(directory)
        engine = "builtin " + TOOL_VERSION

    patch_file = ""
    if args.propose:
        patch_file = propose_patch(directory, findings)

    report = {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "directory": str(directory),
        "findings": findings,
        "modified_files": [],
        "patch_file": patch_file,
        "note": "detection only; nothing was modified. Approve the patch before any external tool applies it.",
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"report written to {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
