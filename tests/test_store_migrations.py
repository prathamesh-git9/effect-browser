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
            CREATE TABLE tasks (
                id VARCHAR(36) PRIMARY KEY,
                tenant_id VARCHAR(36) NOT NULL,
                instruction TEXT NOT NULL,
                start_url TEXT NOT NULL,
                provider VARCHAR(80) NOT NULL,
                status VARCHAR(40) NOT NULL,
                current_ordinal INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                version INTEGER NOT NULL,
                lease_owner VARCHAR(100),
                lease_expires_at DATETIME
            )
            """
        )
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
        task_columns = {
            column["name"] for column in inspect(store.engine).get_columns("tasks")
        }
        task_session_columns = {
            column["name"]
            for column in inspect(store.engine).get_columns("task_sessions")
        }
        task_session_primary_key = inspect(store.engine).get_pk_constraint(
            "task_sessions"
        )
        task_session_foreign_keys = inspect(store.engine).get_foreign_keys(
            "task_sessions"
        )
        with store.engine.connect() as connection:
            payload_sha256 = connection.execute(
                text("SELECT payload_sha256 FROM approvals WHERE id='approval-1'")
            ).scalar_one()
    finally:
        store.close()

    assert "payload_sha256" in columns
    assert {"resume_filename", "resume_sha256"} <= job_columns
    assert {
        "profile_id",
        "document_path",
        "document_sha256",
        "autonomy_scope",
    } <= task_columns
    assert {
        "task_id",
        "tenant_id",
        "ciphertext",
        "format_version",
        "checkpoint_ordinal",
        "updated_at",
        "expires_at",
    } == task_session_columns
    assert task_session_primary_key["constrained_columns"] == ["task_id"]
    assert any(
        key["constrained_columns"] == ["task_id"]
        and key["referred_table"] == "tasks"
        and key["referred_columns"] == ["id"]
        for key in task_session_foreign_keys
    )
    assert proposal.outgoing_review is not None
    assert payload_sha256 == proposal.outgoing_review.payload_sha256
