from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
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
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from effect_browser.domain import (
    ActionState,
    Approval,
    ApprovalDecision,
    AuditEvent,
    AuditVerification,
    BrowserAction,
    BrowserReceipt,
    Observation,
    PolicyDecision,
    ProposedAction,
    RiskClass,
    Task,
    TaskStatus,
    canonical_json,
    utc_now,
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
    status: Mapped[str] = mapped_column(String(40), index=True)
    current_ordinal: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String(100), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


class DemoOrderRow(Base):
    __tablename__ = "demo_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reference: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    product: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column(Integer)
    duplicate_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoreError(RuntimeError):
    pass


class NotFoundError(StoreError):
    pass


class ConflictError(StoreError):
    pass


class DatabaseStore:
    def __init__(self, database_url: str) -> None:
        connect_args = (
            {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        )
        self.engine = create_engine(database_url, connect_args=connect_args)
        self._session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session()
        try:
            yield session
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise ConflictError("database uniqueness conflict") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def reset(self) -> None:
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)

    def create_task(
        self,
        *,
        task_id: UUID,
        tenant_id: UUID,
        instruction: str,
        start_url: str,
        provider: str,
        actions: tuple[ProposedAction, ...],
    ) -> Task:
        now = utc_now()
        with self.session() as session:
            task = TaskRow(
                id=str(task_id),
                tenant_id=str(tenant_id),
                instruction=instruction,
                start_url=start_url,
                provider=provider,
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
                payload={"provider": provider, "action_count": len(actions)},
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

    def approve_action(
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
                raise ConflictError("action version changed; reload before approving")
            if ActionState(row.state) is not ActionState.APPROVAL_REQUIRED:
                raise ConflictError("action is not awaiting approval")
            if not row.observation_sha256:
                raise ConflictError("action has no bound observation")
            approval = ApprovalRow(
                id=str(uuid4()),
                tenant_id=str(tenant_id),
                action_id=row.id,
                decision=ApprovalDecision.APPROVED.value,
                actor_id=actor_id,
                action_sha256=row.action_sha256,
                observation_sha256=row.observation_sha256,
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
            session.add(
                ApprovalRow(
                    id=str(uuid4()),
                    tenant_id=str(tenant_id),
                    action_id=row.id,
                    decision=ApprovalDecision.REJECTED.value,
                    actor_id=actor_id,
                    action_sha256=row.action_sha256,
                    observation_sha256=row.observation_sha256 or "",
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
                payload={"actor_id": actor_id, "action_sha256": row.action_sha256},
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
                },
            )
            session.flush()
            return self._action(row)

    def start_dispatch(self, tenant_id: UUID, action_id: UUID) -> BrowserAction:
        with self.session() as session:
            row = self._locked_action_row(session, tenant_id, action_id)
            if ActionState(row.state) is not ActionState.PREPARED:
                raise ConflictError("only a prepared action can dispatch")
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
                    "effect_key": ProposedAction.model_validate(row.proposal).effect_key,
                },
            )
            session.flush()
            return self._action(row)

    def complete_action(
        self,
        tenant_id: UUID,
        action_id: UUID,
        receipt: BrowserReceipt,
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
                TaskStatus.QUEUED.value if remaining else TaskStatus.SUCCEEDED.value
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
                payload={"failure": failure[:500]},
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
                payload={"reason": reason[:500], "automatic_retry": False},
            )
            session.flush()
            return self._action(row)

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


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
