from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from effect_browser.domain import (
    ActionKind,
    ActionState,
    AnswerSensitivity,
    AnswerSource,
    AnswerSourceKind,
    Approval,
    ApprovalDecision,
    AuditEvent,
    AuditVerification,
    AutonomyScope,
    BrowserAction,
    BrowserReceipt,
    FactualProfile,
    Mission,
    MissionPlan,
    MissionStatus,
    MissionStep,
    MissionStepKind,
    MissionStepStatus,
    Observation,
    OutgoingReview,
    PolicyDecision,
    ProfileAnswer,
    ProposedAction,
    RiskClass,
    Task,
    TaskStatus,
    VerificationState,
    canonical_json,
    digest,
    utc_now,
)
from effect_browser.observability import (
    CommittedAuditTransition,
    OperationalMetrics,
    operational_metrics,
)

STORE_LOG = logging.getLogger("effect_browser.store")
_PENDING_METRICS_KEY = "effect_browser_committed_audit_transitions"
_AUDIT_HASH = re.compile(r"^[0-9a-f]{64}$")
_AUDIT_STEP_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_AUDIT_EVENT_KIND = re.compile(r"^(?:mission|task|action|approval)\.[a-z_]+$")
_AUDIT_BOOLEAN_FIELDS = frozenset(
    {
        "automatic_progress",
        "automatic_retry",
        "commit_intent_detected",
        "effect_key_released",
        "external_commit_authorized",
        "external_commit_granted",
        "requires_new_approval",
    }
)
_AUDIT_INTEGER_FIELDS = frozenset(
    {
        "action_count",
        "authority_version",
        "max_external_commits",
        "ordinal",
        "outgoing_request_count",
        "recovered_running_steps",
        "requeued_skipped_steps",
        "step_count",
    }
)


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    instruction: Mapped[str] = mapped_column(Text)
    start_url: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(80))
    profile_id: Mapped[str | None] = mapped_column(String(36), index=True)
    document_path: Mapped[str | None] = mapped_column(Text)
    document_sha256: Mapped[str | None] = mapped_column(String(64))
    autonomy_scope: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    current_ordinal: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String(100), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MissionRow(Base):
    __tablename__ = "missions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    query: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(80))
    plan_summary: Mapped[str] = mapped_column(Text)
    external_commit_authorized: Mapped[int] = mapped_column(Integer, default=0)
    max_external_commits: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String(100), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MissionStepRow(Base):
    __tablename__ = "mission_steps"
    __table_args__ = (
        UniqueConstraint("mission_id", "ordinal"),
        UniqueConstraint("mission_id", "key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("missions.id"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(30))
    instruction: Mapped[str] = mapped_column(Text)
    depends_on: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(40), index=True)
    child_task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)


class ActionRow(Base):
    __tablename__ = "actions"
    __table_args__ = (
        UniqueConstraint("task_id", "ordinal"),
        UniqueConstraint("tenant_id", "effect_key", name="uq_tenant_effect_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSON)
    effect_key: Mapped[str | None] = mapped_column(String(300), index=True)
    state: Mapped[str] = mapped_column(String(40), index=True)
    risk: Mapped[str | None] = mapped_column(String(40))
    action_sha256: Mapped[str] = mapped_column(String(64))
    observation_sha256: Mapped[str | None] = mapped_column(String(64))
    observation_url: Mapped[str | None] = mapped_column(Text)
    failure: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    action_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("actions.id"), index=True
    )
    decision: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str] = mapped_column(String(200))
    action_sha256: Mapped[str] = mapped_column(String(64))
    observation_sha256: Mapped[str] = mapped_column(String(64))
    payload_sha256: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReceiptRow(Base):
    __tablename__ = "receipts"
    __table_args__ = (UniqueConstraint("action_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    action_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("actions.id"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text)
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    __table_args__ = (UniqueConstraint("tenant_id", "sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    action_id: Mapped[str | None] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    previous_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))


class TenantLedgerRow(Base):
    __tablename__ = "tenant_ledgers"

    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer)
    head_hash: Mapped[str] = mapped_column(String(64))


class FactualProfileRow(Base):
    __tablename__ = "factual_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_tenant_profile_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)


class ProfileAnswerRow(Base):
    __tablename__ = "profile_answers"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "profile_id",
            "field_name",
            name="uq_tenant_profile_answer",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("factual_profiles.id"), index=True
    )
    field_name: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(40))
    source_reference: Mapped[str | None] = mapped_column(String(500))
    sensitivity: Mapped[str] = mapped_column(String(40))
    verification_state: Mapped[str] = mapped_column(String(40))
    verified_by: Mapped[str | None] = mapped_column(String(200))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)


class DemoOrderRow(Base):
    __tablename__ = "demo_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reference: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    product: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column(Integer)
    duplicate_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DemoJobApplicationRow(Base):
    __tablename__ = "demo_job_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reference: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    job_slug: Mapped[str] = mapped_column(String(120), index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    country: Mapped[str] = mapped_column(String(100))
    work_authorization: Mapped[str] = mapped_column(String(100))
    years_python: Mapped[int] = mapped_column(Integer)
    resume_summary: Mapped[str] = mapped_column(Text)
    resume_filename: Mapped[str | None] = mapped_column(String(255))
    resume_sha256: Mapped[str | None] = mapped_column(String(64))
    cover_note: Mapped[str] = mapped_column(Text)
    duplicate_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoreError(RuntimeError):
    pass


class NotFoundError(StoreError):
    pass


class ConflictError(StoreError):
    pass


