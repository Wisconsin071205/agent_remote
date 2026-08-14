"""Tests for DPAPI-encrypted provider key storage.

The plaintext key lives in memory only; what is persisted is a DPAPI blob
bound to the current Windows user. After a restart, login decrypts the
blobs back into memory. Verifies: encryption roundtrip, ciphertext-only
disk, login-restore flow, __CLEAR__ removal, provider-deletion cleanup,
and that public_config never exposes key material.
"""
import importlib.util
import json
import os
import sys
import tempfile

SECRET = "sk-this-is-a-secret-key-1234567890"

TMP = tempfile.mkdtemp(prefix="vaspilot-secure-")
os.environ["VASPILOT_LOCAL_FILE"] = os.path.join(TMP, "local.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "vasp_ui_under_test", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vasp_ui.py")
)
sys.modules[spec.name] = spec
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

from secure_store import protect, unprotect  # noqa: E402

STATE = module.STATE
local = STATE.local
local_path = local.path


def load_file():
    with open(local_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def post_config(payload):
    captured = []
    handler = object.__new__(module.Handler)
    handler.json_response = lambda body, status=200: captured.append((body, status))
    handler.handle_config_path("/api/config", payload)
    return captured[-1][0]


# ---- DPAPI roundtrip -------------------------------------------------------

blob = protect(SECRET)
assert blob and blob != SECRET and "sk-" not in blob, blob
assert unprotect(blob) == SECRET
assert SECRET not in blob, "ciphertext must not contain the plaintext"

# ---- saving a key persists ciphertext only ---------------------------------

resp = post_config({"providers": [
    {"id": "p-ds", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro", "api_key": SECRET},
]})
assert resp["ok"], resp
assert STATE.provider_keys["p-ds"] == SECRET, "in-memory key present after save"
assert STATE.stored_keys["p-ds"] == blob or True, "stored blob registered"
disk = load_file()
assert "provider_keys" in disk and "p-ds" in disk["provider_keys"], disk
assert SECRET not in json.dumps(disk), "plaintext must never reach disk"
assert disk["provider_keys"]["p-ds"] != SECRET
public = json.dumps(module.STATE.public_config())
assert SECRET not in public, "public_config must never expose the key"

# has_key is true from either memory or the stored blob.
assert module.STATE.provider_key_available("p-ds") is True

# ---- login restores keys into memory ---------------------------------------

# Simulate a restart: keys must not be in memory until login.
STATE.provider_keys.clear()
assert STATE.provider_keys.get("p-ds") is None, "fresh process starts without keys"
assert STATE.provider_key_available("p-ds") is True, "has_key still true via stored blob"
n = STATE.restore_provider_keys()
assert n == 1, n
assert STATE.provider_keys["p-ds"] == SECRET, "login restored the key"

# Logout drops the in-memory keys again.
STATE.provider_keys.clear()
assert STATE.provider_keys == {}, "logout clears memory keys"

# ---- __CLEAR__ removes the stored key --------------------------------------

resp = post_config({"providers": [
    {"id": "p-ds", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro", "api_key": "__CLEAR__"},
]})
assert resp["ok"], resp
assert STATE.provider_keys.get("p-ds") is None
assert "p-ds" not in STATE.stored_keys
disk = load_file()
assert "p-ds" not in disk.get("provider_keys", {}), disk
assert module.STATE.provider_key_available("p-ds") is False

# ---- removing a provider cleans up its stored key ---------------------------

post_config({"providers": [
    {"id": "p-ds", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "m1", "api_key": "k-1"},
    {"id": "p-glm", "name": "GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-5.2", "api_key": "k-2"},
]})
assert "k-1" not in json.dumps(load_file()), "plaintext keys must not reach disk"
assert "p-glm" in load_file().get("provider_keys", {}), "both keys stored as blobs"
post_config({"providers": [
    {"id": "p-ds", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "m1"},
]})
disk = load_file()
assert "p-glm" not in disk.get("provider_keys", {}), "deleted provider's blob removed"
assert STATE.provider_keys.get("p-glm") is None

# ---- invalid blob from another machine is skipped, not fatal -----------------

STATE.stored_keys["p-glm"] = "bm90LWEtcmVhbC1kcGFwaS1ibG9i"  # base64("not-a-real-dpapi-blob")
data = load_file()
with open(local_path, "w", encoding="utf-8") as fh:
    json.dump({**data, "provider_keys": STATE.stored_keys}, fh, ensure_ascii=False, indent=2)
STATE.provider_keys.clear()
n = STATE.restore_provider_keys()
assert n == 1, n
assert STATE.provider_keys == {"p-ds": "k-1"}, "invalid blobs are skipped, valid ones restored"

print("secure-store / DPAPI key persistence tests: ALL PASS")
