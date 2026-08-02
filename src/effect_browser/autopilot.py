from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, Field

from effect_browser.browser.base import BrowserDriver
from effect_browser.browser.playwright import PlaywrightDriver
from effect_browser.config import Settings
from effect_browser.domain import (
    ActionKind,
    ActionState,
    AutonomyMode,
    AutonomyScope,
    BrowserAction,
    DomainModel,
    RunResult,
    Task,
    TaskStatus,
)
from effect_browser.engine import EffectBrowserService
from effect_browser.providers import DeterministicPlanner, ReactiveBootstrapPlanner
from effect_browser.providers.base import Planner, ProviderError
from effect_browser.providers.http import (
    _output_text,
    _raise_provider_error,
    _strict_schema,
)
from effect_browser.store import ConflictError
from effect_browser.uploads import sha256_file

URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
WINDOWS_DOCUMENT_PATTERN = re.compile(
    r"[A-Za-z]:\\[^\r\n]*?\.(?:pdf|docx?|odt|rtf|txt)",
    re.IGNORECASE,
)
DOCUMENT_SUFFIXES = {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt"}
DOCUMENT_INTENT_PATTERN = re.compile(
    r"\b(attach|attachment|cv|resume|résumé|upload)\b",
    re.IGNORECASE,
)
PROFILE_WORKFLOW_PATTERN = re.compile(
    r"\b(apply|book|form|register|reserve|schedule|sign up)\b",
    re.IGNORECASE,
)
PROFILE_REFERENCE_PATTERN = re.compile(
    r"\b(use|using)\s+(my\s+)?(profile|saved\s+(details|information))\b",
    re.IGNORECASE,
)
COMMIT_PATTERN = re.compile(
    r"\b(apply|book|buy|cancel|delete|order|pay|post|publish|purchase|"
    r"register|reserve|schedule|send|sign|submit)\b",
    re.IGNORECASE,
)
NO_COMMIT_PATTERN = re.compile(
    r"\b(do not|don't|dont|never)\b(?:\s+\w+){0,3}\s+"
    r"(apply|book|buy|cancel|delete|order|pay|post|publish|purchase|"
    r"register|reserve|schedule|send|sign|submit|submitting)\b|"
    r"\bwithout\s+"
    r"(apply|book|buy|cancel|delete|order|pay|post|publish|purchase|"
    r"register|reserve|schedule|send|sign|submit|submitting)\b|"
    r"\b(apply|book|buy|cancel|delete|order|pay|post|publish|purchase|"
    r"register|reserve|schedule|send|sign|submit)\s+(nothing|none)\b|"
    r"\b(draft|preview|review|research|prepare)\s+only\b",
    re.IGNORECASE,
)


class TargetSource(StrEnum):
    USER_URL = "user_url"
    GROUNDED_SEARCH = "grounded_search"


class AutopilotVerdict(StrEnum):
    VERIFIED_SUCCESS = "verified_success"
    NEEDS_INPUT = "needs_input"
    NEEDS_AUTHORITY = "needs_authority"
    OUTCOME_UNKNOWN = "outcome_unknown"
    BLOCKED = "blocked"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class ProviderRuntime(DomainModel):
    name: str
    model: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None


class ResolvedTarget(DomainModel):
    start_url: str
    source: TargetSource
    reason: str
    research_urls: tuple[str, ...] = ()


class AutopilotResolution(DomainModel):
    start_url: str
    target_source: TargetSource
    provider: str
    research_urls: tuple[str, ...] = ()
    profile_id: UUID | None = None
    document_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    external_commit_granted: bool
    commit_intent_detected: bool
    external_commit_authorized: bool
    max_external_commits: int


class CommitAuthorityDecision(DomainModel):
    """Deterministic two-key authorization result for one external commit."""

    caller_granted: bool
    commit_intent_detected: bool
    denial_detected: bool
    authorized: bool
    matched_commit: str | None = None
    matched_denial: str | None = None
    reason: str


class AutopilotEvidence(DomainModel):
    action_id: UUID
    kind: ActionKind
    effect_key: str | None = None
    external_id: str
    url: str
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AutopilotResult(DomainModel):
    task: Task
    verdict: AutopilotVerdict
    message: str
    resolution: AutopilotResolution
    evidence: tuple[AutopilotEvidence, ...] = ()
    next_action: BrowserAction | None = None


class TargetResolver(Protocol):
    def resolve(self, query: str) -> ResolvedTarget: ...


class TargetPayload(BaseModel):
    resolved: bool
    start_url: str | None
    reason: str = Field(min_length=1, max_length=500)


TARGET_SCHEMA: dict[str, Any] = _strict_schema(TargetPayload.model_json_schema())


class GroundedTargetResolver:
    """Resolve a URL-free task with provider-hosted web search, never model memory."""

    def __init__(
        self,
        runtime: ProviderRuntime,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if not runtime.api_key_env or not runtime.base_url or not runtime.model:
            raise ValueError("grounded target resolution requires a remote provider")
        self.runtime = runtime
        self.client = client or httpx.Client(timeout=60)

    def resolve(self, query: str) -> ResolvedTarget:
        api_key = os.getenv(self.runtime.api_key_env or "")
        if not api_key:
            raise ProviderError(
                f"{self.runtime.api_key_env} is required for query-only target search"
            )
        try:
            response = self.client.post(
                f"{self.runtime.base_url}/responses",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": self.runtime.model,
                    "input": [
                        {
                            "role": "system",
                            "content": (
                                "Resolve only the first public HTTPS page where a "
                                "browser agent should begin the user's task. Use web "
                                "search. Return a direct official or authoritative "
                                "task URL, not a search "
                                "results page and not a URL recalled from memory. Mark "
                                "resolved false when the intended site cannot be "
                                "grounded or the request is too ambiguous. Do not "
                                "rewrite, expand, "
                                "or execute the user's task."
                            ),
                        },
                        {"role": "user", "content": query},
                    ],
                    "tools": [{"type": "web_search", "search_context_size": "low"}],
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "browser_target",
                            "strict": True,
                            "schema": TARGET_SCHEMA,
                        }
                    },
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"grounded target search failed: {type(exc).__name__}"
            ) from exc
        _raise_provider_error(response, self.runtime.name)
        raw = response.json()
        if not _used_web_search(raw):
            raise ProviderError(
                "target resolver returned without grounded web-search evidence"
            )
        payload = TargetPayload.model_validate(json.loads(_output_text(raw)))
        if not payload.resolved or not payload.start_url:
            raise ValueError(f"target could not be resolved: {payload.reason}")
        research_urls = _citation_urls(raw)
        if not research_urls:
            raise ProviderError(
                "grounded target search returned no cited source for the chosen URL"
            )
        if not _target_has_matching_citation(payload.start_url, research_urls):
            raise ProviderError(
                "grounded target URL does not match any cited source origin"
            )
        return ResolvedTarget(
            start_url=payload.start_url,
            source=TargetSource.GROUNDED_SEARCH,
            reason=payload.reason,
            research_urls=research_urls,
        )


