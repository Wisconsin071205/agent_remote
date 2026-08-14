#!/usr/bin/env python3
"""Interactive DeepSeek agent backed by the restricted VASP controller."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


SCRIPT_DIR = Path(__file__).resolve().parent
CONTROLLER = SCRIPT_DIR / "vasp-agent.ps1"
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEFAULT_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
SYSTEM_PROMPT = """You are a VASP-focused computational materials agent operating restricted VASP/Slurm tools through a Vlab gateway.
Use tools for factual server state. Never ask for, store, repeat, or infer passwords, TOTP seeds,
one-time codes, or private keys. Never invent tool results. A per-turn server context message tells
you which server is active; every tool call acts on that server only. If the active server is
disconnected, tell the user to run the local connect command. Validate VASP inputs before submission.
Distinguish program completion, electronic convergence, and ionic convergence.
Explain a proposed submission or cancellation before calling it.
Keep answers concise and reply in the user's language."""


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "connection_status",
            "description": "Check whether the reusable SSH connection to the active server is active.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "server_identity",
            "description": "Show the connected server's hostname, Unix user, home, and working directory.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_jobs",
            "description": "List the current user's active Slurm jobs on the active server.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_jobs",
            "description": "List the current user's Slurm job history since today.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_vasp_calculation",
            "description": "Inspect a VASP calculation directory and return structured input metadata, progress, convergence flags, energies, and recognized errors without sending full POTCAR content.",
            "parameters": {
                "type": "object",
                "properties": {"directory": {"type": "string", "description": "Absolute VASP calculation directory under the active server's allowed root"}},
                "required": ["directory"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_vasp_inputs",
            "description": "Run deterministic VASP preflight checks before submission, including required files and basic POSCAR/POTCAR consistency.",
            "parameters": {
                "type": "object",
                "properties": {"directory": {"type": "string", "description": "Absolute VASP calculation directory under the active server's allowed root"}},
                "required": ["directory"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vasp_progress",
            "description": "Return a compact structured VASP progress and convergence summary for a calculation directory.",
            "parameters": {
                "type": "object",
                "properties": {"directory": {"type": "string", "description": "Absolute VASP calculation directory under the active server's allowed root"}},
                "required": ["directory"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_remote_file",
            "description": "Read a text file under the active server's allowed root. Its content will be shared with the DeepSeek API only after local approval.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Absolute remote path under the active server's allowed root"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tail_remote_file",
            "description": "Read the final lines of a text file under the active server's allowed root after local approval to share them with DeepSeek.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute remote path under the active server's allowed root"},
                    "lines": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_remote_directory",
            "description": "List the contents of a remote directory under the active server's allowed root, including file sizes and modification times.",
            "parameters": {
                "type": "object",
                "properties": {"directory": {"type": "string", "description": "Absolute VASP calculation directory under the active server's allowed root"}},
                "required": ["directory"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_remote_directory",
            "description": "Create a new remote directory, including any missing parent directories, under the active server's allowed root. Requires local approval.",
            "parameters": {
                "type": "object",
                "properties": {"directory": {"type": "string", "description": "Absolute new directory path under the active server's allowed root"}},
                "required": ["directory"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "copy_remote_path",
            "description": "Copy a remote file or directory (recursively) between two paths, both under the active server's allowed root. Requires local approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Absolute source path under the active server's allowed root"},
                    "destination": {"type": "string", "description": "Absolute destination path under the active server's allowed root"},
                },
                "required": ["source", "destination"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_remote_path",
            "description": "Move or rename a remote file or directory, both paths under the active server's allowed root. Requires local approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Absolute source path under the active server's allowed root"},
                    "destination": {"type": "string", "description": "Absolute destination path under the active server's allowed root"},
                },
                "required": ["source", "destination"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_remote_path",
            "description": "Move a remote file or directory into the server's timestamped quarantine area (.vaspilot-trash). Nothing is deleted; entries stay recoverable. Use purge_remote_path for permanent deletion. Requires local approval.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Absolute remote path under the active server's allowed root"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "purge_remote_path",
            "description": "Permanently delete an entry that already lives inside the quarantine area. Cannot be undone. The confirm_path must exactly match path. Requires local approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path inside the quarantine area (.vaspilot-trash)"},
                    "confirm_path": {"type": "string", "description": "Must be typed again exactly as path; anything different is rejected"},
                },
                "required": ["path", "confirm_path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upload_file",
            "description": "Upload a local file from this computer to a remote path under the active server's allowed root. Requires local approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "local_path": {"type": "string", "description": "Absolute path of the local file"},
                    "remote_path": {"type": "string", "description": "Absolute destination path under the active server's allowed root"},
                },
                "required": ["local_path", "remote_path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_file",
            "description": "Download a remote file under the active server's allowed root to a local path on this computer. Requires local approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "remote_path": {"type": "string", "description": "Absolute remote path under the active server's allowed root"},
                    "local_path": {"type": "string", "description": "Absolute destination path on this computer"},
                },
                "required": ["remote_path", "local_path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "server_diagnostic",
            "description": "Run one approved read-only server diagnostic.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "enum": ["hostname", "pwd", "disk", "quota", "partitions", "modules"]}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_job",
            "description": "Submit a Slurm script from an existing remote calculation directory. Requires local approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Absolute directory under the active server's allowed root"},
                    "script": {"type": "string", "description": "Job script filename, such as job.slurm"},
                },
                "required": ["directory", "script"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_job",
            "description": "Cancel one Slurm job by ID. Requires local approval.",
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string", "pattern": "^[0-9]+(?:_[0-9]+)?$"}},
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_remote_files",
            "description": "Copy a file or directory between two different servers. "
                           "Both paths must be under each server's allowed root and "
                           "neither may be the root itself. The transfer only starts "
                           "after the user approves it locally, it then runs in the "
                           "background and the user is notified when it finishes — "
                           "tell the user the transfer was approved and started, not "
                           "that it completed. Requires local approval and is never "
                           "auto-approved.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_server": {"type": "string", "description": "Source server name from the server catalog"},
                    "from_path": {"type": "string", "description": "Absolute path on the source server"},
                    "to_server": {"type": "string", "description": "Destination server name from the server catalog"},
                    "to_path": {"type": "string", "description": "Absolute destination path on the target server"},
                },
                "required": ["from_server", "from_path", "to_server", "to_path"],
                "additionalProperties": False,
            },
        },
    },
]