class DatabaseStore:
    def __init__(
        self,
        database_url: str,
        *,
        metrics: OperationalMetrics | None = operational_metrics,
    ) -> None:
        connect_args = (
            {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        )
        self.engine = create_engine(database_url, connect_args=connect_args)
        self._session = sessionmaker(self.engine, expire_on_commit=False)
        self.metrics = metrics

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        self._apply_additive_migrations()

    def _apply_additive_migrations(self) -> None:
        """Upgrade schemas created by earlier releases without deleting data."""
        inspector = inspect(self.engine)
        tables = set(inspector.get_table_names())
        if "approvals" in tables:
            columns = {column["name"] for column in inspector.get_columns("approvals")}
            if "payload_sha256" not in columns:
                if self.engine.dialect.name == "postgresql":
                    statement = (
                        "ALTER TABLE approvals ADD COLUMN IF NOT EXISTS "
                        "payload_sha256 VARCHAR(64)"
                    )
                else:
                    # SQLite is supported for one operator process only.
                    statement = (
                        "ALTER TABLE approvals ADD COLUMN payload_sha256 VARCHAR(64)"
                    )
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(statement)
            self._backfill_payload_approval_hashes()
        if "tasks" in tables:
            columns = {
                column["name"] for column in inspect(self.engine).get_columns("tasks")
            }
            additions = {
                "profile_id": "VARCHAR(36)",
                "document_path": "TEXT",
                "document_sha256": "VARCHAR(64)",
                "autonomy_scope": "JSON",
            }
            for name, sql_type in additions.items():
                if name in columns:
                    continue
                qualifier = (
                    " IF NOT EXISTS" if self.engine.dialect.name == "postgresql" else ""
                )
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(
                        f"ALTER TABLE tasks ADD COLUMN{qualifier} {name} {sql_type}"
                    )
        if "demo_job_applications" in tables:
            columns = {
                column["name"]
                for column in inspect(self.engine).get_columns("demo_job_applications")
            }
            additions = {
                "resume_filename": "VARCHAR(255)",
                "resume_sha256": "VARCHAR(64)",
            }
            for name, sql_type in additions.items():
                if name in columns:
                    continue
                qualifier = (
                    " IF NOT EXISTS" if self.engine.dialect.name == "postgresql" else ""
                )
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(
                        "ALTER TABLE demo_job_applications "
                        f"ADD COLUMN{qualifier} {name} {sql_type}"
                    )

    def _backfill_payload_approval_hashes(self) -> None:
        """Recover explicit hashes already covered by valid legacy action hashes."""
        with self.engine.begin() as connection:
            legacy_rows = connection.execute(
                select(
                    ApprovalRow.id,
                    ApprovalRow.action_sha256,
                    ActionRow.action_sha256,
                    ActionRow.proposal,
                )
                .join(ActionRow, ApprovalRow.action_id == ActionRow.id)
                .where(ApprovalRow.payload_sha256.is_(None))
            ).all()
            for approval_id, approved_hash, action_hash, raw_proposal in legacy_rows:
                if approved_hash != action_hash:
                    continue
                try:
                    proposal = ProposedAction.model_validate(raw_proposal)
                except ValueError:
                    continue
                if proposal.outgoing_review is None:
                    continue
                connection.execute(
                    update(ApprovalRow)
                    .where(ApprovalRow.id == approval_id)
                    .values(payload_sha256=proposal.outgoing_review.payload_sha256)
                )

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session()
        committed_transitions: tuple[CommittedAuditTransition, ...] = ()
        try:
            yield session
            session.commit()
            committed_transitions = tuple(session.info.pop(_PENDING_METRICS_KEY, ()))
        except IntegrityError as exc:
            session.rollback()
            raise ConflictError("database uniqueness conflict") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        if self.metrics is None:
            return
        for event in committed_transitions:
            try:
                self.metrics.observe_committed(event)
            except Exception:
                # The database is already committed. Telemetry must never turn a
                # successful state transition into an apparent domain failure.
                STORE_LOG.exception(
                    "operational metric observation failed for %s",
                    event.kind,
                )

    def reset(self) -> None:
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)

    def create_profile(
        self,
        *,
        tenant_id: UUID,
        name: str,
    ) -> FactualProfile:
        profile_id = uuid4()
        now = utc_now()
        with self.session() as session:
            row = FactualProfileRow(
                id=str(profile_id),
                tenant_id=str(tenant_id),
                name=name,
                created_at=now,
                updated_at=now,
                version=1,
            )
            session.add(row)
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=profile_id,
                action_id=None,
                kind="profile.created",
                payload={
                    "profile_id": str(profile_id),
                    "name_sha256": digest(name),
                },
            )
            session.flush()
            return self._profile(row)

    def get_profile(self, tenant_id: UUID, profile_id: UUID) -> FactualProfile:
        with self.session() as session:
            return self._profile(self._profile_row(session, tenant_id, profile_id))

    def list_profiles(self, tenant_id: UUID) -> list[FactualProfile]:
        with self.session() as session:
            rows = session.scalars(
                select(FactualProfileRow)
                .where(FactualProfileRow.tenant_id == str(tenant_id))
                .order_by(FactualProfileRow.created_at)
            ).all()
            return [self._profile(row) for row in rows]

    def put_profile_answer(
        self,
        *,
        tenant_id: UUID,
        profile_id: UUID,
        field_name: str,
        value: str,
        source: AnswerSource,
        sensitivity: AnswerSensitivity,
        verification_state: VerificationState,
        expected_version: int | None,
        actor_id: str,
    ) -> ProfileAnswer:
        now = utc_now()
        with self.session() as session:
            profile = self._locked_profile_row(session, tenant_id, profile_id)
            row = session.scalar(
                select(ProfileAnswerRow)
                .where(
                    ProfileAnswerRow.tenant_id == str(tenant_id),
                    ProfileAnswerRow.profile_id == str(profile_id),
                    ProfileAnswerRow.field_name == field_name,
                )
                .with_for_update()
            )
            verified = verification_state is VerificationState.VERIFIED
            if row is None:
                if expected_version is not None:
                    raise ConflictError(
                        "answer does not exist; omit expected_version when creating"
                    )
                row = ProfileAnswerRow(
                    id=str(uuid4()),
                    tenant_id=str(tenant_id),
                    profile_id=str(profile_id),
                    field_name=field_name,
                    value=value,
                    source_kind=source.kind.value,
                    source_reference=source.reference,
                    sensitivity=sensitivity.value,
                    verification_state=verification_state.value,
                    verified_by=actor_id if verified else None,
                    verified_at=now if verified else None,
                    created_at=now,
                    updated_at=now,
                    version=1,
                )
                session.add(row)
                kind = "profile.answer_created"
            else:
                if expected_version is None:
                    raise ConflictError(
                        "expected_version is required when replacing an answer"
                    )
                if row.version != expected_version:
                    raise ConflictError("answer version changed; reload before replacing")
                row.value = value
                row.source_kind = source.kind.value
                row.source_reference = source.reference
                row.sensitivity = sensitivity.value
                row.verification_state = verification_state.value
                row.verified_by = actor_id if verified else None
                row.verified_at = now if verified else None
                row.updated_at = now
                row.version += 1
                kind = "profile.answer_updated"
            profile.updated_at = now
            profile.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=profile_id,
                action_id=None,
                kind=kind,
                payload={
                    "profile_id": str(profile_id),
                    "answer_id": row.id,
                    "field_name_sha256": digest(field_name),
                    "sensitivity": sensitivity.value,
                    "verification_state": verification_state.value,
                    "answer_version": row.version,
                },
            )
            session.flush()
            return self._profile_answer(row)

    def list_profile_answers(
        self,
        tenant_id: UUID,
        profile_id: UUID,
    ) -> list[ProfileAnswer]:
        with self.session() as session:
            self._profile_row(session, tenant_id, profile_id)
            rows = session.scalars(
                select(ProfileAnswerRow)
                .where(
                    ProfileAnswerRow.tenant_id == str(tenant_id),
                    ProfileAnswerRow.profile_id == str(profile_id),
                )
                .order_by(ProfileAnswerRow.field_name)
            ).all()
            return [self._profile_answer(row) for row in rows]

    def profile_events(
        self,
        tenant_id: UUID,
        profile_id: UUID,
    ) -> list[AuditEvent]:
        with self.session() as session:
            self._profile_row(session, tenant_id, profile_id)
            rows = session.scalars(
                select(AuditEventRow)
                .where(
                    AuditEventRow.tenant_id == str(tenant_id),
                    AuditEventRow.task_id == str(profile_id),
                    AuditEventRow.kind.like("profile.%"),
                )
                .order_by(AuditEventRow.sequence)
            ).all()
            return [self._event(row) for row in rows]

    def create_mission(
        self,
        *,
        mission_id: UUID,
        tenant_id: UUID,
        query: str,
        provider: str,
        plan: MissionPlan,
        external_commit_authorized: bool,
        external_commit_granted: bool | None = None,
        commit_intent_detected: bool | None = None,
        authority_reason: str | None = None,
    ) -> Mission:
        now = utc_now()
        max_external_commits = 1 if external_commit_authorized else 0
        with self.session() as session:
            row = MissionRow(
                id=str(mission_id),
                tenant_id=str(tenant_id),
                query=query,
                provider=provider,
                plan_summary=plan.summary,
                external_commit_authorized=int(external_commit_authorized),
                max_external_commits=max_external_commits,
                status=MissionStatus.QUEUED.value,
                created_at=now,
                updated_at=now,
                version=1,
                lease_owner=None,
                lease_expires_at=None,
            )
            session.add(row)
            for ordinal, planned in enumerate(plan.steps):
                step_id = uuid4()
                session.add(
                    MissionStepRow(
                        id=str(step_id),
                        mission_id=str(mission_id),
                        tenant_id=str(tenant_id),
                        ordinal=ordinal,
                        key=planned.key,
                        kind=planned.kind.value,
                        instruction=planned.instruction,
                        depends_on=list(planned.depends_on),
                        status=MissionStepStatus.PENDING.value,
                        child_task_id=(
                            str(uuid4())
                            if planned.kind is MissionStepKind.BROWSER
                            else None
                        ),
                        output=None,
                        output_sha256=None,
                        error=None,
                        created_at=now,
                        updated_at=now,
                        version=1,
                    )
                )
            authority_payload = (
                {
                    "authority_version": 2,
                    "external_commit_granted": external_commit_granted,
                    "commit_intent_detected": commit_intent_detected,
                    "authority_reason": authority_reason,
                }
                if external_commit_granted is not None
                and commit_intent_detected is not None
                else {}
            )
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=mission_id,
                action_id=None,
                kind="mission.created",
                payload={
                    "provider": provider,
                    "step_count": len(plan.steps),
                    "plan_sha256": digest(plan.model_dump(mode="json")),
                    "query_sha256": digest({"query": query}),
                    "external_commit_authorized": external_commit_authorized,
                    "max_external_commits": max_external_commits,
                    **authority_payload,
                },
            )
            session.flush()
            return self._mission(row)

    def get_mission(self, tenant_id: UUID, mission_id: UUID) -> Mission:
        with self.session() as session:
            return self._mission(self._mission_row(session, tenant_id, mission_id))

    def list_missions(self, tenant_id: UUID) -> list[Mission]:
        with self.session() as session:
            rows = session.scalars(
                select(MissionRow)
                .where(MissionRow.tenant_id == str(tenant_id))
                .order_by(MissionRow.created_at.desc())
            ).all()
            return [self._mission(row) for row in rows]

    def list_mission_steps(
        self,
        tenant_id: UUID,
        mission_id: UUID,
    ) -> list[MissionStep]:
        with self.session() as session:
            self._mission_row(session, tenant_id, mission_id)
            rows = session.scalars(
                select(MissionStepRow)
                .where(
                    MissionStepRow.tenant_id == str(tenant_id),
                    MissionStepRow.mission_id == str(mission_id),
                )
                .order_by(MissionStepRow.ordinal)
            ).all()
            return [self._mission_step(row) for row in rows]

    def mission_for_child_task(
        self,
        tenant_id: UUID,
        task_id: UUID,
    ) -> UUID | None:
        with self.session() as session:
            mission_id = session.scalar(
                select(MissionStepRow.mission_id).where(
                    MissionStepRow.tenant_id == str(tenant_id),
                    MissionStepRow.child_task_id == str(task_id),
                )
            )
            return UUID(mission_id) if mission_id else None

    def mission_events(
        self,
        tenant_id: UUID,
        mission_id: UUID,
    ) -> list[AuditEvent]:
        with self.session() as session:
            self._mission_row(session, tenant_id, mission_id)
            rows = session.scalars(
                select(AuditEventRow)
                .where(
                    AuditEventRow.tenant_id == str(tenant_id),
                    AuditEventRow.task_id == str(mission_id),
                    AuditEventRow.kind.like("mission.%"),
                )
                .order_by(AuditEventRow.sequence)
            ).all()
            return [self._event(row) for row in rows]

    def mission_timeline(
        self,
        tenant_id: UUID,
        mission_id: UUID,
    ) -> dict[str, Any]:
        """Return a stable, content-redacted parent/child audit projection.

        Audit sequence numbers are tenant-global. Selecting both the mission ID and
        its reserved child IDs in one query preserves their true interleaving while
        allowing legitimate gaps caused by unrelated work in the same tenant.
        """
        with self.session() as session:
            mission_row = self._mission_row(session, tenant_id, mission_id)
            step_rows = session.scalars(
                select(MissionStepRow)
                .where(
                    MissionStepRow.tenant_id == str(tenant_id),
                    MissionStepRow.mission_id == str(mission_id),
                )
                .order_by(MissionStepRow.ordinal)
            ).all()
            child_ids = tuple(
                row.child_task_id for row in step_rows if row.child_task_id is not None
            )
            scoped_ids = (str(mission_id), *child_ids)
            event_rows = session.scalars(
                select(AuditEventRow)
                .where(
                    AuditEventRow.tenant_id == str(tenant_id),
                    AuditEventRow.task_id.in_(scoped_ids),
                )
                .order_by(AuditEventRow.sequence)
            ).all()
            mission = {
                "id": mission_row.id,
                "status": MissionStatus(mission_row.status).value,
                "external_commit_authorized": bool(
                    mission_row.external_commit_authorized
                ),
                "max_external_commits": mission_row.max_external_commits,
                "version": mission_row.version,
            }
            steps = [
                {
                    "id": row.id,
                    "ordinal": row.ordinal,
                    "key": _redacted_step_key(row.key),
                    "kind": MissionStepKind(row.kind).value,
                    "status": MissionStepStatus(row.status).value,
                    "depends_on": [
                        _redacted_step_key(key) for key in row.depends_on or ()
                    ],
                    "child_task_id": row.child_task_id,
                    "output_sha256": _redacted_hash(row.output_sha256),
                    "version": row.version,
                }
                for row in step_rows
            ]
            events = [
                {
                    "sequence": row.sequence,
                    "scope": (
                        "mission" if row.task_id == str(mission_id) else "browser_child"
                    ),
                    "action_id": row.action_id,
                    "kind": _redacted_event_kind(row.kind),
                    "payload": _redacted_audit_payload(row.payload),
                    "occurred_at": _as_utc(row.occurred_at).isoformat(),
                    "previous_hash": _redacted_hash(row.previous_hash),
                    "event_hash": _redacted_hash(row.event_hash),
                }
                for row in event_rows
            ]
        verification = self.verify_audit(tenant_id)
        return {
            "schema_version": 1,
            "tenant_id": str(tenant_id),
            "mission": mission,
            "steps": steps,
            "events": events,
            "audit": {
                "valid": verification.valid,
                "event_count": verification.event_count,
                "head_hash": _redacted_hash(verification.head_hash),
                "first_invalid_sequence": verification.first_invalid_sequence,
            },
        }

    def claim_mission(
        self,
        *,
        tenant_id: UUID,
        mission_id: UUID,
        owner: str,
        lease_seconds: int = 300,
    ) -> Mission:
        now = utc_now()
        expires_at = now + timedelta(seconds=lease_seconds)
        terminal = {
            MissionStatus.SUCCEEDED.value,
            MissionStatus.BLOCKED.value,
            MissionStatus.FAILED.value,
        }
        with self.session() as session:
            result = session.execute(
                update(MissionRow)
                .where(
                    MissionRow.id == str(mission_id),
                    MissionRow.tenant_id == str(tenant_id),
                    MissionRow.status.not_in(terminal),
                    or_(
                        MissionRow.lease_owner.is_(None),
                        MissionRow.lease_owner == owner,
                        and_(
                            MissionRow.lease_expires_at.is_not(None),
                            MissionRow.lease_expires_at < now,
                        ),
                    ),
                )
                .values(
                    status=MissionStatus.RUNNING.value,
                    lease_owner=owner,
                    lease_expires_at=expires_at,
                    updated_at=now,
                    version=MissionRow.version + 1,
                )
            )
            if result.rowcount != 1:
                exists = session.scalar(
                    select(MissionRow.id).where(
                        MissionRow.id == str(mission_id),
                        MissionRow.tenant_id == str(tenant_id),
                    )
                )
                if exists is None:
                    raise NotFoundError("mission not found")
                raise ConflictError("mission is terminal or leased by another worker")
            stale = session.scalars(
                select(MissionStepRow).where(
                    MissionStepRow.mission_id == str(mission_id),
                    MissionStepRow.tenant_id == str(tenant_id),
                    MissionStepRow.status == MissionStepStatus.RUNNING.value,
                )
            ).all()
            for step in stale:
                step.status = MissionStepStatus.PENDING.value
                step.updated_at = now
                step.version += 1
            row = self._mission_row(session, tenant_id, mission_id)
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=mission_id,
                action_id=None,
                kind="mission.lease_acquired",
                payload={
                    "owner": owner,
                    "expires_at": expires_at.isoformat(),
                    "recovered_running_steps": len(stale),
                },
            )
            return self._mission(row)

    def renew_mission_lease(
        self,
        *,
        tenant_id: UUID,
        mission_id: UUID,
        owner: str,
        lease_seconds: int = 300,
    ) -> None:
        now = utc_now()
        with self.session() as session:
            result = session.execute(
                update(MissionRow)
                .where(
                    MissionRow.id == str(mission_id),
                    MissionRow.tenant_id == str(tenant_id),
                    MissionRow.lease_owner == owner,
                    MissionRow.lease_expires_at >= now,
                )
                .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            )
            if result.rowcount != 1:
                raise ConflictError("mission worker lease was lost or expired")

    def release_mission(
        self,
        *,
        tenant_id: UUID,
        mission_id: UUID,
        owner: str,
    ) -> None:
        with self.session() as session:
            row = self._mission_row(session, tenant_id, mission_id)
            if row.lease_owner != owner:
                return
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = utc_now()
            row.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=mission_id,
                action_id=None,
                kind="mission.lease_released",
                payload={"owner": owner},
            )

    def start_mission_steps(
        self,
        *,
        tenant_id: UUID,
        mission_id: UUID,
        step_ids: tuple[UUID, ...],
    ) -> list[MissionStep]:
        if not step_ids:
            return []
        now = utc_now()
        with self.session() as session:
            mission = self._mission_row(session, tenant_id, mission_id)
            if MissionStatus(mission.status) is not MissionStatus.RUNNING:
                raise ConflictError("mission must be running before a step can start")
            rows = session.scalars(
                select(MissionStepRow)
                .where(
                    MissionStepRow.mission_id == str(mission_id),
                    MissionStepRow.tenant_id == str(tenant_id),
                    MissionStepRow.id.in_([str(item) for item in step_ids]),
                )
                .order_by(MissionStepRow.ordinal)
            ).all()
            if len(rows) != len(step_ids):
                raise NotFoundError("mission step not found")
            if any(
                MissionStepStatus(row.status) is not MissionStepStatus.PENDING
                for row in rows
            ):
                raise ConflictError("only pending mission steps can start")
            for row in rows:
                row.status = MissionStepStatus.RUNNING.value
                row.updated_at = now
                row.version += 1
                self._append_event(
                    session,
                    tenant_id=tenant_id,
                    task_id=mission_id,
                    action_id=UUID(row.id),
                    kind="mission.step_started",
                    payload={"step_key": row.key, "kind": row.kind},
                )
            mission.updated_at = now
            mission.version += 1
            session.flush()
            return [self._mission_step(row) for row in rows]

    def complete_mission_step(
        self,
        *,
        tenant_id: UUID,
        mission_id: UUID,
        step_id: UUID,
        output: dict[str, Any],
    ) -> MissionStep:
        now = utc_now()
        output_hash = digest(output)
        with self.session() as session:
            mission = self._mission_row(session, tenant_id, mission_id)
            row = self._mission_step_row(session, tenant_id, mission_id, step_id)
            if MissionStepStatus(row.status) is MissionStepStatus.SUCCEEDED:
                if row.output_sha256 != output_hash:
                    raise ConflictError("completed mission step output cannot change")
                return self._mission_step(row)
            if MissionStepStatus(row.status) is not MissionStepStatus.RUNNING:
                raise ConflictError("only a running mission step can complete")
            row.status = MissionStepStatus.SUCCEEDED.value
            row.output = output
            row.output_sha256 = output_hash
            row.error = None
            row.updated_at = now
            row.version += 1
            mission.updated_at = now
            mission.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=mission_id,
                action_id=step_id,
                kind="mission.step_completed",
                payload={
                    "step_key": row.key,
                    "kind": row.kind,
                    "output_sha256": output_hash,
                },
            )
            session.flush()
            return self._mission_step(row)

    def stop_mission_step(
        self,
        *,
        tenant_id: UUID,
        mission_id: UUID,
        step_id: UUID,
        status: MissionStepStatus,
        error: str,
        output: dict[str, Any] | None = None,
    ) -> MissionStep:
        if status not in {
            MissionStepStatus.BLOCKED,
            MissionStepStatus.FAILED,
            MissionStepStatus.SKIPPED,
        }:
            raise ValueError("stopped mission step needs a terminal non-success status")
        now = utc_now()
        with self.session() as session:
            mission = self._mission_row(session, tenant_id, mission_id)
            row = self._mission_step_row(session, tenant_id, mission_id, step_id)
            current = MissionStepStatus(row.status)
            allowed = (
                {MissionStepStatus.RUNNING, MissionStepStatus.PENDING}
                if status is MissionStepStatus.SKIPPED
                else {MissionStepStatus.RUNNING}
            )
            if current not in allowed:
                raise ConflictError("mission step cannot transition from its state")
            row.status = status.value
            row.output = output
            row.output_sha256 = digest(output) if output is not None else None
            row.error = error[:2_000]
            row.updated_at = now
            row.version += 1
            mission.updated_at = now
            mission.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=mission_id,
                action_id=step_id,
                kind=f"mission.step_{status.value}",
                payload={
                    "step_key": row.key,
                    "kind": row.kind,
                    "reason": error[:500],
                },
            )
            session.flush()
            return self._mission_step(row)

    def finish_mission(
        self,
        *,
        tenant_id: UUID,
        mission_id: UUID,
        status: MissionStatus,
        reason: str,
    ) -> Mission:
        if status not in {
            MissionStatus.SUCCEEDED,
            MissionStatus.BLOCKED,
            MissionStatus.FAILED,
        }:
            raise ValueError("mission final status must be terminal")
        now = utc_now()
        with self.session() as session:
            row = self._mission_row(session, tenant_id, mission_id)
            if MissionStatus(row.status) in {
                MissionStatus.SUCCEEDED,
                MissionStatus.BLOCKED,
                MissionStatus.FAILED,
            }:
                return self._mission(row)
            row.status = status.value
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = now
            row.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=mission_id,
                action_id=None,
                kind=f"mission.{status.value}",
                payload={"reason": reason[:500]},
            )
            session.flush()
            return self._mission(row)

    def reopen_mission_step(
        self,
        *,
        tenant_id: UUID,
        mission_id: UUID,
        step_id: UUID,
        reason: str,
    ) -> MissionStep:
        """Requeue one paused browser child after its underlying gate changes."""
        now = utc_now()
        with self.session() as session:
            mission = self._mission_row(session, tenant_id, mission_id)
            row = self._mission_step_row(session, tenant_id, mission_id, step_id)
            state_pair = (
                MissionStatus(mission.status),
                MissionStepStatus(row.status),
            )
            if state_pair not in {
                (MissionStatus.BLOCKED, MissionStepStatus.BLOCKED),
                (MissionStatus.FAILED, MissionStepStatus.FAILED),
            }:
                raise ConflictError(
                    "only a matching blocked or interrupted browser step can reopen"
                )
            row.status = MissionStepStatus.PENDING.value
            row.error = None
            row.updated_at = now
            row.version += 1
            skipped = session.scalars(
                select(MissionStepRow).where(
                    MissionStepRow.mission_id == str(mission_id),
                    MissionStepRow.tenant_id == str(tenant_id),
                    MissionStepRow.status == MissionStepStatus.SKIPPED.value,
                )
            ).all()
            for dependent in skipped:
                dependent.status = MissionStepStatus.PENDING.value
                dependent.error = None
                dependent.updated_at = now
                dependent.version += 1
            mission.status = MissionStatus.QUEUED.value
            mission.updated_at = now
            mission.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=mission_id,
                action_id=step_id,
                kind="mission.step_reopened",
                payload={
                    "step_key": row.key,
                    "kind": row.kind,
                    "reason": reason[:500],
                    "requeued_skipped_steps": len(skipped),
                },
            )
            session.flush()
            return self._mission_step(row)

    def create_task(
        self,
        *,
        task_id: UUID,
        tenant_id: UUID,
        instruction: str,
        start_url: str,
        provider: str,
        actions: tuple[ProposedAction, ...],
        profile_id: UUID | None = None,
        document_path: Path | None = None,
        document_sha256: str | None = None,
        autonomy: AutonomyScope | None = None,
        authority_context: dict[str, Any] | None = None,
    ) -> Task:
        now = utc_now()
        with self.session() as session:
            if profile_id is not None:
                self._profile_row(session, tenant_id, profile_id)
            task = TaskRow(
                id=str(task_id),
                tenant_id=str(tenant_id),
                instruction=instruction,
                start_url=start_url,
                provider=provider,
                profile_id=str(profile_id) if profile_id else None,
                document_path=str(document_path) if document_path else None,
                document_sha256=document_sha256,
                autonomy_scope=(autonomy or AutonomyScope()).model_dump(mode="json"),
                status=TaskStatus.QUEUED.value,
                current_ordinal=0,
                created_at=now,
                updated_at=now,
                version=1,
                lease_owner=None,
                lease_expires_at=None,
            )
            session.add(task)
            for ordinal, proposal in enumerate(actions):
                session.add(
                    ActionRow(
                        id=str(uuid4()),
                        tenant_id=str(tenant_id),
                        task_id=str(task_id),
                        ordinal=ordinal,
                        proposal=proposal.model_dump(mode="json"),
                        effect_key=proposal.effect_key,
                        state=ActionState.PENDING.value,
                        risk=None,
                        action_sha256=proposal.action_hash(),
                        observation_sha256=None,
                        observation_url=None,
                        failure=None,
                        version=1,
                    )
                )
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=task_id,
                action_id=None,
                kind="task.created",
                payload={
                    "provider": provider,
                    "action_count": len(actions),
                    "profile_id": str(profile_id) if profile_id else None,
                    "document_sha256": document_sha256,
                    "autonomy": (autonomy or AutonomyScope()).model_dump(mode="json"),
                    "authority_context": authority_context,
                },
            )
            session.flush()
            return self._task(task)

    def get_task(self, tenant_id: UUID, task_id: UUID) -> Task:
        with self.session() as session:
            return self._task(self._task_row(session, tenant_id, task_id))

    def list_tasks(self, tenant_id: UUID) -> list[Task]:
        with self.session() as session:
            rows = session.scalars(
                select(TaskRow)
                .where(TaskRow.tenant_id == str(tenant_id))
                .order_by(TaskRow.created_at.desc())
            ).all()
            return [self._task(row) for row in rows]

    def task_document(
        self,
        tenant_id: UUID,
        task_id: UUID,
    ) -> tuple[Path, str] | None:
        with self.session() as session:
            row = self._task_row(session, tenant_id, task_id)
            if row.document_path is None or row.document_sha256 is None:
                return None
            return Path(row.document_path), row.document_sha256

    def claim_task(
        self,
        *,
        tenant_id: UUID,
        task_id: UUID,
        owner: str,
        lease_seconds: int = 120,
    ) -> Task:
        """Acquire a time-bounded single-worker lease with one conditional update."""
        now = utc_now()
        expires_at = now + timedelta(seconds=lease_seconds)
        terminal = {
            TaskStatus.SUCCEEDED.value,
            TaskStatus.FAILED.value,
            TaskStatus.REJECTED.value,
            TaskStatus.BLOCKED.value,
        }
        with self.session() as session:
            result = session.execute(
                update(TaskRow)
                .where(
                    TaskRow.id == str(task_id),
                    TaskRow.tenant_id == str(tenant_id),
                    TaskRow.status.not_in(terminal),
                    or_(
                        TaskRow.lease_owner.is_(None),
                        TaskRow.lease_owner == owner,
                        and_(
                            TaskRow.lease_expires_at.is_not(None),
                            TaskRow.lease_expires_at < now,
                        ),
                    ),
                )
                .values(
                    lease_owner=owner,
                    lease_expires_at=expires_at,
                    updated_at=now,
                    version=TaskRow.version + 1,
                )
            )
            if result.rowcount != 1:
                exists = session.scalar(
                    select(TaskRow.id).where(
                        TaskRow.id == str(task_id),
                        TaskRow.tenant_id == str(tenant_id),
                    )
                )
                if exists is None:
                    raise NotFoundError("task not found")
                raise ConflictError("task is terminal or leased by another worker")
            row = self._task_row(session, tenant_id, task_id)
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=task_id,
                action_id=None,
                kind="task.lease_acquired",
                payload={"owner": owner, "expires_at": expires_at.isoformat()},
            )
            return self._task(row)

    def renew_task_lease(
        self,
        *,
        tenant_id: UUID,
        task_id: UUID,
        owner: str,
        lease_seconds: int = 120,
    ) -> None:
        now = utc_now()
        with self.session() as session:
            result = session.execute(
                update(TaskRow)
                .where(
                    TaskRow.id == str(task_id),
                    TaskRow.tenant_id == str(tenant_id),
                    TaskRow.lease_owner == owner,
                    TaskRow.lease_expires_at >= now,
                )
                .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            )
            if result.rowcount != 1:
                raise ConflictError("worker lease was lost or expired")

    def release_task(self, *, tenant_id: UUID, task_id: UUID, owner: str) -> None:
        with self.session() as session:
            row = self._task_row(session, tenant_id, task_id)
            if row.lease_owner != owner:
                return
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = utc_now()
            row.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=task_id,
                action_id=None,
                kind="task.lease_released",
                payload={"owner": owner},
            )

    def list_actions(self, tenant_id: UUID, task_id: UUID) -> list[BrowserAction]:
        with self.session() as session:
            self._task_row(session, tenant_id, task_id)
            rows = session.scalars(
                select(ActionRow)
                .where(
                    ActionRow.tenant_id == str(tenant_id),
                    ActionRow.task_id == str(task_id),
                )
                .order_by(ActionRow.ordinal)
            ).all()
            return [self._action(row) for row in rows]

    def get_action(self, tenant_id: UUID, action_id: UUID) -> BrowserAction:
        with self.session() as session:
            return self._action(self._action_row(session, tenant_id, action_id))

    def current_action(self, tenant_id: UUID, task_id: UUID) -> BrowserAction | None:
        with self.session() as session:
            task = self._task_row(session, tenant_id, task_id)
            row = session.scalar(
                select(ActionRow).where(
                    ActionRow.tenant_id == str(tenant_id),
                    ActionRow.task_id == str(task_id),
                    ActionRow.ordinal == task.current_ordinal,
                )
            )
            return self._action(row) if row else None

    def append_action(
        self,
        *,
        tenant_id: UUID,
        task_id: UUID,
        proposal: ProposedAction,
    ) -> BrowserAction:
        with self.session() as session:
            task = self._task_row(session, tenant_id, task_id)
            if task.current_ordinal >= 30:
                raise ConflictError("reactive task reached the 30-action limit")
            existing = session.scalar(
                select(ActionRow).where(
                    ActionRow.tenant_id == str(tenant_id),
                    ActionRow.task_id == str(task_id),
                    ActionRow.ordinal == task.current_ordinal,
                )
            )
            if existing is not None:
                raise ConflictError("task already has a current action")
            row = ActionRow(
                id=str(uuid4()),
                tenant_id=str(tenant_id),
                task_id=str(task_id),
                ordinal=task.current_ordinal,
                proposal=proposal.model_dump(mode="json"),
                effect_key=proposal.effect_key,
                state=ActionState.PENDING.value,
                risk=None,
                action_sha256=proposal.action_hash(),
                observation_sha256=None,
                observation_url=None,
                failure=None,
                version=1,
            )
            session.add(row)
            task.status = TaskStatus.QUEUED.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=task_id,
                action_id=UUID(row.id),
                kind="action.planned_from_snapshot",
                payload={
                    "ordinal": row.ordinal,
                    "action_sha256": row.action_sha256,
                    "planned_from_sha256": proposal.planned_from_sha256,
                },
            )
            session.flush()
            return self._action(row)

    def bind_outgoing_review(
        self,
        *,
        tenant_id: UUID,
        action_id: UUID,
        expected_version: int,
        review: OutgoingReview,
    ) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if row.version != expected_version:
                raise ConflictError("action version changed while binding review")
            if ActionState(row.state) not in {
                ActionState.PENDING,
                ActionState.INVALIDATED,
            }:
                raise ConflictError("only an unprepared action can bind a review")
            proposal = ProposedAction.model_validate(row.proposal)
            if proposal.kind is not ActionKind.SUBMIT:
                raise ConflictError("only submit actions can bind an outgoing review")
            updated = proposal.model_copy(
                update={
                    "outgoing_review": review,
                    "planned_from_sha256": review.observation_sha256,
                }
            )
            row.proposal = updated.model_dump(mode="json")
            row.action_sha256 = updated.action_hash()
            row.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.outgoing_review_bound",
                payload={
                    "action_sha256": row.action_sha256,
                    "observation_sha256": review.observation_sha256,
                    "payload_sha256": review.payload_sha256,
                    "request_sha256s": [
                        request.request_sha256 for request in review.requests
                    ],
                },
            )
            session.flush()
            return self._action(row)

    def prepare_action(
        self,
        tenant_id: UUID,
        action_id: UUID,
        observation: Observation,
        decision: PolicyDecision,
    ) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if ActionState(row.state) not in {
                ActionState.PENDING,
                ActionState.INVALIDATED,
            }:
                raise ConflictError("action is not pending preparation")
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            row.risk = decision.risk.value
            row.observation_sha256 = observation.state_sha256
            row.observation_url = observation.url
            row.failure = None
            row.version += 1
            if not decision.allowed:
                row.state = ActionState.FAILED.value
                row.failure = decision.reason
                task.status = TaskStatus.FAILED.value
                kind = "action.denied"
            elif decision.requires_approval:
                row.state = ActionState.APPROVAL_REQUIRED.value
                task.status = TaskStatus.AWAITING_APPROVAL.value
                kind = "action.approval_required"
            else:
                row.state = ActionState.PREPARED.value
                task.status = TaskStatus.RUNNING.value
                kind = "action.prepared"
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind=kind,
                payload={
                    "action_sha256": row.action_sha256,
                    "observation_sha256": observation.state_sha256,
                    "risk": decision.risk.value,
                    "reason": decision.reason,
                },
            )
            session.flush()
            return self._action(row)

    def require_input(
        self,
        *,
        tenant_id: UUID,
        action_id: UUID,
        observation: Observation,
        reason: str,
    ) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if ActionState(row.state) not in {
                ActionState.PENDING,
                ActionState.INVALIDATED,
            }:
                raise ConflictError("only a pending action can require input")
            proposal = ProposedAction.model_validate(row.proposal)
            if proposal.kind is not ActionKind.HANDOFF:
                raise ConflictError("only handoff actions can require input")
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            row.state = ActionState.INPUT_REQUIRED.value
            row.risk = RiskClass.INPUT.value
            row.observation_sha256 = observation.state_sha256
            row.observation_url = observation.url
            row.failure = reason[:2_000]
            row.version += 1
            task.status = TaskStatus.AWAITING_INPUT.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.input_required",
                payload={
                    "action_sha256": row.action_sha256,
                    "observation_sha256": observation.state_sha256,
                    "reason": reason[:500],
                },
            )
            session.flush()
            return self._action(row)

    def resolve_input(
        self,
        *,
        tenant_id: UUID,
        action_id: UUID,
        expected_version: int,
        actor_id: str,
    ) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if row.version != expected_version:
                raise ConflictError("action version changed; reload before resuming")
            if ActionState(row.state) is not ActionState.INPUT_REQUIRED:
                raise ConflictError("action is not awaiting input")
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            if task.current_ordinal != row.ordinal:
                raise ConflictError("handoff is not the current action")
            row.state = ActionState.SUCCEEDED.value
            row.failure = None
            row.version += 1
            task.current_ordinal += 1
            task.status = TaskStatus.QUEUED.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.input_resolved",
                payload={"actor_id": actor_id},
            )
            session.flush()
            return self._action(row)

    def approve_action(
        self,
        *,
        tenant_id: UUID,
        action_id: UUID,
        expected_version: int,
        actor_id: str,
        authorization_basis: str | None = None,
    ) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if row.version != expected_version:
                raise ConflictError("action version changed; reload before approving")
            if ActionState(row.state) is not ActionState.APPROVAL_REQUIRED:
                raise ConflictError("action is not awaiting approval")
            if not row.observation_sha256:
                raise ConflictError("action has no bound observation")
            proposal = ProposedAction.model_validate(row.proposal)
            payload_sha256 = (
                proposal.outgoing_review.payload_sha256
                if proposal.outgoing_review is not None
                else None
            )
            approval = ApprovalRow(
                id=str(uuid4()),
                tenant_id=str(tenant_id),
                action_id=row.id,
                decision=ApprovalDecision.APPROVED.value,
                actor_id=actor_id,
                action_sha256=row.action_sha256,
                observation_sha256=row.observation_sha256,
                payload_sha256=payload_sha256,
                decided_at=utc_now(),
            )
            session.add(approval)
            row.state = ActionState.PREPARED.value
            row.version += 1
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            task.status = TaskStatus.QUEUED.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.approved",
                payload={
                    "actor_id": actor_id,
                    "action_sha256": row.action_sha256,
                    "observation_sha256": row.observation_sha256,
                    "payload_sha256": payload_sha256,
                    "authorization_basis": authorization_basis,
                },
            )
            session.flush()
            return self._action(row)

    def reject_action(
        self,
        *,
        tenant_id: UUID,
        action_id: UUID,
        expected_version: int,
        actor_id: str,
    ) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if row.version != expected_version:
                raise ConflictError("action version changed; reload before rejecting")
            if ActionState(row.state) is not ActionState.APPROVAL_REQUIRED:
                raise ConflictError("action is not awaiting approval")
            proposal = ProposedAction.model_validate(row.proposal)
            payload_sha256 = (
                proposal.outgoing_review.payload_sha256
                if proposal.outgoing_review is not None
                else None
            )
            session.add(
                ApprovalRow(
                    id=str(uuid4()),
                    tenant_id=str(tenant_id),
                    action_id=row.id,
                    decision=ApprovalDecision.REJECTED.value,
                    actor_id=actor_id,
                    action_sha256=row.action_sha256,
                    observation_sha256=row.observation_sha256 or "",
                    payload_sha256=payload_sha256,
                    decided_at=utc_now(),
                )
            )
            row.state = ActionState.REJECTED.value
            row.version += 1
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            task.status = TaskStatus.REJECTED.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.rejected",
                payload={
                    "actor_id": actor_id,
                    "action_sha256": row.action_sha256,
                    "observation_sha256": row.observation_sha256,
                    "payload_sha256": payload_sha256,
                },
            )
            session.flush()
            return self._action(row)

    def invalidate_approval(
        self,
        tenant_id: UUID,
        action_id: UUID,
        actual_observation_sha256: str,
    ) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if ActionState(row.state) is not ActionState.PREPARED:
                raise ConflictError("only a prepared action can be invalidated")
            expected = row.observation_sha256
            proposal = ProposedAction.model_validate(row.proposal)
            if proposal.outgoing_review is not None:
                proposal = proposal.model_copy(
                    update={
                        "outgoing_review": None,
                        "planned_from_sha256": None,
                    }
                )
                row.proposal = proposal.model_dump(mode="json")
                row.action_sha256 = proposal.action_hash()
            row.state = ActionState.INVALIDATED.value
            row.observation_sha256 = actual_observation_sha256
            row.failure = "page state changed after preparation or approval"
            row.version += 1
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            task.status = TaskStatus.QUEUED.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="approval.invalidated",
                payload={
                    "expected_observation_sha256": expected,
                    "actual_observation_sha256": actual_observation_sha256,
                    "action_sha256": row.action_sha256,
                },
            )
            session.flush()
            return self._action(row)

    def start_dispatch(self, tenant_id: UUID, action_id: UUID) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if ActionState(row.state) is not ActionState.PREPARED:
                raise ConflictError("only a prepared action can dispatch")
            proposal = ProposedAction.model_validate(row.proposal)
            if proposal.action_hash() != row.action_sha256:
                raise ConflictError("stored action no longer matches its bound hash")
            if proposal.kind is ActionKind.SUBMIT and (
                proposal.outgoing_review is None
                or len(proposal.outgoing_review.requests) != 1
            ):
                raise ConflictError("submit lacks one exact approved outgoing request")
            if RiskClass(row.risk) is RiskClass.EXTERNAL_COMMIT:
                approval = session.scalar(
                    select(ApprovalRow)
                    .where(
                        ApprovalRow.tenant_id == str(tenant_id),
                        ApprovalRow.action_id == row.id,
                        ApprovalRow.decision == ApprovalDecision.APPROVED.value,
                    )
                    .order_by(ApprovalRow.decided_at.desc())
                )
                expected_payload_sha256 = (
                    proposal.outgoing_review.payload_sha256
                    if proposal.outgoing_review is not None
                    else None
                )
                if (
                    approval is None
                    or approval.action_sha256 != row.action_sha256
                    or approval.observation_sha256 != row.observation_sha256
                    or approval.payload_sha256 != expected_payload_sha256
                ):
                    raise ConflictError(
                        "external commit lacks exact action, payload, or page approval"
                    )
            row.state = ActionState.DISPATCHING.value
            row.version += 1
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            task.status = TaskStatus.RUNNING.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.dispatching",
                payload={
                    "action_sha256": row.action_sha256,
                    "action_kind": proposal.kind.value,
                    "risk": row.risk,
                    "effect_key": proposal.effect_key,
                    "payload_sha256": (
                        proposal.outgoing_review.payload_sha256
                        if proposal.outgoing_review is not None
                        else None
                    ),
                },
            )
            session.flush()
            return self._action(row)

    def complete_action(
        self,
        tenant_id: UUID,
        action_id: UUID,
        receipt: BrowserReceipt,
        task_continues: bool = False,
    ) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if ActionState(row.state) not in {
                ActionState.DISPATCHING,
                ActionState.OUTCOME_UNKNOWN,
            }:
                raise ConflictError("action is not dispatching or awaiting recovery")
            if (
                session.scalar(select(ReceiptRow).where(ReceiptRow.action_id == row.id))
                is None
            ):
                session.add(
                    ReceiptRow(
                        id=str(uuid4()),
                        tenant_id=str(tenant_id),
                        action_id=row.id,
                        external_id=receipt.external_id,
                        url=receipt.url,
                        evidence_sha256=receipt.evidence_sha256,
                        captured_at=receipt.captured_at,
                    )
                )
            metric_labels = _action_metric_labels(row)
            row.state = ActionState.SUCCEEDED.value
            row.failure = None
            row.version += 1
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            task.current_ordinal = row.ordinal + 1
            remaining = session.scalar(
                select(ActionRow).where(
                    ActionRow.task_id == row.task_id,
                    ActionRow.ordinal == task.current_ordinal,
                )
            )
            task.status = (
                TaskStatus.QUEUED.value
                if remaining or task_continues
                else TaskStatus.SUCCEEDED.value
            )
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.succeeded",
                payload={
                    **metric_labels,
                    "external_id": receipt.external_id,
                    "evidence_sha256": receipt.evidence_sha256,
                    "url": receipt.url,
                },
            )
            session.flush()
            return self._action(row)

    def fail_action(
        self,
        tenant_id: UUID,
        action_id: UUID,
        failure: str,
    ) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if ActionState(row.state) is not ActionState.DISPATCHING:
                raise ConflictError("only a dispatching action can fail")
            metric_labels = _action_metric_labels(row)
            row.state = ActionState.FAILED.value
            row.failure = failure[:2_000]
            row.version += 1
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            task.status = TaskStatus.FAILED.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.failed",
                payload={**metric_labels, "failure": failure[:500]},
            )
            session.flush()
            return self._action(row)

    def retry_blocked_upload(
        self,
        *,
        tenant_id: UUID,
        action_id: UUID,
        expected_version: int,
        actor_id: str,
        authorization_basis: str,
    ) -> BrowserAction:
        """Requeue an upload only when the prior attempt provably sent no bytes."""
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if row.version != expected_version:
                raise ConflictError("action version changed")
            proposal = ProposedAction.model_validate(row.proposal)
            failure = row.failure or ""
            safe_pre_dispatch_failures = (
                "outgoing request blocked before transmission:",
                "page changed after reactive planning; re-planning is required",
            )
            if (
                ActionState(row.state) is not ActionState.FAILED
                or proposal.kind is not ActionKind.UPLOAD
                or not failure.startswith(safe_pre_dispatch_failures)
            ):
                raise ConflictError(
                    "only a provably unsent upload can be rebound and retried"
                )
            if (
                session.scalar(select(ReceiptRow).where(ReceiptRow.action_id == row.id))
                is not None
            ):
                raise ConflictError("an upload receipt already exists")
            rebound = proposal.model_copy(update={"planned_from_sha256": None})
            row.proposal = rebound.model_dump(mode="json")
            row.action_sha256 = rebound.action_hash()
            row.state = ActionState.PENDING.value
            row.risk = None
            row.observation_sha256 = None
            row.observation_url = None
            row.failure = None
            row.version += 1
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            if task.current_ordinal != row.ordinal:
                raise ConflictError("blocked upload is no longer the task cursor")
            task.status = TaskStatus.QUEUED.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.retry_authorized",
                payload={
                    "actor_id": actor_id[:200],
                    "authorization_basis": authorization_basis[:500],
                    "prior_failure": failure[:500],
                    "rebound_action_sha256": row.action_sha256,
                    "automatic_retry": False,
                },
            )
            session.flush()
            return self._action(row)

    def supersede_failed_submit_preview(
        self,
        *,
        tenant_id: UUID,
        action_id: UUID,
        expected_version: int,
        actor_id: str,
        authorization_basis: str,
    ) -> BrowserAction:
        """Advance past a submit proposal that failed before producing a request."""
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if row.version != expected_version:
                raise ConflictError("action version changed")
            proposal = ProposedAction.model_validate(row.proposal)
            failure = row.failure or ""
            review_request_count = (
                len(proposal.outgoing_review.requests)
                if proposal.outgoing_review is not None
                else 0
            )
            safe_preview_failure = (
                failure.startswith("exact outgoing request review failed:")
                and review_request_count == 0
            ) or (
                failure.startswith("outgoing request origin is not allowed:")
                and review_request_count == 1
            )
            if (
                ActionState(row.state) is not ActionState.FAILED
                or proposal.kind is not ActionKind.SUBMIT
                or not safe_preview_failure
            ):
                raise ConflictError(
                    "only a submit preview with no outgoing request can be superseded"
                )
            if (
                session.scalar(select(ReceiptRow).where(ReceiptRow.action_id == row.id))
                is not None
            ):
                raise ConflictError("a submit receipt already exists")
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            if task.current_ordinal != row.ordinal:
                raise ConflictError("failed preview is no longer the task cursor")
            released_effect_key = row.effect_key
            row.effect_key = None
            row.state = ActionState.SUPERSEDED.value
            row.version += 1
            task.current_ordinal = row.ordinal + 1
            task.status = TaskStatus.QUEUED.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.superseded",
                payload={
                    "actor_id": actor_id[:200],
                    "authorization_basis": authorization_basis[:500],
                    "prior_failure": failure[:500],
                    "outgoing_request_count": review_request_count,
                    "effect_key_released": released_effect_key is not None,
                },
            )
            session.flush()
            return self._action(row)

    def release_superseded_effect_key(
        self,
        *,
        tenant_id: UUID,
        action_id: UUID,
        expected_version: int,
        actor_id: str,
    ) -> BrowserAction:
        """Upgrade a previously superseded zero-request action in place."""
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if row.version != expected_version:
                raise ConflictError("action version changed")
            proposal = ProposedAction.model_validate(row.proposal)
            failure = row.failure or ""
            review_request_count = (
                len(proposal.outgoing_review.requests)
                if proposal.outgoing_review is not None
                else 0
            )
            safe_preview_failure = (
                failure.startswith("exact outgoing request review failed:")
                and review_request_count == 0
            ) or (
                failure.startswith("outgoing request origin is not allowed:")
                and review_request_count == 1
            )
            if (
                ActionState(row.state) is not ActionState.SUPERSEDED
                or proposal.kind is not ActionKind.SUBMIT
                or not safe_preview_failure
            ):
                raise ConflictError(
                    "only a superseded zero-request submit can release its effect key"
                )
            if (
                session.scalar(select(ReceiptRow).where(ReceiptRow.action_id == row.id))
                is not None
            ):
                raise ConflictError("a submit receipt already exists")
            if row.effect_key is None:
                return self._action(row)
            row.effect_key = None
            row.version += 1
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.effect_key_released",
                payload={
                    "actor_id": actor_id[:200],
                    "outgoing_request_count": review_request_count,
                },
            )
            session.flush()
            return self._action(row)

    def recover_false_pretransmission_failure(
        self,
        *,
        tenant_id: UUID,
        action_id: UUID,
        receipt: BrowserReceipt,
        actor_id: str,
        authorization_basis: str,
        task_continues: bool = False,
    ) -> BrowserAction:
        """Correct a route-race false negative using concrete external evidence."""
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            proposal = ProposedAction.model_validate(row.proposal)
            prior_failure = row.failure or ""
            if (
                ActionState(row.state) is not ActionState.FAILED
                or proposal.kind is not ActionKind.SUBMIT
                or proposal.outgoing_review is None
                or len(proposal.outgoing_review.requests) != 1
                or not prior_failure.startswith(
                    "exact request was blocked before transmission: "
                    "approved outgoing request was not produced"
                )
            ):
                raise ConflictError(
                    "only the known delayed-dispatch false negative can be recovered"
                )
            if (
                session.scalar(select(ReceiptRow).where(ReceiptRow.action_id == row.id))
                is not None
            ):
                raise ConflictError("a submit receipt already exists")
            session.add(
                ReceiptRow(
                    id=str(uuid4()),
                    tenant_id=str(tenant_id),
                    action_id=row.id,
                    external_id=receipt.external_id,
                    url=receipt.url,
                    evidence_sha256=receipt.evidence_sha256,
                    captured_at=receipt.captured_at,
                )
            )
            row.state = ActionState.SUCCEEDED.value
            row.failure = None
            row.version += 1
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            task.current_ordinal = row.ordinal + 1
            remaining = session.scalar(
                select(ActionRow).where(
                    ActionRow.task_id == row.task_id,
                    ActionRow.ordinal == task.current_ordinal,
                )
            )
            task.status = (
                TaskStatus.QUEUED.value
                if remaining or task_continues
                else TaskStatus.SUCCEEDED.value
            )
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.false_negative_recovered",
                payload={
                    "actor_id": actor_id[:200],
                    "authorization_basis": authorization_basis[:500],
                    "prior_failure": prior_failure[:500],
                    "external_id": receipt.external_id,
                    "evidence_sha256": receipt.evidence_sha256,
                    "url": receipt.url,
                },
            )
            session.flush()
            return self._action(row)

    def mark_outcome_unknown(
        self,
        tenant_id: UUID,
        action_id: UUID,
        reason: str,
    ) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if ActionState(row.state) is not ActionState.DISPATCHING:
                raise ConflictError("only a dispatching action can become unknown")
            metric_labels = _action_metric_labels(row)
            row.state = ActionState.OUTCOME_UNKNOWN.value
            row.failure = reason[:2_000]
            row.version += 1
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            task.status = TaskStatus.AWAITING_RECOVERY.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.outcome_unknown",
                payload={
                    **metric_labels,
                    "reason": reason[:500],
                    "automatic_retry": False,
                },
            )
            session.flush()
            return self._action(row)

    def block_task(
        self,
        tenant_id: UUID,
        task_id: UUID,
        *,
        kind: str,
        reason: str,
        evidence: str,
    ) -> Task:
        """Record an explicit human-handoff state; the loop makes no more progress."""
        with self.session() as session:
            task = self._task_row(session, tenant_id, task_id)
            if TaskStatus(task.status) in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.REJECTED,
                TaskStatus.BLOCKED,
            }:
                raise ConflictError(f"cannot block a task that is {task.status}")
            task.status = TaskStatus.BLOCKED.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=task_id,
                action_id=None,
                kind="task.blocked",
                payload={
                    "challenge": kind,
                    "reason": reason[:500],
                    "evidence": evidence[:500],
                    "automatic_progress": False,
                },
            )
            session.flush()
            return self._task(task)

    def reset_not_committed(
        self,
        *,
        tenant_id: UUID,
        action_id: UUID,
        expected_version: int,
        actor_id: str,
    ) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if row.version != expected_version:
                raise ConflictError("action version changed; reload before resolving")
            if ActionState(row.state) is not ActionState.OUTCOME_UNKNOWN:
                raise ConflictError("action is not awaiting outcome resolution")
            row.state = ActionState.PENDING.value
            row.observation_sha256 = None
            row.observation_url = None
            row.failure = None
            row.version += 1
            task = self._task_row(session, tenant_id, UUID(row.task_id))
            task.status = TaskStatus.QUEUED.value
            task.updated_at = utc_now()
            task.version += 1
            self._append_event(
                session,
                tenant_id=tenant_id,
                task_id=UUID(row.task_id),
                action_id=action_id,
                kind="action.resolved_not_committed",
                payload={"actor_id": actor_id, "requires_new_approval": True},
            )
            session.flush()
            return self._action(row)

    def get_receipt(self, tenant_id: UUID, action_id: UUID) -> BrowserReceipt | None:
        with self.session() as session:
            self._action_row(session, tenant_id, action_id)
            row = session.scalar(
                select(ReceiptRow).where(
                    ReceiptRow.tenant_id == str(tenant_id),
                    ReceiptRow.action_id == str(action_id),
                )
            )
            return self._receipt(row) if row else None

    def latest_approval(self, tenant_id: UUID, action_id: UUID) -> Approval | None:
        with self.session() as session:
            self._action_row(session, tenant_id, action_id)
            row = session.scalar(
                select(ApprovalRow)
                .where(
                    ApprovalRow.tenant_id == str(tenant_id),
                    ApprovalRow.action_id == str(action_id),
                )
                .order_by(ApprovalRow.decided_at.desc())
            )
            return self._approval(row) if row else None

    def events(self, tenant_id: UUID, task_id: UUID) -> list[AuditEvent]:
        with self.session() as session:
            self._task_row(session, tenant_id, task_id)
            rows = session.scalars(
                select(AuditEventRow)
                .where(
                    AuditEventRow.tenant_id == str(tenant_id),
                    AuditEventRow.task_id == str(task_id),
                )
                .order_by(AuditEventRow.sequence)
            ).all()
            return [self._event(row) for row in rows]

    def verify_audit(self, tenant_id: UUID) -> AuditVerification:
        with self.session() as session:
            rows = session.scalars(
                select(AuditEventRow)
                .where(AuditEventRow.tenant_id == str(tenant_id))
                .order_by(AuditEventRow.sequence)
            ).all()
            previous = "0" * 64
            for row in rows:
                material = self._event_material(
                    tenant_id=tenant_id,
                    sequence=row.sequence,
                    task_id=UUID(row.task_id),
                    action_id=UUID(row.action_id) if row.action_id else None,
                    kind=row.kind,
                    payload=row.payload,
                    occurred_at=_as_utc(row.occurred_at),
                    previous_hash=row.previous_hash,
                )
                expected = hashlib.sha256(material.encode()).hexdigest()
                if row.previous_hash != previous or row.event_hash != expected:
                    return AuditVerification(
                        valid=False,
                        event_count=len(rows),
                        head_hash=previous,
                        first_invalid_sequence=row.sequence,
                    )
                previous = row.event_hash
            ledger = session.get(TenantLedgerRow, str(tenant_id))
            if ledger is not None and (
                ledger.sequence != len(rows) or ledger.head_hash != previous
            ):
                return AuditVerification(
                    valid=False,
                    event_count=len(rows),
                    head_hash=previous,
                    first_invalid_sequence=len(rows) + 1,
                )
            return AuditVerification(
                valid=True,
                event_count=len(rows),
                head_hash=previous,
            )

    def create_demo_order(
        self,
        *,
        reference: str,
        product: str,
        quantity: int,
    ) -> tuple[str, bool]:
        with self.session() as session:
            existing = session.scalar(
                select(DemoOrderRow).where(DemoOrderRow.reference == reference)
            )
            if existing:
                existing.duplicate_attempts += 1
                return existing.id, False
            row = DemoOrderRow(
                id=str(uuid4()),
                reference=reference,
                product=product,
                quantity=quantity,
                duplicate_attempts=0,
                created_at=utc_now(),
            )
            session.add(row)
            session.flush()
            return row.id, True

    def demo_order(self, reference: str) -> dict[str, Any] | None:
        with self.session() as session:
            row = session.scalar(
                select(DemoOrderRow).where(DemoOrderRow.reference == reference)
            )
            if not row:
                return None
            return {
                "id": row.id,
                "reference": row.reference,
                "product": row.product,
                "quantity": row.quantity,
                "duplicate_attempts": row.duplicate_attempts,
                "created_at": _as_utc(row.created_at).isoformat(),
            }

    def demo_orders(self) -> list[dict[str, Any]]:
        with self.session() as session:
            rows = session.scalars(
                select(DemoOrderRow).order_by(DemoOrderRow.created_at)
            ).all()
            return [
                {
                    "id": row.id,
                    "reference": row.reference,
                    "product": row.product,
                    "quantity": row.quantity,
                    "duplicate_attempts": row.duplicate_attempts,
                }
                for row in rows
            ]

    def create_demo_job_application(
        self,
        *,
        reference: str,
        job_slug: str,
        full_name: str,
        email: str,
        country: str,
        work_authorization: str,
        years_python: int,
        resume_summary: str,
        resume_filename: str,
        resume_sha256: str,
        cover_note: str,
    ) -> tuple[str, bool]:
        with self.session() as session:
            existing = session.scalar(
                select(DemoJobApplicationRow).where(
                    DemoJobApplicationRow.reference == reference
                )
            )
            if existing:
                existing.duplicate_attempts += 1
                return existing.id, False
            row = DemoJobApplicationRow(
                id=str(uuid4()),
                reference=reference,
                job_slug=job_slug,
                full_name=full_name,
                email=email,
                country=country,
                work_authorization=work_authorization,
                years_python=years_python,
                resume_summary=resume_summary,
                resume_filename=resume_filename,
                resume_sha256=resume_sha256,
                cover_note=cover_note,
                duplicate_attempts=0,
                created_at=utc_now(),
            )
            session.add(row)
            session.flush()
            return row.id, True

    def demo_job_application(self, reference: str) -> dict[str, Any] | None:
        with self.session() as session:
            row = session.scalar(
                select(DemoJobApplicationRow).where(
                    DemoJobApplicationRow.reference == reference
                )
            )
            return self._demo_job_application(row) if row else None

    def demo_job_applications(self) -> list[dict[str, Any]]:
        with self.session() as session:
            rows = session.scalars(
                select(DemoJobApplicationRow).order_by(DemoJobApplicationRow.created_at)
            ).all()
            return [self._demo_job_application(row) for row in rows]

    def _append_event(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        task_id: UUID,
        action_id: UUID | None,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        ledger = session.scalar(
            select(TenantLedgerRow)
            .where(TenantLedgerRow.tenant_id == str(tenant_id))
            .with_for_update()
        )
        if ledger is None:
            ledger = TenantLedgerRow(
                tenant_id=str(tenant_id),
                sequence=0,
                head_hash="0" * 64,
            )
            session.add(ledger)
            session.flush()
        sequence = ledger.sequence + 1
        previous_hash = ledger.head_hash
        occurred_at = utc_now()
        material = self._event_material(
            tenant_id=tenant_id,
            sequence=sequence,
            task_id=task_id,
            action_id=action_id,
            kind=kind,
            payload=payload,
            occurred_at=occurred_at,
            previous_hash=previous_hash,
        )
        event_hash = hashlib.sha256(material.encode()).hexdigest()
        session.add(
            AuditEventRow(
                id=str(uuid4()),
                tenant_id=str(tenant_id),
                sequence=sequence,
                task_id=str(task_id),
                action_id=str(action_id) if action_id else None,
                kind=kind,
                payload=payload,
                occurred_at=occurred_at,
                previous_hash=previous_hash,
                event_hash=event_hash,
            )
        )
        session.info.setdefault(_PENDING_METRICS_KEY, []).append(
            CommittedAuditTransition(
                kind=kind,
                action_id=str(action_id) if action_id else None,
                occurred_at=occurred_at,
                payload=dict(payload),
            )
        )
        ledger.sequence = sequence
        ledger.head_hash = event_hash

    @staticmethod
    def _event_material(
        *,
        tenant_id: UUID,
        sequence: int,
        task_id: UUID,
        action_id: UUID | None,
        kind: str,
        payload: dict[str, Any],
        occurred_at: datetime,
        previous_hash: str,
    ) -> str:
        return canonical_json(
            {
                "tenant_id": str(tenant_id),
                "sequence": sequence,
                "task_id": str(task_id),
                "action_id": str(action_id) if action_id else None,
                "kind": kind,
                "payload": payload,
                "occurred_at": occurred_at.isoformat(),
                "previous_hash": previous_hash,
            }
        )

    @staticmethod
    def _task(row: TaskRow) -> Task:
        return Task(
            id=UUID(row.id),
            tenant_id=UUID(row.tenant_id),
            instruction=row.instruction,
            start_url=row.start_url,
            provider=row.provider,
            profile_id=UUID(row.profile_id) if row.profile_id else None,
            document_sha256=row.document_sha256,
            autonomy=AutonomyScope.model_validate(row.autonomy_scope or {}),
            status=TaskStatus(row.status),
            current_ordinal=row.current_ordinal,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
            version=row.version,
            lease_owner=row.lease_owner,
            lease_expires_at=(
                _as_utc(row.lease_expires_at) if row.lease_expires_at else None
            ),
        )

    @staticmethod
    def _mission(row: MissionRow) -> Mission:
        return Mission(
            id=UUID(row.id),
            tenant_id=UUID(row.tenant_id),
            query=row.query,
            provider=row.provider,
            plan_summary=row.plan_summary,
            external_commit_authorized=bool(row.external_commit_authorized),
            max_external_commits=row.max_external_commits,
            status=MissionStatus(row.status),
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
            version=row.version,
            lease_owner=row.lease_owner,
            lease_expires_at=(
                _as_utc(row.lease_expires_at) if row.lease_expires_at else None
            ),
        )

    @staticmethod
    def _mission_step(row: MissionStepRow) -> MissionStep:
        return MissionStep(
            id=UUID(row.id),
            mission_id=UUID(row.mission_id),
            tenant_id=UUID(row.tenant_id),
            ordinal=row.ordinal,
            key=row.key,
            kind=MissionStepKind(row.kind),
            instruction=row.instruction,
            depends_on=tuple(row.depends_on or ()),
            status=MissionStepStatus(row.status),
            child_task_id=UUID(row.child_task_id) if row.child_task_id else None,
            output=row.output,
            output_sha256=row.output_sha256,
            error=row.error,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
            version=row.version,
        )

    @staticmethod
    def _profile(row: FactualProfileRow) -> FactualProfile:
        return FactualProfile(
            id=UUID(row.id),
            tenant_id=UUID(row.tenant_id),
            name=row.name,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
            version=row.version,
        )

    @staticmethod
    def _profile_answer(row: ProfileAnswerRow) -> ProfileAnswer:
        return ProfileAnswer(
            id=UUID(row.id),
            tenant_id=UUID(row.tenant_id),
            profile_id=UUID(row.profile_id),
            field_name=row.field_name,
            value=row.value,
            source=AnswerSource(
                kind=AnswerSourceKind(row.source_kind),
                reference=row.source_reference,
            ),
            sensitivity=AnswerSensitivity(row.sensitivity),
            verification_state=VerificationState(row.verification_state),
            verified_by=row.verified_by,
            verified_at=_as_utc(row.verified_at) if row.verified_at else None,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
            version=row.version,
        )

    @staticmethod
    def _action(row: ActionRow) -> BrowserAction:
        return BrowserAction(
            id=UUID(row.id),
            tenant_id=UUID(row.tenant_id),
            task_id=UUID(row.task_id),
            ordinal=row.ordinal,
            proposal=ProposedAction.model_validate(row.proposal),
            state=ActionState(row.state),
            risk=RiskClass(row.risk) if row.risk else None,
            action_sha256=row.action_sha256,
            observation_sha256=row.observation_sha256,
            observation_url=row.observation_url,
            failure=row.failure,
            version=row.version,
        )

    @staticmethod
    def _approval(row: ApprovalRow) -> Approval:
        return Approval(
            id=UUID(row.id),
            tenant_id=UUID(row.tenant_id),
            action_id=UUID(row.action_id),
            decision=ApprovalDecision(row.decision),
            actor_id=row.actor_id,
            action_sha256=row.action_sha256,
            observation_sha256=row.observation_sha256,
            payload_sha256=row.payload_sha256,
            decided_at=_as_utc(row.decided_at),
        )

    @staticmethod
    def _receipt(row: ReceiptRow) -> BrowserReceipt:
        return BrowserReceipt(
            external_id=row.external_id,
            url=row.url,
            evidence_sha256=row.evidence_sha256,
            captured_at=_as_utc(row.captured_at),
        )

    @staticmethod
    def _event(row: AuditEventRow) -> AuditEvent:
        return AuditEvent(
            id=UUID(row.id),
            tenant_id=UUID(row.tenant_id),
            sequence=row.sequence,
            task_id=UUID(row.task_id),
            action_id=UUID(row.action_id) if row.action_id else None,
            kind=row.kind,
            payload=row.payload,
            occurred_at=_as_utc(row.occurred_at),
            previous_hash=row.previous_hash,
            event_hash=row.event_hash,
        )

    @staticmethod
    def _demo_job_application(row: DemoJobApplicationRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "reference": row.reference,
            "job_slug": row.job_slug,
            "full_name": row.full_name,
            "email": row.email,
            "country": row.country,
            "work_authorization": row.work_authorization,
            "years_python": row.years_python,
            "resume_summary": row.resume_summary,
            "resume_filename": row.resume_filename,
            "resume_sha256": row.resume_sha256,
            "cover_note": row.cover_note,
            "duplicate_attempts": row.duplicate_attempts,
            "created_at": _as_utc(row.created_at).isoformat(),
        }

    @staticmethod
    def _task_row(session: Session, tenant_id: UUID, task_id: UUID) -> TaskRow:
        row = session.scalar(
            select(TaskRow).where(
                TaskRow.id == str(task_id),
                TaskRow.tenant_id == str(tenant_id),
            )
        )
        if row is None:
            raise NotFoundError("task not found")
        return row

    @staticmethod
    def _mission_row(
        session: Session,
        tenant_id: UUID,
        mission_id: UUID,
    ) -> MissionRow:
        row = session.scalar(
            select(MissionRow).where(
                MissionRow.id == str(mission_id),
                MissionRow.tenant_id == str(tenant_id),
            )
        )
        if row is None:
            raise NotFoundError("mission not found")
        return row

    @staticmethod
    def _mission_step_row(
        session: Session,
        tenant_id: UUID,
        mission_id: UUID,
        step_id: UUID,
    ) -> MissionStepRow:
        row = session.scalar(
            select(MissionStepRow).where(
                MissionStepRow.id == str(step_id),
                MissionStepRow.mission_id == str(mission_id),
                MissionStepRow.tenant_id == str(tenant_id),
            )
        )
        if row is None:
            raise NotFoundError("mission step not found")
        return row

    @staticmethod
    def _profile_row(
        session: Session,
        tenant_id: UUID,
        profile_id: UUID,
    ) -> FactualProfileRow:
        row = session.scalar(
            select(FactualProfileRow).where(
                FactualProfileRow.id == str(profile_id),
                FactualProfileRow.tenant_id == str(tenant_id),
            )
        )
        if row is None:
            raise NotFoundError("profile not found")
        return row

    @staticmethod
    def _locked_profile_row(
        session: Session,
        tenant_id: UUID,
        profile_id: UUID,
    ) -> FactualProfileRow:
        row = session.scalar(
            select(FactualProfileRow)
            .where(
                FactualProfileRow.id == str(profile_id),
                FactualProfileRow.tenant_id == str(tenant_id),
            )
            .with_for_update()
        )
        if row is None:
            raise NotFoundError("profile not found")
        return row

    @staticmethod
    def _action_row(session: Session, tenant_id: UUID, action_id: UUID) -> ActionRow:
        row = session.scalar(
            select(ActionRow).where(
                ActionRow.id == str(action_id),
                ActionRow.tenant_id == str(tenant_id),
            )
        )
        if row is None:
            raise NotFoundError("action not found")
        return row

    @staticmethod
    def _locked_action_row(
        session: Session,
        tenant_id: UUID,
        action_id: UUID,
    ) -> ActionRow:
        row = session.scalar(
            select(ActionRow)
            .where(
                ActionRow.id == str(action_id),
                ActionRow.tenant_id == str(tenant_id),
            )
            .with_for_update()
        )
        if row is None:
            raise NotFoundError("action not found")
        return row


def _action_metric_labels(row: ActionRow) -> dict[str, str]:
    """Expose only executor enums; proposal content never becomes a metric label."""
    proposal = ProposedAction.model_validate(row.proposal)
    return {
        "action_kind": proposal.kind.value,
        "risk": RiskClass(row.risk).value,
    }


def _redacted_audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Allow only hashes, bounded enums, counters, and booleans into CLI output."""
    safe: dict[str, Any] = {}
    redacted: list[str] = []
    action_kinds = {item.value for item in ActionKind}
    step_kinds = {item.value for item in MissionStepKind}
    risks = {item.value for item in RiskClass}
    for key, value in sorted(payload.items()):
        accepted: Any | None = None
        if key.endswith("_sha256"):
            accepted = _redacted_hash(value)
        elif key == "action_kind" and isinstance(value, str) and value in action_kinds:
            accepted = value
        elif key == "kind" and isinstance(value, str) and value in step_kinds:
            accepted = value
        elif key == "risk" and isinstance(value, str) and value in risks:
            accepted = value
        elif key == "step_key" and isinstance(value, str):
            accepted = _redacted_step_key(value)
        elif key in _AUDIT_BOOLEAN_FIELDS and type(value) is bool:
            accepted = value
        elif key in _AUDIT_INTEGER_FIELDS and type(value) is int:
            accepted = value
        if accepted is None:
            redacted.append(key)
        else:
            safe[key] = accepted
    if redacted:
        safe["redacted_fields"] = redacted
    return safe


def _redacted_hash(value: object) -> str | None:
    return value if isinstance(value, str) and _AUDIT_HASH.fullmatch(value) else None


def _redacted_step_key(value: str) -> str:
    return value if _AUDIT_STEP_KEY.fullmatch(value) else "<redacted>"


def _redacted_event_kind(value: str) -> str:
    return value if _AUDIT_EVENT_KIND.fullmatch(value) else "<redacted>"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
