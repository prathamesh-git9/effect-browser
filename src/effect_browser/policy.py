from __future__ import annotations

import re
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
    "api key",
    "card number",
    "cardnumber",
    "card security code",
    "cc number",
    "ccnumber",
    "cvc",
    "cvv",
    "credit card",
    "debit card",
    "one time code",
    "otp",
    "passcode",
    "password",
    "pin",
    "secret",
    "security code",
}
SAFE_PRESS_KEYS = {
    "arrowdown",
    "arrowleft",
    "arrowright",
    "arrowup",
    "backspace",
    "delete",
    "end",
    "escape",
    "home",
    "pagedown",
    "pageup",
    "shift+tab",
    "space",
    "tab",
}


class ActionPolicy:
    def __init__(
        self,
        allowed_origins: tuple[str, ...],
        allowed_upload_roots: tuple[Path, ...] = (),
    ) -> None:
        self.allowed_origins = {item.rstrip("/") for item in allowed_origins}
        self.upload_guard = UploadGuard(allowed_upload_roots)

    def evaluate(
        self,
        action: ProposedAction,
        current_url: str,
        extra_allowed_origins: tuple[str, ...] = (),
    ) -> PolicyDecision:
        target_url = action.url or current_url
        if target_url and not self._origin_allowed(target_url, extra_allowed_origins):
            return PolicyDecision(
                allowed=False,
                risk=RiskClass.READ,
                requires_approval=False,
                reason=f"origin is not allowed: {self._origin(target_url)}",
            )
        if action.kind is ActionKind.FILL:
            semantic_text = _semantic_text(
                filter(
                    None,
                    (
                        action.target_name,
                        action.locator.label if action.locator else None,
                        action.locator.name if action.locator else None,
                        action.locator.test_id if action.locator else None,
                        action.locator.selector if action.locator else None,
                    ),
                )
            )
            if _contains_sensitive_semantics(semantic_text):
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
        if action.kind is ActionKind.CHECK:
            return PolicyDecision(
                allowed=True,
                risk=RiskClass.INPUT,
                requires_approval=False,
                reason="checkbox and radio preparation is reversible",
            )
        if action.kind is ActionKind.PRESS:
            if (action.key or "").casefold() not in SAFE_PRESS_KEYS:
                return PolicyDecision(
                    allowed=False,
                    risk=RiskClass.EXTERNAL_COMMIT,
                    requires_approval=False,
                    reason=(
                        "key is not in the non-commit keyboard allowlist; use "
                        "fill or an exact reviewed submit action"
                    ),
                )
            return PolicyDecision(
                allowed=True,
                risk=RiskClass.INPUT,
                requires_approval=False,
                reason="non-commit keyboard interaction is reversible",
            )
        if action.kind in {ActionKind.SCROLL, ActionKind.WAIT}:
            return PolicyDecision(
                allowed=True,
                risk=RiskClass.READ,
                requires_approval=False,
                reason="viewport movement and bounded waiting are read-only",
            )
        if action.kind is ActionKind.DOWNLOAD:
            return PolicyDecision(
                allowed=True,
                risk=RiskClass.READ,
                requires_approval=False,
                reason="download is an inbound transfer from an allowlisted origin",
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
            if action.target_interaction == "option":
                return PolicyDecision(
                    allowed=True,
                    risk=RiskClass.INPUT,
                    requires_approval=False,
                    reason=(
                        "selecting an observed listbox option is reversible "
                        "form preparation"
                    ),
                )
            if action.target_interaction == "consent":
                return PolicyDecision(
                    allowed=True,
                    risk=RiskClass.INPUT,
                    requires_approval=False,
                    reason=(
                        "rejecting optional cookies is privacy-preserving local "
                        "preparation"
                    ),
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
            if not self._origin_allowed(request.target, extra_allowed_origins):
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

    def _origin_allowed(
        self,
        url: str,
        extra_allowed_origins: tuple[str, ...] = (),
    ) -> bool:
        allowed = self.allowed_origins | {
            self._origin(item) for item in extra_allowed_origins
        }
        return self._origin(url) in allowed

    def allows_url(
        self,
        url: str,
        extra_allowed_origins: tuple[str, ...] = (),
    ) -> bool:
        """Return whether browser egress to this URL is within the configured policy."""
        return self._origin_allowed(url, extra_allowed_origins)

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "invalid"
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _semantic_text(values) -> str:
    """Normalize bound element identity without inspecting the proposed value."""

    joined = " ".join(str(value) for value in values)
    # Live Scrapling locators normally use CSS selectors, while the accessible
    # field name is retained separately as target_name. Normalize both camelCase
    # and selector punctuation so a cardNumber or cc-number field cannot evade the
    # same fail-closed policy that protects role/name locators.
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", joined)
    return re.sub(r"[^a-z0-9]+", " ", separated.casefold()).strip()


def _contains_sensitive_semantics(value: str) -> bool:
    return any(
        re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", value) is not None
        for token in SENSITIVE_LABELS
    )
