from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from effect_browser.domain import ActionKind, ProposedAction, utc_now
from effect_browser.store import ConflictError, DatabaseStore

TENANT = UUID("30000000-0000-0000-0000-000000000003")


def _create_task(store: DatabaseStore) -> UUID:
    task_id = uuid4()
    store.create_task(
        task_id=task_id,
        tenant_id=TENANT,
        instruction="Persist a private browser checkpoint.",
        start_url="https://checkpoint.example/start",
        provider="test",
        actions=(
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url="https://checkpoint.example/start",
                description="Open the checkpoint test page.",
            ),
        ),
    )
    return task_id


def _release_with_checkpoint(
    store: DatabaseStore,
    task_id: UUID,
    owner: str,
    ciphertext: bytes,
    *,
    expires_at: datetime | None = None,
) -> None:
    store.release_task(
        tenant_id=TENANT,
        task_id=task_id,
        owner=owner,
        checkpoint_ciphertext=ciphertext,
        checkpoint_format_version=1,
        checkpoint_ordinal=0,
        checkpoint_expires_at=expires_at or utc_now() + timedelta(days=7),
    )


def test_release_saves_and_updates_an_opaque_task_session(store: DatabaseStore) -> None:
    task_id = _create_task(store)
    first_expiry = utc_now() + timedelta(days=2)
    store.claim_task(tenant_id=TENANT, task_id=task_id, owner="worker-one")
    _release_with_checkpoint(
        store,
        task_id,
        "worker-one",
        b"first-encrypted-envelope",
        expires_at=first_expiry,
    )

    first = store.load_task_session(TENANT, task_id)
    assert first is not None
    assert first.ciphertext == b"first-encrypted-envelope"
    assert first.format_version == 1
    assert first.checkpoint_ordinal == 0
    assert first.expires_at == first_expiry
    assert store.get_task(TENANT, task_id).lease_owner is None

    second_expiry = utc_now() + timedelta(days=3)
    store.claim_task(tenant_id=TENANT, task_id=task_id, owner="worker-two")
    _release_with_checkpoint(
        store,
        task_id,
        "worker-two",
        b"replacement-encrypted-envelope",
        expires_at=second_expiry,
    )

    replacement = store.load_task_session(TENANT, task_id)
    assert replacement is not None
    assert replacement.ciphertext == b"replacement-encrypted-envelope"
    assert replacement.expires_at == second_expiry


def test_loading_expired_session_deletes_it(store: DatabaseStore) -> None:
    task_id = _create_task(store)
    store.claim_task(tenant_id=TENANT, task_id=task_id, owner="worker")
    _release_with_checkpoint(
        store,
        task_id,
        "worker",
        b"expired-encrypted-envelope",
    )
    with store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE task_sessions "
                "SET expires_at='2000-01-01 00:00:00.000000' "
                "WHERE task_id=:task_id"
            ),
            {"task_id": str(task_id)},
        )

    assert store.load_task_session(TENANT, task_id) is None
    with store.engine.connect() as connection:
        remaining = connection.execute(
            text("SELECT count(*) FROM task_sessions WHERE task_id=:task_id"),
            {"task_id": str(task_id)},
        ).scalar_one()
    assert remaining == 0


def test_release_rejects_an_already_expired_checkpoint(
    store: DatabaseStore,
) -> None:
    task_id = _create_task(store)
    store.claim_task(tenant_id=TENANT, task_id=task_id, owner="worker")

    with pytest.raises(ValueError, match="expiry must be in the future"):
        _release_with_checkpoint(
            store,
            task_id,
            "worker",
            b"expired-encrypted-envelope",
            expires_at=utc_now() - timedelta(seconds=1),
        )

    assert store.load_task_session(TENANT, task_id) is None
    assert store.get_task(TENANT, task_id).lease_owner == "worker"
    store.release_task(tenant_id=TENANT, task_id=task_id, owner="worker")


def test_terminal_release_deletes_existing_session(store: DatabaseStore) -> None:
    task_id = _create_task(store)
    store.claim_task(tenant_id=TENANT, task_id=task_id, owner="first-worker")
    _release_with_checkpoint(
        store,
        task_id,
        "first-worker",
        b"encrypted-envelope",
    )

    store.claim_task(tenant_id=TENANT, task_id=task_id, owner="terminal-worker")
    store.block_task(
        TENANT,
        task_id,
        kind="human_verification",
        reason="The site requires a human decision.",
        evidence="The browser displayed a challenge.",
    )
    store.release_task(
        tenant_id=TENANT,
        task_id=task_id,
        owner="terminal-worker",
    )

    assert store.load_task_session(TENANT, task_id) is None
    with store.engine.connect() as connection:
        remaining = connection.execute(
            text("SELECT count(*) FROM task_sessions WHERE task_id=:task_id"),
            {"task_id": str(task_id)},
        ).scalar_one()
    assert remaining == 0


def test_stale_worker_cannot_overwrite_newer_session(store: DatabaseStore) -> None:
    task_id = _create_task(store)
    store.claim_task(
        tenant_id=TENANT,
        task_id=task_id,
        owner="stale-worker",
        lease_seconds=120,
    )
    with store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE tasks "
                "SET lease_expires_at='2000-01-01 00:00:00.000000' "
                "WHERE id=:task_id AND tenant_id=:tenant_id"
            ),
            {
                "task_id": str(task_id),
                "tenant_id": str(TENANT),
            },
        )

    store.claim_task(tenant_id=TENANT, task_id=task_id, owner="current-worker")
    _release_with_checkpoint(
        store,
        task_id,
        "current-worker",
        b"current-encrypted-envelope",
    )

    with pytest.raises(ConflictError, match="lease was lost or expired"):
        _release_with_checkpoint(
            store,
            task_id,
            "stale-worker",
            b"stale-encrypted-envelope",
        )

    loaded = store.load_task_session(TENANT, task_id)
    assert loaded is not None
    assert loaded.ciphertext == b"current-encrypted-envelope"


def test_checkpoint_ordinal_must_match_locked_task_cursor(
    store: DatabaseStore,
) -> None:
    task_id = _create_task(store)
    store.claim_task(tenant_id=TENANT, task_id=task_id, owner="worker")

    with pytest.raises(ConflictError, match="does not match the task cursor"):
        store.release_task(
            tenant_id=TENANT,
            task_id=task_id,
            owner="worker",
            checkpoint_ciphertext=b"encrypted-envelope",
            checkpoint_format_version=1,
            checkpoint_ordinal=1,
            checkpoint_expires_at=utc_now() + timedelta(days=1),
        )

    assert store.load_task_session(TENANT, task_id) is None
    assert store.get_task(TENANT, task_id).lease_owner == "worker"
    store.release_task(tenant_id=TENANT, task_id=task_id, owner="worker")
