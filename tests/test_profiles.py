from __future__ import annotations

from uuid import UUID

import pytest

from effect_browser.domain import (
    AnswerSensitivity,
    AnswerSource,
    AnswerSourceKind,
    VerificationState,
)
from effect_browser.store import ConflictError, DatabaseStore, NotFoundError

from .conftest import TENANT

STRANGER = UUID("20000000-0000-0000-0000-000000000002")


def test_profile_answer_retains_provenance_sensitivity_and_verification(
    store: DatabaseStore,
) -> None:
    profile = store.create_profile(tenant_id=TENANT, name="Synthetic facts")
    created = store.put_profile_answer(
        tenant_id=TENANT,
        profile_id=profile.id,
        field_name="work_authorization",
        value="synthetic-authorized",
        source=AnswerSource(
            kind=AnswerSourceKind.DOCUMENT,
            reference="synthetic-document-001",
        ),
        sensitivity=AnswerSensitivity.CONSEQUENTIAL,
        verification_state=VerificationState.UNVERIFIED,
        expected_version=None,
        actor_id="test-user",
    )

    assert created.value == "synthetic-authorized"
    assert created.source == AnswerSource(
        kind=AnswerSourceKind.DOCUMENT,
        reference="synthetic-document-001",
    )
    assert created.sensitivity is AnswerSensitivity.CONSEQUENTIAL
    assert created.verification_state is VerificationState.UNVERIFIED
    assert created.verified_by is None
    assert created.verified_at is None

    verified = store.put_profile_answer(
        tenant_id=TENANT,
        profile_id=profile.id,
        field_name="work_authorization",
        value="synthetic-authorized",
        source=created.source,
        sensitivity=created.sensitivity,
        verification_state=VerificationState.VERIFIED,
        expected_version=created.version,
        actor_id="test-user",
    )

    assert verified.version == 2
    assert verified.verification_state is VerificationState.VERIFIED
    assert verified.verified_by == "test-user"
    assert verified.verified_at is not None
    assert store.list_profile_answers(TENANT, profile.id) == [verified]
    assert store.get_profile(TENANT, profile.id).version == 3
    assert store.verify_audit(TENANT).valid is True

    durable_events = " ".join(
        event.model_dump_json() for event in store.profile_events(TENANT, profile.id)
    )
    assert "synthetic-authorized" not in durable_events
    assert "synthetic-document-001" not in durable_events


def test_profile_answer_replacement_requires_current_version(
    store: DatabaseStore,
) -> None:
    profile = store.create_profile(tenant_id=TENANT, name="Versioned facts")
    answer = store.put_profile_answer(
        tenant_id=TENANT,
        profile_id=profile.id,
        field_name="country",
        value="synthetic-country",
        source=AnswerSource(kind=AnswerSourceKind.USER),
        sensitivity=AnswerSensitivity.PERSONAL,
        verification_state=VerificationState.VERIFIED,
        expected_version=None,
        actor_id="test-user",
    )
    event_count = store.verify_audit(TENANT).event_count

    with pytest.raises(ConflictError, match="expected_version is required"):
        store.put_profile_answer(
            tenant_id=TENANT,
            profile_id=profile.id,
            field_name="country",
            value="replacement",
            source=answer.source,
            sensitivity=answer.sensitivity,
            verification_state=answer.verification_state,
            expected_version=None,
            actor_id="test-user",
        )
    with pytest.raises(ConflictError, match="version changed"):
        store.put_profile_answer(
            tenant_id=TENANT,
            profile_id=profile.id,
            field_name="country",
            value="replacement",
            source=answer.source,
            sensitivity=answer.sensitivity,
            verification_state=answer.verification_state,
            expected_version=answer.version + 1,
            actor_id="test-user",
        )

    assert store.list_profile_answers(TENANT, profile.id) == [answer]
    assert store.verify_audit(TENANT).event_count == event_count


def test_profile_is_tenant_isolated(store: DatabaseStore) -> None:
    profile = store.create_profile(tenant_id=TENANT, name="Tenant facts")

    with pytest.raises(NotFoundError, match="profile not found"):
        store.get_profile(STRANGER, profile.id)
    with pytest.raises(NotFoundError, match="profile not found"):
        store.list_profile_answers(STRANGER, profile.id)
    with pytest.raises(NotFoundError, match="profile not found"):
        store.put_profile_answer(
            tenant_id=STRANGER,
            profile_id=profile.id,
            field_name="country",
            value="synthetic-country",
            source=AnswerSource(kind=AnswerSourceKind.USER),
            sensitivity=AnswerSensitivity.PERSONAL,
            verification_state=VerificationState.UNVERIFIED,
            expected_version=None,
            actor_id="stranger",
        )
