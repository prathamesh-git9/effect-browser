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


class AnswerSourceKind(StrEnum):
    USER = "user"
    RESUME = "resume"
    DOCUMENT = "document"


class AnswerSensitivity(StrEnum):
    STANDARD = "standard"
    PERSONAL = "personal"
    CONSEQUENTIAL = "consequential"


class VerificationState(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"


class AnswerSource(DomainModel):
    kind: AnswerSourceKind
    reference: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_document_reference(self) -> AnswerSource:
        if self.kind is not AnswerSourceKind.USER and self.reference is None:
            raise ValueError("resume and document sources require a reference")
        return self


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
        description="Use only when label, role, name, and selector are null.",
    )
    selector: str | None = Field(
        default=None,
        description="Candidate-bound CSS selector; all other strategies must be null.",
    )
    adaptive_id: str | None = Field(
        default=None,
        description="Scrapling relocation key associated with a selector strategy.",
    )

    @model_validator(mode="after")
    def exactly_one_strategy(self) -> Locator:
        strategies = [self.label, self.test_id, self.role, self.selector]
        if sum(item is not None for item in strategies) != 1:
            raise ValueError(
                "locator requires exactly one of label, test_id, role, or selector"
            )
        if self.role and not self.name:
            raise ValueError("role locator requires an accessible name")
        if not self.role and self.name:
            raise ValueError("accessible name is valid only with a role locator")
        if self.adaptive_id and not self.selector:
            raise ValueError("adaptive_id requires a selector locator")
        return self


class ReconciliationSpec(DomainModel):
    url: str
    expected_text: str = Field(min_length=1)
    external_reference: str = Field(min_length=1)
    receipt_test_id: str | None = None


class ReviewField(DomainModel):
    candidate_id: str
    label: str
    value: str
    source_action_sha256: str | None = None


class OutgoingReview(DomainModel):
    fields: tuple[ReviewField, ...] = ()
    document_sha256s: tuple[str, ...] = ()
    observation_sha256: str
    payload_sha256: str

    @model_validator(mode="after")
    def verify_payload_hash(self) -> OutgoingReview:
        expected = digest(
            {
                "fields": [field.model_dump(mode="json") for field in self.fields],
                "document_sha256s": list(self.document_sha256s),
                "observation_sha256": self.observation_sha256,
            }
        )
        if self.payload_sha256 != expected:
            raise ValueError("outgoing review payload hash does not match its contents")
        return self


class ProposedAction(DomainModel):
    kind: ActionKind
    locator: Locator | None = None
    url: str | None = None
    value: str | None = None
    description: str = Field(min_length=1, max_length=500)
    effect_key: str | None = None
    expected_outcome: str | None = None
    reconciliation: ReconciliationSpec | None = None
    planned_from_sha256: str | None = None
    target_interaction: str | None = None
    target_name: str | None = None
    outgoing_review: OutgoingReview | None = None

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
        elif self.outgoing_review is not None:
            raise ValueError("outgoing review is valid only for submit actions")
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


class ElementCandidate(DomainModel):
    id: str
    tag: str
    role: str
    name: str
    input_type: str | None = None
    required: bool = False
    disabled: bool = False
    filled: bool = False
    current_value: str | None = None
    href: str | None = None
    options: tuple[str, ...] = ()
    interaction: str
    locator: Locator


class SubmissionContract(DomainModel):
    url_template: str
    expected_text_template: str
    receipt_test_id: str | None = None


class PageSnapshot(DomainModel):
    url: str
    title: str
    state_sha256: str
    text_excerpt: str
    candidates: tuple[ElementCandidate, ...]
    submission_contract: SubmissionContract | None = None
    captured_at: datetime


class StepRequest(DomainModel):
    task_id: UUID
    instruction: str
    start_url: str
    step_number: int = Field(ge=1, le=30)
    effect_reference: str
    previous_actions: tuple[str, ...]
    snapshot: PageSnapshot


class StepChoice(DomainModel):
    kind: ActionKind
    candidate_id: str | None = None
    value: str | None = None
    url: str | None = None
    description: str = Field(min_length=1, max_length=500)
    expected_outcome: str | None = None

    @model_validator(mode="after")
    def validate_choice(self) -> StepChoice:
        if self.kind is ActionKind.NAVIGATE and not self.url:
            raise ValueError("navigate choice requires url")
        if self.kind in {ActionKind.FILL, ActionKind.CLICK, ActionKind.SUBMIT}:
            if not self.candidate_id:
                raise ValueError(f"{self.kind.value} choice requires candidate_id")
        if self.kind is ActionKind.FILL and self.value is None:
            raise ValueError("fill choice requires value")
        if self.kind is ActionKind.SUBMIT and not self.expected_outcome:
            raise ValueError("submit choice requires expected_outcome")
        if self.kind is ActionKind.FINISH and any(
            (self.candidate_id, self.value, self.url)
        ):
            raise ValueError("finish choice cannot target a candidate or URL")
        return self


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


class FactualProfile(DomainModel):
    id: UUID
    tenant_id: UUID
    name: str = Field(min_length=1, max_length=120)
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)


class ProfileAnswer(DomainModel):
    id: UUID
    tenant_id: UUID
    profile_id: UUID
    field_name: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    value: str = Field(min_length=1, max_length=10_000)
    source: AnswerSource
    sensitivity: AnswerSensitivity
    verification_state: VerificationState
    verified_by: str | None = None
    verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)

    @model_validator(mode="after")
    def verification_metadata_matches_state(self) -> ProfileAnswer:
        metadata_present = self.verified_by is not None or self.verified_at is not None
        if self.verification_state is VerificationState.VERIFIED and (
            self.verified_by is None or self.verified_at is None
        ):
            raise ValueError("verified answers require verifier metadata")
        if self.verification_state is VerificationState.UNVERIFIED and metadata_present:
            raise ValueError("unverified answers cannot retain verifier metadata")
        return self


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
