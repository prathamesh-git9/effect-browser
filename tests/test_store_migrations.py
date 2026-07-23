from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import inspect, text

from effect_browser.domain import (
    ActionKind,
    Locator,
    OutgoingReview,
    ProposedAction,
    digest,
)
from effect_browser.store import DatabaseStore


def test_initialize_migrates_and_backfills_legacy_payload_approval(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    observation_sha256 = "a" * 64
    review_body = {
        "fields": [],
        "document_sha256s": [],
        "observation_sha256": observation_sha256,
    }
    proposal = ProposedAction(
        kind=ActionKind.SUBMIT,
        locator=Locator(role="button", name="Submit"),
        description="Submit a reviewed legacy action.",
        effect_key="LEGACY-REVIEW",
        expected_outcome="One legacy effect.",
        planned_from_sha256=observation_sha256,
        outgoing_review=OutgoingReview(
            observation_sha256=observation_sha256,
            payload_sha256=digest(review_body),
        ),
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE actions (
                id VARCHAR(36) PRIMARY KEY,
                action_sha256 VARCHAR(64) NOT NULL,
                proposal JSON NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE approvals (
                id VARCHAR(36) PRIMARY KEY,
                tenant_id VARCHAR(36) NOT NULL,
                action_id VARCHAR(36) NOT NULL,
                decision VARCHAR(20) NOT NULL,
                actor_id VARCHAR(200) NOT NULL,
                action_sha256 VARCHAR(64) NOT NULL,
                observation_sha256 VARCHAR(64) NOT NULL,
                decided_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE demo_job_applications (
                id VARCHAR(36) PRIMARY KEY,
                reference VARCHAR(100) NOT NULL,
                job_slug VARCHAR(120) NOT NULL,
                full_name VARCHAR(200) NOT NULL,
                email VARCHAR(320) NOT NULL,
                country VARCHAR(100) NOT NULL,
                work_authorization VARCHAR(100) NOT NULL,
                years_python INTEGER NOT NULL,
                resume_summary TEXT NOT NULL,
                cover_note TEXT NOT NULL,
                duplicate_attempts INTEGER NOT NULL,
                created_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO actions (id, action_sha256, proposal) VALUES (?, ?, ?)",
            ("action-1", proposal.action_hash(), proposal.model_dump_json()),
        )
        connection.execute(
            """
            INSERT INTO approvals (
                id, tenant_id, action_id, decision, actor_id, action_sha256,
                observation_sha256, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "approval-1",
                "tenant-1",
                "action-1",
                "approved",
                "legacy-operator",
                proposal.action_hash(),
                observation_sha256,
                "2026-07-23 20:00:00",
            ),
        )

    store = DatabaseStore(f"sqlite:///{database_path}")
    try:
        store.initialize()
        columns = {
            column["name"] for column in inspect(store.engine).get_columns("approvals")
        }
        job_columns = {
            column["name"]
            for column in inspect(store.engine).get_columns("demo_job_applications")
        }
        with store.engine.connect() as connection:
            payload_sha256 = connection.execute(
                text("SELECT payload_sha256 FROM approvals WHERE id='approval-1'")
            ).scalar_one()
    finally:
        store.close()

    assert "payload_sha256" in columns
    assert {"resume_filename", "resume_sha256"} <= job_columns
    assert proposal.outgoing_review is not None
    assert payload_sha256 == proposal.outgoing_review.payload_sha256
