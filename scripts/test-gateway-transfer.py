"""Unit tests for the gateway transfer (server-to-server staging) tool."""
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

module.CFG = {"servers": {
    "s0": {"target": "user@alpha", "port": 22, "remote_root": "/pub/home/user", "persist": "8h"},
    "s1": {"target": "user@beta", "port": 22, "remote_root": "", "persist": "8h"},
}, "default_server": "s0"}
module.atomic_write_config = lambda config: None
module.audit = lambda *a: None
module.config_lock = lambda: contextlib.nullcontext()
module.connected = lambda name, timeout=4: True
module.require_connection = lambda name: None
module.remote = lambda name, command, *, capture=False: subprocess.CompletedProcess(
    ["ssh"], 0, stdout="/pub/home/user\n", stderr="")
module.HOME_CACHE.clear()

RUNS = []


def fake_run(args_list, check=False, timeout=None):
    RUNS.append((list(args_list), timeout))
    return subprocess.CompletedProcess(args_list, 0)


module.subprocess.run = fake_run
module.TRANSFER_TIMEOUT = 1800


def transfer_args_side_effect(name):
    return ["scp", "-q", "-P", "22", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f"ControlPath={name}.sock"]


module.transfer_args = transfer_args_side_effect

entry_home = {"target": "user@beta", "port": 22, "remote_root": "", "persist": "8h"}

# Happy path: pull from s0 (fixed root), push to s1 (home boundary).
args = argparse.Namespace(from_server="s0", from_path="/pub/home/user/calc/a",
                          to_server="s1", to_path="/pub/home/user/calc/b")
assert module.do_transfer(args) == 0, "transfer must report success"
assert len(RUNS) == 2, RUNS
pull, push = RUNS
assert pull[0] == ["scp", "-q", "-P", "22", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", "ControlPath=s0.sock", "-r", "user@alpha:/pub/home/user/calc/a",
                   pull[0][-1]], pull[0]
assert push[0] == ["scp", "-q", "-P", "22", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", "ControlPath=s1.sock", "-r", push[0][-2], "user@beta:/pub/home/user/calc/b"], push[0]
assert pull[1] == 1800 and push[1] == 1800, "both legs must use the transfer timeout"
assert pull[0][-1] == push[0][-2], "stage path must be shared between legs"
assert "vasp-remote-agent-transfer-" in pull[0][-1], pull[0]
assert not os.path.exists(pull[0][-1]), "stage must be cleaned up"

# Same server both ends is rejected.
RUNS.clear()
try:
    module.do_transfer(argparse.Namespace(from_server="s0", from_path="/a", to_server="s0", to_path="/b"))
    assert False, "same-server transfer must be rejected"
except ValueError as exc:
    assert "different servers" in str(exc)
assert RUNS == [], "no scp may run for a same-server transfer"

# Pull failure aborts before the push and raises.
RUNS.clear()


def fail_on_first(args_list, check=False, timeout=None):
    RUNS.append(list(args_list))
    return subprocess.CompletedProcess(args_list, 1, stderr="no such file")


module.subprocess.run = fail_on_first
try:
    module.do_transfer(argparse.Namespace(from_server="s0", from_path="/pub/home/user/x", to_server="s1", to_path="/pub/home/user/y"))
    assert False, "failed pull must raise"
except RuntimeError as exc:
    assert "could not pull" in str(exc)
assert len(RUNS) == 1, "push must not run after a failed pull"

# Paths outside the allowed roots are rejected on both ends.
module.subprocess.run = fake_run
for bad_path in ("/etc/passwd",):
    try:
        module.do_transfer(argparse.Namespace(from_server="s0", from_path=bad_path, to_server="s1", to_path="/ok"))
        assert False, f"source {bad_path!r} must be rejected"
    except ValueError:
        pass
try:
    module.do_transfer(argparse.Namespace(from_server="s0", from_path="/ok", to_server="s1", to_path="/etc/passwd"))
    assert False, "destination outside home must be rejected"
except ValueError:
    pass

# require_not_root applies to both endpoints.
module.require_not_root = lambda path, entry: (_ for _ in ()).throw(ValueError("must not be remote_root"))
try:
    module.do_transfer(argparse.Namespace(from_server="s0", from_path="/ok", to_server="s1", to_path="/ok"))
    assert False, "root-path transfer must be rejected"
except ValueError:
    pass

print("gateway transfer unit tests: ALL PASS")
