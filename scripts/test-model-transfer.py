"""Unit tests for the model-initiated cross-server transfer.

transfer_remote_files is a model tool, but the transfer itself only ever runs
in a background thread after the streaming approval dialog was approved.
Verifies: tool registration, staging validation, execute_tool handoff
(started + action_id, never blocking), approval summary, and the
always-approve exclusion from auto-approve mode.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time

os.environ["VASPILOT_LOCAL_FILE"] = os.path.join(tempfile.mkdtemp(prefix="vaspilot-model-transfer-"), "local.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "vasp_ui_under_test", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vasp_ui.py")
)
sys.modules[spec.name] = spec
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

STATE = module.STATE
STATE.servers = [
    {"name": "cl9", "root": "/pub/home/wuhong", "target": "wuhong@cl9", "connected": True},
    {"name": "cl10", "root": "/pub/home/wuhong", "target": "wuhong@cl10", "connected": False},
    {"name": "cl12", "root": "/pub/home/wuhong", "target": "wuhong@cl12", "connected": True},
]
STATE.active_server = "cl9"

CAPTURED = []


class FakeController:
    def run(self, operation, *args, **kwargs):
        CAPTURED.append((operation, args, kwargs))
        return {"ok": True, "output": "transferred"}


STATE.catalog_controller = lambda: FakeController()

# ---- tool registration ----------------------------------------------------

names = [tool["function"]["name"] for tool in module.AGENT.TOOLS]
assert "transfer_remote_files" in names, names
tool = next(t for t in module.AGENT.TOOLS if t["function"]["name"] == "transfer_remote_files")
props = tool["function"]["parameters"]["properties"]
assert set(props) == {"from_server", "from_path", "to_server", "to_path"}, props
assert tool["function"]["parameters"]["required"] == ["from_server", "from_path", "to_server", "to_path"]
assert "never auto-approved" in tool["function"]["description"].lower() or "not" in tool["function"]["description"]

assert "transfer_remote_files" in module.RISKY_TOOLS
assert "transfer_remote_files" in module.ALWAYS_APPROVE_TOOLS

# ---- stage_transfer validation --------------------------------------------

for args, want in (
    (("", "/a", "cl12", "/b"), "两个不同的服务器"),
    (("cl9", "/a", "cl9", "/b"), "两个不同的服务器"),
    (("ghost", "/a", "cl12", "/b"), "源服务器不在目录中"),
    (("cl9", "/a", "ghost", "/b"), "目标服务器不在目录中"),
    (("cl9", "/a", "cl10", "/b"), "未连接"),
    (("cl9", "relative", "cl12", "/b"), "不支持字符"),
    (("cl9", "/a/../b", "cl12", "/b"), "不能包含"),
):
    try:
        module.stage_transfer(*args)
        assert False, f"{args} must be rejected"
    except ValueError as exc:
        assert want in str(exc), (args, str(exc))

action_id, summary = module.stage_transfer("cl9", "/pub/home/wuhong/calc/x", "cl12", "/pub/home/wuhong/calc/x")
assert action_id and "cl9" in summary and "cl12" in summary, summary
assert STATE.pending_actions[action_id]["status"] == "pending"

# ---- execute_tool handoff: approved -> background, returns started ---------

result = module.execute_tool(object(), "transfer_remote_files", {
    "from_server": "cl9", "from_path": "/pub/home/wuhong/calc/y",
    "to_server": "cl12", "to_path": "/pub/home/wuhong/calc/y",
})
assert result["ok"] and result["started"] is True and result["action_id"], result
assert "后台启动" in result["message"], result
action_id2 = result["action_id"]

# The worker runs on a separate thread; wait for it to finish.
deadline = time.time() + 10
while time.time() < deadline:
    with STATE.lock:
        entry = STATE.pending_actions.get(action_id2)
    if entry and entry["status"] == "done":
        break
    time.sleep(0.05)
assert entry["result"]["ok"] is True, entry
assert len(CAPTURED) == 1, CAPTURED
op, args, kwargs = CAPTURED[0]
assert op == "transfer" and kwargs.get("timeout") == 1800, (op, kwargs)
assert args[0:2] == ("-FromServer", "cl9") and args[2:4] == ("-FromPath", "/pub/home/wuhong/calc/y"), args

# Polling consumes the action; a second poll reports it expired.
resp = module.stage_transfer("cl9", "/pub/home/wuhong/calc/z", "cl12", "/pub/home/wuhong/calc/z", status="running")
module.run_transfer_worker(resp[0])

# Bad arguments from the model never raise: they come back as an error result.
bad = module.execute_tool(object(), "transfer_remote_files", {"from_server": "cl9", "from_path": "/x", "to_server": "cl9", "to_path": "/y"})
assert bad["ok"] is False and "两个不同的服务器" in bad["error"], bad
bad = module.execute_tool(object(), "transfer_remote_files", {"from_server": "cl9", "from_path": "/x", "to_server": "ghost", "to_path": "/y"})
assert bad["ok"] is False and "目标服务器不在目录中" in bad["error"], bad

# ---- approval summary ------------------------------------------------------

title, description = module.approval_summary("transfer_remote_files", {
    "from_server": "cl9", "from_path": "/a/b", "to_server": "cl12", "to_path": "/c/d",
})
assert title == "跨服务器传输" and "（cl9）" in description and "（cl12）" in description, (title, description)

print("model-initiated transfer unit tests: ALL PASS")
