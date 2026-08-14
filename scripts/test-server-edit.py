"""Throwaway unit tests for gateway do_server_edit (mocked config/connection)."""
import argparse
import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util

spec = importlib.util.spec_from_file_location(
    "vasp_gateway_under_test", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vasp_gateway.py")
)
sys.modules[spec.name] = spec  # must be registered before exec_module
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

BASE = {
    "cl9": {"target": "user@192.0.2.1", "port": 22, "remote_root": "/public/home/user", "persist": "8h"},
    "cl10": {"target": "user@10.0.0.10", "port": 22, "remote_root": "/public/home/user", "persist": "8h"},
}
module.CFG = {"servers": dict(BASE), "default_server": "cl9"}

written = []
audited = []

module.atomic_write_config = lambda config: written.append(config)
module.audit = lambda *a: audited.append(a)
module.config_lock = lambda: contextlib.nullcontext()
module.connected = lambda name, timeout=4: name == "cl10"  # only cl10 is "connected"


def run(**kw):
    kw.setdefault("new_name", None)
    return module.do_server_edit(argparse.Namespace(**kw))


# Unknown server.
try:
    run(name="nope", target="a@b", port=None, remote_root=None, persist=None)
    assert False, "unknown server must raise"
except ValueError as exc:
    assert "unknown server" in str(exc)

# Nothing to change.
try:
    run(name="cl9", target=None, port=None, remote_root=None, persist=None)
    assert False, "no-change must raise"
except ValueError as exc:
    assert "nothing to change" in str(exc)

# Target change on the connected server is refused.
try:
    run(name="cl10", target="other@host", port=None, remote_root=None, persist=None)
    assert False, "target change while connected must raise"
except ValueError as exc:
    assert "disconnect cl10" in str(exc), exc

# Port change on the connected server is refused too.
try:
    run(name="cl10", target=None, port=2222, remote_root=None, persist=None)
    assert False, "port change while connected must raise"
except ValueError:
    pass

# Root change on the connected server is allowed (no routing confusion).
assert run(name="cl10", target=None, port=None, remote_root="/public/home/user/new", persist=None) == 0
assert written[-1]["servers"]["cl10"]["remote_root"] == "/public/home/user/new", written[-1]
assert audited[-1][0] == "server-edit" and "root" in audited[-1][2], audited[-1]
written.clear(); audited.clear()

# Persist-only change on the disconnected server.
assert run(name="cl9", target=None, port=None, remote_root=None, persist="4h") == 0
assert written[-1]["servers"]["cl9"]["persist"] == "4h"
assert written[-1]["servers"]["cl9"]["target"] == "user@192.0.2.1", "other fields untouched"
assert "persist" in audited[-1][2] and "root" not in audited[-1][2], audited[-1]
written.clear(); audited.clear()

# Target+port change while disconnected is allowed.
assert run(name="cl9", target="new@host", port=2222, remote_root=None, persist=None) == 0
entry = written[-1]["servers"]["cl9"]
assert entry["target"] == "new@host" and entry["port"] == 2222
assert set(audited[-1][2].split()) == {"cl9", "target", "port"}, audited[-1]
assert written[-1]["default_server"] == "cl9", "default preserved"

# Invalid new target rejected by validation (does not reach write).
try:
    run(name="cl9", target="not-a-target", port=None, remote_root=None, persist=None)
    assert False, "invalid target must raise"
except ValueError:
    pass
assert len(written) == 1, "no write after validation failure"

# Invalid new root rejected.
try:
    run(name="cl9", target=None, port=None, remote_root="/bad root with space", persist=None)
    assert False, "invalid root must raise"
except ValueError:
    pass

# Invalid persist rejected.
try:
    run(name="cl9", target=None, port=None, remote_root=None, persist="tomorrow")
    assert False, "invalid persist must raise"
except ValueError:
    pass

# --- Rename support ---
written.clear(); audited.clear()

# Rename + persist on a disconnected server.
assert run(name="cl9", new_name="cl9b", target=None, port=None, remote_root=None, persist="4h") == 0
cfg = written[-1]
assert "cl9" not in cfg["servers"] and cfg["servers"]["cl9b"]["persist"] == "4h", cfg
assert cfg["servers"]["cl9b"]["target"] == "user@192.0.2.1", "entry carried over"
assert cfg["default_server"] == "cl9b", "default follows the rename"
assert "cl9->cl9b" in audited[-1][2] and "persist" in audited[-1][2], audited[-1]
assert audited[-1][3] == "cl9b", audited[-1]
written.clear(); audited.clear()

# Rename to an existing name is refused.
try:
    run(name="cl9", new_name="cl10", target=None, port=None, remote_root=None, persist=None)
    assert False, "rename to existing name must raise"
except ValueError as exc:
    assert "already exists" in str(exc)
assert len(written) == 0, "no write after rename collision"

# Rename while connected is refused.
try:
    run(name="cl10", new_name="cl10b", target=None, port=None, remote_root=None, persist=None)
    assert False, "rename of connected server must raise"
except ValueError as exc:
    assert "disconnect cl10 before renaming" in str(exc)

# Invalid new name rejected.
try:
    run(name="cl9", new_name="bad name!", target=None, port=None, remote_root=None, persist=None)
    assert False, "invalid new name must raise"
except ValueError as exc:
    assert "invalid server name" in str(exc)

# Pure rename (no field change) is allowed when disconnected.
assert run(name="cl9", new_name="cl9c", target=None, port=None, remote_root=None, persist=None) == 0
assert "cl9" not in written[-1]["servers"] and "cl9c" in written[-1]["servers"]
assert written[-1]["default_server"] == "cl9c"
assert "cl9->cl9c" in audited[-1][2]
written.clear(); audited.clear()

# new_name equal to name is a no-op (falls through to nothing-to-change).
try:
    run(name="cl9", new_name="cl9", target=None, port=None, remote_root=None, persist=None)
    assert False, "same-name no-change must raise"
except ValueError as exc:
    assert "nothing to change" in str(exc)

print("gateway do_server_edit unit tests: ALL PASS")