PlannerFactory = Callable[[ProviderRuntime], Planner]
DriverFactory = Callable[[tuple[str, ...]], BrowserDriver]
ResolverFactory = Callable[[ProviderRuntime], TargetResolver]


class AutopilotCoordinator:
    """Turn one query into one durable run and one evidence-derived verdict."""

    def __init__(
        self,
        *,
        service: EffectBrowserService,
        settings: Settings,
        planner_factory: PlannerFactory | None = None,
        driver_factory: DriverFactory | None = None,
        resolver_factory: ResolverFactory | None = None,
    ) -> None:
        self.service = service
        self.settings = settings
        self.planner_factory = planner_factory or self._planner
        self.driver_factory = driver_factory or self._driver
        self.resolver_factory = resolver_factory or GroundedTargetResolver

    def execute(
        self,
        *,
        tenant_id: UUID,
        query: str,
        task_id: UUID | None = None,
        allow_external_commit: bool = False,
    ) -> AutopilotResult:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query cannot be blank")
        # Authority is resolved before target discovery or document/profile access so
        # a contradictory grant cannot cause work outside the rejected request.
        authority = decide_commit_authority(
            normalized_query,
            caller_granted=allow_external_commit,
        )

        instruction, document_path = resolve_document(
            normalized_query,
            (
                self.settings.default_document_path
                if DOCUMENT_INTENT_PATTERN.search(normalized_query)
                else None
            ),
        )
        explicit_url = extract_url(instruction)
        runtime = select_provider(self.settings, explicit_url, instruction)
        target = (
            ResolvedTarget(
                start_url=explicit_url,
                source=TargetSource.USER_URL,
                reason="the user supplied the browser target",
            )
            if explicit_url is not None
            else self.resolver_factory(runtime).resolve(instruction)
        )
        start_url = validate_target_url(target.start_url, self.settings.allowed_origins)
        if runtime.name == "deterministic":
            start_url = _origin(start_url)
        document_sha256 = sha256_file(document_path) if document_path else None
        profile_requested = bool(
            PROFILE_REFERENCE_PATTERN.search(normalized_query)
            or (
                self.settings.default_profile_id is not None
                and PROFILE_WORKFLOW_PATTERN.search(normalized_query)
            )
        )
        profile_id = (
            select_profile(
                self.service,
                tenant_id,
                self.settings.default_profile_id,
            )
            if profile_requested
            else None
        )
        commits = authority.authorized
        autonomy = AutonomyScope(
            mode=AutonomyMode.BOUNDED,
            allow_query_target_origin=True,
            allow_file_uploads=document_path is not None,
            allow_external_commits=commits,
            max_external_commits=1 if commits else 0,
        )
        task = self.service.create_task(
            tenant_id=tenant_id,
            instruction=instruction,
            start_url=start_url,
            planner=self.planner_factory(runtime),
            task_id=task_id,
            profile_id=profile_id,
            document_path=document_path,
            document_sha256=document_sha256,
            autonomy=autonomy,
            authority_context=authority.model_dump(mode="json"),
        )
        run_result = self._run_to_pause(tenant_id, task, start_url)
        resolution = AutopilotResolution(
            start_url=start_url,
            target_source=target.source,
            provider=runtime.name,
            research_urls=target.research_urls,
            profile_id=profile_id,
            document_sha256=document_sha256,
            external_commit_granted=authority.caller_granted,
            commit_intent_detected=authority.commit_intent_detected,
            external_commit_authorized=commits,
            max_external_commits=1 if commits else 0,
        )
        return assess_result(self.service, run_result, resolution)

    def resume(self, *, tenant_id: UUID, task_id: UUID) -> AutopilotResult:
        """Resume a pre-existing child task without planning a duplicate task."""
        task = self.service.store.get_task(tenant_id, task_id)
        authority = _persisted_task_authority(self.service, task)
        result = self._run_to_pause(tenant_id, task, task.start_url)
        resolution = AutopilotResolution(
            start_url=task.start_url,
            target_source=(
                TargetSource.USER_URL
                if extract_url(task.instruction)
                else TargetSource.GROUNDED_SEARCH
            ),
            provider=task.provider,
            profile_id=task.profile_id,
            document_sha256=task.document_sha256,
            external_commit_granted=authority.caller_granted,
            commit_intent_detected=authority.commit_intent_detected,
            external_commit_authorized=task.autonomy.allow_external_commits,
            max_external_commits=task.autonomy.max_external_commits,
        )
        return assess_result(self.service, result, resolution)

    def _run_to_pause(
        self,
        tenant_id: UUID,
        task: Task,
        start_url: str,
    ) -> RunResult:
        """Cross explicit safe session-rollover points, never human blockers."""
        result: RunResult | None = None
        for _session in range(5):
            try:
                browser = self.driver_factory((start_url,))
            except Exception as exc:
                blocked = self.service.store.block_task(
                    tenant_id,
                    task.id,
                    kind="browser_start_failure",
                    reason=(f"the browser session could not start: {type(exc).__name__}"),
                    evidence="browser_factory",
                )
                return RunResult(
                    task=blocked,
                    message="browser could not start; success is not claimed",
                )
            try:
                result = self.service.run(
                    tenant_id=tenant_id,
                    task_id=task.id,
                    driver=browser,
                )
            finally:
                browser.close()
            rollover = (
                result.next_action is not None
                and result.next_action.state is ActionState.PREPARED
                and result.next_action.proposal.kind is ActionKind.SUBMIT
                and "fresh browser session" in result.message
            )
            if not rollover:
                return result
        if result is None:  # pragma: no cover - the loop always runs
            raise RuntimeError("autopilot did not start a browser session")
        blocked = self.service.store.block_task(
            tenant_id,
            task.id,
            kind="session_rollover_budget_exhausted",
            reason=(
                "five crash-safe browser session rollovers did not reach a stable "
                "dispatch or truthful pause"
            ),
            evidence=f"task_id={task.id}",
        )
        return RunResult(
            task=blocked,
            next_action=result.next_action,
            message="browser session rollover budget exhausted; success is not claimed",
        )

    def _planner(self, runtime: ProviderRuntime) -> Planner:
        if runtime.name == "deterministic":
            return DeterministicPlanner()
        return ReactiveBootstrapPlanner(runtime.name)

    def _driver(self, query_origins: tuple[str, ...]) -> BrowserDriver:
        return PlaywrightDriver(
            executable_path=self.settings.browser_executable,
            headless=self.settings.browser_headless,
            sandbox=self.settings.browser_sandbox,
            artifacts_directory=self.settings.artifacts_directory,
            allowed_upload_roots=self.settings.allowed_upload_roots,
            allowed_upload_origins=self.settings.allowed_upload_origins,
            allowed_origins=(*self.settings.allowed_origins, *query_origins),
        )


