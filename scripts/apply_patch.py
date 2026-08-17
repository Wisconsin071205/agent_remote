#!/usr/bin/env python3
"""L2 patch applier: applies an APPROVED unified-diff patch to whitelisted files.

Part of the three-level repair policy (spec/workflows.md):
  L1 detect   - custodian_detect.py reports, modifies nothing
  L2 apply    - this tool, after HUMAN approval: whitelisted files only,
                backup before write, append-only audit trail
  L3 auto     - not implemented; the whitelist is empty by design

Safety properties:
- Only files on the allow list may be touched (default: INCAR; --allow adds
  KPOINTS). POTCAR/POSCAR/CONTCAR/OUTCAR and everything else are always
  rejected.
- The diff is applied by a strict in-process unified-diff engine (no external
  patch command): every removed/context line must match the file exactly,
  otherwise the patch is rejected wholesale and nothing changes.
- Before writing, the original is backed up next to the file with a
  timestamped .pre-*.bak name.
- Every application appends an audit record to .vaspilot-patches.jsonl in
  the calculation directory (before/after hashes, patch hash, actor).

Usage:
  py apply_patch.py DIR --patch INCAR.proposed.patch            # apply
  py apply_patch.py DIR --patch INCAR.proposed.patch --dry-run  # preview only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOL_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"
AUDIT_FILE = ".vaspilot-patches.jsonl"
DEFAULT_ALLOWED = {"INCAR"}
EXTRA_ALLOWED = {"KPOINTS"}

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: (.*))?$")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PatchError(RuntimeError):
    pass


def apply_hunk(original: list[str], hunk_lines: list[str], old_start: int) -> list[str]:
    """Apply one hunk; returns the COMPLETE new line list."""
    out: list[str] = []
    # old_start == 0 marks an insertion at the very top (@@ -0,0 ...); clamp to
    # 0 so the prefix is empty instead of -1 (which would drop the last line).
    pos = max(0, old_start - 1)  # 0-based line index in original
    out.extend(original[:pos])  # untouched prefix before the hunk
    index = 0
    while index < len(hunk_lines):
        line = hunk_lines[index]
        if line.startswith(" "):
            if pos >= len(original) or original[pos] != line[1:]:
                raise PatchError(f"context mismatch at line {pos + 1}: expected {original[pos][:60] if pos < len(original) else 'EOF'!r}, got {line[1:][:60]!r}")
            out.append(original[pos])
            pos += 1
        elif line.startswith("-"):
            if pos >= len(original) or original[pos] != line[1:]:
                raise PatchError(f"removal mismatch at line {pos + 1}: expected {original[pos][:60] if pos < len(original) else 'EOF'!r}, got {line[1:][:60]!r}")
            pos += 1
        elif line.startswith("+"):
            out.append(line[1:])
        elif line == "\\ No newline at end of file":
            pass  # tolerated: we normalize trailing newlines ourselves
        else:
            raise PatchError(f"unrecognized hunk line: {line[:60]!r}")
        index += 1
    out.extend(original[pos:])  # untouched suffix after the hunk
    return out


def apply_unified_diff(original_text: str, diff_text: str) -> str:
    """Strict unified-diff applier. Returns the new content or raises PatchError."""
    original = original_text.splitlines()
    lines = diff_text.splitlines()
    hunks: list[tuple[int, list[str]]] = []
    current: list[str] = []
    old_start = 0
    in_hunk = False
    for line in lines:
        m = HUNK_RE.match(line)
        if m:
            if current:
                hunks.append((old_start, current))
            old_start = int(m.group(1))
            current = []
            in_hunk = True
        elif in_hunk and (current or line.startswith(("+", "-", " ")) or line.startswith("\\")):
            current.append(line)
        # diff headers (---/+++) and anything before the first @@ are ignored
    if current:
        hunks.append((old_start, current))
    if not hunks:
        raise PatchError("no hunks found in diff")
    # Apply hunks back-to-front so earlier hunks never shift the line numbers
    # of later ones (hunk headers always refer to the ORIGINAL file).
    working = list(original)
    for start, hunk in sorted(hunks, key=lambda item: item[0], reverse=True):
        working = apply_hunk(working, hunk, start)
    return "\n".join(working) + ("\n" if working else "")


def parse_diff_files(diff_text: str, directory: Path) -> tuple[Path, Path]:
    """Extract the from/to file basenames and resolve them inside directory."""
    targets: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            targets.append(line[4:].strip().split("\t")[0].split(" (")[0])
    if len(targets) != 1:
        raise PatchError("diff must modify exactly one file")
    target = Path(targets[0])
    if target.is_absolute():
        target = Path(target.name)
    if target.name not in (DEFAULT_ALLOWED | EXTRA_ALLOWED):
        raise PatchError(f"file {target.name} is not on the patch allow list")
    path = directory / target.name
    if not path.is_file():
        raise PatchError(f"target file does not exist: {path}")
    return path, target


def append_audit(directory: Path, record: dict[str, Any]) -> None:
    path = directory / AUDIT_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", help="calculation directory")
    parser.add_argument("--patch", required=True, help="unified diff file (e.g. INCAR.proposed.patch)")
    parser.add_argument("--allow", action="append", default=[], choices=sorted(EXTRA_ALLOWED),
                        help="additionally allow this file type (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="preview the result without writing")
    parser.add_argument("--by", default="human", help="actor recorded in the audit trail")
    args = parser.parse_args(argv)

    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 2
    patch_file = Path(args.patch)
    if not patch_file.is_file():
        print(f"error: patch file missing: {patch_file}", file=sys.stderr)
        return 2

    allowed = DEFAULT_ALLOWED | set(args.allow)
    diff_text = patch_file.read_text(encoding="utf-8")
    try:
        target, target_name = parse_diff_files(diff_text, directory)
        if target_name.name not in allowed:
            raise PatchError(f"file {target_name.name} not in allowed set {sorted(allowed)}")
        original_text = target.read_text(encoding="utf-8")
        new_text = apply_unified_diff(original_text, diff_text)
    except PatchError as exc:
        print(f"patch rejected: {exc}", file=sys.stderr)
        return 1

    before_hash = sha256_bytes(original_text.encode("utf-8"))
    after_hash = sha256_bytes(new_text.encode("utf-8"))
    if before_hash == after_hash:
        print("patch would change nothing; nothing to do")
        return 0

    changed_lines = len(new_text.splitlines()) - len(original_text.splitlines())
    print(f"target: {target_name.name}")
    print(f"before: {len(original_text.splitlines())} lines, sha256 {before_hash[:16]}...")
    print(f"after:  {len(new_text.splitlines())} lines, sha256 {after_hash[:16]}... (net {changed_lines:+d} lines)")
    for line in diff_text.splitlines():
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            print("  " + line[:100])

    if args.dry_run:
        print("dry-run: nothing written")
        return 0

    backup = target.with_name(target.name + ".pre-" + now_iso().replace(":", "") + ".bak")
    backup.write_text(original_text, encoding="utf-8", newline="\n")
    target.write_text(new_text, encoding="utf-8", newline="\n")
    record = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "time": now_iso(),
        "file": target.name,
        "backup": backup.name,
        "patch": str(patch_file),
        "patch_sha256": sha256_file(patch_file),
        "before_sha256": before_hash,
        "after_sha256": after_hash,
        "by": args.by,
        "dry_run": False,
    }
    append_audit(directory, record)
    print(f"applied; backup: {backup.name}; audit appended to {AUDIT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
