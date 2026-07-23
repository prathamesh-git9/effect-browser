from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

from scrapling import Selector
from scrapling.parser import Selector as ScraplingElement

from effect_browser.domain import (
    ElementCandidate,
    Locator,
    PageSnapshot,
    SubmissionContract,
    digest,
    utc_now,
)

ACTIONABLE_SELECTOR = (
    "input, textarea, select, button, a[href], [role='button'], "
    "[role='link'], [contenteditable='true']"
)
COMMIT_WORDS = re.compile(
    r"\b(submit|confirm|purchase|buy|order|send|publish|delete|remove|"
    r"book|reserve|pay|apply now|complete application)\b",
    re.IGNORECASE,
)


class ScraplingSnapshotter:
    """Turn rendered HTML into candidate-bound actions without persisting page text."""

    def __init__(self, storage_file: Path) -> None:
        storage_file.parent.mkdir(parents=True, exist_ok=True)
        self.storage_file = storage_file

    def build(
        self,
        *,
        html: str,
        url: str,
        title: str,
        state_sha256: str,
        max_candidates: int = 120,
        max_text: int = 8_000,
    ) -> PageSnapshot:
        page = self._page(html, url)
        candidates: list[ElementCandidate] = []
        semantic_counts: dict[str, int] = {}
        for element in page.css(ACTIONABLE_SELECTOR):
            if self._excluded(element):
                continue
            role = self._role(element)
            name = self._name(page, element)
            input_type = str(element.attrib.get("type", "")).casefold() or None
            base_key = self._semantic_key(role, name, input_type)
            occurrence = semantic_counts.get(base_key, 0)
            semantic_counts[base_key] = occurrence + 1
            adaptive_id = f"{base_key}:{occurrence}"
            page.save(element, adaptive_id)
            selector = str(element.generate_full_css_selector)
            href = (
                urljoin(url, str(element.attrib["href"]))
                if element.attrib.get("href")
                else None
            )
            options = tuple(
                f"{str(option.attrib.get('value', ''))} | "
                f"{self._clean(option.get_all_text(separator=' ', strip=True))}"
                for option in element.css("option")
            )
            candidates.append(
                ElementCandidate(
                    id=f"C{len(candidates) + 1:03d}",
                    tag=str(element.tag),
                    role=role,
                    name=name,
                    input_type=input_type,
                    required="required" in element.attrib,
                    disabled="disabled" in element.attrib,
                    href=href,
                    options=options,
                    interaction=self._interaction(element, name, href),
                    locator=Locator(
                        selector=selector,
                        adaptive_id=adaptive_id,
                    ),
                )
            )
            if len(candidates) >= max_candidates:
                break
        text = self._clean(page.get_all_text(separator=" ", strip=True))[:max_text]
        contract = self._submission_contract(page)
        return PageSnapshot(
            url=url,
            title=title,
            state_sha256=state_sha256,
            text_excerpt=text,
            candidates=tuple(candidates),
            submission_contract=contract,
            captured_at=utc_now(),
        )

    def relocate(self, *, html: str, url: str, adaptive_id: str) -> str | None:
        page = self._page(html, url)
        saved = page.retrieve(adaptive_id)
        if not saved:
            return None
        matches = page.relocate(saved, percentage=55, selector_type=True)
        if len(matches) != 1:
            return None
        return str(matches[0].generate_full_css_selector)

    def _page(self, html: str, url: str) -> Selector:
        return Selector(
            html,
            url=url,
            adaptive=True,
            storage_args={
                "storage_file": str(self.storage_file),
                "url": url,
            },
        )

    @staticmethod
    def _submission_contract(page: Selector) -> SubmissionContract | None:
        forms = page.css("form[data-effect-reconciliation-url]")
        if len(forms) != 1:
            return None
        form = forms[0]
        url_template = str(form.attrib.get("data-effect-reconciliation-url", "")).strip()
        expected_template = str(
            form.attrib.get("data-effect-reconciliation-text", "")
        ).strip()
        if not url_template or not expected_template:
            return None
        return SubmissionContract(
            url_template=url_template,
            expected_text_template=expected_template,
            receipt_test_id=(
                str(form.attrib.get("data-effect-receipt-test-id", "")).strip() or None
            ),
        )

    @staticmethod
    def _excluded(element: ScraplingElement) -> bool:
        attributes = element.attrib
        return bool(
            str(attributes.get("type", "")).casefold() == "hidden"
            or "hidden" in attributes
            or str(attributes.get("aria-hidden", "")).casefold() == "true"
        )

    @staticmethod
    def _role(element: ScraplingElement) -> str:
        explicit = str(element.attrib.get("role", "")).strip()
        if explicit:
            return explicit
        tag = str(element.tag).casefold()
        input_type = str(element.attrib.get("type", "text")).casefold()
        if tag == "a":
            return "link"
        if tag == "button" or input_type in {"button", "submit", "reset"}:
            return "button"
        if tag == "select":
            return "combobox"
        if input_type == "checkbox":
            return "checkbox"
        if input_type == "radio":
            return "radio"
        return "textbox"

    def _name(self, page: Selector, element: ScraplingElement) -> str:
        attributes = element.attrib
        aria = str(attributes.get("aria-label", "")).strip()
        if aria:
            return self._clean(aria)
        element_id = str(attributes.get("id", "")).strip()
        if element_id:
            labels = page.xpath("//label[@for=$target]", target=element_id)
            if labels:
                value = labels[0].get_all_text(separator=" ", strip=True)
                if value:
                    return self._clean(value)
        ancestor = element.find_ancestor(lambda item: item.tag == "label")
        if ancestor is not None:
            value = ancestor.get_all_text(separator=" ", strip=True)
            if value:
                return self._clean(value)
        text = element.get_all_text(separator=" ", strip=True)
        return self._clean(
            text
            or str(attributes.get("placeholder", ""))
            or str(attributes.get("name", ""))
            or str(attributes.get("value", ""))
        )

    @staticmethod
    def _semantic_key(role: str, name: str, input_type: str | None) -> str:
        raw = f"{role}|{name.casefold()}|{input_type or ''}"
        return f"candidate-{digest(raw)[:20]}"

    @staticmethod
    def _interaction(
        element: ScraplingElement,
        name: str,
        href: str | None,
    ) -> str:
        if href:
            return "navigation"
        input_type = str(element.attrib.get("type", "")).casefold()
        if input_type == "submit" or COMMIT_WORDS.search(name):
            return "commit"
        if str(element.tag).casefold() in {"input", "textarea", "select"}:
            return "input"
        return "ambiguous"

    @staticmethod
    def _clean(value: object) -> str:
        return re.sub(r"\s+", " ", str(value)).strip()
