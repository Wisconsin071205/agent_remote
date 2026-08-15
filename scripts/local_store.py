"""Local UI settings store: providers + model list + project registry.

Two UI-facing features need durable state that is NOT the API key or any
remote credential (those stay in process memory only):

  1. the model providers shown in the composer selector and edited in the
     settings panel. A provider is {id, name, base_url, model} — the API key
     for a provider lives in process memory and is never written here;
  2. the project registry: each project is a name plus the user's own local
     folder path, and owns a private conversation directory.

The legacy "models" list (plain model names) is kept for compatibility;
providers supersede it in the UI. Everything lives in one JSON file,
~/.vaspilot/local.json (override with VASPILOT_LOCAL_FILE, mainly for
tests). The project id is server-generated from the same charset as
conversation ids so it can be used as a directory name inside the
conversation root.

Security invariants:
  - no API keys, passwords, TOTP seeds, or remote paths are ever stored here
  - the local folder path is metadata only; nothing reads or writes that folder
  - the file is replaced atomically (tempfile + os.replace)
"""

import json
import os
import re
import secrets
import tempfile
import time

DEFAULT_PATH = os.path.expanduser("~/.vaspilot/local.json")

DEFAULT_MODELS = ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro"]

# Same charset as conversation ids so a project id is safe as a directory name.
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
URL_RE = re.compile(r"^https?://\S+$")

MODEL_MAX = 20
MODEL_NAME_MAX = 64
NAME_MAX = 40
PATH_MAX = 260
PROVIDER_MAX = 20
URL_MAX = 300


