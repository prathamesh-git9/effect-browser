from pathlib import Path

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
