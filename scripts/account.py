"""Local UI account store: email + scrypt-hashed password.

The account password is the user's own local UI login credential for
unlocking history conversations. It never leaves this machine and is never
stored in plaintext: only a scrypt hash (memory-hard, stdlib-only) is kept in
~/.vaspilot/account.json.

Security invariants:
  - plaintext passwords never touch disk, logs, or error messages
  - verify() returns False uniformly on any failure (no email enumeration)
  - register() is the only place that reveals "email already registered"
    (the registration form needs it; login never does)
  - the account file is replaced atomically (tempfile + os.replace)
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time

DEFAULT_PATH = os.path.expanduser("~/.vaspilot/account.json")

# Local part may contain the usual chars; domain must be dotted (top-level
# >= 2 letters). Length capped so oversized input cannot be stored.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,200}\.[A-Za-z]{2,}$")
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 256

# scrypt parameters: OWASP-style memory-hard settings, ~100 ms on desktop
# hardware. n must be a power of two.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1


class AccountStore:
    """Persist accounts as {accounts: {email: {salt, hash, created}}}."""

    def __init__(self, path=None):
        # VASPILOT_ACCOUNT_FILE override is for tests.
        self.path = path or os.environ.get("VASPILOT_ACCOUNT_FILE") or DEFAULT_PATH

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {"accounts": {}}

    def _write(self, data):
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".account-", suffix=".json", dir=d)
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

    def exists(self):
        """True when at least one account is registered."""
        return bool(self._load().get("accounts"))

    def register(self, email, password):
        """Create an account. Raises ValueError with a user-safe message."""
        email = (email or "").strip().lower()
        if not EMAIL_RE.match(email):
            raise ValueError("invalid email address")
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
            raise ValueError("password must be at least 8 characters")
        if len(password) > MAX_PASSWORD_LEN:
            raise ValueError("password too long")

        data = self._load()
        accounts = data.setdefault("accounts", {})
        if email in accounts:
            raise ValueError("email already registered")

        salt = secrets.token_bytes(16)
        digest = self._hash(password, salt)
        accounts[email] = {
            "salt": base64.b64encode(salt).decode("ascii"),
            "hash": base64.b64encode(digest).decode("ascii"),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._write(data)

    def verify(self, email, password):
        """True only when the stored scrypt hash matches. Uniform False on
        every failure path so callers cannot distinguish wrong email from
        wrong password."""
        email = (email or "").strip().lower()
        try:
            record = self._load()["accounts"].get(email)
            if not record or not isinstance(password, str) or not record.get("salt") or not record.get("hash"):
                return False
            salt = base64.b64decode(record["salt"])
            stored = base64.b64decode(record["hash"])
            digest = self._hash(password, salt)
            return hmac.compare_digest(digest, stored)
        except (KeyError, TypeError, ValueError):
            return False

    def _hash(self, password, salt):
        return hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P
        )