def select_provider(
    settings: Settings,
    explicit_url: str | None,
    query: str = "",
) -> ProviderRuntime:
    if (
        explicit_url
        and _configured_local_url(explicit_url, settings.allowed_origins)
        and (
            urlsplit(explicit_url).path.rstrip("/") == "/demo-shop"
            or "backup drive" in query.casefold()
        )
    ):
        return ProviderRuntime(name="deterministic")

    configured = settings.provider.casefold().strip()
    if configured in {"openai", "openai-reactive"}:
        return _remote_runtime("openai-reactive", settings)
    if configured in {"grok", "grok-reactive"}:
        return _remote_runtime("grok-reactive", settings)
    if configured not in {"auto", "deterministic"}:
        raise ValueError(
            "EFFECT_BROWSER_PROVIDER must be auto, openai-reactive, or grok-reactive"
        )
    if os.getenv("OPENAI_API_KEY"):
        return _remote_runtime("openai-reactive", settings)
    if os.getenv("XAI_API_KEY"):
        return _remote_runtime("grok-reactive", settings)
    raise ValueError(
        "query-only public-web tasks require OPENAI_API_KEY or XAI_API_KEY; "
        "a configured local demo URL remains available without a provider key"
    )


def _remote_runtime(name: str, settings: Settings) -> ProviderRuntime:
    if name == "openai-reactive":
        key_env = "OPENAI_API_KEY"
        model = settings.openai_model
        base_url = "https://api.openai.com/v1"
    else:
        key_env = "XAI_API_KEY"
        model = settings.grok_model
        base_url = "https://api.x.ai/v1"
    if not os.getenv(key_env):
        raise ValueError(f"{key_env} is required for configured provider {name}")
    return ProviderRuntime(
        name=name,
        model=model,
        api_key_env=key_env,
        base_url=base_url,
    )


