from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest

from effect_browser.domain import (
    ActionKind,
    BrowserReceipt,
    Observation,
    OutgoingReview,
    ProposedAction,
    ReconciliationSpec,
    digest,
    utc_now,
)
from effect_browser.engine import EffectBrowserService
from effect_browser.policy import ActionPolicy
from effect_browser.store import DatabaseStore
from effect_browser.transmission import fingerprint_request

TENANT = UUID("10000000-0000-0000-0000-000000000001")
BASE_URL = "http://127.0.0.1:8765"


@dataclass
class RemoteSystem:
    commits: int = 0
    reference: str | None = None
    page_revision: int = 0
    receipt: BrowserReceipt | None = None


@dataclass
class FakeDriver:
    remote: RemoteSystem
    url: str = "about:blank"
    values: dict[str, str] = field(default_factory=dict)

    def observe(self) -> Observation:
        return Observation(
            url=self.url,
            title="Fake portal",
            state_sha256=digest(
                {
                    "url": self.url,
                    "values": self.values,
                    "page_revision": self.remote.page_revision,
                }
            ),
            captured_at=utc_now(),
        )

    def preview_submit(
        self,
        action: ProposedAction,
        observation_sha256: str,
    ) -> OutgoingReview:
        base = action.outgoing_review
        if base is None:
            body = {
                "fields": [],
                "document_sha256s": [],
                "observation_sha256": observation_sha256,
            }
            base = OutgoingReview(
                observation_sha256=observation_sha256,
                payload_sha256=digest(body),
            )
        request_body = json.dumps(
            {
                "effect_key": action.effect_key,
                "values": self.values,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        reviewed = fingerprint_request(
            method="POST",
            url=f"{BASE_URL}/synthetic-commit",
            headers={"content-type": "application/json"},
            body=request_body,
        )
        return base.bind_requests((reviewed,))

    def execute(self, action: ProposedAction) -> BrowserReceipt:
        if action.kind is ActionKind.NAVIGATE:
            self.url = action.url or self.url
        elif action.kind is ActionKind.FILL:
            assert action.locator is not None
            self.values[action.locator.label or "unknown"] = action.value or ""
        elif action.kind is ActionKind.SUBMIT:
            self.remote.commits += 1
            self.remote.reference = action.effect_key
            self.url = f"{BASE_URL}/demo-shop/orders/receipt"
        now = utc_now()
        receipt = BrowserReceipt(
            external_id=action.effect_key or f"local-{action.kind.value}",
            url=self.url,
            evidence_sha256=digest({"url": self.url, "values": self.values}),
            captured_at=now,
        )
        if action.kind is ActionKind.SUBMIT:
            self.remote.receipt = receipt
        return receipt

    def reconcile(self, spec: ReconciliationSpec) -> BrowserReceipt | None:
        if self.remote.reference == spec.external_reference:
            return self.remote.receipt
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DatabaseStore]:
    database = DatabaseStore(f"sqlite:///{tmp_path / 'test.db'}")
    database.initialize()
    yield database
    database.close()


@pytest.fixture
def service(store: DatabaseStore) -> EffectBrowserService:
    return EffectBrowserService(store, ActionPolicy((BASE_URL,)))
