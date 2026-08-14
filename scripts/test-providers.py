"""Throwaway unit tests for multi-provider API configuration.

Each provider {id, name, base_url, model} has its own API key that lives in
process memory only; local.json never contains a key. Covers migration,
persistence, switching, routing and validation.
"""
import importlib.util
import json
import os
import sys
import tempfile

os.environ["DEEPSEEK_API_KEY"] = "seed-key-123"

TMP = tempfile.mkdtemp(prefix="vaspilot-providers-")
os.environ["VASPILOT_LOCAL_FILE"] = os.path.join(TMP, "local.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "vasp_ui_under_test", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vasp_ui.py")
)
sys.modules[spec.name] = spec
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)  # module-level STATE = AppState() uses the tmp file

STATE = module.STATE
local_path = STATE.local.path


def load_file():
    with open(local_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def resp_cfg():
    return STATE.public_config()


def post_config(payload):
    captured = []
    handler = object.__new__(module.Handler)
    handler.json_response = lambda body, status=200: captured.append((body, status))
    handler.handle_config_path("/api/config", payload)
    return captured[-1][0]


# ---- migration ------------------------------------------------------------

assert STATE.providers, "providers must be seeded"
assert STATE.providers[0]["id"] == "p-default", STATE.providers
assert STATE.providers[0]["model"] == module.AGENT.DEFAULT_MODEL
assert STATE.providers[0]["base_url"] == module.AGENT.DEFAULT_BASE_URL
assert any("open.bigmodel.cn" in p["base_url"] for p in STATE.providers), "GLM preset missing"
glm = next(p for p in STATE.providers if p["id"] == "p-glm")
assert glm["model"] == "glm-5.2" and glm["base_url"] == "https://open.bigmodel.cn/api/paas/v4", glm
assert STATE.current_provider == "p-default"
assert STATE.provider_keys == {"p-default": "seed-key-123"}, "env key must seed the current provider"
assert next(p for p in resp_cfg()["providers"] if p["id"] == "p-default")["has_key"] is True
assert next(p for p in resp_cfg()["providers"] if p["id"] == "p-glm")["has_key"] is False
assert "seed-key-123" not in json.dumps(resp_cfg()["providers"]), "public_config must never expose keys"

# ---- POST providers: replace list, keys stay in memory only ---------------

payload = [
    {"id": "p-ds", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro", "api_key": "ds-key"},
    {"id": "p-glm", "name": "GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-5.2"},
    {"name": "中转", "base_url": "http://127.0.0.1:3000/v1", "model": "custom-m", "api_key": "local-key"},
]
resp = post_config({"providers": payload})
assert resp["ok"], resp
assert STATE.provider_keys.get("p-ds") == "ds-key"
assert STATE.provider_keys.get("p-glm") is None, "empty key must not be stored"
third = [p for p in STATE.providers if p["name"] == "中转"][0]
assert third["id"].startswith("p-") and third["base_url"] == "http://127.0.0.1:3000/v1"
assert STATE.provider_keys.get(third["id"]) == "local-key"
disk = load_file()
for p in disk["providers"]:
    assert "api_key" not in p and "key" not in json.dumps(p), "key must never reach disk"
# current provider no longer in the list -> falls back to the first
assert STATE.current_provider == "p-ds"

# ---- switch current provider and route the client -------------------------

resp = post_config({"current_provider": "p-glm"})
assert resp["ok"] and resp["config"]["current_provider"] == "p-glm"
assert load_file()["current_provider"] == "p-glm"
try:
    STATE.client()
    assert False, "GLM has no key; client() must refuse"
except ValueError as exc:
    assert "GLM" in str(exc), exc
# Seed the GLM key, then the client must target the GLM endpoint.
resp = post_config({"api_key": "glm-key"})
assert STATE.provider_keys["p-glm"] == "glm-key"
client = STATE.client()
assert client.url == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
assert client.api_key == "glm-key" and client.model == "glm-5.2"

# ---- legacy fields map onto the current provider --------------------------

resp = post_config({"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"})
glm_now = next(p for p in STATE.providers if p["id"] == "p-glm")
assert glm_now["base_url"] == "https://api.deepseek.com/v1" and glm_now["model"] == "deepseek-chat"
assert STATE.client().url == "https://api.deepseek.com/v1/chat/completions"
resp = post_config({"api_key": ""})
assert STATE.provider_keys["p-glm"] == "glm-key", "empty api_key must not clear the memory key"

# ---- removing the current provider falls back to the first ----------------

resp = post_config({"providers": [{"id": "only", "name": "Only", "base_url": "https://x.dev/v1", "model": "m"}]})
assert resp["ok"]
assert STATE.current_provider == "only" and load_file()["current_provider"] == "only"

# ---- validation ------------------------------------------------------------

def expect_value_error(payload, needle):
    try:
        post_config(payload)
        assert False, f"payload {payload!r} must raise"
    except ValueError as exc:
        assert needle in str(exc), (payload, exc)


expect_value_error({"providers": []}, "至少保留")
expect_value_error({"providers": [{"id": "a", "name": "X", "base_url": "ftp://x", "model": "m"}]}, "http")
expect_value_error({"providers": [{"id": "a", "name": "", "base_url": "https://x", "model": "m"}]}, "名称")
expect_value_error({"providers": [{"id": "a", "name": "X", "base_url": "https://x", "model": ""}]}, "模型")
expect_value_error({"providers": [{"id": "a", "name": "X", "base_url": "https://x", "model": "m"}, {"id": "a", "name": "Y", "base_url": "https://y", "model": "n"}]}, "重复")
expect_value_error({"current_provider": "nope"}, "未知的服务商")

# providers with an explicit port are accepted and persisted verbatim.
resp = post_config({"providers": [{"id": "pp", "name": "中转", "base_url": "http://localhost:8000/v1", "model": "m"}]})
assert resp["ok"] and load_file()["providers"][0]["base_url"] == "http://localhost:8000/v1"

print("provider unit tests: ALL PASS")