def extract_url(query: str) -> str | None:
    match = URL_PATTERN.search(query)
    if match is None:
        return None
    return match.group(0).rstrip(".,;:!?)\\]}")


def validate_target_url(url: str, configured_origins: tuple[str, ...]) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("resolved browser target must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("browser target URL cannot contain credentials")
    origin = _origin(url)
    configured = {_origin(item) for item in configured_origins}
    host = parsed.hostname.casefold().rstrip(".")
    if origin not in configured:
        if parsed.scheme != "https":
            raise ValueError("public query targets must use HTTPS")
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            raise ValueError("local network targets must be explicitly allowlisted")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError(
                "private or reserved IP targets must be explicitly allowlisted"
            )
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
    )


def resolve_document(query: str, default: Path | None) -> tuple[str, Path | None]:
    raw = _document_from_query(query)
    candidate = Path(raw).expanduser() if raw else None
    if candidate is None and default is not None:
        candidate = default.expanduser()
    if candidate is not None and not candidate.is_absolute():
        raise ValueError("approved browser document paths must be absolute")
    selected = candidate.resolve() if candidate is not None else None
    if selected is None:
        return query, None
    if selected.suffix.casefold() not in DOCUMENT_SUFFIXES:
        raise ValueError(
            "approved browser documents must be PDF, DOC(X), ODT, RTF, or TXT"
        )
    if not selected.is_file():
        raise ValueError(f"approved browser document does not exist: {selected.name}")
    sanitized = query.replace(raw, "[approved local document]") if raw else query
    return sanitized, selected


