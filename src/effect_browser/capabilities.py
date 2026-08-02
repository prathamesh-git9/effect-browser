"""Machine-readable browser capability contract.

The catalog tells providers and operators what the executor can actually do. It is
not a marketing claim: unsupported capabilities must produce a handoff instead of an
invented success.
"""

from __future__ import annotations

from pydantic import BaseModel

from effect_browser.domain import ActionKind, RiskClass


class CapabilitySpec(BaseModel):
    kind: ActionKind
    target_required: bool
    risk: RiskClass
    approval: str
    guarantee: str


CAPABILITIES: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        kind=ActionKind.NAVIGATE,
        target_required=False,
        risk=RiskClass.READ,
        approval="none",
        guarantee="Exact-origin allowlist; rendered page is observed after navigation.",
    ),
    CapabilitySpec(
        kind=ActionKind.FILL,
        target_required=True,
        risk=RiskClass.INPUT,
        approval="none unless factual input is missing",
        guarantee="Candidate-bound fill/select; consequential facts come from a profile.",
    ),
    CapabilitySpec(
        kind=ActionKind.CHECK,
        target_required=True,
        risk=RiskClass.INPUT,
        approval="none",
        guarantee="Candidate-bound checkbox or radio state preparation.",
    ),
    CapabilitySpec(
        kind=ActionKind.PRESS,
        target_required=True,
        risk=RiskClass.INPUT,
        approval="unsafe keys are blocked",
        guarantee="Candidate-bound input using a non-commit key allowlist.",
    ),
    CapabilitySpec(
        kind=ActionKind.SCROLL,
        target_required=False,
        risk=RiskClass.READ,
        approval="none",
        guarantee="Bounded viewport movement.",
    ),
    CapabilitySpec(
        kind=ActionKind.WAIT,
        target_required=False,
        risk=RiskClass.READ,
        approval="none",
        guarantee="Bounded 100-5000 ms wait followed by fresh observation.",
    ),
    CapabilitySpec(
        kind=ActionKind.DOWNLOAD,
        target_required=True,
        risk=RiskClass.READ,
        approval="none",
        guarantee="Allowlisted inbound file with a recorded SHA-256.",
    ),
    CapabilitySpec(
        kind=ActionKind.UPLOAD,
        target_required=True,
        risk=RiskClass.EXTERNAL_COMMIT,
        approval="operator or bounded task scope",
        guarantee=(
            "Allowlisted local file and exact content hash; unreviewed network "
            "auto-upload is blocked."
        ),
    ),
    CapabilitySpec(
        kind=ActionKind.CLICK,
        target_required=True,
        risk=RiskClass.EXTERNAL_COMMIT,
        approval="depends on observed semantics",
        guarantee="Fresh candidate only; ambiguous controls require approval.",
    ),
    CapabilitySpec(
        kind=ActionKind.SUBMIT,
        target_required=True,
        risk=RiskClass.EXTERNAL_COMMIT,
        approval="operator or bounded task scope",
        guarantee=(
            "Abort-first request review plus mandatory authoritative reconciliation."
        ),
    ),
    CapabilitySpec(
        kind=ActionKind.HANDOFF,
        target_required=False,
        risk=RiskClass.READ,
        approval="human continuation required",
        guarantee=(
            "Unsupported, missing-fact, CAPTCHA, MFA, and policy blockers are explicit."
        ),
    ),
    CapabilitySpec(
        kind=ActionKind.FINISH,
        target_required=False,
        risk=RiskClass.READ,
        approval="none",
        guarantee="Completes only from visible/verifiable fresh state.",
    ),
)


def capability_catalog() -> tuple[CapabilitySpec, ...]:
    return CAPABILITIES
