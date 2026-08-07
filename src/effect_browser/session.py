from __future__ import annotations

import base64
import binascii
import json
import math
import os
import sys
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

SESSION_STATE_FORMAT_VERSION = 1
DEFAULT_SESSION_STATE_MAX_BYTES = 2 * 1024 * 1024
MIN_SESSION_STATE_BYTES = 64 * 1024
MAX_SESSION_STATE_BYTES = 16 * 1024 * 1024
MAX_SESSION_STATE_CIPHERTEXT_BYTES = MAX_SESSION_STATE_BYTES + 64 * 1024
DEFAULT_SESSION_RETENTION_HOURS = 7 * 24
MAX_SESSION_RETENTION_HOURS = 30 * 24

_AES_HEADER = b"EBS\x01A"
_DPAPI_HEADER = b"EBS\x01D"
_AES_NONCE_BYTES = 12
_MAX_JSON_DEPTH = 32


class SessionStateError(ValueError):
    """Base error for a browser session checkpoint that cannot be used safely."""


class SessionStateTooLarge(SessionStateError):
    """The plaintext or ciphertext exceeds its configured hard bound."""


class SessionStateValidationError(SessionStateError):
    """The decrypted value is not canonical Playwright storage state."""


class SessionProtectionUnavailable(SessionStateError):
    """No secure session-state protector is configured on this platform."""


class _ProtectorBackend(Protocol):
    algorithm: str
    header: bytes

    def protect(self, plaintext: bytes, binding: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes, binding: bytes) -> bytes: ...


class SessionStateProtector:
    """Protect bounded Playwright storage state without writing plaintext to disk.

    An explicit URL-safe-base64 AES-256 key is portable and takes precedence. When
    no key is supplied, Windows uses per-user DPAPI. Other platforms fail closed so
    callers cannot accidentally introduce a plaintext persistence fallback.
    """

    def __init__(
        self,
        *,
        encryption_key: SecretStr | str | bytes | None = None,
        max_bytes: int = DEFAULT_SESSION_STATE_MAX_BYTES,
    ) -> None:
        if not MIN_SESSION_STATE_BYTES <= max_bytes <= MAX_SESSION_STATE_BYTES:
            raise ValueError("session state max bytes must be between 65536 and 16777216")
        self.max_bytes = max_bytes
        if encryption_key is not None:
            self._backend: _ProtectorBackend = _AesGcmBackend(
                decode_session_encryption_key(encryption_key)
            )
        elif sys.platform == "win32":
            self._backend = _WindowsDpapiBackend()
        else:
            raise SessionProtectionUnavailable(
                "session persistence requires an explicit encryption key outside Windows"
            )

    @property
    def algorithm(self) -> str:
        """Return a non-secret identifier suitable for diagnostics."""

        return self._backend.algorithm

    def protect(
        self,
        state: Mapping[str, Any],
        tenant_id: UUID,
        task_id: UUID,
    ) -> bytes:
        plaintext = canonical_session_state_bytes(state, max_bytes=self.max_bytes)
        try:
            protected = self._backend.protect(
                plaintext,
                _session_binding(tenant_id, task_id),
            )
        except SessionStateError:
            raise
        except Exception as exc:
            raise SessionStateError(
                "browser session state could not be protected"
            ) from exc
        if len(protected) > self._ciphertext_limit:
            raise SessionStateTooLarge(
                "protected browser session state exceeds the configured size limit"
            )
        return protected

    def unprotect(
        self,
        ciphertext: bytes,
        tenant_id: UUID,
        task_id: UUID,
    ) -> dict[str, Any]:
        if not isinstance(ciphertext, bytes) or not ciphertext:
            raise SessionStateValidationError(
                "protected browser session state is invalid or unavailable"
            )
        if len(ciphertext) > self._ciphertext_limit:
            raise SessionStateTooLarge(
                "protected browser session state exceeds the configured size limit"
            )
        try:
            plaintext = self._backend.unprotect(
                ciphertext,
                _session_binding(tenant_id, task_id),
            )
        except (InvalidTag, SessionStateError, OSError, ValueError) as exc:
            raise SessionStateValidationError(
                "protected browser session state is invalid or unavailable"
            ) from exc
        except Exception as exc:
            raise SessionStateValidationError(
                "protected browser session state is invalid or unavailable"
            ) from exc
        if len(plaintext) > self.max_bytes:
            raise SessionStateTooLarge(
                "browser session state exceeds the configured size limit"
            )
        try:
            value = json.loads(plaintext)
            canonical = canonical_session_state_bytes(value, max_bytes=self.max_bytes)
        except (
            RecursionError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            SessionStateError,
        ) as exc:
            raise SessionStateValidationError(
                "protected browser session state is invalid or unavailable"
            ) from exc
        if canonical != plaintext:
            raise SessionStateValidationError(
                "protected browser session state is not canonically encoded"
            )
        return dict(value)

    @property
    def _ciphertext_limit(self) -> int:
        return min(
            self.max_bytes + 64 * 1024,
            MAX_SESSION_STATE_CIPHERTEXT_BYTES,
        )


def available_session_state_protector(
    *,
    encryption_key: SecretStr | str | bytes | None = None,
    max_bytes: int = DEFAULT_SESSION_STATE_MAX_BYTES,
) -> SessionStateProtector | None:
    """Return a secure protector, or disable persistence when none is available."""

    try:
        return SessionStateProtector(
            encryption_key=encryption_key,
            max_bytes=max_bytes,
        )
    except SessionProtectionUnavailable:
        return None


