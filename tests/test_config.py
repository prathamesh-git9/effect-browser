import base64
from pathlib import Path

import pytest
from pydantic import ValidationError

from effect_browser.config import Settings


def test_comma_separated_origins_and_upload_roots_are_parsed_independently(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    settings = Settings(
        _env_file=None,
        allowed_origins="https://one.example/, https://two.example/",
        allowed_upload_roots=f"{first},{second}",
    )

    assert settings.allowed_origins == (
        "https://one.example",
        "https://two.example",
    )
    assert settings.allowed_upload_roots == (first, second)


def test_session_security_settings_are_bounded_and_key_is_secret() -> None:
    encoded_key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    settings = Settings(
        _env_file=None,
        session_encryption_key=encoded_key,
        session_state_max_bytes=128 * 1024,
        session_retention_hours=24,
    )

    assert settings.session_encryption_key is not None
    assert settings.session_encryption_key.get_secret_value() == encoded_key
    assert encoded_key not in repr(settings)
    assert settings.session_state_max_bytes == 128 * 1024
    assert settings.session_retention_hours == 24


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("session_encryption_key", "not-a-key"),
        ("session_state_max_bytes", 1024),
        ("session_retention_hours", 0),
        ("session_retention_hours", 721),
    ),
)
def test_invalid_session_security_settings_are_rejected(field: str, value) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
