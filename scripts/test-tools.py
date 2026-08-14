#!/usr/bin/env python3
"""One-shot smoke test for the four deterministic tools (no server needed).

Creates a synthetic VASP directory, runs vasp_parse / workflow_prepare /
custodian_detect against it, and runs slurm_adapter's offline selftest.
Verifies: determinism, quarantine-safe inputs, no file modification by
custodian_detect, idempotent workflow generation.

Usage: py scripts/test-tools.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).parent


def run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run([sys.executable, *cmd], text=True, capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{result.stderr}")
    return result


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(("PASS  " if ok else "FAIL  ") + label + (f"  ({detail})" if detail and ok else ""))
        if not ok:
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="vasp-smoke-") as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "src"
        src.mkdir()
        (src / "INCAR").write_text(
            "SYSTEM = smoke\nENCUT = 500\nEDIFF = 1E-6\nEDIFFG = -0.02\nIBRION = 2\nISIF = 3\nNSW = 80\nISMEAR = 0\nSIGMA = 0.05\n",
            encoding="utf-8")
        (src / "POSCAR").write_text(
            "Smoke\n   1.0\n     4.7 0.0 0.0\n     0.0 6.0 0.0\n     0.0 0.0 4.7\n   Li   Fe   P    O\n   1    1    1    4\nDirect\n  0.5 0.5 0.5\n  0.0 0.0 0.0\n  0.25 0.25 0.25\n  0.25 0.75 0.25\n  0.75 0.25 0.75\n  0.75 0.75 0.75\n",
            encoding="utf-8")
        (src / "KPOINTS").write_text("Auto\n0\nGamma\n3 3 3\n0 0 0\n", encoding="utf-8")
        (src / "POTCAR").write_text("PAW_PBE Li\n   TITEL  = PAW_PBE Li\n", encoding="utf-8")
        (src / "OUTCAR").write_text(
            " vasp.6.4.3\n BRMIX: very serious problems\n General timing and accounting informations for this job\n",
            encoding="utf-8")
        (src / "OSZICAR").write_text(
            "   1 F= -.100E+03 E0= -.100E+03  d E =-.358E+03  mag= 0.0000\n",
            encoding="utf-8")

        # 1. vasp_parse determinism
        m1, m2 = src / "m1.json", src / "m2.json"
        run(str(SCRIPTS / "vasp_parse.py"), str(src), "--out", str(m1), "--workflow", "relax")
        run(str(SCRIPTS / "vasp_parse.py"), str(src), "--out", str(m2), "--workflow", "relax")
        check("vasp_parse deterministic", sha256(m1) == sha256(m2))
        manifest = json.loads(m1.read_text(encoding="utf-8"))
        check("vasp_parse energy parsed", manifest["results"]["energy"]["e0_ev"] == -100.0,
              str(manifest["results"]["energy"]["e0_ev"]))
        check("vasp_parse species", manifest["structure"]["species"] == ["Li", "Fe", "P", "O"])

        # 2. workflow_prepare idempotency + overwrite protection
        out_dir = tmp_path / "out"
        run(str(SCRIPTS / "workflow_prepare.py"), "relax-static", "--from-dir", str(src),
            "--base-dir", str(out_dir), "--set", "ENCUT=520")
        plan = json.loads((out_dir / "Smoke" / "plan.json").read_text(encoding="utf-8"))
        check("workflow plan written", plan["state"] == "PREPARED")
        run(str(SCRIPTS / "workflow_prepare.py"), "relax-static", "--from-dir", str(src),
            "--base-dir", str(out_dir), "--set", "ENCUT=520")
        check("workflow idempotent re-run", True)
        refused = run(str(SCRIPTS / "workflow_prepare.py"), "relax-static", "--from-dir", str(src),
                      "--base-dir", str(out_dir), "--set", "ENCUT=600", check=False)
        check("workflow refuses differing overwrite", refused.returncode != 0)
        static_incar = (out_dir / "Smoke" / "01_static" / "INCAR").read_text(encoding="utf-8")
        check("static forces IBRION=-1 NSW=0", "IBRION = -1" in static_incar and "NSW = 0" in static_incar)
        check("static keeps CHGCAR", "LCHARG = .TRUE." in static_incar)

        # 3. custodian_detect: finds errors, modifies nothing, proposes patch
        incar_before = (src / "INCAR").read_text(encoding="utf-8")
        report_raw = run(str(SCRIPTS / "custodian_detect.py"), str(src), "--propose")
        report = json.loads(report_raw.stdout)
        handlers = {f["handler"] for f in report["findings"]}
        check("detector finds BRMIX", "BRMIXVaspErrorHandler" in handlers)
        check("detector modified nothing", report["modified_files"] == [] and
              (src / "INCAR").read_text(encoding="utf-8") == incar_before)
        patch = src / "INCAR.proposed.patch"
        check("patch preview written", patch.is_file() and "+ICHARG = 1" in patch.read_text(encoding="utf-8"))

        # 4. slurm_adapter selftest
        result = run(str(SCRIPTS / "slurm_adapter.py"), "selftest", check=False)
        check("slurm_adapter selftest", result.returncode == 0)

        # 5. workflow_state machine
        state_result = run(str(SCRIPTS / "workflow_state.py"), "selftest", check=False)
        check("workflow_state selftest", state_result.returncode == 0)
        calc_dir = out_dir / "Smoke" / "00_relax"
        run(str(SCRIPTS / "workflow_state.py"), "init", str(calc_dir),
            "--workflow", "relax-static", "--plan-sha", plan["plan_sha256"], "--by", "test-tools")
        run(str(SCRIPTS / "workflow_state.py"), "advance", str(calc_dir),
            "--to", "VALIDATED", "--by", "test-tools", "--note", "smoke")
        status = json.loads(run(str(SCRIPTS / "workflow_state.py"), "status", str(calc_dir)).stdout)
        check("state advanced VALIDATED", status["state"] == "VALIDATED" and len(status["history"]) == 2)

        # 6. apply_patch L2 (detect -> propose -> dry-run -> apply -> audit)
        run(str(SCRIPTS / "custodian_detect.py"), str(src), "--propose")
        patch = src / "INCAR.proposed.patch"
        dry = run(str(SCRIPTS / "apply_patch.py"), str(src), "--patch", str(patch), "--dry-run")
        check("apply_patch dry-run", "dry-run: nothing written" in dry.stdout)
        incar_before_apply = (src / "INCAR").read_text(encoding="utf-8")
        run(str(SCRIPTS / "apply_patch.py"), str(src), "--patch", str(patch), "--by", "test-tools")
        applied = (src / "INCAR").read_text(encoding="utf-8")
        check("apply_patch applied", applied != incar_before_apply and "ICHARG = 1" in applied)
        audit_lines = (src / ".vaspilot-patches.jsonl").read_text(encoding="utf-8").strip().splitlines()
        check("apply_patch audit", len(audit_lines) == 1 and json.loads(audit_lines[0])["by"] == "test-tools")
        bad_patch = src / "POSCAR.patch"
        bad_patch.write_text("--- POSCAR (original)\n+++ POSCAR (proposed)\n@@ -1,1 +1,1 @@\n-Evil\n+Hacked\n", encoding="utf-8")
        refused = run(str(SCRIPTS / "apply_patch.py"), str(src), "--patch", str(bad_patch), check=False)
        check("apply_patch rejects non-whitelist", refused.returncode != 0 and "allow list" in refused.stderr)

        # 7. agent_tools surface
        agent_result = run(str(SCRIPTS / "agent_tools.py"), "selftest", check=False)
        check("agent_tools selftest", agent_result.returncode == 0)

    print()
    if failures:
        print(f"{len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("all smoke tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