class LocalStore:
    """Persist {"models": [..], "projects": [{id, name, path, created}]}."""

    def __init__(self, path=None):
        # VASPILOT_LOCAL_FILE override is for tests.
        self.path = path or os.environ.get("VASPILOT_LOCAL_FILE") or DEFAULT_PATH

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except (OSError, ValueError):
            return {}

    def _write(self, data):
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".local-", suffix=".json", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- models -----------------------------------------------------------

    def get_models(self):
        """Current model list; defaults when unset or malformed."""
        models = self._load().get("models")
        if not isinstance(models, list):
            return list(DEFAULT_MODELS)
        cleaned = self._clean_models(models)
        return cleaned or list(DEFAULT_MODELS)

    @staticmethod
    def _clean_models(models):
        seen, out = set(), []
        for m in models:
            if not isinstance(m, str):
                continue
            m = m.strip()
            if not m or len(m) > MODEL_NAME_MAX or any(ch.isspace() for ch in m):
                continue
            if m in seen:
                continue
            seen.add(m)
            out.append(m)
        return out

    def set_models(self, models):
        """Validate and persist a full model list. Raises ValueError with a
        user-safe message."""
        if not isinstance(models, list) or not models:
            raise ValueError("模型列表不能为空")
        if len(models) > MODEL_MAX:
            raise ValueError(f"模型列表最多 {MODEL_MAX} 个")
        cleaned = self._clean_models(models)
        if len(cleaned) != len(models):
            raise ValueError("模型名称含非法字符或重复")
        data = self._load()
        data["models"] = cleaned
        self._write(data)

    # ---- providers --------------------------------------------------------

    def list_providers(self):
        """[{id, name, base_url, model}] as stored; never contains keys."""
        data = self._load()
        providers = data.get("providers")
        if not isinstance(providers, list):
            return []
        out = []
        for p in providers:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            if not isinstance(pid, str) or not PROVIDER_ID_RE.fullmatch(pid):
                continue
            out.append({
                "id": pid,
                "name": str(p.get("name", "未命名服务"))[:NAME_MAX],
                "base_url": str(p.get("base_url", ""))[:URL_MAX],
                "model": str(p.get("model", ""))[:MODEL_NAME_MAX],
            })
        return out

    def set_providers(self, providers):
        """Validate and persist a full provider list. Any api_key field is
        ignored — keys never reach disk. Raises ValueError with a user-safe
        message."""
        if not isinstance(providers, list) or not providers:
            raise ValueError("至少保留一个模型服务商")
        if len(providers) > PROVIDER_MAX:
            raise ValueError(f"模型服务商最多 {PROVIDER_MAX} 个")
        cleaned, seen = [], set()
        for p in providers:
            if not isinstance(p, dict):
                raise ValueError("服务商条目无效")
            pid = str(p.get("id") or "").strip()
            if not pid or not PROVIDER_ID_RE.fullmatch(pid):
                raise ValueError("服务商 ID 无效")
            if pid in seen:
                raise ValueError("服务商 ID 重复")
            seen.add(pid)
            name = str(p.get("name") or "").strip()
            url = str(p.get("base_url") or "").strip()
            model = str(p.get("model") or "").strip()
            if not name or len(name) > NAME_MAX:
                raise ValueError(f"服务商名称需为 1–{NAME_MAX} 个字符")
            if not URL_RE.fullmatch(url) or len(url) > URL_MAX:
                raise ValueError("API 地址需以 http:// 或 https:// 开头")
            if not model or len(model) > MODEL_NAME_MAX or any(ch.isspace() for ch in model):
                raise ValueError(f"模型名称需为非空且不含空格的 1–{MODEL_NAME_MAX} 字符")
            cleaned.append({"id": pid, "name": name, "base_url": url, "model": model})
        data = self._load()
        data["providers"] = cleaned
        self._write(data)
        return cleaned

    def get_current_provider(self):
        data = self._load()
        cid = data.get("current_provider")
        return cid if isinstance(cid, str) else ""

    def set_current_provider(self, provider_id):
        data = self._load()
        data["current_provider"] = provider_id
        self._write(data)

    # ---- provider keys (DPAPI ciphertext only) -----------------------------

    def stored_provider_keys(self):
        """{provider_id: base64 DPAPI blob} — ciphertext, never plaintext.
        Keys are decrypted to memory only after login (see vasp_ui.py)."""
        data = self._load()
        stored = data.get("provider_keys")
        if not isinstance(stored, dict):
            return {}
        out = {}
        for pid, blob in stored.items():
            if not isinstance(pid, str) or not PROVIDER_ID_RE.fullmatch(pid):
                continue
            if isinstance(blob, str) and blob:
                out[pid] = blob
        return out

    def save_provider_key(self, provider_id, ciphertext_b64):
        data = self._load()
        stored = data.get("provider_keys")
        if not isinstance(stored, dict):
            stored = {}
        stored[provider_id] = ciphertext_b64
        data["provider_keys"] = stored
        self._write(data)

    def remove_provider_key(self, provider_id):
        data = self._load()
        stored = data.get("provider_keys")
        if not isinstance(stored, dict) or provider_id not in stored:
            return
        stored.pop(provider_id, None)
        data["provider_keys"] = stored
        self._write(data)

    # ---- projects ---------------------------------------------------------

    def list_projects(self):
        data = self._load()
        projects = data.get("projects")
        if not isinstance(projects, list):
            return []
        out = []
        for p in projects:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            if not isinstance(pid, str) or not PROJECT_ID_RE.fullmatch(pid):
                continue
            out.append({
                "id": pid,
                "name": str(p.get("name", "未命名项目"))[:NAME_MAX],
                "path": str(p.get("path", ""))[:PATH_MAX],
                "created": str(p.get("created", "")),
            })
        return out

    def add_project(self, name, path, **extra):
        """Register a project. Raises ValueError with a user-safe message.

        Extra keyword fields (e.g. server="cl9") are stored verbatim in the
        project record so server-side projects keep their host association.
        """
        name = (name or "").strip()
        path = (path or "").strip()
        if not name or len(name) > NAME_MAX:
            raise ValueError(f"项目名称需为 1–{NAME_MAX} 个字符")
        if not path or len(path) > PATH_MAX:
            raise ValueError(f"文件夹路径需为 1–{PATH_MAX} 个字符")
        data = self._load()
        projects = data.setdefault("projects", [])
        if not isinstance(projects, list):
            projects = data["projects"] = []
        for p in projects:
            if isinstance(p, dict) and p.get("name") == name:
                raise ValueError("同名项目已存在")
            if isinstance(p, dict) and p.get("path") == path:
                raise ValueError("该文件夹已关联到其他项目")
        project = {
            "id": secrets.token_urlsafe(8),
            "name": name,
            "path": path,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            **extra,
        }
        projects.append(project)
        self._write(data)
        return project

    def remove_project(self, project_id):
        """Drop a project from the registry. Raises ValueError when unknown.
        (The caller is responsible for cleaning up its conversation
        directory.)"""
        data = self._load()
        projects = data.get("projects")
        if not isinstance(projects, list):
            raise ValueError("项目不存在")
        remaining = [p for p in projects if not (isinstance(p, dict) and p.get("id") == project_id)]
        if len(remaining) == len(projects):
            raise ValueError("项目不存在")
        data["projects"] = remaining
        self._write(data)
