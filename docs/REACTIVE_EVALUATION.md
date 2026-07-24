# Reactive browser evaluation

Evaluation date: 2026-07-23. No real employer was contacted. Every value used a
fictional `.test` identity against the bundled local ATS.

## Reproducible result ledger (2026-07-24)

These are raw repository-run counts, not a claim about arbitrary live sites:

| Evaluation | Pass | Fail | Skipped | Evidence |
| --- | ---: | ---: | ---: | --- |
| Deterministic reactive ATS (Edge) | 6 | 0 | 0 | `tests/test_reactive_browser_e2e.py` |
| Browser policy / challenge (Edge) | 3 | 0 | 0 | `tests/test_browser_e2e.py` |
| OpenAI reactive provider | 3 | 0 | 0 | Live runs documented below |
| Grok reactive provider | 0 | 0 | 1 | `XAI_API_KEY` was absent; no result fabricated |

The OpenAI row counts the three documented synthetic runs (preparation,
authoritative submission, and fake-success recovery). It is a smoke sample, not a
statistically meaningful benchmark. Run the same harness with `XAI_API_KEY` to
turn the Grok row into a live result.

## Question

Does observing the rendered page before every model decision fix the failure of
the one-shot planner, or does the model still invent fields and receipts?

## Setup

- Model: `gpt-5.6` through the OpenAI Responses API.
- Browser: installed Microsoft Edge controlled by Playwright `1.61.0`.
- Parser: Scrapling `0.4.11`.
- Start page: the synthetic jobs index, not the application form.
- Planner input: one fresh page snapshot per decision, containing visible text
  and candidate-bound controls.
- Planner output: one typed action selecting an existing candidate ID.
- Facts: a complete fictional profile supplied in the task instruction.
- Commit: normal Effect Browser action-bound approval and reconciliation.

## Baseline

The legacy one-shot planner failed before this change. It planned without opening
the page, guessed four nonexistent fields, invented a receipt URL, and was denied
at its first ambiguous click. The ledger remained empty.

## Live run 1: preparation boundary

The instruction explicitly said to stop before submitting. The provider:

1. Opened the jobs page.
2. Selected the observed Apply link.
3. Waited for asynchronous form hydration.
4. Filled every required field, including the conditional work-authorization
   select.
5. Chose `finish` without selecting Submit.

Result: 11 actions, task succeeded at the requested preparation boundary, and no
application was stored.

## Live run 2: verified submission

The instruction explicitly required submission. The provider selected:

| Ordinal | Action | Bound target |
| --- | --- | --- |
| 0 | Navigate | Synthetic jobs index |
| 1 | Click | `Apply` |
| 2 | Fill | `Full name` |
| 3 | Fill | `Email` |
| 4 | Fill | `Country` |
| 5 | Fill | Conditional `Work authorization` |
| 6 | Fill | `Years using Python` |
| 7 | Fill | Stable `Application reference` |
| 8 | Fill | `Resume summary` |
| 9 | Fill | `Why this role?` |
| 10 | Submit | `Submit application` |
| 11 | Finish | Independently verified receipt |

The engine stopped at ordinal 10 with `AWAITING_APPROVAL`. After approval:

- task status: `SUCCEEDED`;
- ledger matches for `EB-FBEF5DDE`: `1`;
- durable application ID:
  `f69e4317-927d-4eaa-a00b-0216460a1a66`;
- browser receipt ID:
  `f69e4317-927d-4eaa-a00b-0216460a1a66`;
- duplicate attempts: `0`;
- tenant audit chain: valid.

## Adversarial result

Both the deterministic test planner and live `gpt-5.6` reactive provider were run
against `mode=fake_success`. The page displayed **Application received** but
persisted no row. The live run used effect key `EB-4AA6C143`. Result:

- task status: `AWAITING_RECOVERY`;
- submit state: `OUTCOME_UNKNOWN`;
- receipt: absent;
- ledger: empty.

## Honest conclusion

The reactive architecture fixes the specific one-shot failure on the tested
dynamic workflow. The live model used existing controls and supplied facts rather
than inventing fields.

This is still not proof of universal browser automation. The deterministic
surface now covers multipart résumé uploads, an embedded-frame human challenge,
hydration, multi-step transitions, and fake success. It still does not support
arbitrary rich editors, authenticated sessions, or portals without an
authoritative receipt; those flows fail closed or remain unconfirmed.
