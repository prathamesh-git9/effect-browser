from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from effect_browser.domain import (
    ActionKind,
    BrowserReceipt,
    PageSnapshot,
    ProposedAction,
    utc_now,
)
from effect_browser.policy import ActionPolicy
from effect_browser.research import capture_research


@dataclass
class ResearchDriver:
    snapshots: dict[str, PageSnapshot]
    visited: list[str] = field(default_factory=list)

    def execute(self, action: ProposedAction) -> BrowserReceipt:
        assert action.kind is ActionKind.NAVIGATE
        assert action.url is not None
        self.visited.append(action.url)
        return BrowserReceipt(
            external_id="read-only-navigation",
            url=action.url,
            evidence_sha256="0" * 64,
            captured_at=utc_now(),
        )

    def snapshot(self) -> PageSnapshot:
        return self.snapshots[self.visited[-1]]


def snapshot(url: str, title: str, text: str) -> PageSnapshot:
    return PageSnapshot(
        url=url,
        title=title,
        state_sha256="1" * 64,
        text_excerpt=text,
        candidates=(),
        captured_at=utc_now(),
    )


def test_research_captures_rendered_sources_with_hashes() -> None:
    url = "https://research.example/source"
    driver = ResearchDriver({url: snapshot(url, "Source", "Rendered evidence")})
    report = capture_research(
        question="What does the source say?",
        urls=(url,),
        driver=driver,
        policy=ActionPolicy(("https://research.example",)),
    )

    assert driver.visited == [url]
    assert report.sources[0].excerpt == "Rendered evidence"
    assert len(report.sources[0].evidence_sha256) == 64
    assert any("No submit" in item for item in report.limitations)


def test_research_rejects_unallowlisted_sources_before_navigation() -> None:
    driver = ResearchDriver({})
    with pytest.raises(ValueError, match="origin is not allowed"):
        capture_research(
            question="No external browsing",
            urls=("https://blocked.example/source",),
            driver=driver,
            policy=ActionPolicy(("https://research.example",)),
        )
    assert driver.visited == []


def test_research_limits_source_count() -> None:
    driver = ResearchDriver({})
    with pytest.raises(ValueError, match="between one and five"):
        capture_research(
            question="Too many",
            urls=tuple(f"https://research.example/{index}" for index in range(6)),
            driver=driver,
            policy=ActionPolicy(("https://research.example",)),
        )
