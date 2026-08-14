#!/usr/bin/env python3
"""Deterministic VASP result parser: directory -> CalculationManifest JSON.

Architecture:
- Core parsing (hashing, INCAR/POSCAR/KPOINTS/OSZICAR/OUTCAR essentials) is
  implemented with the Python standard library only and always works.
- pymatgen, when importable, upgrades structure fields (formula, lattice,
  normalized structure identity) and is recorded as the active parser.
- The same directory parsed twice yields the same manifest: every dynamic
  field comes from file content or explicit CLI arguments, never from
  wall-clock time.

Usage:
  py vasp_parse.py DIRECTORY --out manifest.json
  py vasp_parse.py DIRECTORY --probe            # parser availability only
  py vasp_parse.py DIRECTORY --compare gateway.json   # diff vs gateway inspector
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

SCHEMA_VERSION = "1.0"
TOOL_VERSION = "0.1.0"
MANIFEST_ID_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # v1 UUID namespace

# INCAR keys allowed into manifest.inputs.incar.key_params (non-secret subset).
SAFE_INCAR_KEYS = {
    "SYSTEM", "ENCUT", "EDIFF", "EDIFFG", "IBRION", "ISIF", "NSW", "ISMEAR",
    "SIGMA", "ISPIN", "NELM", "ALGO", "PREC", "LREAL", "LCHARG", "LWAVE",
    "IVDW", "GGA", "METAGGA", "ICHARG", "NEDOS", "EMIN", "EMAX", "LORBIT",
    "NBANDS", "KSPACING", "EDIFFG_PER_ATOM", "LAECHG",
}

# INCAR key type mapping: str stays str, these convert to float/int/bool.
INT_KEYS = {"IBRION", "ISIF", "NSW", "ISPIN", "NELM", "ICHARG", "NEDOS", "LORBIT", "NBANDS", "ISMEAR"}
FLOAT_KEYS = {"ENCUT", "EDIFF", "EDIFFG", "SIGMA", "EMIN", "EMAX", "KSPACING"}
BOOL_KEYS = {"LCHARG", "LWAVE", "LAECHG"}

# VASP error signatures recognized in OUTCAR/OSZICAR, mapped to custodian
# handler names and severity. Detection only: nothing is ever modified.
ERROR_SIGNATURES: list[tuple[str, str, str]] = [
    # (code, regex, custodian_handler)
    ("zbrent", r"ZBRENT", "ZBRENTVaspErrorHandler"),
    ("brmix", r"BRMIX", "BRMIXVaspErrorHandler"),
    ("edddav", r"EDDDAV|Error EDDDAV", "EddDavErrorHandler"),
    ("nelm", r"WARNING in EDDRMM|NELM", "NELMVaspErrorHandler"),
    ("walltime", r"wall time|DUE TO TIME LIMIT", "WalltimeHandler"),
    ("zpotrf", r"LAPACK: Routine ZPOTRF|ZPOTRF", "ZpotrfVaspErrorHandler"),
    ("pssyevx", r"PSSYEVX", "PssyevxVaspErrorHandler"),
    ("not_converged", r"reached required accuracy - stopping structural energy minimisation", ""),
    ("segfault", r"segmentation fault", ""),
]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text(path: Path, limit: int = 8_000_000) -> str:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        data = data[-limit:]  # keep the tail: VASP writes progress at the end
    return data.decode("utf-8", errors="replace")


def parse_incar(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.split("!", 1)[0].split("#", 1)[0]
        for item in line.split(";"):
            if "=" in item:
                key, value = item.split("=", 1)
                key = key.strip().upper()
                if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                    values[key] = value.strip()
    return values


def incar_key_params(values: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    # sorted() is mandatory: set intersection order varies with PYTHONHASHSEED,
    # which would break byte-for-byte determinism across runs.
    for key in sorted(SAFE_INCAR_KEYS & values.keys()):
        raw = values[key]
        if key in BOOL_KEYS:
            out[key.lower()] = raw.upper().startswith(("T", ".TRUE", "1"))
        elif key in INT_KEYS:
            try:
                out[key.lower()] = int(float(raw))
            except ValueError:
                continue
        elif key in FLOAT_KEYS:
            try:
                out[key.lower()] = float(raw)
            except ValueError:
                continue
        else:
            out[key.lower()] = raw
    return out


def parse_poscar(raw: str) -> dict[str, Any]:
    """Standard-library POSCAR parser (VASP 4/5 style; selective dynamics tolerated)."""
    lines = raw.splitlines()
    info: dict[str, Any] = {"source_file": "POSCAR"}
    if not lines:
        return info
    info["title"] = lines[0].strip()
    # lines[0]=title, lines[1]=scale factor, lines[2:5]=three lattice vectors.
    numbers = re.findall(r"[-+0-9.eE]+", " ".join(lines[2:5]) if len(lines) > 4 else "")
    if len(numbers) >= 9:
        cell = [float(x) for x in numbers[:9]]
        info["lattice"] = cell
        # a,b,c are the vector lengths of the three lattice rows.
        info["lattice_abc"] = [round((cell[i * 3] ** 2 + cell[i * 3 + 1] ** 2 + cell[i * 3 + 2] ** 2) ** 0.5, 6)
                               for i in range(3)]
    # Species/counts: line 6 (index 5), or line 7 (index 6) with selective dynamics.
    found = False
    for idx in (5, 6):
        if len(lines) <= idx + 1:
            continue
        species_line = lines[idx].split()
        counts_line = lines[idx + 1].split()
        if species_line and all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", x) for x in species_line) \
                and all(x.isdigit() for x in counts_line):
            found = True
            break
    if not found:
        info["parse_error"] = "could not locate species/counts lines"
        return info
    info["species"] = species_line
    info["counts"] = [int(x) for x in counts_line]
    info["n_atoms"] = sum(info["counts"])
    info["lattice_hash"] = lattice_hash([float(x) for x in numbers[:9]] if len(numbers) >= 9 else [])
    return info


def lattice_hash(cell: list[float]) -> str:
    """Normalized lattice fingerprint: rounded matrix -> canonical string hash.

    Rounds to 4 decimals so equivalent ASCII representations of the same cell
    (different whitespace/precision) hash identically.
    """
    if not cell:
        return ""
    normalized = ",".join(f"{v:.4f}" for v in cell)
    return hashlib.sha256(("lattice:" + normalized).encode()).hexdigest()


def parse_kpoints(raw: str) -> dict[str, Any]:
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().lower().startswith(("!", "#"))]
    info: dict[str, Any] = {"type": "unknown"}
    if not lines:
        return info
    # Line-mode (能带): 4th line starts with 'l'/'L' (ignoring the auto header).
    explicit = lines[3] if len(lines) > 3 else ""
    if explicit.lower().startswith("l"):
        info["type"] = "line"
        coords = lines[4:]
        info["n_kpoints"] = len(coords)
        return info
    header = lines[0].lower()
    if header.startswith("a"):
        info["type"] = "automatic"
        grid_line = lines[3] if len(lines) > 3 else ""
        grid = re.findall(r"\d+", grid_line)
        if len(grid) == 3:
            info["grid"] = [int(x) for x in grid]
        if header.startswith("ag"):
            info["type"] = "gamma"
        return info
    if header.startswith(("g", "m", "c", "k")):
        grid = [int(x) for x in re.findall(r"\d+", header)]
        if len(grid) >= 3:
            info["type"] = "gamma" if header.startswith("g") else "monkhorst-pack"
            info["grid"] = grid[:3]
            info["shift"] = grid[3:6] if len(grid) >= 6 else [0, 0, 0]
        return info
    # Explicit list: counts as a generic list.
    try:
        count = int(lines[0])
        info["type"] = "explicit"
        info["n_kpoints"] = count
    except ValueError:
        pass
    return info


def potcar_titles(raw: str) -> list[str]:
    return [m.strip()[:160] for m in re.findall(r"^\s*TITEL\s*=\s*(.+)$", raw, re.M)]


def parse_oszicar(raw: str) -> dict[str, Any]:
    """Ionic/electronic steps, energies, forces, magnetization from OSZICAR."""
    ionic = 0
    energies: list[float] = []
    forces: list[float] = []
    mags: list[float] = []
    for line in raw.splitlines():
        if re.match(r"^\s*\d+\s+F=", line):
            ionic += 1
        if "F=" in line:
            m = re.search(r"F=\s*([-+0-9.eE]+)", line)
            if m:
                forces.append(float(m.group(1)))
        m = re.search(r"E0=\s*([-+0-9.eE]+)", line)
        if m:
            energies.append(float(m.group(1)))
        if "mag=" in line:
            m = re.search(r"mag=\s*([-+0-9.eE]+)", line)
            if m:
                mags.append(float(m.group(1)))
    # Total electronic SCF iterations across all ionic steps.
    scf_total = len(re.findall(r"\b(?:DAV|RMM):\s*\d+", raw))
    return {
        "ionic_steps": ionic,
        "energies": energies,
        "e0_ev": energies[-1] if energies else None,
        "forces": forces,
        "max_force_ev_a": max(abs(f) for f in forces) if forces else None,
        "magnetization": mags[-1] if mags else None,
        "scf_total": scf_total,
    }


def parse_outcar_tail(raw: str) -> dict[str, Any]:
    info: dict[str, Any] = {}
    m = re.search(r"vasp\.([0-9.]+)", raw, re.I)
    if m:
        info["vasp_version"] = m.group(1)
    if "General timing and accounting informations for this job" in raw:
        info["completed"] = True
    else:
        info["completed"] = False
    return info


def detect_errors(outcar: str, oszicar: str) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    haystack = outcar[-4_000_000:] + oszicar
    for code, pattern, handler in ERROR_SIGNATURES:
        if not pattern:
            continue
        matches = re.findall(pattern, haystack)
        if matches:
            errors.append({
                "code": code,
                "severity": "error" if code not in ("walltime", "not_converged") else "warning",
                "count": len(matches),
                "evidence": pattern,
                "custodian_handler": handler,
            })
    return errors


def try_pymatgen():
    try:
        import pymatgen  # noqa: F401
        from pymatgen.io.vasp import Poscar

        return Poscar
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", help="VASP calculation directory (optional with --probe)")
    parser.add_argument("--out", "-o", help="write manifest JSON here (default: stdout)")
    parser.add_argument("--probe", action="store_true", help="print parser availability and exit")
    parser.add_argument("--workflow", default="static",
                        choices=["relax", "static", "band", "dos", "convergence-scan"])
    parser.add_argument("--stage", default="", help="stage label such as 00_relax")
    parser.add_argument("--server", default="", help="de-identified server name")
    parser.add_argument("--job-id", default="", help="Slurm job id if known")
    parser.add_argument("--parser", choices=["auto", "pymatgen", "fallback"], default="auto")
    parser.add_argument("--compare", help="gateway inspector JSON to diff against")
    args = parser.parse_args(argv)

    if args.probe:
        poscar_cls = try_pymatgen()
        print(json.dumps({
            "pymatgen_available": poscar_cls is not None,
            "active_parser": "pymatgen" if poscar_cls else "fallback",
            "tool_version": TOOL_VERSION,
            "schema_version": SCHEMA_VERSION,
        }, indent=2))
        return 0

    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 2

    poscar_cls = None
    if args.parser != "fallback":
        poscar_cls = try_pymatgen()
        if args.parser == "pymatgen" and poscar_cls is None:
            print("error: pymatgen requested but not importable", file=sys.stderr)
            return 2

    incar_path = directory / "INCAR"
    poscar_path = directory / "POSCAR"
    kpoints_path = directory / "KPOINTS"
    potcar_path = directory / "POTCAR"
    outcar_path = directory / "OUTCAR"
    oszicar_path = directory / "OSZICAR"

    structure: dict[str, Any] = {"source_file": "POSCAR"}
    if poscar_path.is_file():
        structure = parse_poscar(read_text(poscar_path, 2_000_000))
        structure["sha256"] = file_sha256(poscar_path)
        if poscar_cls is not None:
            try:
                pstruct = poscar_cls.from_file(poscar_path).structure
                structure["formula"] = pstruct.composition.reduced_formula
                structure["lattice_abc"] = [round(float(x), 6) for x in pstruct.lattice.abc]
                structure["n_atoms"] = int(pstruct.num_sites)
                structure["lattice_hash"] = lattice_hash([float(v) for v in pstruct.lattice.matrix.flatten()])
            except Exception as exc:  # keep fallback fields on pymatgen failure
                structure["pymatgen_error"] = str(exc)[:200]
    elif (directory / "CONTCAR").is_file():
        structure = parse_poscar(read_text(directory / "CONTCAR", 2_000_000))
        structure["source_file"] = "CONTCAR"
        structure["sha256"] = file_sha256(directory / "CONTCAR")

    incar_values = parse_incar(read_text(incar_path, 500_000)) if incar_path.is_file() else {}

    potcar_info: dict[str, Any] = {"sha256": ""}
    if potcar_path.is_file():
        potcar_info = {
            "sha256": file_sha256(potcar_path),
            "titles": potcar_titles(read_text(potcar_path, 8_000_000)),
            "n_datasets": len(potcar_titles(read_text(potcar_path, 8_000_000))),
        }

    outcar_raw = read_text(outcar_path) if outcar_path.is_file() else ""
    oszicar_raw = read_text(oszicar_path, 2_000_000) if oszicar_path.is_file() else ""
    outcar_tail = parse_outcar_tail(outcar_raw)
    osz = parse_oszicar(oszicar_raw)
    errors = detect_errors(outcar_raw, oszicar_raw)

    # Scientific status, strictly separated from scheduler/program status.
    electronic_converged = outcar_tail.get("completed", False) and not any(
        e["code"] == "nelm" for e in errors)
    ionic_converged = False
    ionic_criterion = ""
    if incar_values.get("IBRION") in ("-1", "0") or incar_values.get("NSW") == "0":
        ionic_converged = outcar_tail.get("completed", False)  # static: no ionic loop
        ionic_criterion = "static"
    else:
        reached = bool(re.search(
            r"reached required accuracy - stopping structural energy minimisation",
            outcar_raw))
        ionic_converged = reached and electronic_converged
        ionic_criterion = "EDIFFG/EDIFF" if reached else ("NSW exhausted" if osz["ionic_steps"] >= int(incar_values.get("NSW", 0) or 0) else "")

    if not outcar_raw:
        scientific_status = "not_started"
    elif ionic_converged:
        scientific_status = "ionic_converged"
    elif electronic_converged:
        scientific_status = "electronic_converged"
    elif errors:
        scientific_status = "failed"
    elif outcar_tail.get("completed"):
        scientific_status = "unconverged"
    else:
        scientific_status = "running"

    n_atoms = structure.get("n_atoms") or 0
    e0 = osz["e0_ev"]
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": str(uuid.uuid5(MANIFEST_ID_NS, str(directory.resolve()))),
        "workflow": {"name": args.workflow, "version": "1.0"},
        "structure": structure,
        "inputs": {
            "incar": {
                "sha256": file_sha256(incar_path) if incar_path.is_file() else "",
                "key_params": incar_key_params(incar_values),
            },
            "kpoints": (lambda p: {"sha256": file_sha256(p), **parse_kpoints(read_text(p, 100_000))})(kpoints_path)
            if kpoints_path.is_file() else {"sha256": ""},
            "potcar": potcar_info,
            "vasp_version": outcar_tail.get("vasp_version", ""),
        },
        "environment": {
            "server": args.server,
            "job_id": args.job_id or None,
        },
        "software": {
            "toolchain_version": TOOL_VERSION,
            "vasp_parse_version": TOOL_VERSION,
            "parser": "pymatgen" if poscar_cls else "fallback",
        },
        "execution": {
            "status": "finished" if outcar_tail.get("completed") else ("unknown" if not outcar_raw else "running"),
            "started_at": None,
            "finished_at": None,
            "walltime_s": None,
        },
        "results": {
            "scientific_status": scientific_status,
            "energy": {
                "e0_ev": e0,
                "e0_ev_per_atom": (e0 / n_atoms) if (e0 is not None and n_atoms) else None,
                "total_magnetization": osz["magnetization"],
            },
            "electronic": {
                "converged": electronic_converged,
                "scf_total": osz["scf_total"],
            },
            "ionic": {
                "converged": ionic_converged,
                "steps": osz["ionic_steps"],
                "max_force_ev_a": osz["max_force_ev_a"],
                "criterion": ionic_criterion,
            },
            "errors": errors,
            "files": {p.name: {"sha256": file_sha256(p), "size": p.stat().st_size}
                      for p in directory.iterdir()
                      if p.is_file() and p.name in
                      {"INCAR", "POSCAR", "CONTCAR", "KPOINTS", "POTCAR", "OUTCAR",
                       "OSZICAR", "EIGENVAL", "DOSCAR", "CHGCAR", "WAVECAR", "XDATCAR"}},
        },
        "modifications": [],
        "review": {"reviewed": False, "reviewer": None, "conclusion": "pending", "notes": "", "reviewed_at": None},
    }
    if args.stage:
        manifest["workflow"]["stage"] = args.stage

    if args.compare:
        try:
            gateway = json.loads(Path(args.compare).read_text(encoding="utf-8"))
            manifest["comparison"] = compare_with_gateway(manifest, gateway)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: cannot compare: {exc}", file=sys.stderr)

    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"manifest written to {args.out}")
    else:
        print(text)
    return 0


def compare_with_gateway(manifest: dict[str, Any], gateway: dict[str, Any]) -> dict[str, Any]:
    """Field-level diff against the gateway's schema_version 1 inspector JSON."""
    diffs: list[str] = []
    gw_files = gateway.get("files", {})
    my_files = manifest["results"]["files"]
    for name in sorted(set(gw_files) | set(my_files)):
        g, m = gw_files.get(name), my_files.get(name)
        g_exists = bool(g and g.get("exists"))
        m_exists = bool(m)
        if g_exists != m_exists:
            diffs.append(f"files.{name}: gateway exists={g_exists}, parser exists={m_exists}")
    gw_incar = {k.lower(): v for k, v in (gateway.get("incar") or {}).items()}
    my_incar = manifest["inputs"]["incar"]["key_params"]
    for key in sorted(set(gw_incar) | set(my_incar)):
        if str(gw_incar.get(key)) != str(my_incar.get(key)):
            diffs.append(f"incar.{key}: gateway={gw_incar.get(key)!r}, parser={my_incar.get(key)!r}")
    gw_st = gateway.get("structure") or {}
    my_st = manifest["structure"]
    if gw_st.get("species") != my_st.get("species"):
        diffs.append(f"structure.species: gateway={gw_st.get('species')}, parser={my_st.get('species')}")
    if gw_st.get("counts") != my_st.get("counts"):
        diffs.append(f"structure.counts: gateway={gw_st.get('counts')}, parser={my_st.get('counts')}")
    gw_err = {e.get("code") for e in (gateway.get("errors") or [])}
    my_err = {e["code"] for e in manifest["results"]["errors"]}
    if gw_err != my_err:
        diffs.append(f"errors: gateway-only={sorted(gw_err - my_err)}, parser-only={sorted(my_err - gw_err)}")
    return {"differences": diffs, "equal": not diffs}


if __name__ == "__main__":
    sys.exit(main())
