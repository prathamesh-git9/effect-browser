from __future__ import annotations

import json

import pytest

from effect_browser.transmission import (
    MAX_REVIEWED_BODY_BYTES,
    TransmissionReviewError,
    fingerprint_request,
)


def test_json_request_fingerprint_exposes_transformed_fields_and_redacts_tokens() -> None:
    body = json.dumps(
        {
            "full_name": "Synthetic Candidate",
            "years_python": 6,
            "job_slug": "platform-reliability-engineer",
            "csrf_token": "private-token",
        },
        separators=(",", ":"),
    ).encode()

    reviewed = fingerprint_request(
        method="post",
        url="https://jobs.example.test/api/applications?mode=real",
        headers={"Content-Type": "application/json"},
        body=body,
    )

    values = {field.name: field.value for field in reviewed.fields}
    csrf = next(field for field in reviewed.fields if field.name == "csrf_token")
    assert reviewed.method == "POST"
    assert reviewed.target == "https://jobs.example.test/api/applications"
    assert values["query.mode"] == "real"
    assert values["years_python"] == "6"
    assert values["job_slug"] == "platform-reliability-engineer"
    assert csrf.redacted is True
    assert csrf.value is None
    assert "private-token" not in reviewed.model_dump_json()


def test_urlencoded_request_keeps_duplicate_fields_in_order() -> None:
    reviewed = fingerprint_request(
        method="POST",
        url="https://jobs.example.test/apply",
        headers={"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
        body=b"skill=Python&skill=PostgreSQL&empty=",
    )

    assert [(field.name, field.value) for field in reviewed.fields] == [
        ("skill", "Python"),
        ("skill", "PostgreSQL"),
        ("empty", ""),
    ]


def test_unsupported_or_oversized_request_fails_closed() -> None:
    with pytest.raises(TransmissionReviewError, match="unsupported"):
        fingerprint_request(
            method="POST",
            url="https://jobs.example.test/apply",
            headers={"content-type": "application/octet-stream"},
            body=b"opaque",
        )
    with pytest.raises(TransmissionReviewError, match="12 MiB"):
        fingerprint_request(
            method="POST",
            url="https://jobs.example.test/apply",
            headers={"content-type": "application/json"},
            body=b"x" * (MAX_REVIEWED_BODY_BYTES + 1),
        )