def _document_from_query(query: str) -> str | None:
    for match in re.finditer(r"""(["'])(.+?)\1""", query):
        candidate = match.group(2)
        if Path(candidate).suffix.casefold() in DOCUMENT_SUFFIXES:
            return candidate
    windows = WINDOWS_DOCUMENT_PATTERN.search(query)
    return windows.group(0) if windows else None


def decide_commit_authority(
    query: str,
    *,
    caller_granted: bool,
) -> CommitAuthorityDecision:
    """Require both explicit caller authority and deterministic commit intent.

    Language matching is deliberately incapable of granting authority by itself.
    A contradictory explicit grant fails loudly instead of silently discarding the
    caller's words.
    """

    normalized = query.casefold().replace("don't", "do not").replace("dont", "do not")
    normalized = re.sub(r"[^\w\s]+", " ", normalized)
    denial = NO_COMMIT_PATTERN.search(normalized)
    commit = next(
        (
            match
            for match in COMMIT_PATTERN.finditer(normalized)
            if not _is_incidental_commit_usage(normalized, match)
        ),
        None,
    )
    if caller_granted and denial is not None:
        raise ValueError(
            "external commit grant contradicts the request's explicit read-only "
            f"language ({denial.group(0)!r}); remove --commit or rewrite the request"
        )
    authorized = caller_granted and commit is not None and denial is None
    if authorized:
        reason = "explicit caller grant and commit intent permit at most one commit"
    elif not caller_granted:
        reason = "the caller did not explicitly grant an external commit"
    else:
        reason = "the request does not explicitly name a supported external action"
    return CommitAuthorityDecision(
        caller_granted=caller_granted,
        commit_intent_detected=commit is not None,
        denial_detected=denial is not None,
        authorized=authorized,
        matched_commit=commit.group(0) if commit is not None else None,
        matched_denial=denial.group(0) if denial is not None else None,
        reason=reason,
    )


