"""Windows DPAPI encryption for the provider API keys.

The keys themselves still live in process memory only. When the user saves
them from the settings panel they are encrypted here with the current Windows
user's credentials (CryptProtectData) and the ciphertext is persisted next to
the provider list. After a restart, logging in restores the keys to memory by
decrypting them; the plaintext never touches disk and never appears in any
config payload sent to the browser.

DPAPI binds the blob to the Windows user (and machine) that created it:
another Windows account, or the same account on another machine, cannot
decrypt it. The Windows account is the trust boundary — exactly like the
Windows account being the one that runs this app at all.
"""

import base64
import binascii
import ctypes
import os
from ctypes import wintypes

CRYPTPROTECT_UI_FORBIDDEN = 0x1

_local_free = ctypes.windll.kernel32.LocalFree
_crypt_protect = ctypes.windll.crypt32.CryptProtectData
_crypt_unprotect = ctypes.windll.crypt32.CryptUnprotectData
_crypt_protect.restype = wintypes.BOOL
_crypt_unprotect.restype = wintypes.BOOL


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))


def _to_bytes(blob: _DataBlob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        _local_free(blob.pbData)


def protect(plaintext: str) -> str:
    """Encrypt a string with the current Windows user's credentials; returns
    base64 ciphertext suitable for storage."""
    if os.name != "nt":
        raise OSError("DPAPI encryption is only available on Windows")
    blob_in = _blob(plaintext.encode("utf-8"))
    blob_out = _DataBlob()
    if not _crypt_protect(
        ctypes.byref(blob_in), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    ):
        raise OSError(ctypes.get_last_error() or 1, "CryptProtectData failed")
    return base64.b64encode(_to_bytes(blob_out)).decode("ascii")


def unprotect(ciphertext_b64: str) -> str:
    """Decrypt a base64 DPAPI blob; raises OSError when the ciphertext is
    invalid or belongs to another Windows user / machine."""
    if os.name != "nt":
        raise OSError("DPAPI encryption is only available on Windows")
    try:
        raw = base64.b64decode(ciphertext_b64)
    except (binascii.Error, ValueError) as exc:
        raise OSError("invalid ciphertext") from exc
    blob_in = _blob(raw)
    blob_out = _DataBlob()
    if not _crypt_unprotect(
        ctypes.byref(blob_in), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    ):
        raise OSError(ctypes.get_last_error() or 1, "CryptUnprotectData failed")
    try:
        return _to_bytes(blob_out).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OSError("decrypted plaintext is not valid UTF-8") from exc
