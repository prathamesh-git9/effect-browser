from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_RECOVERY = "awaiting_recovery"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class ActionKind(StrEnum):
    NAVIGATE = "navigate"
    FILL = "fill"
    CLICK = "click"
    SUBMIT = "submit"
    FINISH = "finish"


class ActionState(StrEnum):
    PENDING = "pending"
    APPROVAL_REQUIRED = "approval_required"
    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    REJECTED = "rejected"
    INVALIDATED = "invalidated"


class RiskClass(StrEnum):
    READ = "read"
    INPUT = "input"
    EXTERNAL_COMMIT = "external_commit"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class Resolution(StrEnum):
    SUCCEEDED = "succeeded"
    NOT_COMMITTED = "not_committed"


class Locator(DomainModel):
    role: str | None = Field(
        default=None,
        description="Use only with name; label and test_id must be null.",
    )
    name: str | None = Field(
        default=None,
        description="Accessible name used only with role; otherwise null.",
    )
    label: str | None = Field(
        default=None,
        description="Preferred strategy; role, name, and test_id must be null.",
    )
    test_id: str | None = Field(
        default=None,
        description="Use only when label, role, and name are null.",
    )

    @model_validator(mode="after")
    def exactly_one_strategy(self) -> Locator:
        strategies = [self.label, self.test_id, self.role]
        if sum(item is not None for item in strategies) != 1:
            raise ValueError("locator requires exactly one of label, test_id, or role")
        if self.role and not self.name:
            raise ValueError("role locator requires an accessible name")
        return self


class ReconciliationSpec(DomainModel):
    url: str
    expected_text: str = Field(min_length=1)
    external_reference: str = Field(min_length=1)
    receipt_test_id: str | None = None


class ProposedAction(DomainModel):
    kind: ActionKind
    locator: Locator | None = None
    url: str | None = None
    value: str | None = None
    description: str = Field(min_length=1, max_length=500)
    effect_key: str | None = None
    expected_outcome: str | None = None
    reconciliation: ReconciliationSpec | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> ProposedAction:
        if self.kind is ActionKind.NAVIGATE and not self.url:
            raise ValueError("navigate requires url")
        if self.kind in {ActionKind.FILL, ActionKind.CLICK, ActionKind.SUBMIT}:
            if self.locator is None:
                raise ValueError(f"{self.kind.value} requires locator")
        if self.kind is ActionKind.FILL and self.value is None:
            raise ValueError("fill requires value")
        if self.kind is ActionKind.SUBMIT:
            if not self.effect_key or not self.expected_outcome:
                raise ValueError("submit requires effect_key and expected_outcome")
        return self

    def action_hash(self) -> str:
        return digest(self.model_dump(mode="json"))


class PlanRequest(DomainModel):
    task_id: UUID
    instruction: str = Field(min_length=1, max_length=4_000)
    start_url: str


class Observation(DomainModel):
    url: str
    title: str
    state_sha256: str
    captured_at: datetime
    screenshot_path: str | None = None


class BrowserReceipt(DomainModel):
    external_id: str
    url: str
    evidence_sha256: str
    captured_at: datetime


class PolicyDecision(DomainModel):
    allowed: bool
    risk: RiskClass
    requires_approval: bool
    reason: str


class Task(DomainModel):
    id: UUID
    tenant_id: UUID
    instruction: str
    start_url: str
    provider: str
    status: TaskStatus
    current_ordinal: int
    created_at: datetime
    updated_at: datetime
    version: int
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None


class BrowserAction(DomainModel):
    id: UUID
    tenant_id: UUID
    task_id: UUID
    ordinal: int
    proposal: ProposedAction
    state: ActionState
    risk: RiskClass | None = None
    action_sha256: str
    observation_sha256: str | None = None
    observation_url: str | None = None
    failure: str | None = None
    version: int


class Approval(DomainModel):
    id: UUID
    tenant_id: UUID
    action_id: UUID
    decision: ApprovalDecision
    actor_id: str
    action_sha256: str
    observation_sha256: str
    decided_at: datetime


class AuditEvent(DomainModel):
    id: UUID
    tenant_id: UUID
    sequence: int
    task_id: UUID
    action_id: UUID | None
    kind: str
    payload: dict[str, Any]
    occurred_at: datetime
    previous_hash: str
    event_hash: str


class AuditVerification(DomainModel):
    valid: bool
    event_count: int
    head_hash: str
    first_invalid_sequence: int | None = None


class RunResult(DomainModel):
    task: Task
    next_action: BrowserAction | None = None
    message: str