def _is_incidental_commit_usage(normalized: str, match: re.Match[str]) -> bool:
    """Reject common noun and answer-format uses of otherwise dangerous verbs.

    This matcher intentionally prefers a false negative: an explicit caller grant is
    only useful when the query also expresses an unambiguous external action.
    """

    verb = match.group(0).casefold()
    before = normalized[: match.start()].rstrip()
    after = normalized[match.end() :].lstrip()
    if verb == "apply" and re.match(r"(?:the\s+)?filters?\b", after):
        return True
    if verb == "book" and re.match(r"about\b", after):
        return True
    if verb == "cancel" and re.match(r"culture\b", after):
        return True
    if verb == "delete" and re.match(r"(?:the\s+)?key\b|documentation\b", after):
        return True
    if verb == "order" and (
        (re.search(r"\bin$", before) and re.match(r"to\b", after))
        or re.search(r"\bpurchase$", before)
    ):
        return True
    if verb == "post" and re.match(
        r"(?:the\s+)?results?\s+in\s+(?:a\s+)?table\b",
        after,
    ):
        return True
    if (
        verb == "purchase"
        and re.search(
            r"\b(?:compare|explain|inspect|research|review)$",
            before,
        )
        and re.match(r"order\b", after)
    ):
        return True
    if verb == "register" and re.match(r"(?:the\s+)?trend\b", after):
        return True
    if verb == "schedule" and re.search(
        r"\b(?:check|compare|inspect|read|review)(?:\s+\w+){0,3}$",
        before,
    ):
        return True
    if verb == "send" and re.match(
        r"(?:me\s+)?(?:(?:a|the)\s+)?(?:results?|summary)\b",
        after,
    ):
        return True
    return verb == "sign" and re.match(r"of\b", after) is not None


def _persisted_task_authority(
    service: EffectBrowserService,
    task: Task,
) -> CommitAuthorityDecision:
    """Recover the original two-key decision without expanding legacy authority."""

    created = next(
        (
            event
            for event in service.store.events(task.tenant_id, task.id)
            if event.kind == "task.created"
        ),
        None,
    )
    context = created.payload.get("authority_context") if created is not None else None
    if context is not None:
        persisted = None
        recomputed = None
        if isinstance(context, dict):
            try:
                persisted = CommitAuthorityDecision.model_validate(context)
                recomputed = decide_commit_authority(
                    task.instruction,
                    caller_granted=persisted.caller_granted,
                )
            except ValueError:
                persisted = None
                recomputed = None
        expected_max = 1 if persisted is not None and persisted.authorized else 0
        if (
            persisted is None
            or recomputed is None
            or persisted != recomputed
            or persisted.authorized is not task.autonomy.allow_external_commits
            or task.autonomy.max_external_commits != expected_max
        ):
            raise ConflictError(
                "persisted task authority does not match its durable scope"
            )
        return persisted

    # Older task events did not retain the two input keys. Preserve their effective
    # scope, but never infer new authority while filling the reporting fields.
    authorized = task.autonomy.allow_external_commits
    return CommitAuthorityDecision(
        caller_granted=authorized,
        commit_intent_detected=authorized,
        denial_detected=False,
        authorized=authorized,
        reason="legacy task scope retained without expanding external authority",
    )


def select_profile(
    service: EffectBrowserService,
    tenant_id: UUID,
    configured: UUID | None,
) -> UUID | None:
    if configured is not None:
        service.store.get_profile(tenant_id, configured)
        return configured
    profiles = service.store.list_profiles(tenant_id)
    return profiles[0].id if len(profiles) == 1 else None


