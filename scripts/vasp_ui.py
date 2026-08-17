#!/usr/bin/env python3
"""Local Codex-style web interface for the VASP Remote Agent."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from history import ConversationStore
from account import AccountStore
from local_store import LocalStore
from secure_store import protect as dpapi_protect, unprotect as dpapi_unprotect


SCRIPT_DIR = Path(__file__).resolve().parent
UI_DIR = SCRIPT_DIR.parent / "ui"
AGENT_PATH = SCRIPT_DIR / "deepseek-agent.py"


def load_agent_module():
    spec = importlib.util.spec_from_file_location("vasp_deepseek_agent", AGENT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 DeepSeek 智能体模块")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AGENT = load_agent_module()
RISKY_TOOLS = {
    "read_remote_file",
    "tail_remote_file",
    "submit_job",
    "cancel_job",
    "create_remote_directory",
    "copy_remote_path",
    "move_remote_path",
    "remove_remote_path",
    "purge_remote_path",
    "run_remote_command",
    "upload_file",
    "download_file",
    "transfer_remote_files",
}

# Cross-server transfers are write operations on TWO hosts: they always need
# the user's explicit approval and are excluded from "auto-approve" mode.
ALWAYS_APPROVE_TOOLS = {"transfer_remote_files"}


SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
TARGET_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$")
PERSIST_RE = re.compile(r"^(yes|no|[0-9]+[smhdw]?)$")
SAFE_ROOT_RE = re.compile(r"^/[A-Za-z0-9._/+@=-]+$")

# Model presets offered in the composer dropdown (custom names are allowed).
MODEL_PRESETS = ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro"]


class LoginRequired(Exception):
    """A history endpoint was hit without a valid session (HTTP 401)."""


class RateLimited(Exception):
    """Too many failed login attempts (HTTP 429)."""


def _idle_event() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


@dataclass
class ChatState:
    messages: list[dict[str, Any]] = field(
        default_factory=lambda: [{"role": "system", "content": AGENT.SYSTEM_PROMPT}]
    )
    queued_calls: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, Any] | None = None
    rounds: int = 0
    # Position of the server-context message in messages (always index 1 when
    # present). Kept so the context can be updated in place when the user
    # switches servers without rebuilding the conversation.
    context_index: int | None = None
    # Conversation history: persisted session id and creation time.
    session_id: str | None = None
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    # Project (local folder) this conversation belongs to; None = "all".
    project: str | None = None
    # A streaming turn is in flight; busy_event is set when idle so a reset or
    # history switch can wait for the old turn to leave.
    busy: bool = False
    busy_event: threading.Event = field(default_factory=_idle_event)
    # Set while an approval dialog is open; the streaming thread blocks on it.
    pending_event: threading.Event | None = None


class AppState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        # Guards the chat state machine (messages, pending, session switches).
        # Never held while a streaming turn runs; the streaming thread only
        # takes it for short critical sections, so /api/approve can always
        # reach the pending approval even mid-stream.
        self.chat_lock = threading.RLock()
        # Incremented on reset / history switch; a streaming turn compares it
        # at every checkpoint and aborts when it changed.
        self.abort_token = 0
        self.store = ConversationStore()
        self.account = AccountStore()
        # In-memory login sessions: token -> email. Lost on restart; history
        # files persist and re-login restores access.
        self.sessions: dict[str, str] = {}
        # Failed-login timestamps per email, for the 60 s / 5-attempt cap.
        self.login_attempts: dict[str, list[float]] = {}
        # Write actions staged for approval outside the chat stream (transfer).
        # action_id -> {operation, args, status: pending|running|done, result,
        # created}. Approved transfers run in a worker thread; the UI polls.
        self.pending_actions: dict[str, dict[str, Any]] = {}
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        self.local = LocalStore()
        default_identity = Path.home() / ".ssh" / "vlab-identity.pem"
        # Priority: environment override -> saved setting -> default path.
        persisted_identity = self.local.get_identity_file()
        self.identity_file = os.environ.get(
            "VLAB_IDENTITY_FILE",
            persisted_identity or (str(default_identity) if default_identity.is_file() else ""),
        )
        self.model = os.environ.get("DEEPSEEK_MODEL", AGENT.DEFAULT_MODEL)
        self.base_url = os.environ.get("DEEPSEEK_BASE_URL", AGENT.DEFAULT_BASE_URL)
        # Configurable provider list and project registry (names/urls/models
        # only — no keys), persisted in ~/.vaspilot/local.json. Each provider
        # {id, name, base_url, model} has its own API key held in memory only
        # (self.provider_keys), so switching models between providers never
        # rewrites settings.
        self.models = self.local.get_models()
        self.providers = self.local.list_providers()
        self.provider_keys: dict[str, str] = {}
        if not self.providers:
            # Migrate the legacy single configuration into one provider per
            # model name, then persist so the same set survives restarts.
            seed = [{"id": "p-default", "name": "DeepSeek", "base_url": self.base_url, "model": self.model}]
            for extra in self.models:
                if extra != self.model:
                    seed.append({"id": f"p-{secrets.token_urlsafe(8)}", "name": extra, "base_url": self.base_url, "model": extra})
            self.providers = self.local.set_providers(seed)
        # Preset a Zhipu GLM entry when none is configured yet — the user
        # asked for glm-5.2; name/model/url stay editable in settings and the
        # key is left for the user to fill in.
        if not any("open.bigmodel.cn" in p["base_url"] for p in self.providers):
            self.providers = self.local.set_providers([
                *self.providers,
                {"id": "p-glm", "name": "GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-5.2"},
            ])
        self.current_provider = self.local.get_current_provider()
        if not any(p["id"] == self.current_provider for p in self.providers):
            self.current_provider = self.providers[0]["id"]
        if self.api_key:
            # An env-supplied key seeds the current provider only; other
            # providers start without a key.
            self.provider_keys[self.current_provider] = self.api_key
        # DPAPI-encrypted key blobs persisted next to the provider list.
        # Ciphertext only; the plaintext enters memory via
        # restore_provider_keys() after login.
        self.stored_keys = self.local.stored_provider_keys()
        self.vlab_host = "vlab.ustc.edu.cn"
        self.vlab_user = "ubuntu"
        self.chat = ChatState()
        self.csrf = secrets.token_urlsafe(24)
        self.auto_approve = False
        self.servers: list[dict[str, Any]] = []
        self.servers_error: str = ""
        self.default_server: str = ""
        self.active_server: str = ""

    def _identity(self) -> Path:
        if not self.identity_file:
            raise ValueError("请先在设置中选择 Vlab PEM 私钥")
        identity = Path(self.identity_file).expanduser().resolve()
        if not identity.is_file():
            raise ValueError(f"找不到 PEM 私钥：{identity}")
        return identity

    def controller(self, server: str | None = None):
        return AGENT.Controller(
            self._identity(), self.vlab_host, self.vlab_user,
            server=server or self.active_server or None,
        )

    def catalog_controller(self):
        """Controller without a server scope, for catalog operations only."""
        return AGENT.Controller(self._identity(), self.vlab_host, self.vlab_user)

    def refresh_servers(self) -> dict[str, Any]:
        """Refresh the server catalog from the gateway; keep the old cache on failure."""
        try:
            result = self.catalog_controller().run("servers")
        except Exception as exc:
            self.servers_error = str(exc)
            return {"ok": False, "error": self.servers_error}
        if not result.get("ok"):
            message = result.get("error") or result.get("output") or "网关未返回服务器目录"
            self.servers_error = str(message)[:300]
            return {"ok": False, "error": self.servers_error}
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
            self.servers_error = "网关返回了无法识别的服务器目录"
            return {"ok": False, "error": self.servers_error}
        self.servers = data["servers"]
        self.default_server = str(data.get("default", ""))
        self.servers_error = ""
        if not self.active_server or not any(s.get("name") == self.active_server for s in self.servers):
            self.active_server = self.default_server
        return {"ok": True}

    def ensure_server_context(self, chat: ChatState) -> str:
        """Make messages[1] state which server is active; returns the server name.

        Never silently defaults to cl9: when the catalog is unknown the caller
        gets a clear error instead of the model acting on the wrong host.
        """
        if not self.active_server:
            outcome = self.refresh_servers()
            if not outcome.get("ok") or not self.active_server:
                raise RuntimeError(f"无法确定当前服务器：{self.servers_error or '未配置任何服务器'}")
        entry = next((s for s in self.servers if s.get("name") == self.active_server), None)
        if entry is None:
            raise RuntimeError(f"服务器目录中没有活动服务器 {self.active_server}")
        context = {"role": "user", "content": AGENT.server_context(entry)}
        if chat.context_index is not None and 0 < chat.context_index < len(chat.messages):
            chat.messages[chat.context_index] = context
        else:
            chat.messages.insert(1, context)
            chat.context_index = 1
        return self.active_server

    def current_provider_entry(self) -> dict[str, Any]:
        provider = next((p for p in self.providers if p["id"] == self.current_provider), None)
        if provider is None and self.providers:
            provider = self.providers[0]
        if provider is None:
            raise ValueError("未配置任何模型服务商，请先在设置中添加")
        return provider

    def client(self):
        provider = self.current_provider_entry()
        key = self.provider_keys.get(provider["id"], "")
        if not key:
            raise ValueError(f"请先在设置中填写 {provider['name']} 的 API Key")
        return AGENT.DeepSeekClient(key, provider["base_url"], provider["model"])

    def save_current_conversation(self) -> None:
        """Persist the current chat, skipping the system prompt and the
        injected server-context message so a conversation is not bound to a
        server (the context is re-injected for the active server on load).
        """
        chat = self.chat
        if chat.busy or not chat.messages:
            return
        skip = {0}
        if chat.context_index is not None and 0 < chat.context_index < len(chat.messages):
            skip.add(chat.context_index)
        messages = [m for i, m in enumerate(chat.messages) if i not in skip]
        if not any(m.get("role") == "user" for m in messages):
            return
        if chat.session_id is None:
            chat.session_id = secrets.token_urlsafe(8)
        title = next(
            (str(m.get("content", ""))[:40] for m in messages if m.get("role") == "user" and m.get("content")),
            "未命名对话",
        )
        self.store.save({
            "id": chat.session_id,
            "title": title,
            "created": chat.created_at,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "messages": messages,
        }, project=chat.project)

    def provider_key_available(self, provider_id: str) -> bool:
        """True when the key is in memory or stored encrypted on disk (it will
        be restored to memory on the next login)."""
        if self.provider_keys.get(provider_id, ""):
            return True
        return provider_id in self.stored_keys

    def restore_provider_keys(self) -> int:
        """Decrypt the DPAPI-stored keys into memory; called after login.
        Returns how many keys were restored. Blobs that fail to decrypt (e.g.
        from another Windows user) are skipped and left on disk.
        """
        restored = 0
        for provider_id, blob in self.stored_keys.items():
            try:
                self.provider_keys[provider_id] = dpapi_unprotect(blob)
                restored += 1
            except (OSError, ValueError):
                continue
        return restored

    def public_config(self) -> dict[str, Any]:
        identity = Path(self.identity_file).expanduser() if self.identity_file else None
        provider = self.current_provider_entry()
        # providers and current_provider are the primary fields; model,
        # base_url and models remain for backwards compatibility.
        return {
            "api_configured": bool(self.provider_key_available(self.current_provider)),
            "identity_file": self.identity_file,
            "identity_exists": bool(identity and identity.is_file()),
            "model": provider["model"],
            "base_url": provider["base_url"],
            "models": self.models,
            # has_key is a boolean so the UI can show "configured" vs
            # "missing"; the key itself never leaves the process.
            "providers": [{**p, "has_key": self.provider_key_available(p["id"])} for p in self.providers],
            "current_provider": self.current_provider,
            "vlab_host": self.vlab_host,
            "vlab_user": self.vlab_user,
            "auto_approve": self.auto_approve,
        }


STATE = AppState()


def approval_summary(name: str, arguments: dict[str, Any]) -> tuple[str, str]:
    if name == "read_remote_file":
        return "共享远端文件", f"允许把 {arguments.get('path', '')} 的内容发送给 DeepSeek 分析吗？"
    if name == "tail_remote_file":
        return "共享输出片段", f"允许把 {arguments.get('path', '')} 的末尾内容发送给 DeepSeek 分析吗？"
    if name == "submit_job":
        return "提交 VASP 任务", f"确认提交 {arguments.get('directory', '')}/{arguments.get('script', '')} 吗？"
    if name == "cancel_job":
        return "取消计算任务", f"确认取消 Slurm 任务 {arguments.get('job_id', '')} 吗？"
    if name == "create_remote_directory":
        return "创建远端目录", f"确认在远端创建目录 {arguments.get('directory', '')} 吗？"
    if name == "copy_remote_path":
        return "复制远端文件", f"确认把远端 {arguments.get('source', '')} 复制到 {arguments.get('destination', '')} 吗？"
    if name == "move_remote_path":
        return "移动远端文件", f"确认把远端 {arguments.get('source', '')} 移动到 {arguments.get('destination', '')} 吗？"
    if name == "remove_remote_path":
        return "移入隔离区", f"确认把远端 {arguments.get('path', '')} 移入隔离区吗？文件不会丢失，可用 purge 永久删除（需另行审批）。"
    if name == "purge_remote_path":
        return "永久删除（隔离区）", f"确认永久删除隔离区内的 {arguments.get('path', '')} 吗？此操作不可恢复！"
    if name == "run_remote_command":
        return "远端分析命令", f"确认在 {arguments.get('directory', '')} 执行白名单分析命令：\n{arguments.get('command', '')}"
    if name == "upload_file":
        return "上传文件到远端", f"确认把本地 {arguments.get('local_path', '')} 上传到远端 {arguments.get('remote_path', '')} 吗？"
    if name == "download_file":
        return "下载远端文件", f"确认把远端 {arguments.get('remote_path', '')} 下载到本地 {arguments.get('local_path', '')} 吗？"
    if name == "transfer_remote_files":
        return "跨服务器传输", (
            f"确认把 {arguments.get('from_path', '')}（{arguments.get('from_server', '')}）"
            f"传输到 {arguments.get('to_path', '')}（{arguments.get('to_server', '')}）吗？"
            "批准后在后台执行，完成时会提示你。"
        )
    return "确认操作", f"确认执行 {name} 吗？"


def execute_tool(controller: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "connection_status":
        return controller.run("status")
    if name == "switch_active_server":
        target = str(arguments.get("server_name") or "").strip()
        if not target:
            return {"ok": False, "error": "server_name is required"}
        if not any(s.get("name") == target for s in STATE.servers):
            STATE.refresh_servers()
        entry = next((s for s in STATE.servers if s.get("name") == target), None)
        if entry is None:
            return {"ok": False, "error": f"服务器目录中没有 {target}"}
        STATE.active_server = target
        # Keep this turn's controller in sync, otherwise the rest of the turn
        # would still route every write to the OLD server while approval and
        # context show the new one.
        controller.server = target
        # Rewrite the per-turn context so the model knows the new active server.
        chat = STATE.chat
        if chat is not None and chat.context_index is not None:
            try:
                chat.messages[chat.context_index] = {
                    "role": "user",
                    "content": AGENT.server_context(entry),
                }
            except (IndexError, AttributeError):
                pass
        return {"ok": True, "switched": True, "server": target}
    if name == "server_identity":
        return controller.run("whoami")
    if name == "list_jobs":
        return controller.run("jobs")
    if name == "recent_jobs":
        return controller.run("recent")
    if name == "list_remote_directory":
        directory = arguments.get("directory")
        if not isinstance(directory, str) or not directory:
            return {"ok": False, "error": "缺少目录"}
        return controller.run("list", "-RemotePath", directory)
    if name == "create_remote_directory":
        directory = arguments.get("directory")
        if not isinstance(directory, str) or not directory:
            return {"ok": False, "error": "缺少目录"}
        return controller.run("mkdir", "-RemotePath", directory)
    if name == "copy_remote_path":
        source, destination = arguments.get("source"), arguments.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str) or not source or not destination:
            return {"ok": False, "error": "缺少源路径或目标路径"}
        return controller.run("copy", "-RemotePath", source, "-DestinationPath", destination)
    if name == "move_remote_path":
        source, destination = arguments.get("source"), arguments.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str) or not source or not destination:
            return {"ok": False, "error": "缺少源路径或目标路径"}
        return controller.run("move", "-RemotePath", source, "-DestinationPath", destination)
    if name == "remove_remote_path":
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            return {"ok": False, "error": "缺少路径"}
        return controller.run("remove", "-RemotePath", path)
    if name == "purge_remote_path":
        path = arguments.get("path")
        confirm_path = arguments.get("confirm_path")
        if not isinstance(path, str) or not path:
            return {"ok": False, "error": "缺少路径"}
        if not isinstance(confirm_path, str) or confirm_path != path:
            return {"ok": False, "error": "确认路径必须与待删除路径完全一致"}
        return controller.run("purge", "-RemotePath", path, "-ConfirmPath", confirm_path)
    if name == "run_remote_command":
        directory = arguments.get("directory")
        command = arguments.get("command")
        if not isinstance(directory, str) or not directory:
            return {"ok": False, "error": "缺少目录"}
        if not isinstance(command, str) or not command.strip():
            return {"ok": False, "error": "缺少命令"}
        return controller.run("run", "-RemotePath", directory, "-Command", command.strip())
    if name == "upload_file":
        local_path, remote_path = arguments.get("local_path"), arguments.get("remote_path")
        if not isinstance(local_path, str) or not isinstance(remote_path, str) or not local_path or not remote_path:
            return {"ok": False, "error": "缺少本地路径或远端路径"}
        return controller.run("upload", "-LocalPath", local_path, "-RemotePath", remote_path)
    if name == "download_file":
        remote_path, local_path = arguments.get("remote_path"), arguments.get("local_path")
        if not isinstance(remote_path, str) or not isinstance(local_path, str) or not remote_path or not local_path:
            return {"ok": False, "error": "缺少远端路径或本地路径"}
        return controller.run("download", "-RemotePath", remote_path, "-LocalPath", local_path)
    if name in {"inspect_vasp_calculation", "validate_vasp_inputs", "get_vasp_progress"}:
        directory = arguments.get("directory")
        if not isinstance(directory, str) or not directory:
            return {"ok": False, "error": "缺少计算目录"}
        operation = {
            "inspect_vasp_calculation": "vasp-inspect",
            "validate_vasp_inputs": "vasp-validate",
            "get_vasp_progress": "vasp-progress",
        }[name]
        return controller.run(operation, "-RemotePath", directory)
    if name == "server_diagnostic":
        return controller.run("diagnostic", "-Diagnostic", str(arguments.get("name", "")))
    if name == "read_remote_file":
        return controller.run("read", "-RemotePath", str(arguments.get("path", "")))
    if name == "tail_remote_file":
        lines = arguments.get("lines", 80)
        if not isinstance(lines, int) or not 1 <= lines <= 500:
            return {"ok": False, "error": "读取行数必须在 1 到 500 之间"}
        return controller.run(
            "tail", "-RemotePath", str(arguments.get("path", "")), "-Lines", str(lines)
        )
    if name == "submit_job":
        return controller.run(
            "submit",
            "-RemotePath", str(arguments.get("directory", "")),
            "-JobScript", str(arguments.get("script", "")),
        )
    if name == "cancel_job":
        job_id = str(arguments.get("job_id", ""))
        return controller.run("cancel", "-JobId", job_id, "-ConfirmJobId", job_id)
    if name == "transfer_remote_files":
        # The user approved the streaming approval dialog; stage the action and
        # start the background worker immediately. The model must never wait on
        # the transfer itself (it can take up to 30 minutes).
        try:
            action_id, summary = stage_transfer(
                str(arguments.get("from_server", "")).strip(),
                str(arguments.get("from_path", "")).strip(),
                str(arguments.get("to_server", "")).strip(),
                str(arguments.get("to_path", "")).strip(),
                status="running",
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        run_transfer_worker(action_id)
        return {
            "ok": True, "started": True, "action_id": action_id,
            "message": f"传输已批准并在后台启动：{summary}。完成后会通知用户。",
        }
    return {"ok": False, "error": f"不支持的工具：{name}"}


def stage_transfer(from_server: str, from_path: str, to_server: str, to_path: str,
                   status: str = "pending") -> tuple[str, str]:
    """Validate a cross-server transfer and stage it in pending_actions.

    Shared by the UI transfer dialog (/api/action/transfer) and the model tool
    (transfer_remote_files). Raises ValueError on bad input; returns
    (action_id, summary) once staged. The transfer itself only ever runs in
    run_transfer_worker after the user approved it.
    """
    if not from_server or not to_server or from_server == to_server:
        raise ValueError("请选择两个不同的服务器")
    if not any(s.get("name") == from_server for s in STATE.servers):
        raise ValueError("源服务器不在目录中")
    if not any(s.get("name") == to_server for s in STATE.servers):
        raise ValueError("目标服务器不在目录中")
    if not any(s.get("name") == from_server and s.get("connected") for s in STATE.servers):
        raise ValueError("源服务器未连接，请先连接后再传输")
    if not any(s.get("name") == to_server and s.get("connected") for s in STATE.servers):
        raise ValueError("目标服务器未连接，请先连接后再传输")
    if not SAFE_ROOT_RE.fullmatch(from_path) or not SAFE_ROOT_RE.fullmatch(to_path):
        raise ValueError("远端路径含不支持字符")
    if any(segment in {".", ".."} for segment in from_path.split("/")[1:]):
        raise ValueError("源路径不能包含 . 或 ..")
    if any(segment in {".", ".."} for segment in to_path.split("/")[1:]):
        raise ValueError("目标路径不能包含 . 或 ..")
    action_id = secrets.token_urlsafe(16)
    with STATE.lock:
        STATE.pending_actions[action_id] = {
            "operation": "transfer",
            "args": {
                "from_server": from_server, "from_path": from_path,
                "to_server": to_server, "to_path": to_path,
            },
            "status": status,
            "result": None,
            "created": time.time(),
        }
    return action_id, f"{from_path}（{from_server}）→ {to_path}（{to_server}）"


def run_transfer_worker(action_id: str) -> None:
    """Run a staged transfer in a background thread; poll reports the outcome."""
    with STATE.lock:
        action = STATE.pending_actions.get(action_id)
        if action is None or action["status"] != "running":
            return
        args = dict(action["args"])

    def worker() -> None:
        result = STATE.catalog_controller().run(
            "transfer",
            "-FromServer", args["from_server"],
            "-FromPath", args["from_path"],
            "-ToServer", args["to_server"],
            "-ToPath", args["to_path"],
            timeout=1800,
        )
        with STATE.lock:
            entry = STATE.pending_actions.get(action_id)
            if entry:
                entry["status"] = "done"
                entry["result"] = result

    threading.Thread(target=worker, daemon=True).start()


def tool_result_text(result: dict[str, Any], limit: int = 2000) -> str:
    """Compact, safe text preview of a tool result for the Harness-style UI."""
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(result)
    if len(text) > limit:
        text = text[:limit] + f"... (截断，共 {len(text)} 字符)"
    return text


def append_tool_result(chat: ChatState, call: dict[str, Any], result: dict[str, Any]) -> None:
    chat.messages.append({
        "role": "tool",
        "tool_call_id": call.get("id", ""),
        "content": json.dumps(result, ensure_ascii=False),
    })


def repair_incomplete_tool_messages(messages: list[dict[str, Any]]) -> int:
    """Ensure every assistant tool call is immediately followed by one tool result."""
    repaired: list[dict[str, Any]] = []
    inserted = 0
    index = 0
    while index < len(messages):
        message = messages[index]
        repaired.append(message)
        index += 1
        calls = message.get("tool_calls") or [] if message.get("role") == "assistant" else []
        if not calls:
            continue
        expected_ids = [str(call.get("id", "")) for call in calls]
        present_ids: set[str] = set()
        while index < len(messages) and messages[index].get("role") == "tool":
            tool_message = messages[index]
            repaired.append(tool_message)
            present_ids.add(str(tool_message.get("tool_call_id", "")))
            index += 1
        for call_id in expected_ids:
            if call_id not in present_ids:
                repaired.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps({
                        "ok": False,
                        "error": "tool call was interrupted locally before completion; retry if still needed",
                    }),
                })
                inserted += 1
    if inserted:
        messages[:] = repaired
    return inserted


class AbortStream(Exception):
    """The client disconnected or the chat was reset while a turn streamed."""


def tool_summary(result: dict[str, Any]) -> str:
    """Compact human summary of a tool result for the tool_end event."""
    if not result.get("ok"):
        return ""
    text = result.get("data")
    if not isinstance(text, str):
        text = result.get("output") or result.get("error") or ""
    text = " ".join(str(text).split())
    return text[:120]


def run_chat_loop_stream(emit) -> None:
    """Run one full turn, emitting SSE events as it goes.

    emit(event, data) writes one SSE frame; it raises AbortStream when the
    client is gone, which stops tool execution at the next checkpoint.
    """
    chat = STATE.chat
    # Refresh the injected server context first: any server switch made between
    # turns applies to every tool call in this turn.
    STATE.ensure_server_context(chat)
    controller = STATE.controller()
    client = STATE.client()
    token = STATE.abort_token

    def check_abort() -> None:
        if STATE.abort_token != token or STATE.chat is not chat:
            raise AbortStream()

    for _ in range(8):
        while chat.queued_calls:
            check_abort()
            call = chat.queued_calls.pop(0)
            function = call.get("function") or {}
            name = function.get("name", "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("工具参数不是 JSON 对象")
            except (json.JSONDecodeError, ValueError) as exc:
                append_tool_result(chat, call, {"ok": False, "error": f"工具参数无效：{exc}"})
                emit("tool_end", {"name": name, "ok": False, "error": f"工具参数无效：{exc}"})
                continue
            emit("tool_start", {"name": name, "args": arguments})
            if name in RISKY_TOOLS and (not STATE.auto_approve or name in ALWAYS_APPROVE_TOOLS):
                approval_id = secrets.token_urlsafe(16)
                title, description = approval_summary(name, arguments)
                chat.pending = {
                    "id": approval_id,
                    "call": call,
                    "name": name,
                    "arguments": arguments,
                    "title": title,
                    "description": description,
                    # Server at approval time; approving after a switch must not
                    # let the write land on the wrong host.
                    "server": STATE.active_server,
                }
                chat.pending_event = threading.Event()
                emit("approval", {
                    "id": approval_id,
                    "tool": name,
                    "title": title,
                    "description": description,
                })
                # The approval dialog is answered through POST /api/approve on
                # another thread, which writes pending["approved"] and sets
                # this event. Time out rather than hang forever.
                chat.pending_event.wait(300)
                chat.pending_event = None
                pending = chat.pending
                chat.pending = None
                if pending is None or pending.get("approved") is None:
                    append_tool_result(chat, call, {"ok": False, "error": "确认请求已超时，请重新发起操作"})
                    emit("tool_end", {"name": name, "ok": False, "error": "确认请求已超时"})
                    continue
                if not pending["approved"]:
                    append_tool_result(chat, call, {"ok": False, "denied": True, "error": "用户取消了该操作"})
                    emit("tool_end", {"name": name, "ok": False, "error": "用户取消"})
                    continue
                if STATE.active_server and pending.get("server") != STATE.active_server:
                    append_tool_result(chat, call, {
                        "ok": False,
                        "error": "服务器已切换，该操作已失效，请重新发起操作",
                    })
                    emit("tool_end", {"name": name, "ok": False, "error": "服务器已切换，操作失效"})
                    continue
            check_abort()
            try:
                result = execute_tool(controller, name, arguments)
            except Exception as exc:
                result = {"ok": False, "error": f"local tool execution failed: {exc}"}
            check_abort()
            append_tool_result(chat, call, result)
            emit("tool_end", {
                "name": name,
                "ok": bool(result.get("ok")),
                "error": str(result.get("error") or ""),
                "summary": tool_summary(result),
                "result_text": tool_result_text(result),
                # Transfer runs in the background; the front end polls it.
                "action_id": result.get("action_id"),
            })

        check_abort()
        repair_incomplete_tool_messages(chat.messages)
        reasoning_round = chat.rounds + 1
        message = client.complete_stream(
            chat.messages,
            lambda kind, text: emit("reasoning" if kind == "reasoning" else "delta",
                                    {"delta": text, "round": reasoning_round}),
        )
        chat.messages.append(message)
        calls = message.get("tool_calls") or []
        if calls:
            chat.queued_calls.extend(calls)
            continue
        chat.rounds += 1
        emit("done", {"rounds": chat.rounds})
        return
    raise RuntimeError("本轮工具调用次数过多，已停止")


def continue_approval(approval_id: str, approved: bool) -> dict[str, Any]:
    """Resolve a pending approval; the streaming thread continues the turn."""
    chat = STATE.chat
    pending = chat.pending
    if not pending or pending.get("id") != approval_id:
        raise ValueError("确认请求已过期，请重新发送指令")
    pending["approved"] = bool(approved)
    if chat.pending_event:
        chat.pending_event.set()
    return {"ok": True}


def launch_connect_terminal(server_name: str) -> None:
    controller = STATE.controller()
    script = AGENT.CONTROLLER
    banner = f"胡伟团队专用智能体 已连接到 {server_name}。"
    connect_command = (
        "& " + subprocess.list2cmdline([str(script)]) +
        " connect -IdentityFile " + subprocess.list2cmdline([str(controller.identity_file)]) +
        " -VlabHost " + subprocess.list2cmdline([controller.vlab_host]) +
        " -VlabUser " + subprocess.list2cmdline([controller.vlab_user]) +
        " -ServerName " + subprocess.list2cmdline([server_name]) +
        "; if ($LASTEXITCODE -eq 0) { "
        "Write-Host ''; Write-Host '" + banner + "' -ForegroundColor Green; "
        "Write-Host '您可以关闭此窗口，返回胡伟团队专用智能体。' "
        "} else { Write-Host ''; Write-Host 'Connection failed. Keep this window open and check the message above.' -ForegroundColor Red }"
    )
    command = [
        "powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass",
        "-Command", connect_command,
    ]
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(command, creationflags=creationflags)


# ---------------------------------------------------------------------------
# In-browser human terminal: each docked tab/standalone page gets its own ssh
# session behind a random term_id. The model has no tool for these endpoints;
# the human types the password and six-digit OTP in the terminal frame itself.
# ---------------------------------------------------------------------------
TERMINAL_SESSIONS: dict[str, dict[str, Any]] = {}
TERMINAL_LOCK = threading.Lock()
MAX_TERMINALS = 8


def launch_web_terminal(server_name: str) -> str:
    """Spawn an interactive ssh pty and return its term_id."""
    controller = STATE.controller()
    entry = next((s for s in STATE.servers if s.get("name") == server_name), None)
    if entry is None:
        raise ValueError(f"服务器目录中没有 {server_name}")
    target = str(entry.get("target", "")).strip()
    if not target:
        raise ValueError(f"服务器 {server_name} 没有目标地址")
    port = int(entry.get("port", 22) or 22)
    # Single hop to the Vlab gateway, with the inner ssh to the target server
    # passed as the REMOTE COMMAND: no Vlab MOTD, no bash startup, the target's
    # password prompt appears immediately (same pattern as gateway connect).
    # ProxyCommand/-J are avoided because they share stdin with the child ssh
    # on Windows and swallow the human's input.
    remote_command = f"ssh -tt -p {port} {target}"
    command = [
        "ssh", "-tt",
        "-i", str(controller.identity_file),
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostKeys=no",
        controller.vlab_user + "@" + controller.vlab_host,
        remote_command,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with TERMINAL_LOCK:
        # Drop finished sessions as a fallback when a window closed without
        # /api/term/close (e.g. the browser tab was killed).
        stale = [tid for tid, session in TERMINAL_SESSIONS.items()
                 if session["proc"].poll() is not None]
        for tid in stale:
            TERMINAL_SESSIONS.pop(tid, None)
        if len(TERMINAL_SESSIONS) >= MAX_TERMINALS:
            raise ValueError(f"终端会话已达上限 {MAX_TERMINALS}，请先关闭不再使用的终端窗口")
        # Keep the capacity check and process registration in one critical
        # section. Otherwise concurrent opens can exceed the limit, and a
        # rejected request can leave an untracked ssh process behind.
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=0,
            creationflags=creationflags,
        )
        term_id = secrets.token_hex(8)
        session: dict[str, Any] = {"proc": proc, "output": [], "pos": 0,
                                   "lock": threading.Lock(), "server": server_name}
        TERMINAL_SESSIONS[term_id] = session

    def reader() -> None:
        stream = proc.stdout
        try:
            while True:
                chunk = stream.read(1)
                if chunk == "":
                    break
                with session["lock"]:
                    session["output"].append(chunk)
        except (ValueError, OSError):
            pass

    threading.Thread(target=reader, daemon=True).start()
    return term_id


def term_read(term_id: str) -> dict[str, Any]:
    session = TERMINAL_SESSIONS.get(term_id)
    if session is None:
        return {"ok": False, "error": "终端会话不存在或已关闭", "alive": False}
    with session["lock"]:
        data = "".join(session["output"][session["pos"]:])
        session["pos"] = len(session["output"])
    return {"ok": True, "data": data, "alive": session["proc"].poll() is None}


def term_write(term_id: str, data: str) -> dict[str, Any]:
    session = TERMINAL_SESSIONS.get(term_id)
    if session is None:
        return {"ok": False, "error": "终端会话不存在或已关闭"}
    if session["proc"].poll() is not None:
        return {"ok": False, "error": "终端会话已结束"}
    try:
        session["proc"].stdin.write(data)
        session["proc"].stdin.flush()
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"写入失败: {exc}"}
    return {"ok": True}


def term_close(term_id: str) -> dict[str, Any]:
    session = TERMINAL_SESSIONS.pop(term_id, None)
    if session is not None:
        try:
            session["proc"].terminate()
        except OSError:
            pass
    return {"ok": True}


class Server(ThreadingHTTPServer):
    # Keep SO_REUSEADDR off: on Windows it lets a second VASPilot process
    # bind the same port, and two servers answering one URL make every POST
    # flaky (CSRF tokens differ per process). One instance per port.
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    server_version = "VASPilot/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[ui] " + fmt % args + "\n")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'",
        )
        super().end_headers()

    def json_response(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("请求内容为空或过大")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求必须是 JSON 对象")
        return payload

    def allowed_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.hostname in {"127.0.0.1", "localhost"}

    def require_csrf(self, payload: dict[str, Any]) -> None:
        if not self.allowed_origin() or payload.get("csrf") != STATE.csrf:
            raise PermissionError("请求来源校验失败")

    def require_login(self) -> str:
        """Return the logged-in email or raise LoginRequired (HTTP 401)."""
        email = STATE.sessions.get(self.headers.get("X-Auth-Token", ""))
        if not email:
            raise LoginRequired("请先登录后查看历史对话")
        return email

    def login_blocked(self, email: str) -> bool:
        """True when the email has 5 failed logins in the last 60 seconds."""
        now = time.time()
        attempts = [t for t in STATE.login_attempts.get(email, []) if now - t < 60]
        STATE.login_attempts[email] = attempts
        return len(attempts) >= 5

    def _query_project(self, query: str) -> str | None:
        """Normalize a ?project= query value: absent/empty means "all"."""
        values = parse_qs(query).get("project")
        project = values[0] if values else None
        return str(project).strip() or None

    def _normalize_project(self, raw: Any) -> str | None:
        """Validate a project id from a POST payload against the registry.
        None/empty means "all conversations"; unknown ids are rejected so a
        stale client cannot read or write into a removed project."""
        project = str(raw or "").strip()
        if not project:
            return None
        if not any(p.get("id") == project for p in STATE.local.list_projects()):
            raise ValueError("项目不存在")
        return project

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if path == "/favicon.ico":
            path = "/favicon.svg"
        if path == "/api/health":
            self.json_response({"ok": True, "csrf": STATE.csrf, "config": STATE.public_config()})
            return
        if path == "/api/status":
            try:
                result = STATE.controller().run("status")
                self.json_response({"ok": True, "result": result})
            except Exception as exc:
                self.json_response({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/servers":
            outcome = STATE.refresh_servers()
            if not outcome.get("ok"):
                if STATE.servers:
                    # Keep the stale cache visible while the gateway is unreachable.
                    self.json_response({"ok": True, "servers": STATE.servers, "active": STATE.active_server, "error": outcome.get("error", "")})
                    return
                self.json_response({"ok": False, "error": outcome.get("error", "无法获取服务器目录")}, 400)
                return
            self.json_response({"ok": True, "servers": STATE.servers, "active": STATE.active_server})
            return
        if path == "/api/auth/status":
            email = STATE.sessions.get(self.headers.get("X-Auth-Token", ""))
            self.json_response({"ok": True, "logged_in": bool(email), "email": email or ""})
            return
        if path == "/api/conversations":
            try:
                self.require_login()
            except LoginRequired as exc:
                self.json_response({"ok": False, "error": str(exc)}, 401)
                return
            try:
                project = self._query_project(parsed_url.query)
                conversations = STATE.store.list(project)
            except ValueError as exc:
                self.json_response({"ok": False, "error": str(exc)}, 400)
                return
            self.json_response({"ok": True, "conversations": conversations})
            return
        if path == "/api/projects":
            try:
                self.require_login()
            except LoginRequired as exc:
                self.json_response({"ok": False, "error": str(exc)}, 401)
                return
            self.json_response({
                "ok": True,
                "projects": STATE.local.list_projects(),
                "active": STATE.chat.project,
            })
            return
        if path == "/":
            path = "/index.html"
        relative = path.lstrip("/")
        if relative not in {
            "index.html", "app.js", "styles.css", "favicon.svg",
            "terminal.html", "terminal.js",
        }:
            self.send_error(404)
            return
        file_path = UI_DIR / relative
        if not file_path.is_file():
            self.send_error(404)
            return
        mime = {".html": "text/html", ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml"}[file_path.suffix]
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def sse(self, event: str, data: dict[str, Any]) -> None:
        """Write one SSE frame; raise AbortStream when the client is gone."""
        try:
            self.wfile.write(
                f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
            )
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise AbortStream() from None

    def handle_chat_sse(self, payload: dict[str, Any]) -> None:
        """POST /api/chat: stream the whole turn as text/event-stream.

        The chat lock is only held for the short initialization; the streaming
        turn itself runs without it so /api/approve can always resolve a
        pending approval from another thread.
        """
        content = str(payload.get("message", "")).strip()
        if not content or len(content) > 20_000:
            raise ValueError("请输入有效消息")
        with STATE.chat_lock:
            chat = STATE.chat
            if chat.busy:
                raise ValueError("当前正在处理消息，请稍候")
            if chat.pending:
                raise ValueError("请先处理当前确认请求")
            chat.messages.append({"role": "user", "content": content})
            marker = len(chat.messages)
            chat.busy = True
            chat.busy_event.clear()
            if chat.session_id is None:
                chat.session_id = secrets.token_urlsafe(8)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            run_chat_loop_stream(self.sse)
            with STATE.chat_lock:
                chat.busy = False
                chat.busy_event.set()
            STATE.save_current_conversation()
        except AbortStream:
            pass
        except Exception as exc:
            # Roll back the failed turn so a failed message does not stay in
            # the conversation sent to the model later.
            with STATE.chat_lock:
                del chat.messages[marker:]
            try:
                self.sse("error", {"message": str(exc)})
            except AbortStream:
                pass
        finally:
            with STATE.chat_lock:
                if chat.busy:
                    chat.busy = False
                    chat.busy_event.set()
                chat.pending = None
                chat.pending_event = None

    def handle_chat_path(self, path: str, payload: dict[str, Any]) -> None:
        """Chat-state endpoints that must not hold STATE.lock (chat_lock only)."""
        chat = STATE.chat
        if path in {
            "/api/conversations/new", "/api/conversations/load",
            "/api/conversations/delete", "/api/projects/switch",
        }:
            # History and projects are account-gated; the chat itself and
            # approvals are not.
            self.require_login()
        if path == "/api/approve":
            result = continue_approval(str(payload.get("id", "")), bool(payload.get("approved")))
            self.json_response({"ok": True, **result})
            return
        if path == "/api/reset":
            # Drop the current conversation without saving (legacy endpoint;
            # the UI now uses /api/conversations/new).
            STATE.abort_token += 1
            chat.busy_event.wait(5)
            STATE.chat = ChatState()
            self.json_response({"ok": True})
            return
        if path == "/api/conversations/new":
            self.finish_current_conversation()
            STATE.chat = ChatState()
            self.json_response({"ok": True})
            return
        if path == "/api/conversations/load":
            conv_id = str(payload.get("id", "")).strip()
            project = self._normalize_project(payload.get("project"))
            self.finish_current_conversation()
            data = STATE.store.load(conv_id, project)
            STATE.chat = ChatState(
                messages=[{"role": "system", "content": AGENT.SYSTEM_PROMPT}],
                session_id=conv_id,
                created_at=str(data.get("created", "")),
                project=project,
            )
            STATE.chat.messages.extend(data["messages"])
            # The server context is re-injected by ensure_server_context on the
            # next turn, for the then-active server.
            self.json_response({"ok": True, "messages": data["messages"]})
            return
        if path == "/api/conversations/delete":
            conv_id = str(payload.get("id", "")).strip()
            project = self._normalize_project(payload.get("project"))
            if chat.session_id == conv_id:
                STATE.abort_token += 1
                chat.busy_event.wait(5)
                STATE.chat = ChatState(project=project)
            STATE.store.delete(conv_id, project)
            self.json_response({"ok": True})
            return
        if path == "/api/projects/switch":
            project = self._normalize_project(payload.get("project"))
            self.finish_current_conversation()
            STATE.chat = ChatState(project=project)
            self.json_response({"ok": True, "project": project})
            return
        self.send_error(404)

    def finish_current_conversation(self) -> None:
        """Wait for any streaming turn to leave, then persist the current chat."""
        chat = STATE.chat
        if chat.busy:
            STATE.abort_token += 1
            chat.busy_event.wait(8)
        STATE.save_current_conversation()

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
            self.require_csrf(payload)
            path = urlparse(self.path).path
            if path == "/api/chat":
                self.handle_chat_sse(payload)
                return
            if path == "/api/file/open":
                # Read-only against STATE plus a slow subprocess: running it
                # under STATE.lock would make every other POST wait out the
                # download (and os.startfile must never run while locked).
                self.handle_config_path(path, payload)
                return
            if path in {"/api/terminal", "/api/term/output", "/api/term/input", "/api/term/close"}:
                # Terminal endpoints must never hold STATE.lock: launching ssh
                # or refreshing the catalog (an SSH round trip that can stall
                # for minutes on network trouble) under the lock deadlocks the
                # whole UI - which is exactly what "save has no effect" was.
                self.handle_terminal_path(path, payload)
                return
            if path in {
                "/api/approve", "/api/reset",
                "/api/conversations/new", "/api/conversations/load",
                "/api/conversations/delete", "/api/projects/switch",
            }:
                with STATE.chat_lock:
                    self.handle_chat_path(path, payload)
                return
            with STATE.lock:
                self.handle_config_path(path, payload)
            return
        except LoginRequired as exc:
            self.json_response({"ok": False, "error": str(exc)}, 401)
        except RateLimited as exc:
            self.json_response({"ok": False, "error": str(exc)}, 429)
        except PermissionError as exc:
            self.json_response({"ok": False, "error": str(exc)}, 403)
        except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            self.json_response({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self.json_response({"ok": False, "error": f"内部错误：{exc}"}, 500)

    def handle_terminal_path(self, path: str, payload: dict[str, Any]) -> None:
        """Terminal endpoints, deliberately OUTSIDE STATE.lock: launching ssh
        or refreshing the catalog can stall for minutes on network trouble and
        must never block the rest of the UI (this caused the 'save settings
        has no effect' freeze)."""
        if path == "/api/terminal":
            # Web terminal for the HUMAN operator. It can run in a docked iframe
            # or a standalone page; the model has no tool for these endpoints
            # and the UI never copies session contents into the chat context.
            name = str(payload.get("name") or STATE.active_server or "").strip()
            if not name:
                raise ValueError("尚未确定当前服务器，请先刷新服务器列表")
            if not any(s.get("name") == name for s in STATE.servers):
                # Refresh outside the lock; on gateway trouble this stalls only
                # THIS request, not the whole UI.
                STATE.refresh_servers()
            if not any(s.get("name") == name for s in STATE.servers):
                raise ValueError(f"服务器目录中没有 {name}")
            term_id = launch_web_terminal(name)
            self.json_response({"ok": True, "term_id": term_id,
                                "message": f"已为 {name} 打开网页终端（仅限您本人操作）"})
            return
        if path == "/api/term/output":
            term_id = str(payload.get("term_id") or "").strip()
            self.json_response(term_read(term_id))
            return
        if path == "/api/term/input":
            term_id = str(payload.get("term_id") or "").strip()
            data = str(payload.get("data") or "")
            if len(data) > 16384:
                raise ValueError("单次输入过长")
            self.json_response(term_write(term_id, data))
            return
        if path == "/api/term/close":
            term_id = str(payload.get("term_id") or "").strip()
            self.json_response(term_close(term_id))
            return
        self.send_error(404)

    def handle_config_path(self, path: str, payload: dict[str, Any]) -> None:
        if path == "/api/config":
            if "identity_file" in payload:
                # Persist the Vlab PEM path so the pre-flight check and every
                # later session see the user's choice (previously this field
                # was silently dropped: saving "had no effect").
                raw_identity = str(payload.get("identity_file") or "").strip()
                if raw_identity and len(raw_identity) > 300:
                    raise ValueError("PEM 路径过长")
                STATE.identity_file = raw_identity
                STATE.local.set_identity_file(raw_identity)
            if "providers" in payload:
                raw = payload["providers"]
                if not isinstance(raw, list):
                    raise ValueError("服务商列表无效")
                normalized = []
                for p in raw:
                    if not isinstance(p, dict):
                        raise ValueError("服务商条目无效")
                    pid = str(p.get("id") or "").strip()
                    if not pid:
                        pid = f"p-{secrets.token_urlsafe(8)}"
                    normalized.append({**p, "id": pid})
                old_ids = {p["id"] for p in STATE.providers}
                STATE.providers = STATE.local.set_providers(normalized)  # user-safe ValueError
                # Removing a provider drops its in-memory and stored keys.
                for gone in old_ids - {p["id"] for p in STATE.providers}:
                    STATE.provider_keys.pop(gone, None)
                    STATE.stored_keys.pop(gone, None)
                    STATE.local.remove_provider_key(gone)
                # Keys live in memory; when the user fills one in, persist it
                # DPAPI-encrypted so a restart + login restores it.
                # "__CLEAR__" means "forget the saved key" (used by the
                # settings panel's clear button), never a literal key.
                for p in normalized:
                    key = str(p.get("api_key") or "").strip()
                    if key == "__CLEAR__":
                        STATE.provider_keys.pop(p["id"], None)
                        STATE.stored_keys.pop(p["id"], None)
                        STATE.local.remove_provider_key(p["id"])
                    elif key:
                        STATE.provider_keys[p["id"]] = key
                        STATE.stored_keys[p["id"]] = dpapi_protect(key)
                        STATE.local.save_provider_key(p["id"], STATE.stored_keys[p["id"]])
                if STATE.providers and not any(p["id"] == STATE.current_provider for p in STATE.providers):
                    STATE.current_provider = STATE.providers[0]["id"]
                    STATE.local.set_current_provider(STATE.current_provider)
            if "current_provider" in payload:
                cid = str(payload["current_provider"] or "").strip()
                if not any(p["id"] == cid for p in STATE.providers):
                    raise ValueError("未知的服务商")
                STATE.current_provider = cid
                STATE.local.set_current_provider(cid)
            provider = STATE.current_provider_entry()
            # Legacy single-config fields map onto the current provider.
            new_model = str(payload.get("model") or "").strip() or provider["model"]
            new_url = str(payload.get("base_url") or "").strip() or provider["base_url"]
            model_changed = "model" in payload and new_model != provider["model"]
            url_changed = "base_url" in payload and new_url != provider["base_url"]
            if model_changed or url_changed:
                STATE.providers = STATE.local.set_providers([
                    {
                        **p,
                        "model": new_model if p["id"] == provider["id"] else p["model"],
                        "base_url": new_url if p["id"] == provider["id"] else p["base_url"],
                    }
                    for p in STATE.providers
                ])
            if "api_key" in payload and payload["api_key"]:
                key = str(payload["api_key"]).strip()
                if key == "__CLEAR__":
                    STATE.provider_keys.pop(provider["id"], None)
                    STATE.stored_keys.pop(provider["id"], None)
                    STATE.local.remove_provider_key(provider["id"])
                else:
                    STATE.provider_keys[provider["id"]] = key
                    STATE.stored_keys[provider["id"]] = dpapi_protect(key)
                    STATE.local.save_provider_key(provider["id"], STATE.stored_keys[provider["id"]])
            if "models" in payload:
                models = payload["models"]
                if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
                    raise ValueError("模型列表无效")
                STATE.local.set_models(models)  # user-safe ValueError
                STATE.models = STATE.local.get_models()
            self.json_response({"ok": True, "config": STATE.public_config()})
            return
        if path == "/api/auth/register":
            email = str(payload.get("email", "")).strip().lower()
            password = payload.get("password")
            if not isinstance(password, str):
                raise ValueError("请输入邮箱和密码")
            STATE.account.register(email, password)  # user-safe ValueError
            token = secrets.token_urlsafe(32)
            STATE.sessions[token] = email
            STATE.login_attempts.pop(email, None)
            # Restore the DPAPI-stored provider keys now that the user is
            # authenticated (first register on a fresh install has nothing).
            STATE.restore_provider_keys()
            self.json_response({"ok": True, "token": token, "email": email})
            return
        if path == "/api/auth/login":
            email = str(payload.get("email", "")).strip().lower()
            password = payload.get("password")
            if not email or not isinstance(password, str) or not password:
                raise ValueError("请输入邮箱和密码")
            if self.login_blocked(email):
                raise RateLimited("尝试过于频繁，请 60 秒后再试")
            if STATE.account.verify(email, password):
                token = secrets.token_urlsafe(32)
                STATE.sessions[token] = email
                STATE.login_attempts.pop(email, None)
                STATE.restore_provider_keys()
                self.json_response({"ok": True, "token": token, "email": email})
                return
            STATE.login_attempts.setdefault(email, []).append(time.time())
            raise ValueError("邮箱或密码错误")
        if path == "/api/auth/logout":
            STATE.sessions.pop(self.headers.get("X-Auth-Token", ""), None)
            # Keys were decrypted into memory for this login; drop them so a
            # fresh login (or no login at all) starts from disk ciphertext.
            STATE.provider_keys.clear()
            self.json_response({"ok": True})
            return
        if path == "/api/projects/add":
            self.require_login()
            name = str(payload.get("name", "")).strip()
            folder = str(payload.get("path", "")).strip()
            server = str(payload.get("server", "")).strip()
            create_remote = bool(payload.get("create_remote"))
            if create_remote:
                # Server-side project: create the directory on the chosen
                # server, then bind the project record to that remote path.
                if not server:
                    raise ValueError("请先选择服务器")
                if not any(s.get("name") == server for s in STATE.servers):
                    STATE.refresh_servers()
                if not any(s.get("name") == server for s in STATE.servers):
                    raise ValueError(f"服务器目录中没有 {server}")
                result = STATE.controller(server=server).run("mkdir", "-RemotePath", folder)
                if not result.get("ok"):
                    raise ValueError(result.get("error") or result.get("output") or "远端目录创建失败")
                STATE.local.add_project(name, folder, server=server)
            else:
                STATE.local.add_project(name, folder)  # user-safe ValueError
            self.json_response({"ok": True, "projects": STATE.local.list_projects()})
            return
        if path == "/api/projects/remove":
            self.require_login()
            project_id = str(payload.get("id", "")).strip()
            if not any(p.get("id") == project_id for p in STATE.local.list_projects()):
                raise ValueError("项目不存在")
            if STATE.chat.project == project_id:
                # The project's conversation directory is about to go: drop the
                # in-memory chat instead of re-saving it into the removed
                # project (save_current_conversation would recreate the dir).
                STATE.abort_token += 1
                STATE.chat.busy_event.wait(5)
                STATE.chat = ChatState()
            STATE.store.delete_project(project_id)
            STATE.local.remove_project(project_id)
            self.json_response({"ok": True, "projects": STATE.local.list_projects()})
            return
        if path == "/api/connect":
            name = str(payload.get("name") or STATE.active_server or "").strip()
            if not name:
                raise ValueError("尚未确定当前服务器，请先刷新服务器列表")
            launch_connect_terminal(name)
            self.json_response({"ok": True, "message": f"已为 {name} 打开 SSH 认证窗口"})
            return
        if path == "/api/servers/select":
            name = str(payload.get("name", "")).strip()
            if not any(s.get("name") == name for s in STATE.servers):
                STATE.refresh_servers()
            if not any(s.get("name") == name for s in STATE.servers):
                raise ValueError("服务器不在目录中，请先添加")
            STATE.active_server = name
            self.json_response({"ok": True, "active": name})
            return
        if path == "/api/servers/add":
            name = str(payload.get("name", "")).strip()
            target = str(payload.get("target", "")).strip()
            try:
                port = int(payload.get("port", 22))
            except (TypeError, ValueError):
                raise ValueError("端口必须是 1–65535 的整数")
            root = str(payload.get("root", "")).strip()
            persist = str(payload.get("persist", "")).strip() or ""
            # Mirror the gateway validation locally for early feedback;
            # the gateway re-validates authoritatively.
            if not SERVER_NAME_RE.fullmatch(name):
                raise ValueError("服务器名仅限字母、数字、点、下划线和短横线（2–32 位）")
            if not TARGET_RE.fullmatch(target):
                raise ValueError("目标地址必须是 user@host 形式")
            if not 1 <= port <= 65535:
                raise ValueError("端口必须在 1–65535 之间")
            if root:
                if not SAFE_ROOT_RE.fullmatch(root):
                    raise ValueError("远端根路径含不支持字符")
                if any(segment in {".", ".."} for segment in root.split("/")[1:]):
                    raise ValueError("远端根路径不能包含 . 或 ..")
            if persist and not PERSIST_RE.fullmatch(persist):
                raise ValueError("persist 值无效（yes/no 或时长如 8h）")
            add_args = ["server-add", name, "-ServerTarget", target, "-ServerPort", str(port)]
            if root:
                add_args += ["-ServerRoot", root]
            if persist:
                add_args += ["-ServerPersist", persist]
            scheduler = str(payload.get("scheduler", "") or "").strip()
            if scheduler and scheduler != "auto":
                if scheduler not in ("slurm", "pbs"):
                    raise ValueError("调度器必须是 auto、slurm 或 pbs")
                add_args += ["-ServerScheduler", scheduler]
            result = STATE.catalog_controller().run(*add_args)
            if not result.get("ok"):
                raise ValueError(result.get("error") or result.get("output") or "添加服务器失败")
            STATE.refresh_servers()
            self.json_response({"ok": True, "servers": STATE.servers, "active": STATE.active_server})
            return
        if path == "/api/servers/remove":
            name = str(payload.get("name", "")).strip()
            result = STATE.catalog_controller().run("server-remove", "-ServerName", name)
            if not result.get("ok"):
                raise ValueError(result.get("error") or result.get("output") or "删除服务器失败")
            if STATE.active_server == name:
                STATE.active_server = ""
            STATE.refresh_servers()
            self.json_response({"ok": True, "servers": STATE.servers, "active": STATE.active_server})
            return
        if path == "/api/servers/edit":
            name = str(payload.get("name", "")).strip()
            if not any(s.get("name") == name for s in STATE.servers):
                raise ValueError("服务器不在目录中，请先刷新列表")
            edit_args = ["server-edit", name]
            new_name = str(payload.get("new_name", "")).strip()
            if new_name:
                if not SERVER_NAME_RE.fullmatch(new_name):
                    raise ValueError("服务器名仅支持字母数字与 . _ -，首字符不能是数字以外的符号")
                if new_name == name:
                    new_name = ""  # no-op rename
                elif any(s.get("name") == new_name for s in STATE.servers):
                    raise ValueError(f"服务器 {new_name} 已存在")
                else:
                    edit_args += ["-NewName", new_name]
            target = str(payload.get("target", "")).strip()
            if target:
                if not TARGET_RE.fullmatch(target):
                    raise ValueError("目标地址必须是 user@host 形式")
                edit_args += ["-ServerTarget", target]
            port_raw = payload.get("port")
            if port_raw is not None and str(port_raw).strip() != "":
                try:
                    port = int(port_raw)
                except (TypeError, ValueError):
                    raise ValueError("端口必须是 1–65535 的整数")
                if not 1 <= port <= 65535:
                    raise ValueError("端口必须在 1–65535 之间")
                edit_args += ["-ServerPort", str(port)]
            # An explicitly present "root" key (even an empty one) is an edit:
            # empty clears the boundary back to the login home directory.
            if "root" in payload:
                root = str(payload.get("root", "")).strip()
                if root:
                    if not SAFE_ROOT_RE.fullmatch(root):
                        raise ValueError("远端根路径含不支持字符")
                    if any(segment in {".", ".."} for segment in root.split("/")[1:]):
                        raise ValueError("远端根路径不能包含 . 或 ..")
                edit_args += ["-ServerRoot", root]
            persist = str(payload.get("persist", "")).strip()
            if persist:
                if not PERSIST_RE.fullmatch(persist):
                    raise ValueError("persist 值无效（yes/no 或时长如 8h）")
                edit_args += ["-ServerPersist", persist]
            scheduler = str(payload.get("scheduler", "")).strip()
            if scheduler:
                if scheduler not in ("auto", "slurm", "pbs"):
                    raise ValueError("调度器必须是 auto、slurm 或 pbs")
                edit_args += ["-ServerScheduler", scheduler]
            if len(edit_args) == 2:
                raise ValueError("没有可修改的属性")
            result = STATE.catalog_controller().run(*edit_args)
            if not result.get("ok"):
                raise ValueError(result.get("error") or result.get("output") or "修改服务器失败")
            STATE.refresh_servers()
            if new_name and STATE.active_server == name:
                STATE.active_server = new_name
            self.json_response({"ok": True, "servers": STATE.servers, "active": STATE.active_server})
            return
        if path == "/api/servers/disconnect":
            name = str(payload.get("name") or STATE.active_server or "").strip()
            if not any(s.get("name") == name for s in STATE.servers):
                raise ValueError("服务器不在目录中")
            result = STATE.controller(server=name).run("disconnect")
            if not result.get("ok"):
                raise ValueError(result.get("error") or result.get("output") or "断开连接失败")
            self.json_response({"ok": True})
            return
        if path == "/api/action":
            operation = str(payload.get("operation", ""))
            allowed = {
                "status", "jobs", "recent", "whoami", "vasp-inspect",
                "vasp-validate", "vasp-progress", "diagnostic",
            }
            if operation not in allowed:
                raise ValueError("不支持该快捷操作")
            arguments = payload.get("arguments") or []
            if not isinstance(arguments, list) or not all(isinstance(x, str) for x in arguments):
                raise ValueError("操作参数无效")
            result = STATE.controller().run(operation, *arguments)
            self.json_response({"ok": True, "result": result})
            return
        if path == "/api/action/transfer":
            # Server-to-server file transfer. A write operation crossing two
            # hosts, so it is never executed directly: it lands in the pending
            # actions queue and only runs after POST /api/action/approve.
            action_id, summary = stage_transfer(
                str(payload.get("from_server", "")).strip(),
                str(payload.get("from_path", "")).strip(),
                str(payload.get("to_server", "")).strip(),
                str(payload.get("to_path", "")).strip(),
            )
            self.json_response({
                "ok": True, "needs_approval": True, "action_id": action_id,
                "summary": summary,
            })
            return
        if path == "/api/action/approve":
            action_id = str(payload.get("action_id", ""))
            approved = bool(payload.get("approve"))
            with STATE.lock:
                action = STATE.pending_actions.get(action_id)
                if action is None:
                    raise ValueError("该操作不存在或已过期")
                if action["status"] != "pending":
                    raise ValueError("该操作已被处理")
                action["status"] = "done" if not approved else "running"
            if not approved:
                with STATE.lock:
                    STATE.pending_actions.pop(action_id, None)
                self.json_response({"ok": True, "result": {"ok": True, "cancelled": True}})
                return
            run_transfer_worker(action_id)
            self.json_response({"ok": True, "result": {"ok": True, "started": True}})
            return
        if path == "/api/action/poll":
            action_id = str(payload.get("action_id", ""))
            with STATE.lock:
                action = STATE.pending_actions.get(action_id)
            if action is None:
                self.json_response({"ok": True, "done": True, "result": {"ok": False, "error": "操作已过期"}})
                return
            if action["status"] == "pending":
                self.json_response({"ok": True, "done": False, "waiting": True})
                return
            if action["status"] == "running":
                self.json_response({"ok": True, "done": False, "running": True})
                return
            result = action.get("result") or {"ok": False, "error": "操作无结果"}
            with STATE.lock:
                STATE.pending_actions.pop(action_id, None)
            self.json_response({"ok": True, "done": True, "result": result})
            return
        if path == "/api/autoapprove":
            STATE.auto_approve = bool(payload.get("enabled"))
            self.json_response({"ok": True, "auto_approve": STATE.auto_approve})
            return
        if path == "/api/file/open":
            # The user explicitly clicked a file entry: download it into the
            # local Downloads area and open it with the default program. Not a
            # model tool call, so no approval dialog is involved.
            remote_path = str(payload.get("path", "")).strip()
            if not remote_path or not SAFE_ROOT_RE.fullmatch(remote_path):
                raise ValueError("远端路径含不支持字符")
            if any(segment in {".", ".."} for segment in remote_path.split("/")[1:]):
                raise ValueError("远端路径不能包含 . 或 ..")
            entry = next((s for s in STATE.servers if s.get("name") == STATE.active_server), None)
            if entry is None:
                STATE.refresh_servers()
                entry = next((s for s in STATE.servers if s.get("name") == STATE.active_server), None)
            if entry is None:
                raise ValueError("尚未确定当前服务器")
            root = str(entry.get("root", ""))
            if not root or remote_path == root or not remote_path.startswith(root + "/"):
                raise ValueError("路径不在当前服务器的允许根目录内")
            server = str(entry["name"])
            basename = remote_path.rsplit("/", 1)[-1]
            if not basename or basename in {".", ".."} or "/" in basename:
                raise ValueError("无效的文件名")
            target_dir = Path.home() / "Downloads" / "vaspilot" / server
            target_dir.mkdir(parents=True, exist_ok=True)
            stem, suffix = os.path.splitext(basename)
            target = target_dir / basename
            counter = 1
            while target.exists():
                target = target_dir / f"{stem} ({counter}){suffix}"
                counter += 1
            result = STATE.controller(server=server).run(
                "download", "-RemotePath", remote_path, "-LocalPath", str(target)
            )
            if not result.get("ok"):
                raise ValueError(result.get("error") or result.get("output") or "文件下载失败")
            # Launch the default program asynchronously. os.startfile blocks
            # until any "open with" dialog the OS may show is dismissed, which
            # would stall this request for minutes; cmd start returns at once.
            try:
                subprocess.Popen(
                    ["cmd", "/c", "start", "", str(target)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                pass
            self.json_response({"ok": True, "local_path": str(target)})
            return
        self.send_error(404)


def check_installation() -> int:
    required = [
        UI_DIR / "index.html", UI_DIR / "styles.css", UI_DIR / "app.js", UI_DIR / "favicon.svg",
        UI_DIR / "terminal.html", UI_DIR / "terminal.js",
        AGENT_PATH, SCRIPT_DIR / "vasp-agent.ps1",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        print("Missing UI files:\n" + "\n".join(missing), file=sys.stderr)
        return 1
    print("胡伟团队专用智能体 UI 安装检查：OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="运行胡伟团队专用智能体界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        return check_installation()
    if args.host not in {"127.0.0.1", "localhost"}:
        print("For safety, the interface can only bind to localhost.", file=sys.stderr)
        return 2
    try:
        server = Server((args.host, args.port), Handler)
    except OSError:
        print(f"端口 {args.port} 已被另一个胡伟团队专用智能体实例占用，请先停止它。", file=sys.stderr)
        return 3
    url = f"http://{args.host}:{server.server_address[1]}"
    print(f"胡伟团队专用智能体正在运行：{url}")
    print("Press Ctrl+C to stop. API keys stay in memory only.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n胡伟团队专用智能体已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
