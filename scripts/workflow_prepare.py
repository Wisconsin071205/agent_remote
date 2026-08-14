#!/usr/bin/env python3
"""Deterministic workflow planner: generates the relax -> static directory tree.

Design rules (spec/workflows.md):
- Only the relax-static workflow exists for now; more stages land later.
- Same inputs always produce the byte-identical plan (timestamps stay out
  of plan.json; a run log with wall-clock time goes to stdout only).
- Parameter overrides are whitelist-only; every change is recorded in
  plan.json modifications with before/after values.
- Generation never overwrites differing files: identical content is skipped
  (idempotent), any difference is an error.
- --dry-run prints the plan without writing anything; submission is a
  separate, explicitly approved step (slurm_adapter.py submit).

Usage:
  py workflow_prepare.py relax-static --from-dir SRCDIR --base-dir OUTDIR
  py workflow_prepare.py relax-static --from-dir SRCDIR --base-dir OUTDIR --set ENCUT=520 --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

TOOL_VERSION = "0.1.0"
PLAN_NS = uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")

# Whitelist of INCAR keys the planner may ever touch. Everything else in the
# template passes through untouched, and non-whitelist --set values are rejected.
ALLOWED_INCAR_KEYS = {
    "SYSTEM", "ENCUT", "EDIFF", "EDIFFG", "IBRION", "ISIF", "NSW",
    "ISMEAR", "SIGMA", "ISPIN", "MAGMOM", "LREAL", "PREC", "ALGO",
    "NELM", "ISYM", "LCHARG", "LWAVE", "LASPH", "LDAU", "IVDW",
    "GGA", "METAGGA", "ICHARG", "NEDOS", "EMIN", "EMAX", "LORBIT",
}

# INCAR adjustments the planner itself applies per stage (recorded as
# modifications with author=tool-l2 semantics: patch preview + approval).
STATIC_FORCE = {"IBRION": "-1", "NSW": "0"}
STATIC_DEFAULT_LCHARG = "LCHARG=.TRUE."

RELAX_STAGE = "00_relax"
STATIC_STAGE = "01_static"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_incar_lines(raw: str) -> dict[str, str]:
    """INCAR key -> full assignment text (value only, comments dropped)."""
    values: dict[str, str] = {}
    for line in raw.splitlines():
        body = line.split("!", 1)[0].split("#", 1)[0]
        for item in body.split(";"):
            if "=" in item:
                key, value = item.split("=", 1)
                key = key.strip().upper()
                if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                    values[key] = value.strip()
    return values


def apply_incar_edits(raw: str, edits: dict[str, str]) -> tuple[str, list[dict[str, Any]]]:
    """Apply edits preserving line structure; append new keys at the end."""
    existing = parse_incar_lines(raw)
    out_lines: list[str] = []
    changed: set[str] = set()
    for line in raw.splitlines():
        body = line.split("!", 1)[0].split("#", 1)[0]
        rewritten = False
        for item in body.split(";"):
            if "=" in item:
                key, _value = item.split("=", 1)
                key = key.strip().upper()
                if key in edits and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                    new_line = re.sub(r"(?i)\b" + re.escape(key) + r"\s*=\s*[^;!]*",
                                      key + " = " + edits[key], line, count=1)
                    out_lines.append(new_line)
                    rewritten = True
                    changed.add(key)
                    break
        if not rewritten:
            out_lines.append(line)
    appended = [k for k in edits if k not in changed]
    if appended:
        out_lines.extend(k + " = " + edits[k] for k in appended)
    modifications = []
    for key in edits:
        before = existing.get(key)
        if before == edits[key]:
            continue  # value already equal: skip the no-op record
        modifications.append({
            "file": "INCAR", "field": "INCAR." + key,
            "before": before, "after": edits[key],
            "reason": "workflow_prepare stage policy or --set override",
            "author": "tool-l2",
        })
    return "\n".join(out_lines) + "\n", modifications


def render_kpoints(grid: list[int]) -> str:
    return "\n".join([
        "Automatic generation",
        "0",
        "Gamma",
        " ".join(str(x) for x in grid),
        "0 0 0",
        "",
    ])


def render_slurm(job_name: str, partition: str, nodes: int, ntasks: int, walltime: str) -> str:
    return "\n".join([
        "#!/bin/bash",
        "#SBATCH --job-name=" + job_name,
        "#SBATCH --nodes=" + str(nodes),
        "#SBATCH --ntasks-per-node=" + str(ntasks),
        "#SBATCH --partition=" + partition,
        "#SBATCH --time=" + walltime,
        "#SBATCH --output=slurm-%j.out",
        "#SBATCH --error=slurm-%j.err",
        "",
        "echo job $SLURM_JOB_ID on $SLURM_NODELIST",
        "mpirun -np $SLURM_NTASKS vasp_std",
        "",
    ])


def write_deterministic(path: Path, content: str, files: dict[str, Any], modifications: list[dict]) -> None:
    """Write a file; identical existing content is a no-op, differing content is an error."""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            return  # idempotent re-generation
        raise RuntimeError(f"refusing to overwrite differing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    files[path.name] = {"sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "size": len(content.encode("utf-8"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", choices=["relax-static"])
    parser.add_argument("--from-dir", required=True, help="source directory with POSCAR, INCAR, KPOINTS, POTCAR templates")
    parser.add_argument("--base-dir", required=True, help="where to create <system>/00_relax etc.")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="whitelisted INCAR override, repeatable")
    parser.add_argument("--kpoints", nargs=3, type=int, metavar="N",
                        help="generate KPOINTS gamma grid instead of copying")
    parser.add_argument("--partition", default="normal")
    parser.add_argument("--nodes", type=int, default=1)
    parser.add_argument("--ntasks", type=int, default=32)
    parser.add_argument("--walltime", default="24:00:00")
    parser.add_argument("--dry-run", action="store_true", help="print plan only, write nothing")
    parser.add_argument("--plan-out", default="", help="also write plan.json to this path (with --dry-run)")
    args = parser.parse_args(argv)

    source = Path(args.from_dir).resolve()
    base = Path(args.base_dir)
    required = ["POSCAR", "INCAR", "KPOINTS", "POTCAR"]
    missing = [name for name in required if not (source / name).is_file()]
    if missing:
        print("error: source missing: " + ", ".join(missing), file=sys.stderr)
        return 2

    overrides: dict[str, str] = {}
    for item in args.set:
        if "=" not in item:
            print(f"error: --set expects KEY=VALUE, got {item!r}", file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        key = key.strip().upper()
        if key not in ALLOWED_INCAR_KEYS:
            print(f"error: INCAR key {key} is not in the whitelist", file=sys.stderr)
            return 2
        overrides[key] = value.strip()

    # System label from POSCAR title (safe subset only).
    title = (source / "POSCAR").read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
    system = re.sub(r"[^A-Za-z0-9._-]", "_", title)[:40] or "calc"

    incar_template = (source / "INCAR").read_text(encoding="utf-8")
    kpoints_template = (source / "KPOINTS").read_text(encoding="utf-8")

    modifications: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    plan_files: dict[str, Any] = {}

    # ---- stage 1: relax ----
    relax_incar, relax_mods = apply_incar_edits(incar_template, overrides)
    modifications += relax_mods
    relax_dir = base / system / RELAX_STAGE
    relax_files: dict[str, Any] = {}
    relax_kpoints = render_kpoints(args.kpoints) if args.kpoints else kpoints_template
    relax_writes = [
        ("INCAR", relax_incar),
        ("POSCAR", (source / "POSCAR").read_text(encoding="utf-8")),
        ("KPOINTS", relax_kpoints),
        ("POTCAR", (source / "POTCAR").read_bytes()),
        ("run.slurm", render_slurm(system + "_relax", args.partition, args.nodes, args.ntasks, args.walltime)),
    ]

    # ---- stage 2: static ----
    # Force IBRION=-1 / NSW=0 (static policy), ensure CHGCAR retention, then
    # re-apply user overrides (no-ops are skipped by apply_incar_edits).
    static_incar_raw, force_mods = apply_incar_edits(relax_incar, STATIC_FORCE)
    modifications += force_mods
    if "LCHARG" not in parse_incar_lines(static_incar_raw):
        static_incar_raw, lc_mods = apply_incar_edits(static_incar_raw, {"LCHARG": ".TRUE."})
        modifications += lc_mods
    static_incar, static_mods = apply_incar_edits(static_incar_raw, overrides)
    modifications += static_mods
    static_dir = base / system / STATIC_STAGE
    static_files: dict[str, Any] = {}
    static_writes = [
        ("INCAR", static_incar),
        ("POSCAR", (source / "POSCAR").read_text(encoding="utf-8")),
        ("KPOINTS", relax_kpoints),
        ("POTCAR", (source / "POTCAR").read_bytes()),
        ("run.slurm", render_slurm(system + "_static", args.partition, args.nodes, args.ntasks, args.walltime)),
    ]

    if args.dry_run:
        plan = build_plan(system, overrides, modifications, args, relax_writes, static_writes)
        if args.plan_out:
            plan_file = Path(args.plan_out)
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    for name, content in relax_writes:
        if isinstance(content, str):
            write_deterministic(relax_dir / name, content, relax_files, modifications)
        else:
            write_bytes(relax_dir / name, content, relax_files)
    for name, content in static_writes:
        if isinstance(content, str):
            write_deterministic(static_dir / name, content, static_files, modifications)
        else:
            write_bytes(static_dir / name, content, static_files)

    plan = build_plan(system, overrides, modifications, args, relax_writes, static_writes)
    plan["stages"] = [
        {"name": RELAX_STAGE, "role": "relax", "depends_on": [], "files": relax_files, "structure_source": str(source / "POSCAR")},
        {"name": STATIC_STAGE, "role": "static", "depends_on": [RELAX_STAGE], "files": static_files,
         "structure_source_note": "replace POSCAR with 00_relax/CONTCAR after relax completes"},
    ]
    plan_path = base / system / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print("plan written to " + str(plan_path))
    print("next: validate inputs, then submit 00_relax via slurm_adapter submit")
    return 0


def write_bytes(path: Path, content: bytes, files: dict[str, Any]) -> None:
    if path.exists():
        if path.read_bytes() == content:
            return
        raise RuntimeError(f"refusing to overwrite differing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    files[path.name] = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}


def build_plan(system: str, overrides: dict[str, str], modifications: list[dict],
               args: argparse.Namespace, relax_writes: list, static_writes: list) -> dict[str, Any]:
    def content_hash(content) -> str:
        data = content.encode("utf-8") if isinstance(content, str) else content
        return hashlib.sha256(data).hexdigest()

    relax_hashes = [content_hash(c) for _n, c in relax_writes]
    static_hashes = [content_hash(c) for _n, c in static_writes]
    digest = hashlib.sha256()
    for item in sorted(overrides.items()) + relax_hashes + static_hashes:
        digest.update(str(item).encode("utf-8"))
    plan_sha = digest.hexdigest()
    return {
        "schema_version": "1.0",
        "plan_id": str(uuid.uuid5(PLAN_NS, plan_sha)),
        "workflow": {"name": "relax-static", "version": "1.0"},
        "created_via": "workflow_prepare " + TOOL_VERSION,
        "system": system,
        "plan_sha256": plan_sha,
        "parameter_overrides": overrides,
        "modifications": modifications,
        "slurm": {"partition": args.partition, "nodes": args.nodes, "ntasks": args.ntasks, "walltime": args.walltime},
        "stages": [
            {"name": RELAX_STAGE, "role": "relax", "depends_on": [], "files": {n: None for n, _c in relax_writes}},
            {"name": STATIC_STAGE, "role": "static", "depends_on": [RELAX_STAGE], "files": {n: None for n, _c in static_writes}},
        ],
        "state": "PREPARED",
    }


if __name__ == "__main__":
    sys.exit(main())