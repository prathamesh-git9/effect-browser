from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from effect_browser.domain import (
    ActionKind,
    PolicyDecision,
    ProposedAction,
    RiskClass,
)
from effect_browser.uploads import UploadGuard, UploadValidationError

SENSITIVE_LABELS = {
    "password",
    "passcode",
    "one-time code",
    "otp",
    "credit card",
    "card number",
    "security code",
    "api key",
    "pin",
    "secret",
    "cvv",
}


class ActionPolicy:
    def __init__(
        self,
        allowed_origins: tuple[str, ...],
        allowed_upload_roots: tuple[Path, ...] = (),
    ) -> None:
        self.allowed_origins = {item.rstrip("/") for item in allowed_origins}
        self.upload_guard = UploadGuard(allowed_upload_roots)

    def evaluate(self, action: ProposedAction, current_url: str) -> PolicyDecision:
        target_url = action.url or current_url
        if target_url and not self._origin_allowed(target_url):
            return PolicyDecision(
                allowed=False,
                risk=RiskClass.READ,
                requires_approval=False,
                reason=f"origin is not allowed: {self._origin(target_url)}",
            )
        if action.kind is ActionKind.FILL:
            locator_text = " ".join(
                filter(
                    None,
                    (
                        action.locator.label if action.locator else None,
                        action.locator.name if action.locator else None,
                    ),
                )
            ).casefold()
            if any(token in locator_text for token in SENSITIVE_LABELS):
                return PolicyDecision(
                    allowed=False,
                    risk=RiskClass.INPUT,
                    requires_approval=False,
                    reason="MVP blocks credential, payment, and secret entry",
                )
            return PolicyDecision(
                allowed=True,
                risk=RiskClass.INPUT,
                requires_approval=False,
                reason="non-sensitive form preparation is reversible",
            )
        if action.kind is ActionKind.UPLOAD:
            try:
                self.upload_guard.validate(
                    action.file_path or Path(),
                    action.document_sha256 or "",
                )
            except UploadValidationError as exc:
                return PolicyDecision(
                    allowed=False,
                    risk=RiskClass.INPUT,
                    requires_approval=False,
                    reason=str(exc),
                )
            return PolicyDecision(
                allowed=True,
                risk=RiskClass.EXTERNAL_COMMIT,
                requires_approval=True,
                reason=(
                    "file selection may transmit immediately; operator approval "
                    "binds the allowlisted content hash and current page"
                ),
            )
        if action.kind is ActionKind.CLICK:
            if action.target_interaction == "navigation":
                return PolicyDecision(
                    allowed=True,
                    risk=RiskClass.READ,
                    requires_approval=False,
                    reason="candidate-bound link navigation is read-only",
                )
            if action.target_interaction == "ambiguous":
                return PolicyDecision(
                    allowed=True,
                    risk=RiskClass.EXTERNAL_COMMIT,
                    requires_approval=True,
                    reason="ambiguous candidate-bound click requires operator approval",
                )
            return PolicyDecision(
                allowed=False,
                risk=RiskClass.EXTERNAL_COMMIT,
                requires_approval=False,
                reason=(
                    "generic click semantics are ambiguous; use navigate for reads or "
                    "submit with an effect contract"
                ),
            )
        if action.kind is ActionKind.SUBMIT:
            if (
                action.outgoing_review is None
                or len(action.outgoing_review.requests) != 1
            ):
                return PolicyDecision(
                    allowed=False,
                    risk=RiskClass.EXTERNAL_COMMIT,
                    requires_approval=False,
                    reason="submit is missing one exact outgoing request review",
                )
            if (
                action.planned_from_sha256
                and action.outgoing_review.observation_sha256
                != action.planned_from_sha256
            ):
                return PolicyDecision(
                    allowed=False,
                    risk=RiskClass.EXTERNAL_COMMIT,
                    requires_approval=False,
                    reason="outgoing payload review is not bound to the planned page",
                )
            request = action.outgoing_review.requests[0]
            if not self._origin_allowed(request.target):
                return PolicyDecision(
                    allowed=False,
                    risk=RiskClass.EXTERNAL_COMMIT,
                    requires_approval=False,
                    reason=(
                        "outgoing request origin is not allowed: "
                        f"{self._origin(request.target)}"
                    ),
                )
            return PolicyDecision(
                allowed=True,
                risk=RiskClass.EXTERNAL_COMMIT,
                requires_approval=True,
                reason="external commit requires an action-bound operator approval",
            )
        return PolicyDecision(
            allowed=True,
            risk=RiskClass.READ,
            requires_approval=False,
            reason="read or local navigation action",
        )

    def _origin_allowed(self, url: str) -> bool:
        return self._origin(url) in self.allowed_origins

    def allows_url(self, url: str) -> bool:
        """Return whether browser egress to this URL is within the configured policy."""
        return self._origin_allowed(url)

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "invalid"
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