def approve(question: str) -> bool:
    try:
        answer = input(f"\n{question} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def server_context(server: dict[str, Any]) -> str:
    """Per-turn message telling the model which server is active.

    Shared by the CLI and the web UI so every conversation carries the same
    server identity and boundary before any tool call happens.
    """
    name = server.get("name", "?")
    target = server.get("target", "?")
    root = server.get("root", "?")
    connected = "yes" if server.get("connected") else "no"
    return (
        f'The active server is "{name}" (target {target}, allowed root {root}, '
        f"connected: {connected}). All tools act on this server. Remote paths "
        f"outside {root} are rejected. If the server is disconnected, tell the "
        f"user to establish the connection interactively (SSH password and "
        f"six-digit code are never handled by this agent)."
    )


class Controller:
    def __init__(self, identity_file: Path, vlab_host: str, vlab_user: str, server: str | None = None) -> None:
        self.identity_file = identity_file
        self.vlab_host = vlab_host
        self.vlab_user = vlab_user
        self.server = server

    def run(self, operation: str, *arguments: str, timeout: int = 210) -> dict[str, Any]:
        if self.server and operation != "servers":
            arguments = arguments + ("-ServerName", self.server)
        command = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(CONTROLLER), operation,
            "-IdentityFile", str(self.identity_file),
            "-VlabHost", self.vlab_host,
            "-VlabUser", self.vlab_user,
            *arguments,
        ]
        try:
            # PowerShell 5.1 forwards the gateway's UTF-8 bytes verbatim. Decode
            # them as UTF-8 explicitly: on a Chinese Windows locale the default
            # GBK decoding crashes the reader thread, which turns stdout into
            # None and makes result.stdout.strip() raise AttributeError.
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "operation timed out"}
        payload: dict[str, Any] = {"ok": result.returncode == 0, "exit_code": result.returncode}
        if result.stdout.strip():
            output = result.stdout.strip()[:200_000]
            if operation.startswith("vasp-") or operation == "servers":
                try:
                    payload["data"] = json.loads(output)
                except json.JSONDecodeError:
                    payload["output"] = output
            else:
                payload["output"] = output
        if result.stderr.strip():
            payload["error"] = result.stderr.strip()[:20_000]
        return payload


