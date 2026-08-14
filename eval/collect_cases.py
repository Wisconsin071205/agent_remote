#!/usr/bin/env python3
"""Evaluation case collector: from historical data to de-identified case dirs.

Three sources, all de-identifying by construction (no passwords, no TOTP,
no full POTCAR content, no personal paths written into case files):

  scan-local      - scan ~/.vaspilot/conversations/*.json and list candidate
                    remote calculation directories mentioned in tool calls
                    (local machine only, no server access)
  collect-remote  - for each approved path, pull a de-identified sample
                    through the gateway into eval/cases/<id>/ (needs an
                    active server connection)
  mirror-local    - turn LOCAL full calculation directories into de-identified
                    case directories (INCAR/POSCAR/KPOINTS full; OSZICAR tail;
                    OUTCAR error-context excerpts; POTCAR TITEL lines only)

Usage:
  py eval/collect_cases.py scan-local --min-mentions 2
  py eval/collect_cases.py collect-remote --server cl9 --paths paths.txt --out eval/cases
  py eval/collect_cases.py mirror-local --dirs dirlist.txt --out eval/cases
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

CONVERSATIONS = Path.home() / ".vaspilot" / "conversations"
REMOTE_PATH_RE = re.compile(r"(/[A-Za-z0-9._/+@=-]{2,})")
# VASP files we keep in full inside a case directory.
FULL_FILES = {"INCAR", "POSCAR", "KPOINTS"}
OSZICAR_TAIL_LINES = 200
OUTCAR_EXCERPT = 1_000_000  # bytes of the OUTCAR tail (error signatures live there)


def slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", path.strip("/")).replace("/", "__")[:80]


def scan_conversations() -> list[dict[str, Any]]:
    """Extract candidate remote directories from local conversation JSON files."""
    mentions: dict[str, dict[str, Any]] = {}
    files = sorted(CONVERSATIONS.glob("*.json"))
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        messages = data.get("messages") or data.get("conversation") or []
        for message in messages:
            if not isinstance(message, dict):
                continue
            text = json.dumps(message, ensure_ascii=False)
            for match in REMOTE_PATH_RE.finditer(text):
                raw = match.group(1)
                # Keep only plausible VASP calculation-ish paths: deeper than
                # two segments and not inside obvious system dirs.
                if raw.count("/") < 3:
                    continue
                if any(seg in raw for seg in ("/proc/", "/sys/", "/etc/", "/usr/", "/bin/", "/lib/", "/var/")):
                    continue
                entry = mentions.setdefault(raw, {"path": raw, "mentions": 0, "examples": []})
                entry["mentions"] += 1
                if len(entry["examples"]) < 3:
                    entry["examples"].append(file.name)
    ranked = sorted(mentions.values(), key=lambda item: item["mentions"], reverse=True)
    return ranked


def mirror_case(local_dir: Path, case_id: str, out_dir: Path) -> dict[str, Any]:
    """De-identify one local calculation directory into a case directory."""
    case_dir = out_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"case_id": case_id, "source": str(local_dir), "files": {}}
    for name in FULL_FILES:
        source = local_dir / name
        if source.is_file():
            content = source.read_text(encoding="utf-8", errors="replace")
            (case_dir / name).write_text(content, encoding="utf-8", newline=chr(10))
            summary["files"][name] = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    oszicar = local_dir / "OSZICAR"
    if oszicar.is_file():
        lines = oszicar.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = chr(10).join(lines[-OSZICAR_TAIL_LINES:]) + chr(10)
        (case_dir / "OSZICAR").write_text(tail, encoding="utf-8", newline=chr(10))
        summary["files"]["OSZICAR"] = f"tail {len(lines[-OSZICAR_TAIL_LINES:])} lines"
    outcar = local_dir / "OUTCAR"
    if outcar.is_file():
        with outcar.open("rb") as handle:
            data = handle.read(OUTCAR_EXCERPT + 1)
        if len(data) > OUTCAR_EXCERPT:
            data = data[-OUTCAR_EXCERPT:]
        # Keep only the tail (error signatures + timing block live there).
        (case_dir / "OUTCAR").write_bytes(data)
        summary["files"]["OUTCAR"] = f"tail {len(data)} bytes"
    potcar = local_dir / "POTCAR"
    if potcar.is_file():
        raw = potcar.read_text(encoding="utf-8", errors="replace")
        titles = [m.strip() for m in re.findall(r"^\s*TITEL\s*=\s*(.+)$", raw, re.M)]
        (case_dir / "POTCAR.titles.txt").write_text(chr(10).join(titles) + chr(10), encoding="utf-8")
        summary["files"]["POTCAR"] = f"{len(titles)} TITEL lines only (content NOT copied)"
    # Preserve the gateway inspector output when present (parser comparison).
    for extra in ("gateway-inspect.json",):
        source = local_dir / extra
        if source.is_file():
            (case_dir / extra).write_bytes(source.read_bytes())
            summary["files"][extra] = "copied"
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)

    p_scan = sub.add_parser("scan-local", help="scan local conversation history for candidates")
    p_scan.add_argument("--min-mentions", type=int, default=1)
    p_scan.add_argument("--out", default="", help="write the candidate list here (default stdout)")

    p_remote = sub.add_parser("collect-remote", help="pull de-identified samples through the gateway")
    p_remote.add_argument("--server", default="cl9")
    p_remote.add_argument("--identity-file", default="~/.ssh/vlab-identity.pem",
                          help="Vlab PEM key path (default: ~/.ssh/vlab-identity.pem)")
    p_remote.add_argument("--paths", required=True, help="file with one remote directory per line")
    p_remote.add_argument("--out", default="eval/cases")
    p_remote.add_argument("--dry-run", action="store_true")

    p_mirror = sub.add_parser("mirror-local", help="de-identify local calculation directories")
    p_mirror.add_argument("--dirs", required=True, help="file with one local directory per line")
    p_mirror.add_argument("--out", default="eval/cases")

    args = parser.parse_args(argv)

    if args.operation == "scan-local":
        candidates = [c for c in scan_conversations() if c["mentions"] >= args.min_mentions]
        text = json.dumps(candidates, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(text + chr(10), encoding="utf-8")
            print(f"{len(candidates)} candidates written to {args.out}")
        else:
            print(text)
        return 0

    if args.operation == "mirror-local":
        paths = [line.strip() for line in Path(args.dirs).read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        out_dir = Path(args.out)
        for index, raw in enumerate(paths, start=1):
            local_dir = Path(raw)
            if not local_dir.is_dir():
                print(f"skip (not a dir): {raw}", file=sys.stderr)
                continue
            case_id = f"case-{index:02d}-{slug(local_dir.name)}"
            summary = mirror_case(local_dir, case_id, out_dir)
            print(f"[{case_id}] <- {raw} files={sorted(summary['files'])}")
        return 0

    if args.operation == "collect-remote":
        import subprocess as sp

        paths = [line.strip() for line in Path(args.paths).read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        if not paths:
            print("error: --paths file is empty", file=sys.stderr)
            return 2
        if args.dry_run:
            for raw in paths:
                print(f"would collect: {raw}")
            print(f"total {len(paths)} paths (dry-run, nothing downloaded)")
            return 0
        identity = Path(args.identity_file).expanduser()
        if not identity.is_file():
            print(f"error: identity file missing: {identity}", file=sys.stderr)
            return 2
        controller = Path(__file__).resolve().parent.parent / "scripts" / "vasp-agent.ps1"
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        ok_count = 0
        for index, raw in enumerate(paths, start=1):
            remote_dir = raw.rstrip("/")

            def gateway(*operation_args: str) -> tuple[int, str]:
                command = [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(controller), operation_args[0],
                    "-ServerName", args.server,
                    "-IdentityFile", str(identity),
                ]
                if len(operation_args) > 1:
                    command += list(operation_args[1:])
                result = sp.run(command, capture_output=True, timeout=180,
                                encoding="utf-8", errors="replace")
                return result.returncode, (result.stdout or "").strip()

            code, inspect_out = gateway("vasp-inspect", "-RemotePath", remote_dir)
            if code != 0:
                print(f"skip [{remote_dir}]: vasp-inspect failed")
                continue
            try:
                inspect = json.loads(inspect_out)
            except json.JSONDecodeError:
                print(f"skip [{remote_dir}]: vasp-inspect output is not JSON")
                continue
            if not inspect.get("files", {}).get("INCAR", {}).get("exists"):
                print(f"skip [{remote_dir}]: no INCAR (not a calculation directory)")
                continue
            case_id = f"case-{index:02d}-{slug(remote_dir.rsplit('/', 1)[-1])}"
            case_dir = out_dir / case_id
            case_dir.mkdir(exist_ok=True)
            (case_dir / "gateway-inspect.json").write_text(
                json.dumps(inspect, ensure_ascii=False, indent=2), encoding="utf-8")
            for name in ("INCAR", "POSCAR", "KPOINTS"):
                code, content = gateway("read", "-RemotePath", remote_dir + "/" + name)
                if code == 0 and content:
                    (case_dir / name).write_text(content + chr(10), encoding="utf-8", newline=chr(10))
            code, content = gateway("tail", "-RemotePath", remote_dir + "/OSZICAR", "-Lines", "500")
            if code == 0 and content:
                (case_dir / "OSZICAR").write_text(content + chr(10), encoding="utf-8", newline=chr(10))
            code, content = gateway("tail", "-RemotePath", remote_dir + "/OUTCAR", "-Lines", "2000")
            if code == 0 and content:
                (case_dir / "OUTCAR").write_text(content + chr(10), encoding="utf-8", newline=chr(10))
            titles = inspect.get("potcar_titles") or []
            if titles:
                (case_dir / "POTCAR.titles.txt").write_text(
                    chr(10).join(titles) + chr(10), encoding="utf-8", newline=chr(10))
            collected = sorted(p.name for p in case_dir.iterdir())
            print(f"[{case_id}] <- {remote_dir} files={collected}")
            ok_count += 1
        print(f"collected {ok_count}/{len(paths)} paths into {out_dir}")
        return 0 if ok_count else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
