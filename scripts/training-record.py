#!/usr/bin/env python3
"""Create a redacted JSONL record for later VASP agent training/evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


SECRET_KEYS = re.compile(r"(?i)(password|passwd|secret|token|api.?key|private.?key|totp|otp)")
REMOTE_HOME = re.compile(r"(?:/public)?/home/[^/\s]+")
PEM_BLOCK = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S)
API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            cleaned[key] = "<REDACTED>" if SECRET_KEYS.search(str(key)) else redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = PEM_BLOCK.sub("<PRIVATE_KEY_REDACTED>", value)
        value = API_KEY.sub("<API_KEY_REDACTED>", value)
        value = REMOTE_HOME.sub("<REMOTE_ROOT>", value)
        return value
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a redacted VASP training/evaluation record")
    parser.add_argument("--task", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--tool-name", required=True)
    parser.add_argument("--tool-result", required=True, type=Path)
    parser.add_argument("--expert-answer", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expert-reviewed", action="store_true")
    args = parser.parse_args()

    try:
        result = json.loads(args.tool_result.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read tool result: {exc}", file=sys.stderr)
        return 2
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        print("error: expected a schema_version 1 VASP tool result", file=sys.stderr)
        return 2

    record = redact({
        "schema_version": 1,
        "task": args.task,
        "user_request": args.request,
        "tool_name": args.tool_name,
        "tool_arguments": {"directory": result.get("directory", "<REMOTE_ROOT>")},
        "tool_result": result,
        "expert_answer": args.expert_answer,
        "labels": {"safe": True, "expert_reviewed": args.expert_reviewed},
    })
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("a", encoding="utf-8") as handle:
            handle.write(line)
    else:
        sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
