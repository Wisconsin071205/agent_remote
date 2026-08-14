"""Unit tests for the transfer approval chain in the UI.

POST /api/action/transfer only stages the operation (never executes it);
POST /api/action/approve starts a worker thread; POST /api/action/poll
reports the outcome. The model has no route to these endpoints.
"""
import importlib.util
import os
import sys
import tempfile
import time

os.environ["VASPILOT_LOCAL_FILE"] = os.path.join(tempfile.mkdtemp(prefix="vaspilot-approval-"), "local.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "vasp_ui_under_test", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vasp_ui.py")
)
sys.modules[spec.name] = spec
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

STATE = module.STATE
STATE.servers = [
    {"name": "cl9", "root": "/public/home/wuhong", "target": "wuhong@cl9", "connected": True},
    {"name": "cl10", "root": "/public/home/wuhong", "target": "wuhong@cl10", "connected": True},
]
STATE.active_server = "cl9"

CAPTURED = []


class FakeController:
    def run(self, operation, *args, **kwargs):
        CAPTURED.append((operation, args, kwargs))
        return {"ok": True, "output": "transfer ok"}


STATE.catalog_controller = lambda: FakeController()


def post(path, payload):
    captured = []
    handler = object.__new__(module.Handler)
    handler.json_response = lambda body, status=200: captured.append((body, status))
    handler.handle_config_path(path, payload)
    return captured[-1][0]


# ---- staging: validates and returns an action_id, never executes ----------

for bad in (
    {"from_server": "cl9", "from_path": "/a", "to_server": "cl9", "to_path": "/b"},   # same server
    {"from_server": "ghost", "from_path": "/a", "to_server": "cl10", "to_path": "/b"},  # unknown
    {"from_server": "cl9", "from_path": "relative", "to_server": "cl10", "to_path": "/b"},  # bad path
    {"from_server": "cl9", "from_path": "/a/../b", "to_server": "cl10", "to_path": "/b"},
):
    try:
        post("/api/action/transfer", bad)
        assert False, f"{bad} must be rejected"
    except ValueError:
        pass
assert CAPTURED == [], "no controller call may run during staging"

resp = post("/api/action/transfer", {
    "from_server": "cl9", "from_path": "/public/home/wuhong/calc/x",
    "to_server": "cl10", "to_path": "/public/home/wuhong/calc/x",
})
assert resp["ok"] and resp["needs_approval"] is True, resp
action_id = resp["action_id"]
assert action_id and "（cl9）→" in resp["summary"], resp
assert CAPTURED == [], "staging must not execute the transfer"

# ---- approval decision ------------------------------------------------

resp = post("/api/action/poll", {"action_id": action_id})
assert resp["ok"] and resp["done"] is False and resp["waiting"] is True, resp

# Reject: removed, no execution, poll reports it gone.
resp = post("/api/action/approve", {"action_id": action_id, "approve": False})
assert resp["ok"] and resp["result"]["cancelled"] is True, resp
assert CAPTURED == [], "rejecting must not execute"
resp = post("/api/action/poll", {"action_id": action_id})
assert resp["ok"] and resp["done"] is True and resp["result"]["ok"] is False, resp

# Approve: worker thread runs the transfer, poll reports the outcome.
resp = post("/api/action/transfer", {
    "from_server": "cl9", "from_path": "/public/home/wuhong/calc/y",
    "to_server": "cl10", "to_path": "/public/home/wuhong/calc/y",
})
action_id = resp["action_id"]
resp = post("/api/action/approve", {"action_id": action_id, "approve": True})
assert resp["ok"] and resp["result"]["started"] is True, resp
deadline = time.time() + 10
while time.time() < deadline:
    resp = post("/api/action/poll", {"action_id": action_id})
    if resp.get("done"):
        break
    time.sleep(0.05)
assert resp["done"] and resp["result"]["ok"] is True, resp
assert len(CAPTURED) == 1, CAPTURED
op, args, kwargs = CAPTURED[0]
assert op == "transfer", op
assert kwargs.get("timeout") == 1800, kwargs
assert args[:2] == ("-FromServer", "cl9") and args[4:6] == ("-ToServer", "cl10"), args
assert args[2:4] == ("-FromPath", "/public/home/wuhong/calc/y"), args

# Approving an unknown / stale action is refused.
try:
    post("/api/action/approve", {"action_id": "nope", "approve": True})
    assert False, "unknown action must be refused"
except ValueError:
    pass

print("action approval chain unit tests: ALL PASS")
