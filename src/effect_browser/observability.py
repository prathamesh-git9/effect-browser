"""Process-local telemetry derived from successfully committed audit transitions.

These metrics are operational signals, not a second source of truth. A process can
die after the database commit and before the in-memory collector observes it, and a
restart cannot recover an in-flight duration. The hash-chained audit ledger remains
the authoritative record for correctness and reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram

from effect_browser.domain import (
    ActionKind,
    ActionState,
    MissionStepKind,
    MissionStepStatus,
    RiskClass,
)

_ACTION_KINDS = frozenset(item.value for item in ActionKind)
_ACTION_STATES = frozenset(item.value for item in ActionState)
_MISSION_STEP_KINDS = frozenset(item.value for item in MissionStepKind)
_MISSION_STEP_STATUSES = frozenset(item.value for item in MissionStepStatus)
_RISK_CLASSES = frozenset(item.value for item in RiskClass)


@dataclass(frozen=True)
class CommittedAuditTransition:
    """Minimal safe metadata delivered only after its transaction commits."""

    kind: str
    action_id: str | None
    occurred_at: datetime
    payload: dict[str, Any]


class OperationalMetrics:
    """Observe bounded transition labels without retaining application content."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self.mission_step_transitions = Counter(
            "effect_browser_mission_step_transitions_total",
            "Committed mission-step state transitions (operational, not audit).",
            ("kind", "status"),
            registry=registry,
        )
        self.mission_step_duration = Histogram(
            "effect_browser_mission_step_duration_seconds",
            "Observed in-process mission-step duration between committed transitions.",
            ("kind", "status"),
            registry=registry,
        )
        self.browser_action_transitions = Counter(
            "effect_browser_browser_action_transitions_total",
            "Committed browser-action state transitions (operational, not audit).",
            ("kind", "risk", "status"),
            registry=registry,
        )
        self.browser_action_duration = Histogram(
            "effect_browser_browser_action_duration_seconds",
            (
                "Observed in-process browser dispatch duration between committed "
                "transitions."
            ),
            ("kind", "risk", "status"),
            registry=registry,
        )
        self.external_commit_dispatch_attempts = Counter(
            "effect_browser_external_commit_dispatch_attempts_total",
            "Committed dispatch transitions for externally committing actions.",
            ("kind",),
            registry=registry,
        )
        self.outcome_unknown_transitions = Counter(
            "effect_browser_outcome_unknown_transitions_total",
            "Committed transitions to OUTCOME_UNKNOWN with automatic retry disabled.",
            ("kind",),
            registry=registry,
        )
        self._mission_starts: dict[str, tuple[datetime, str]] = {}
        self._action_starts: dict[str, tuple[datetime, str, str]] = {}
        self._lock = Lock()

    def observe_committed(self, event: CommittedAuditTransition) -> None:
        """Record one event using enum-only labels; ignore legacy incomplete events."""
        if event.kind.startswith("mission.step_"):
            self._observe_mission_step(event)
        elif event.kind.startswith("action."):
            self._observe_browser_action(event)

    def _observe_mission_step(self, event: CommittedAuditTransition) -> None:
        status_by_event = {
            "mission.step_started": MissionStepStatus.RUNNING.value,
            "mission.step_completed": MissionStepStatus.SUCCEEDED.value,
            "mission.step_blocked": MissionStepStatus.BLOCKED.value,
            "mission.step_failed": MissionStepStatus.FAILED.value,
            "mission.step_skipped": MissionStepStatus.SKIPPED.value,
            "mission.step_reopened": MissionStepStatus.PENDING.value,
        }
        status = status_by_event.get(event.kind)
        step_kind = _bounded(event.payload.get("kind"), _MISSION_STEP_KINDS)
        if status is None or step_kind is None or event.action_id is None:
            return
        if status not in _MISSION_STEP_STATUSES:  # pragma: no cover - fixed mapping
            return
        self.mission_step_transitions.labels(step_kind, status).inc()
        with self._lock:
            if status == MissionStepStatus.RUNNING.value:
                self._mission_starts[event.action_id] = (event.occurred_at, step_kind)
                return
            if status == MissionStepStatus.PENDING.value:
                self._mission_starts.pop(event.action_id, None)
                return
            started = self._mission_starts.pop(event.action_id, None)
        if started is not None and started[1] == step_kind:
            duration = max(0.0, (event.occurred_at - started[0]).total_seconds())
            self.mission_step_duration.labels(step_kind, status).observe(duration)

    def _observe_browser_action(self, event: CommittedAuditTransition) -> None:
        status_by_event = {
            "action.dispatching": ActionState.DISPATCHING.value,
            "action.succeeded": ActionState.SUCCEEDED.value,
            "action.failed": ActionState.FAILED.value,
            "action.outcome_unknown": ActionState.OUTCOME_UNKNOWN.value,
        }
        status = status_by_event.get(event.kind)
        action_kind = _bounded(event.payload.get("action_kind"), _ACTION_KINDS)
        risk = _bounded(event.payload.get("risk"), _RISK_CLASSES)
        if (
            status is None
            or status not in _ACTION_STATES
            or action_kind is None
            or risk is None
            or event.action_id is None
        ):
            return
        self.browser_action_transitions.labels(action_kind, risk, status).inc()
        if status == ActionState.DISPATCHING.value:
            if risk == RiskClass.EXTERNAL_COMMIT.value:
                self.external_commit_dispatch_attempts.labels(action_kind).inc()
            with self._lock:
                self._action_starts[event.action_id] = (
                    event.occurred_at,
                    action_kind,
                    risk,
                )
            return
        if status == ActionState.OUTCOME_UNKNOWN.value:
            self.outcome_unknown_transitions.labels(action_kind).inc()
        with self._lock:
            started = self._action_starts.pop(event.action_id, None)
        if started is not None and started[1:] == (action_kind, risk):
            duration = max(0.0, (event.occurred_at - started[0]).total_seconds())
            self.browser_action_duration.labels(action_kind, risk, status).observe(
                duration
            )


def _bounded(value: object, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


operational_metrics = OperationalMetrics()
