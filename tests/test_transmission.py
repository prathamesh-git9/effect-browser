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


def test_multipart_fingerprint_ignores_boundary_but_binds_file_bytes() -> None:
    def body(boundary: str, document: bytes) -> bytes:
        return (
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="full_name"\r\n\r\n'
                "Synthetic Candidate\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="resume"; '
                'filename="synthetic-resume.txt"\r\n'
                "Content-Type: text/plain\r\n\r\n"
            ).encode()
            + document
            + f"\r\n--{boundary}--\r\n".encode()
        )

    first = fingerprint_request(
        method="POST",
        url="https://jobs.example.test/apply",
        headers={"content-type": "multipart/form-data; boundary=first-boundary"},
        body=body("first-boundary", b"synthetic resume bytes"),
    )
    regenerated = fingerprint_request(
        method="POST",
        url="https://jobs.example.test/apply",
        headers={"content-type": "multipart/form-data; boundary=second-boundary"},
        body=body("second-boundary", b"synthetic resume bytes"),
    )
    changed_document = fingerprint_request(
        method="POST",
        url="https://jobs.example.test/apply",
        headers={"content-type": "multipart/form-data; boundary=third-boundary"},
        body=body("third-boundary", b"changed synthetic resume bytes"),
    )

    assert first.content_type == "multipart/form-data"
    assert first.request_sha256 == regenerated.request_sha256
    assert first.body_sha256 == regenerated.body_sha256
    assert first.wire_body_sha256 != regenerated.wire_body_sha256
    assert first.request_sha256 != changed_document.request_sha256
    assert len(first.document_sha256s) == 1
    file_field = next(field for field in first.fields if field.name == "resume")
    assert "synthetic-resume.txt" in (file_field.value or "")
    assert first.document_sha256s[0] in (file_field.value or "")


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
