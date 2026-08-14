"""Throwaway unit tests for optional remote_root (home-directory boundary)."""
import argparse
import contextlib
import importlib.util
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "vasp_gateway_under_test", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vasp_gateway.py")
)
sys.modules[spec.name] = spec
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

module.CFG = {"servers": {"s0": {"target": "user@host", "port": 22, "remote_root": "", "persist": "8h"}},
              "default_server": "s0"}
module.atomic_write_config = lambda config: None
module.audit = lambda *a: None
module.config_lock = lambda: contextlib.nullcontext()
module.connected = lambda name, timeout=4: True

HOME = "/public/home/user"
PROBE = []


def fake_remote(name, command, *, capture=False):
    PROBE.append((name, command))
    return subprocess.CompletedProcess(["ssh"], 0, stdout=HOME + "\n", stderr="")


module.remote = fake_remote
module.HOME_CACHE.clear()

entry_home = {"target": "user@host", "port": 22, "remote_root": "", "persist": "8h"}
entry_fixed = {"target": "user@host", "port": 22, "remote_root": "/public/home/user/calc", "persist": "8h"}

# validate_server accepts an empty remote_root.
module.validate_server("s1", entry_home)

# Fixed root keeps working without any connection probe.
assert module.validated_remote_path("/public/home/user/calc/a", entry_fixed, "s2") == "/public/home/user/calc/a"
try:
    module.validated_remote_path("/etc/passwd", entry_fixed, "s2")
    assert False, "path outside fixed root must raise"
except ValueError as exc:
    assert "must remain under" in str(exc)

# Empty root: probes $HOME once and caches it.
assert module.validated_remote_path("/public/home/user/calc/b", entry_home, "s1") == "/public/home/user/calc/b"
assert module.validated_remote_path("/public/home/user", entry_home, "s1") == "/public/home/user"
assert module.HOME_CACHE == {"s1": HOME}, module.HOME_CACHE
assert len(PROBE) == 1 and PROBE[0] == ("s1", "echo $HOME"), PROBE

# Everything under home is allowed; anything above it is rejected.
try:
    module.validated_remote_path("/public/home/other/secret", entry_home, "s1")
    assert False, "path outside probed home must raise"
except ValueError as exc:
    assert "must remain under" in str(exc)
assert len(PROBE) == 1, "cache must prevent a second probe"

# Probe failure surfaces a clear error (disconnected / no master socket).
def failing_remote(name, command, *, capture=False):
    return subprocess.CompletedProcess(["ssh"], 1, stdout="", stderr="connection refused")


module.remote = failing_remote
try:
    module.validated_remote_path("/public/home/user/x", {"remote_root": ""}, "s3")
    assert False, "probe failure must raise"
except RuntimeError as exc:
    assert "cannot determine the home directory" in str(exc)

# do_server_add stores "" when --root is omitted.
added = []
module.atomic_write_config = lambda config: added.append(config)
args = argparse.Namespace(name="s9", target="user@host", port=22, remote_root=None, persist="8h")
assert module.do_server_add(args) == 0
assert added[-1]["servers"]["s9"]["remote_root"] == "", added[-1]

# do_server_edit can clear a root back to "" (home boundary).
module.CFG = {"servers": {"s9": {"target": "user@host", "port": 22, "remote_root": "/a/b", "persist": "8h"}},
              "default_server": "s9"}
args = argparse.Namespace(name="s9", new_name=None, target=None, port=None, remote_root="", persist=None)
assert module.do_server_edit(args) == 0
assert added[-1]["servers"]["s9"]["remote_root"] == "", added[-1]
assert added[-1]["default_server"] == "s9", "default preserved"

# Invalid root values are still rejected at validation.
for bad in ("/bad root", "relative/path", "/a/../b"):
    entry = {"target": "user@host", "port": 22, "remote_root": bad, "persist": "8h"}
    try:
        module.validate_server("sX", entry)
        assert False, f"bad root {bad!r} must be rejected"
    except ValueError:
        pass

print("gateway root-optional unit tests: ALL PASS")