def canonical_session_state_bytes(
    state: Mapping[str, Any],
    *,
    max_bytes: int = DEFAULT_SESSION_STATE_MAX_BYTES,
) -> bytes:
    """Validate and canonically encode one Playwright storage-state document."""

    if not isinstance(state, Mapping):
        raise SessionStateValidationError("browser session state must be a JSON object")
    if not isinstance(state.get("cookies"), list) or not isinstance(
        state.get("origins"), list
    ):
        raise SessionStateValidationError(
            "browser session state must contain cookie and origin lists"
        )
    _validate_json_value(state, depth=0)
    try:
        encoded = json.dumps(
            state,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SessionStateValidationError(
            "browser session state is not valid JSON"
        ) from exc
    if len(encoded) > max_bytes:
        raise SessionStateTooLarge(
            "browser session state exceeds the configured size limit"
        )
    return encoded


def decode_session_encryption_key(value: SecretStr | str | bytes) -> bytes:
    """Decode an explicit AES-256 key without including it in validation errors."""

    if isinstance(value, bytes):
        decoded = value
    else:
        secret = value.get_secret_value() if isinstance(value, SecretStr) else value
        try:
            encoded = secret.strip().encode("ascii")
            encoded += b"=" * (-len(encoded) % 4)
            decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise ValueError(
                "session encryption key must encode exactly 32 bytes"
            ) from exc
    if len(decoded) != 32:
        raise ValueError("session encryption key must encode exactly 32 bytes")
    return decoded


class _AesGcmBackend:
    algorithm = "aes-256-gcm"
    header = _AES_HEADER

    def __init__(self, key: bytes) -> None:
        self._cipher = AESGCM(key)

    def protect(self, plaintext: bytes, binding: bytes) -> bytes:
        nonce = os.urandom(_AES_NONCE_BYTES)
        return self.header + nonce + self._cipher.encrypt(nonce, plaintext, binding)

    def unprotect(self, ciphertext: bytes, binding: bytes) -> bytes:
        if not ciphertext.startswith(self.header):
            raise SessionStateValidationError(
                "protected browser session state uses an unexpected format"
            )
        payload = ciphertext[len(self.header) :]
        if len(payload) <= _AES_NONCE_BYTES:
            raise SessionStateValidationError(
                "protected browser session state is incomplete"
            )
        nonce = payload[:_AES_NONCE_BYTES]
        return self._cipher.decrypt(nonce, payload[_AES_NONCE_BYTES:], binding)


class _WindowsDpapiBackend:
    algorithm = "windows-dpapi-current-user"
    header = _DPAPI_HEADER

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise SessionProtectionUnavailable(
                "Windows DPAPI is unavailable on this platform"
            )

    def protect(self, plaintext: bytes, binding: bytes) -> bytes:
        return self.header + _windows_dpapi_protect(plaintext, binding)

    def unprotect(self, ciphertext: bytes, binding: bytes) -> bytes:
        if not ciphertext.startswith(self.header):
            raise SessionStateValidationError(
                "protected browser session state uses an unexpected format"
            )
        payload = ciphertext[len(self.header) :]
        if not payload:
            raise SessionStateValidationError(
                "protected browser session state is incomplete"
            )
        return _windows_dpapi_unprotect(payload, binding)


def _session_binding(tenant_id: UUID, task_id: UUID) -> bytes:
    return b"effect-browser/session-state/v1\x00" + tenant_id.bytes + task_id.bytes


def _validate_json_value(value: Any, *, depth: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise SessionStateValidationError("browser session state is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SessionStateValidationError(
                "browser session state contains a non-finite number"
            )
        return
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise SessionStateValidationError(
                "browser session state object keys must be strings"
            )
        for item in value.values():
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    raise SessionStateValidationError("browser session state contains a non-JSON value")


def _windows_dpapi_protect(plaintext: bytes, entropy: bytes) -> bytes:
    return _windows_dpapi_call("protect", plaintext, entropy)


def _windows_dpapi_unprotect(ciphertext: bytes, entropy: bytes) -> bytes:
    return _windows_dpapi_call("unprotect", ciphertext, entropy)


def _windows_dpapi_call(operation: str, value: bytes, entropy: bytes) -> bytes:
    if sys.platform != "win32":
        raise SessionProtectionUnavailable("Windows DPAPI is unavailable")

    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cb_data", wintypes.DWORD),
            ("pb_data", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_pointer = ctypes.POINTER(DataBlob)
    crypt32.CryptProtectData.argtypes = [
        blob_pointer,
        wintypes.LPCWSTR,
        blob_pointer,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        blob_pointer,
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        blob_pointer,
        ctypes.c_void_p,
        blob_pointer,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        blob_pointer,
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    value_buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    entropy_buffer = (ctypes.c_ubyte * len(entropy)).from_buffer_copy(entropy)
    input_blob = DataBlob(len(value), value_buffer)
    entropy_blob = DataBlob(len(entropy), entropy_buffer)
    output_blob = DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if operation == "protect":
        succeeded = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "Effect Browser task session",
            ctypes.byref(entropy_blob),
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
    else:
        succeeded = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
    if not succeeded:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pb_data, output_blob.cb_data)
    finally:
        if output_blob.pb_data:
            ctypes.memset(output_blob.pb_data, 0, output_blob.cb_data)
            kernel32.LocalFree(output_blob.pb_data)
