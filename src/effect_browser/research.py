"""Read-only source capture for bounded browser research.

Research deliberately does not ask a model to invent citations or execute form
effects. It navigates only configured origins, records the rendered page evidence,
and returns hashes that let an operator prove which page was observed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, Field, HttpUrl

from effect_browser.browser.base import BrowserDriver
from effect_browser.domain import (
    ActionKind,
    PageSnapshot,
    ProposedAction,
    digest,
    utc_now,
)
from effect_browser.policy import ActionPolicy


class ResearchSource(BaseModel):
    requested_url: HttpUrl
    observed_url: HttpUrl
    title: str = Field(max_length=500)
    excerpt: str = Field(max_length=12_000)
    state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime


class ResearchSourceFailure(BaseModel):
    requested_url: HttpUrl
    error_type: str = Field(min_length=1, max_length=100)
    detail: str = Field(min_length=1, max_length=500)
    failed_at: datetime


class ResearchReport(BaseModel):
    question: str = Field(min_length=1, max_length=2_000)
    sources: tuple[ResearchSource, ...] = Field(max_length=5)
    failures: tuple[ResearchSourceFailure, ...] = Field(default=(), max_length=5)
    limitations: tuple[str, ...]
    generated_at: datetime


class ResearchPolicy(Protocol):
    def allows_url(self, url: str) -> bool: ...


def capture_research(
    *,
    question: str,
    urls: tuple[str, ...],
    driver: BrowserDriver,
    policy: ResearchPolicy | ActionPolicy,
) -> ResearchReport:
    """Capture rendered evidence from up to five allowlisted HTTP(S) sources."""

    if not 1 <= len(urls) <= 5:
        raise ValueError("research requires between one and five source URLs")
    for requested in urls:
        parsed = urlparse(requested)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("research URLs must be absolute HTTP(S) URLs")
        if not policy.allows_url(requested):
            raise ValueError(
                f"research origin is not allowed: {parsed.scheme}://{parsed.netloc}"
            )
    sources: list[ResearchSource] = []
    failures: list[ResearchSourceFailure] = []
    for requested in urls:
        try:
            driver.execute(
                ProposedAction(
                    kind=ActionKind.NAVIGATE,
                    url=requested,
                    description=(
                        "Read the allowlisted source without executing a form effect."
                    ),
                )
            )
            snapshot = driver.snapshot()
            sources.append(_source(requested, snapshot))
        except Exception as exc:
            # Driver exceptions can embed response or page text. Preserve the typed
            # per-source failure without reflecting that untrusted content.
            detail = "source capture failed before rendered evidence was retained"
            failures.append(
                ResearchSourceFailure(
                    requested_url=requested,
                    error_type=type(exc).__name__[:100],
                    detail=detail,
                    failed_at=utc_now(),
                )
            )
    limitations = [
        "Research captures rendered evidence; it does not prove factual truth.",
        "No submit, click, fill, upload, login, or booking action is executed.",
        "Only configured origins are visited and page text may be incomplete.",
    ]
    if failures:
        limitations.append(
            f"{len(failures)} source capture(s) failed; successful sources remain usable."
        )
    return ResearchReport(
        question=question,
        sources=tuple(sources),
        failures=tuple(failures),
        limitations=tuple(limitations),
        generated_at=utc_now(),
    )


def _source(requested: str, snapshot: PageSnapshot) -> ResearchSource:
    excerpt = snapshot.text_excerpt[:12_000]
    evidence = digest(
        {
            "requested_url": requested,
            "observed_url": snapshot.url,
            "title": snapshot.title,
            "excerpt": excerpt,
            "state_sha256": snapshot.state_sha256,
        }
    )
    return ResearchSource(
        requested_url=requested,
        observed_url=snapshot.url,
        title=snapshot.title,
        excerpt=excerpt,
        state_sha256=snapshot.state_sha256,
        evidence_sha256=evidence,
        captured_at=snapshot.captured_at,
    )
