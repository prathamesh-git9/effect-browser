from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class UploadValidationError(ValueError):
    """An upload path or its content failed the local-file boundary."""


@dataclass(frozen=True)
class ValidatedUpload:
    path: Path
    sha256: str
    content: bytes = field(repr=False)


class UploadGuard:
    """Resolve local uploads beneath explicit roots and verify their raw bytes."""

    def __init__(self, allowed_roots: tuple[Path, ...] = ()) -> None:
        roots: list[Path] = []
        for configured in allowed_roots:
            try:
                root = configured.expanduser().resolve(strict=True)
            except OSError as exc:
                raise ValueError("configured upload root must exist") from exc
            if not root.is_dir():
                raise ValueError("configured upload root must be a directory")
            roots.append(root)
        self.allowed_roots = tuple(roots)

    def validate(self, path: Path, expected_sha256: str) -> ValidatedUpload:
        if not self.allowed_roots:
            raise UploadValidationError(
                "file uploads are disabled because no allowed roots are configured"
            )
        if not path.is_absolute():
            raise UploadValidationError("upload path must be absolute")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise UploadValidationError(
                "upload path must identify an existing regular file"
            ) from exc
        if not any(resolved.is_relative_to(root) for root in self.allowed_roots):
            raise UploadValidationError("upload path is outside the allowed roots")
        if not resolved.is_file():
            raise UploadValidationError(
                "upload path must identify an existing regular file"
            )
        content = _read_bounded(resolved)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != expected_sha256:
            raise UploadValidationError(
                "upload content no longer matches the action-bound SHA-256"
            )
        return ValidatedUpload(
            path=resolved,
            sha256=actual_sha256,
            content=content,
        )


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError as exc:
        raise UploadValidationError("upload file could not be read safely") from exc
    return hasher.hexdigest()


def _read_bounded(path: Path) -> bytes:
    try:
        with path.open("rb") as source:
            content = source.read(MAX_UPLOAD_BYTES + 1)
    except OSError as exc:
        raise UploadValidationError("upload file could not be read safely") from exc
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadValidationError("upload file exceeds the 10 MiB safety limit")
    return content