def assess_result(
    service: EffectBrowserService,
    result: RunResult,
    resolution: AutopilotResolution,
) -> AutopilotResult:
    actions = service.store.list_actions(result.task.tenant_id, result.task.id)
    evidence = tuple(
        AutopilotEvidence(
            action_id=action.id,
            kind=action.proposal.kind,
            effect_key=action.proposal.effect_key,
            external_id=receipt.external_id,
            url=receipt.url,
            evidence_sha256=receipt.evidence_sha256,
        )
        for action in actions
        if action.state is ActionState.SUCCEEDED
        and (receipt := service.store.get_receipt(result.task.tenant_id, action.id))
        is not None
    )
    proven_commit = any(item.kind is ActionKind.SUBMIT for item in evidence)
    proven_rendered_finish = any(
        action.state is ActionState.SUCCEEDED
        and action.proposal.kind is ActionKind.FINISH
        and bool((action.proposal.expected_outcome or "").strip())
        and service.store.get_receipt(result.task.tenant_id, action.id) is not None
        for action in actions
    )
    status = result.task.status

    if status is TaskStatus.SUCCEEDED:
        if resolution.external_commit_authorized and not proven_commit:
            verdict = AutopilotVerdict.UNVERIFIED
            message = (
                "the planner stopped, but no authoritative external-effect receipt "
                "proves the requested commit; success is not claimed"
            )
        elif proven_commit or any(item.kind is ActionKind.DOWNLOAD for item in evidence):
            verdict = AutopilotVerdict.VERIFIED_SUCCESS
            message = (
                "task completed with an authoritative external-effect receipt"
                if proven_commit
                else "task completed with a hash-verified download receipt"
            )
        elif proven_rendered_finish:
            verdict = AutopilotVerdict.VERIFIED_SUCCESS
            message = (
                "task completed with receipt-backed rendered evidence for its "
                "declared read-only outcome"
            )
        else:
            verdict = AutopilotVerdict.UNVERIFIED
            message = (
                "the final browser state was captured, but no deterministic "
                "goal-specific receipt proves the requested read-only outcome"
            )
    elif status is TaskStatus.AWAITING_INPUT:
        verdict, message = AutopilotVerdict.NEEDS_INPUT, result.message
    elif status is TaskStatus.AWAITING_APPROVAL:
        verdict, message = AutopilotVerdict.NEEDS_AUTHORITY, result.message
    elif status is TaskStatus.AWAITING_RECOVERY:
        verdict, message = AutopilotVerdict.OUTCOME_UNKNOWN, result.message
    elif status is TaskStatus.BLOCKED:
        verdict, message = AutopilotVerdict.BLOCKED, result.message
    else:
        verdict, message = AutopilotVerdict.FAILED, result.message
    return AutopilotResult(
        task=result.task,
        verdict=verdict,
        message=message,
        resolution=resolution,
        evidence=evidence,
        next_action=result.next_action,
    )


def _used_web_search(payload: dict[str, Any]) -> bool:
    return any(
        isinstance(item, dict)
        and item.get("type") == "web_search_call"
        and item.get("status") in {None, "completed"}
        for item in payload.get("output", [])
    )


def _citation_urls(payload: dict[str, Any]) -> tuple[str, ...]:
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "url_citation" and isinstance(value.get("url"), str):
                found.append(value["url"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload.get("output", []))
    for citation in payload.get("citations", []):
        if isinstance(citation, str):
            found.append(citation)
    return tuple(dict.fromkeys(found))


def _target_has_matching_citation(
    target_url: str,
    citation_urls: tuple[str, ...],
) -> bool:
    target_host = (urlsplit(target_url).hostname or "").casefold().rstrip(".")
    return any(
        target_host == citation_host
        or target_host.endswith(f".{citation_host}")
        or citation_host.endswith(f".{target_host}")
        for url in citation_urls
        if (citation_host := (urlsplit(url).hostname or "").casefold().rstrip("."))
    )


def _configured_local_url(url: str, configured_origins: tuple[str, ...]) -> bool:
    host = (urlsplit(url).hostname or "").casefold()
    local = host == "localhost" or host.endswith(".localhost")
    try:
        local = local or not ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    return local and _origin(url) in {_origin(item) for item in configured_origins}


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}".rstrip("/")
