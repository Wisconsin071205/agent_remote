"""Throwaway unit tests for account.py (run with a temp VASPILOT_ACCOUNT_FILE)."""
import base64
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".account-test")
os.environ["VASPILOT_ACCOUNT_FILE"] = os.path.join(TEST_DIR, "account.json")
shutil.rmtree(TEST_DIR, ignore_errors=True)

from account import AccountStore

s = AccountStore()
assert not s.exists(), "no accounts initially"

# Register + verify round trip.
s.register("User@Example.COM", "correct-horse")
assert s.exists()
assert s.verify("user@example.com", "correct-horse"), "email lowercased on register"
assert not s.verify("user@example.com", "wrong-password")
assert not s.verify("nobody@example.com", "correct-horse"), "unknown email -> uniform False"
assert not s.verify("user@example.com", ""), "empty password -> False"
assert not s.verify("user@example.com", None), "None password -> False"

# Duplicate email rejected.
try:
    s.register("user@example.com", "another-pass")
    assert False, "duplicate must raise"
except ValueError as exc:
    assert "already" in str(exc)

# Invalid inputs rejected.
for bad_email in ("", "not-an-email", "a@b", "x@y.c", "a" * 100 + "@x.com"):
    try:
        s.register(bad_email, "longenough1")
        assert False, f"bad email accepted: {bad_email!r}"
    except ValueError:
        pass
try:
    s.register("ok@example.com", "short")
    assert False, "short password accepted"
except ValueError:
    pass

# Password never stored in plaintext anywhere.
with open(s.path, "r", encoding="utf-8-sig") as fh:
    raw = fh.read()
assert "correct-horse" not in raw and "another-pass" not in raw, "plaintext leaked"
assert base64.b64decode(__import__("json").loads(raw)["accounts"]["user@example.com"]["salt"]) and True

# Corrupt file: verify returns False, register still works (atomic rewrite).
with open(s.path, "w", encoding="utf-8") as fh:
    fh.write("{broken")
assert not s.verify("user@example.com", "correct-horse"), "corrupt file -> uniform False"
s.register("second@example.com", "another-pass-123")
assert s.verify("second@example.com", "another-pass-123"), "register repairs corrupt file"

# Register the first account again after the corrupt-file rewrite, then check
# a fresh store instance reads the same file (persistence across restart).
s.register("user@example.com", "correct-horse")
s2 = AccountStore()
assert s2.verify("user@example.com", "correct-horse"), "file persisted"
assert s2.verify("second@example.com", "another-pass-123"), "both accounts persisted"
assert not s2.verify("user@example.com", "wrong"), "wrong password after reload"

shutil.rmtree(TEST_DIR, ignore_errors=True)
print("account.py unit tests: ALL PASS")
