# Scrapling integration decision

Research date: 2026-07-23.

## Decision

Use Scrapling's parser to turn each fresh, rendered DOM into a compact set of
action candidates and to relocate previously known elements after layout drift.
Keep Playwright as the browser executor.

Scrapling is not an autonomous browser agent. `DynamicFetcher` is a convenience
layer around Chromium and explicitly delegates browser actions to Playwright's
`Page` API. Replacing Effect Browser's executor with it would not fix the actual
failure: the current model plans a complete workflow before seeing the page.

The useful split is:

1. Playwright loads the page and owns the live browser session.
2. Scrapling parses the rendered HTML and derives semantic candidates.
3. A step planner selects exactly one candidate from the fresh snapshot.
4. Deterministic policy classifies the selected interaction.
5. Playwright executes the typed action.
6. The engine observes the new page before planning the next action.

This removes invented fields and stale whole-task plans while preserving the
existing approval, crash, audit, and reconciliation guarantees.

## What Scrapling contributes

- Fast parsing of rendered HTML with CSS, XPath, text, and attribute queries.
- Generated CSS/XPath selectors for elements found in a snapshot.
- Adaptive storage and similarity-based relocation when a saved selector stops
  matching after page drift.
- A Python 3.11-compatible package and a Playwright-compatible browser stack.

## What it does not contribute

- Goal decomposition or action selection.
- Reliable classification of whether a click creates an external side effect.
- Factual answers to job-application questions.
- Approval semantics.
- Exactly-once submission.
- Proof that an ATS persisted an application.
- Universal CAPTCHA, MFA, iframe, or upload handling.

Those remain Effect Browser responsibilities. Anti-bot bypass is deliberately
outside the product contract; legitimate automation must pause when a site
requires human verification or disallows automation.

## Version choice

Pin Scrapling `0.4.11`, the current release at research time. It declares
Playwright `1.61.0` in its fetcher extra, so Effect Browser pins the same
Playwright version even though it initially uses Scrapling's core parser only.

## Primary sources

- <https://github.com/D4Vinci/Scrapling>
- <https://scrapling.readthedocs.io/en/latest/parsing/adaptive.html>
- <https://scrapling.readthedocs.io/en/latest/fetching/dynamic.html>
- <https://scrapling.readthedocs.io/en/latest/api-reference/selector.html>
