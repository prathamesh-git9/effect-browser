from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from email import policy
from email.parser import BytesParser
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
    "multipart/form-data",
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
    wire_content_type = _content_type(headers)
    media_type = wire_content_type.partition(";")[0]
    content_type = (
        media_type if media_type == "multipart/form-data" else wire_content_type
    )
    query_fields = _query_fields(url)
    body_fields, document_sha256s = _body_fields(
        raw_body,
        media_type,
        wire_content_type,
    )
    fields = tuple(query_fields + body_fields)
    body_sha256 = (
        digest(
            {
                "fields": [field.model_dump(mode="json") for field in body_fields],
                "document_sha256s": list(document_sha256s),
            }
        )
        if media_type == "multipart/form-data"
        else _raw_sha256(raw_body)
    )
    request_body = {
        "method": normalized_method,
        "target": _display_target(url),
        "url_sha256": _raw_sha256(url.encode("utf-8")),
        "content_type": content_type,
        "body_sha256": body_sha256,
        "fields": [field.model_dump(mode="json") for field in fields],
        "document_sha256s": list(document_sha256s),
    }
    return ReviewedRequest(
        **request_body,
        wire_body_sha256=_raw_sha256(raw_body),
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


def _body_fields(
    body: bytes,
    media_type: str,
    wire_content_type: str,
) -> tuple[list[ReviewedRequestField], tuple[str, ...]]:
    if not body:
        return [], ()
    if media_type not in SUPPORTED_BODY_TYPES:
        raise TransmissionReviewError(
            f"unsupported outgoing content type for exact review: {media_type or 'none'}"
        )
    if media_type == "multipart/form-data":
        return _multipart_fields(body, wire_content_type)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TransmissionReviewError("outgoing request body is not valid UTF-8") from exc
    if media_type == "application/x-www-form-urlencoded":
        return (
            [
                _field(name, value)
                for name, value in parse_qsl(text, keep_blank_values=True)
            ],
            (),
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransmissionReviewError("outgoing JSON body is invalid") from exc
    flattened: list[tuple[str, Any]] = []
    _flatten_json("$", payload, flattened)
    return (
        [_field(name, _display_json_value(value)) for name, value in flattened],
        (),
    )


def _multipart_fields(
    body: bytes,
    wire_content_type: str,
) -> tuple[list[ReviewedRequestField], tuple[str, ...]]:
    if "boundary=" not in wire_content_type.casefold():
        raise TransmissionReviewError("multipart request has no boundary")
    envelope = (
        f"Content-Type: {wire_content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    try:
        message = BytesParser(policy=policy.default).parsebytes(envelope)
    except (TypeError, ValueError) as exc:
        raise TransmissionReviewError("outgoing multipart body is invalid") from exc
    if not message.is_multipart():
        raise TransmissionReviewError("outgoing multipart body is invalid")

    fields: list[ReviewedRequestField] = []
    document_sha256s: list[str] = []
    for part in message.iter_parts():
        if part.is_multipart():
            raise TransmissionReviewError("nested multipart bodies are not supported")
        header_names = {name.casefold() for name in part.keys()}
        if not header_names <= {"content-disposition", "content-type"}:
            raise TransmissionReviewError("multipart part contains unsupported headers")
        if part.get_content_disposition() != "form-data":
            raise TransmissionReviewError("multipart part is not form-data")
        disposition_params = {
            str(name).casefold()
            for name, _value in part.get_params(
                header="content-disposition",
                unquote=True,
            )[1:]
        }
        if not disposition_params <= {"name", "filename"}:
            raise TransmissionReviewError(
                "multipart content disposition has unsupported parameters"
            )
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or not name:
            raise TransmissionReviewError("multipart field has no name")
        content = part.get_payload(decode=True)
        if content is None:
            content = b""
        filename = part.get_filename()
        if filename is not None:
            raw_part_content_type = str(part.get("Content-Type", "")).strip()
            if not raw_part_content_type or ";" in raw_part_content_type:
                raise TransmissionReviewError(
                    "multipart file requires a parameter-free content type"
                )
            document_sha256 = _raw_sha256(content)
            descriptor = canonical_json(
                {
                    "filename": filename,
                    "content_type": part.get_content_type(),
                    "size": len(content),
                    "sha256": document_sha256,
                }
            )
            fields.append(_field(name, descriptor))
            document_sha256s.append(document_sha256)
            continue
        if part.get("Content-Type") is not None:
            raise TransmissionReviewError(
                "multipart text fields cannot set a content type"
            )
        charset = part.get_content_charset() or "utf-8"
        try:
            value = content.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise TransmissionReviewError(
                f"multipart field {name!r} is not valid text"
            ) from exc
        fields.append(_field(name, value))
    return fields, tuple(document_sha256s)


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