def execute_tool(controller: Controller, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "connection_status":
        return controller.run("status")
    if name == "server_identity":
        return controller.run("whoami")
    if name == "list_jobs":
        return controller.run("jobs")
    if name == "recent_jobs":
        return controller.run("recent")
    if name == "list_remote_directory":
        directory = arguments.get("directory")
        if not isinstance(directory, str) or not directory:
            return {"ok": False, "error": "directory is required"}
        return controller.run("list", "-RemotePath", directory)
    if name == "create_remote_directory":
        directory = arguments.get("directory")
        if not isinstance(directory, str) or not directory:
            return {"ok": False, "error": "directory is required"}
        if not approve(f"Create remote directory {directory}?"):
            return {"ok": False, "denied": True, "error": "user denied directory creation"}
        return controller.run("mkdir", "-RemotePath", directory)
    if name == "copy_remote_path":
        source, destination = arguments.get("source"), arguments.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str) or not source or not destination:
            return {"ok": False, "error": "source and destination are required"}
        if not approve(f"Copy {source} to {destination}?"):
            return {"ok": False, "denied": True, "error": "user denied copy"}
        return controller.run("copy", "-RemotePath", source, "-DestinationPath", destination)
    if name == "move_remote_path":
        source, destination = arguments.get("source"), arguments.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str) or not source or not destination:
            return {"ok": False, "error": "source and destination are required"}
        if not approve(f"Move {source} to {destination}?"):
            return {"ok": False, "denied": True, "error": "user denied move"}
        return controller.run("move", "-RemotePath", source, "-DestinationPath", destination)
    if name == "remove_remote_path":
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            return {"ok": False, "error": "path is required"}
        if not approve(f"Move {path} into the quarantine area (recoverable)?"):
            return {"ok": False, "denied": True, "error": "user denied quarantine move"}
        return controller.run("remove", "-RemotePath", path)
    if name == "purge_remote_path":
        path = arguments.get("path")
        confirm_path = arguments.get("confirm_path")
        if not isinstance(path, str) or not path:
            return {"ok": False, "error": "path is required"}
        if not isinstance(confirm_path, str) or confirm_path != path:
            return {"ok": False, "error": "confirm_path must exactly match path"}
        if not approve(f"PERMANENTLY delete {path}? This cannot be undone."):
            return {"ok": False, "denied": True, "error": "user denied permanent deletion"}
        return controller.run("purge", "-RemotePath", path, "-ConfirmPath", confirm_path)
    if name == "upload_file":
        local_path, remote_path = arguments.get("local_path"), arguments.get("remote_path")
        if not isinstance(local_path, str) or not isinstance(remote_path, str) or not local_path or not remote_path:
            return {"ok": False, "error": "local_path and remote_path are required"}
        if not approve(f"Upload {local_path} to {remote_path}?"):
            return {"ok": False, "denied": True, "error": "user denied upload"}
        return controller.run("upload", "-LocalPath", local_path, "-RemotePath", remote_path)
    if name == "download_file":
        remote_path, local_path = arguments.get("remote_path"), arguments.get("local_path")
        if not isinstance(remote_path, str) or not isinstance(local_path, str) or not remote_path or not local_path:
            return {"ok": False, "error": "remote_path and local_path are required"}
        if not approve(f"Download {remote_path} to {local_path}?"):
            return {"ok": False, "denied": True, "error": "user denied download"}
        return controller.run("download", "-RemotePath", remote_path, "-LocalPath", local_path)
    if name in {"inspect_vasp_calculation", "validate_vasp_inputs", "get_vasp_progress"}:
        directory = arguments.get("directory")
        if not isinstance(directory, str) or not directory:
            return {"ok": False, "error": "directory is required"}
        operation = {
            "inspect_vasp_calculation": "vasp-inspect",
            "validate_vasp_inputs": "vasp-validate",
            "get_vasp_progress": "vasp-progress",
        }[name]
        return controller.run(operation, "-RemotePath", directory)
    if name == "server_diagnostic":
        return controller.run("diagnostic", "-Diagnostic", str(arguments.get("name", "")))
    if name in {"read_remote_file", "tail_remote_file"}:
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            return {"ok": False, "error": "path is required"}
        if not approve(f"Allow content from {path} to be sent to the DeepSeek API?"):
            return {"ok": False, "denied": True, "error": "user denied sharing remote file content"}
        if name == "read_remote_file":
            return controller.run("read", "-RemotePath", path)
        lines = arguments.get("lines", 80)
        if not isinstance(lines, int) or not 1 <= lines <= 500:
            return {"ok": False, "error": "lines must be an integer from 1 to 500"}
        return controller.run("tail", "-RemotePath", path, "-Lines", str(lines))
    if name == "submit_job":
        directory, script = arguments.get("directory"), arguments.get("script")
        if not isinstance(directory, str) or not isinstance(script, str):
            return {"ok": False, "error": "directory and script are required"}
        if not approve(f"Submit {directory}/{script} to Slurm?"):
            return {"ok": False, "denied": True, "error": "user denied job submission"}
        return controller.run("submit", "-RemotePath", directory, "-JobScript", script)
    if name == "cancel_job":
        job_id = arguments.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return {"ok": False, "error": "job_id is required"}
        if not approve(f"Cancel Slurm job {job_id}?"):
            return {"ok": False, "denied": True, "error": "user denied job cancellation"}
        return controller.run("cancel", "-JobId", job_id, "-ConfirmJobId", job_id)
    return {"ok": False, "error": f"unsupported tool: {name}"}


