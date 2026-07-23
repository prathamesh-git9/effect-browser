from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from effect_browser.domain import (
    ReviewedRequest,
    ReviewedRequestField,
    canonical_json,
    digest,
)

MAX_REVIEWED_BODY_BYTES = 12 * 1024 * 1024
SUPPORTED_BODY_TYPES = {
    "application/json",
    "application/x-www-form-urlencoded",
}
SENSITIVE_FIELD_NAMES = {
    "api_key",
    "apikey",
    "access_token",
    "authenticity_token",
    "csrf",
    "csrf_token",
    "cookie",
    "password",
    "passwd",
    "refresh_token",
    "secret",
    "session",
    "session_id",
    "xsrf",
    "xsrf_token",
}


class TransmissionReviewError(ValueError):
    """A write request cannot be safely represented or compared."""


class TransmissionBlocked(TransmissionReviewError):
    """The approved request did not match and routing blocked it before sending."""


def fingerprint_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
) -> ReviewedRequest:
    raw_body = body or b""
    if len(raw_body) > MAX_REVIEWED_BODY_BYTES:
        raise TransmissionReviewError("outgoing request exceeds the 12 MiB review limit")
    normalized_method = method.upper()
    content_type = _content_type(headers)
    media_type = content_type.partition(";")[0]
    query_fields = _query_fields(url)
    body_fields = _body_fields(raw_body, media_type)
    fields = tuple(query_fields + body_fields)
    request_body = {
        "method": normalized_method,
        "target": _display_target(url),
        "url_sha256": _raw_sha256(url.encode("utf-8")),
        "content_type": content_type,
        "body_sha256": _raw_sha256(raw_body),
        "fields": [field.model_dump(mode="json") for field in fields],
    }
    return ReviewedRequest(
        **request_body,
        request_sha256=digest(request_body),
    )


def _content_type(headers: Mapping[str, str]) -> str:
    raw = next(
        (value for name, value in headers.items() if name.casefold() == "content-type"),
        "",
    )
    parts = [part.strip() for part in raw.split(";") if part.strip()]
    if not parts:
        return ""
    return ";".join([parts[0].casefold(), *sorted(parts[1:], key=str.casefold)])


def _query_fields(url: str) -> list[ReviewedRequestField]:
    query = urlsplit(url).query
    return [
        _field(f"query.{name}", value)
        for name, value in parse_qsl(query, keep_blank_values=True)
    ]


def _body_fields(body: bytes, media_type: str) -> list[ReviewedRequestField]:
    if not body:
        return []
    if media_type not in SUPPORTED_BODY_TYPES:
        raise TransmissionReviewError(
            f"unsupported outgoing content type for exact review: {media_type or 'none'}"
        )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TransmissionReviewError("outgoing request body is not valid UTF-8") from exc
    if media_type == "application/x-www-form-urlencoded":
        return [
            _field(name, value) for name, value in parse_qsl(text, keep_blank_values=True)
        ]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransmissionReviewError("outgoing JSON body is invalid") from exc
    flattened: list[tuple[str, Any]] = []
    _flatten_json("$", payload, flattened)
    return [_field(name, _display_json_value(value)) for name, value in flattened]


def _flatten_json(
    name: str,
    value: Any,
    target: list[tuple[str, Any]],
) -> None:
    if isinstance(value, dict):
        if not value:
            target.append((name, value))
        for child_name in sorted(value):
            prefix = child_name if name == "$" else f"{name}.{child_name}"
            _flatten_json(prefix, value[child_name], target)
        return
    if isinstance(value, list):
        if not value:
            target.append((name, value))
        for index, child in enumerate(value):
            _flatten_json(f"{name}[{index}]", child, target)
        return
    target.append((name, value))


def _display_json_value(value: Any) -> str:
    return value if isinstance(value, str) else canonical_json(value)


def _field(name: str, value: str) -> ReviewedRequestField:
    normalized = (
        name.casefold()
        .replace("-", "_")
        .replace(".", "_")
        .replace("[", "_")
        .replace("]", "")
    )
    sensitive = any(
        normalized == token or normalized.endswith(f"_{token}")
        for token in SENSITIVE_FIELD_NAMES
    )
    redacted = sensitive or len(value) > 10_000
    return ReviewedRequestField(
        name=name,
        value=None if redacted else value,
        value_sha256=digest({"value": value}),
        redacted=redacted,
    )


def _display_target(url: str) -> str:
    parsed = urlsplit(url)
    netloc = parsed.netloc.rsplit("@", maxsplit=1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _raw_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
