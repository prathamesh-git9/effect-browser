from __future__ import annotations

import base64
import sys
from uuid import UUID

import pytest

from effect_browser.session import (
    SessionProtectionUnavailable,
    SessionStateProtector,
    SessionStateTooLarge,
    SessionStateValidationError,
    canonical_session_state_bytes,
    decode_session_encryption_key,
)

TENANT = UUID("10000000-0000-0000-0000-000000000001")
TASK = UUID("20000000-0000-0000-0000-000000000002")
OTHER_TASK = UUID("30000000-0000-0000-0000-000000000003")
KEY = bytes(range(32))


def _state(token: str = "session-token") -> dict[str, object]:
    return {
        "origins": [
            {
                "origin": "https://example.test",
                "localStorage": [{"value": "authenticated", "name": "status"}],
            }
        ],
        "cookies": [
            {
                "value": token,
                "name": "effect_auth",
                "domain": "example.test",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
    }


def test_aes_gcm_round_trip_is_bound_and_contains_no_plaintext_secret() -> None:
    protector = SessionStateProtector(encryption_key=KEY)

    ciphertext = protector.protect(_state(), TENANT, TASK)

    assert b"session-token" not in ciphertext
    assert protector.algorithm == "aes-256-gcm"
    assert protector.unprotect(ciphertext, TENANT, TASK) == _state()
    with pytest.raises(SessionStateValidationError, match="invalid or unavailable"):
        protector.unprotect(ciphertext, TENANT, OTHER_TASK)


def test_aes_gcm_rejects_tampering_without_disclosing_state() -> None:
    protector = SessionStateProtector(encryption_key=KEY)
    ciphertext = bytearray(protector.protect(_state("do-not-report-me"), TENANT, TASK))
    ciphertext[-1] ^= 1

    with pytest.raises(SessionStateValidationError) as captured:
        protector.unprotect(bytes(ciphertext), TENANT, TASK)

    assert "do-not-report-me" not in str(captured.value)


def test_canonical_encoding_is_order_independent_and_ascii() -> None:
    first = _state()
    second = {"cookies": first["cookies"], "origins": first["origins"]}

    assert canonical_session_state_bytes(first) == canonical_session_state_bytes(second)
    assert all(byte < 128 for byte in canonical_session_state_bytes(_state("café")))


def test_storage_state_schema_and_size_are_bounded() -> None:
    with pytest.raises(SessionStateValidationError, match="cookie and origin lists"):
        canonical_session_state_bytes({"cookies": []})

    protector = SessionStateProtector(encryption_key=KEY, max_bytes=64 * 1024)
    with pytest.raises(SessionStateTooLarge, match="size limit"):
        protector.protect(_state("x" * (64 * 1024)), TENANT, TASK)
    with pytest.raises(SessionStateTooLarge, match="size limit"):
        protector.unprotect(b"x" * (128 * 1024 + 1), TENANT, TASK)


def test_explicit_key_accepts_padded_or_unpadded_urlsafe_base64() -> None:
    encoded = base64.urlsafe_b64encode(KEY).decode("ascii")

    assert decode_session_encryption_key(encoded) == KEY
    assert decode_session_encryption_key(encoded.rstrip("=")) == KEY
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        decode_session_encryption_key("not-a-key")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI only")
def test_windows_default_uses_current_user_dpapi() -> None:
    protector = SessionStateProtector()

    ciphertext = protector.protect(_state(), TENANT, TASK)

    assert protector.algorithm == "windows-dpapi-current-user"
    assert b"session-token" not in ciphertext
    assert protector.unprotect(ciphertext, TENANT, TASK) == _state()


def test_non_windows_without_explicit_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(SessionProtectionUnavailable, match="explicit encryption key"):
        SessionStateProtector()
