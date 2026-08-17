"""Conversation history persistence for VASPilot.

One JSON file per conversation under ~/.vaspilot/conversations/ (override with
the VASPILOT_HISTORY_DIR environment variable, mainly for tests). Messages are
stored without the system prompt and without the injected server-context
message: the system prompt is rebuilt on load and the context is re-injected
for the then-active server, so a conversation is never bound to one host.

Projects: when a project id is given, conversations live in the private
subdirectory conversations/<project>/ so each project owns an isolated
history. project=None addresses the top level ("all conversations").
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

DEFAULT_DIR = Path.home() / ".vaspilot" / "conversations"
CONV_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ConversationStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or os.environ.get("VASPILOT_HISTORY_DIR") or DEFAULT_DIR)

    def _subdir(self, project: str | None) -> Path:
        """Conversation directory for a project (None = top level). The
        charset check makes ../ traversal impossible."""
        if project is None:
            return self.root
        if not isinstance(project, str) or not PROJECT_ID_RE.fullmatch(project):
            raise ValueError("无效的项目编号")
        return self.root / project

    def _path(self, conv_id: str, project: str | None = None) -> Path:
        if not CONV_ID_RE.fullmatch(conv_id):
            raise ValueError("无效的对话编号")
        return self._subdir(project) / f"{conv_id}.json"

    def list(self, project: str | None = None) -> list[dict[str, Any]]:
        """Metadata for every stored conversation in one project, newest first."""
        subdir = self._subdir(project)
        if not subdir.is_dir():
            return []
        conversations: list[dict[str, Any]] = []
        for path in subdir.glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8-sig") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            conv_id = data.get("id")
            if not isinstance(conv_id, str) or not CONV_ID_RE.fullmatch(conv_id):
                continue
            conversations.append({
                "id": conv_id,
                "title": str(data.get("title", "未命名对话"))[:60],
                "created": str(data.get("created", "")),
                "updated": str(data.get("updated", "")),
            })
        conversations.sort(key=lambda item: item["updated"], reverse=True)
        return conversations

    def load(self, conv_id: str, project: str | None = None) -> dict[str, Any]:
        path = self._path(conv_id, project)
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"找不到历史对话 {conv_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"历史对话 {conv_id} 已损坏") from exc
        if not isinstance(data.get("messages"), list):
            raise ValueError(f"历史对话 {conv_id} 已损坏")
        return data

    def save(self, conversation: dict[str, Any], project: str | None = None) -> None:
        conv_id = conversation.get("id")
        if not isinstance(conv_id, str) or not CONV_ID_RE.fullmatch(conv_id):
            raise ValueError("对话缺少有效 id")
        subdir = self._subdir(project)
        subdir.mkdir(parents=True, exist_ok=True)
        path = subdir / f"{conv_id}.json"
        data = json.dumps(conversation, ensure_ascii=False).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(dir=str(subdir), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def delete(self, conv_id: str, project: str | None = None) -> None:
        path = self._path(conv_id, project)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"找不到历史对话 {conv_id}") from exc

    def delete_project(self, project: str) -> None:
        """Remove a project's whole conversation directory. The project id
        already passed the charset check in _subdir, so this cannot escape
        the conversation root."""
        if project is None:
            raise ValueError("无效的项目编号")
        subdir = self._subdir(project)
        if subdir.is_dir():
            shutil.rmtree(subdir)