class DeepSeekClient:
    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.url = base_url.rstrip("/")
        if self.url.endswith("/chat/completions"):
            self.url = self.url[: -len("/chat/completions")]
        self.url += "/chat/completions"
        self.model = model
        # Connect directly and ignore proxy environment variables: a stale
        # HTTPS_PROXY entry pointing at a dead local port makes urlopen fail
        # with WinError 10061 even though the API is reachable directly.
        self._opener = build_opener(ProxyHandler({}))

    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": 0.1,
        }).encode("utf-8")
        request = Request(
            self.url,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=120) as response:
                payload = json.load(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"DeepSeek API returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Could not reach the DeepSeek API: {exc.reason}") from exc
        try:
            return payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("DeepSeek API returned an unexpected response") from exc

    def complete_stream(self, messages: list[dict[str, Any]], on_delta) -> dict[str, Any]:
        """Stream a completion; on_delta(kind, text) receives reasoning/content deltas.

        Returns the same full message dict shape as complete(), with tool_calls
        arguments reassembled from the streamed fragments.
        """
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": 0.1,
            "stream": True,
        }).encode("utf-8")
        request = Request(
            self.url,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            response = self._opener.open(request, timeout=120)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"DeepSeek API returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Could not reach the DeepSeek API: {exc.reason}") from exc
        try:
            with response:
                return self._consume_stream(response, on_delta)
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("DeepSeek API returned an unexpected response") from exc

    @staticmethod
    def _consume_stream(response, on_delta) -> dict[str, Any]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise RuntimeError(f"DeepSeek API returned an error: {chunk['error']}")
            try:
                delta = chunk["choices"][0]["delta"]
            except (KeyError, IndexError, TypeError):
                continue
            if not isinstance(delta, dict):
                continue
            reasoning = delta.get("reasoning_content")
            if reasoning:
                reasoning_parts.append(reasoning)
                on_delta("reasoning", reasoning)
            content = delta.get("content")
            if content:
                content_parts.append(content)
                on_delta("content", content)
            for piece in delta.get("tool_calls") or []:
                index = int(piece.get("index", 0))
                slot = tool_calls.setdefault(
                    index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if piece.get("id"):
                    slot["id"] = piece["id"]
                fn = piece.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
        message: dict[str, Any] = {"role": "assistant"}
        if content_parts:
            message["content"] = "".join(content_parts)
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        return message


def agent_turn(client: DeepSeekClient, controller: Controller, messages: list[dict[str, Any]]) -> str:
    for _ in range(8):
        message = client.complete(messages)
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            return message.get("content") or ""
        for call in calls:
            function = call.get("function") or {}
            name = function.get("name", "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be a JSON object")
            except (json.JSONDecodeError, ValueError) as exc:
                result = {"ok": False, "error": f"invalid tool arguments: {exc}"}
            else:
                print(f"[tool] {name}")
                result = execute_tool(controller, name, arguments)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": json.dumps(result, ensure_ascii=False),
            })
    raise RuntimeError("DeepSeek exceeded the tool-call limit for one turn")


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek VASP agent with restricted SSH tools")
    parser.add_argument("--identity-file", default=os.environ.get("VLAB_IDENTITY_FILE"))
    parser.add_argument("--vlab-host", default="vlab.ustc.edu.cn")
    parser.add_argument("--vlab-user", default="ubuntu")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--server", default=None, help="server name from the gateway catalog (default: default_server)")
    parser.add_argument("prompt", nargs="*")
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("error: set DEEPSEEK_API_KEY in the current PowerShell session", file=sys.stderr)
        return 2
    if not args.identity_file:
        print("error: pass --identity-file or set VLAB_IDENTITY_FILE", file=sys.stderr)
        return 2
    identity = Path(args.identity_file).expanduser().resolve()
    if not identity.is_file():
        print(f"error: Vlab identity file not found: {identity}", file=sys.stderr)
        return 2

    client = DeepSeekClient(api_key, args.base_url, args.model)
    controller = Controller(identity, args.vlab_host, args.vlab_user, server=args.server)
    catalog = controller.run("servers")
    if not catalog.get("ok"):
        print(f"error: could not read the server catalog: {catalog.get('error', 'unknown error')}", file=sys.stderr)
        return 2
    chosen = args.server or catalog["data"].get("default", "")
    entry = next((s for s in catalog["data"].get("servers", []) if s.get("name") == chosen), None)
    if entry is None:
        print(f"error: unknown server: {chosen}", file=sys.stderr)
        return 2
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": server_context(entry)},
    ]

    one_shot = " ".join(args.prompt).strip()
    if one_shot:
        messages.append({"role": "user", "content": one_shot})
        print(agent_turn(client, controller, messages))
        return 0

    print("DeepSeek VASP agent. Type /quit to exit. Secrets must never be pasted here.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text in {"/quit", "/exit"}:
            return 0
        if not text:
            continue
        messages.append({"role": "user", "content": text})
        try:
            print(f"\nagent> {agent_turn(client, controller, messages)}")
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
