from __future__ import annotations

from pathlib import Path

import pytest

from effect_browser.uploads import (
    MAX_UPLOAD_BYTES,
    UploadGuard,
    UploadValidationError,
    sha256_file,
)


def test_upload_guard_requires_explicit_roots(tmp_path: Path) -> None:
    document = tmp_path / "synthetic.txt"
    document.write_bytes(b"synthetic document")

    with pytest.raises(UploadValidationError, match="disabled"):
        UploadGuard().validate(document.resolve(), sha256_file(document))


def test_upload_guard_resolves_nested_file_and_verifies_raw_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "approved"
    nested = root / "documents"
    nested.mkdir(parents=True)
    document = nested / "synthetic.txt"
    document.write_bytes(b"\x00synthetic\r\ncontent\xff")

    upload = UploadGuard((root,)).validate(
        document.resolve(),
        sha256_file(document),
    )

    assert upload.path == document.resolve()
    assert (
        upload.sha256
        == "4d0261395d338f6a7f38d3fa46a9a57f43f63b058cb2780c87016cb1f55c8b54"
    )
    assert upload.content == b"\x00synthetic\r\ncontent\xff"


def test_upload_guard_rejects_relative_and_non_file_paths(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    guard = UploadGuard((root,))

    with pytest.raises(UploadValidationError, match="absolute"):
        guard.validate(Path("synthetic.txt"), "0" * 64)
    with pytest.raises(UploadValidationError, match="regular file"):
        guard.validate(root.resolve(), "0" * 64)


def test_upload_guard_rejects_oversized_file(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    document = root / "oversized.bin"
    document.write_bytes(b"x" * (MAX_UPLOAD_BYTES + 1))

    with pytest.raises(UploadValidationError, match="10 MiB"):
        UploadGuard((root,)).validate(document.resolve(), sha256_file(document))
