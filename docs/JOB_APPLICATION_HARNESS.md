# Dynamic job-application truthfulness harness

Tested on 2026-07-23. No real employer was contacted and no real application was
submitted.

## Why this harness exists

The original order demo proved crash handling on a conventional server-rendered form.
That did not prove the browser could handle a dynamic applicant-tracking-system page,
and it did not prove that a success-looking page corresponded to a stored application.

Real ATS forms are job-specific. Greenhouse exposes questions per job and supports
multipart résumé uploads; its own documentation warns that custom application forms must
perform required-field validation. Ashby likewise describes job-specific application
forms and field definitions. These are dynamic workflows, not one universal form.

Sources:

- [Greenhouse Job Board API](https://developer.greenhouse.io/job-board.html)
- [Ashby application forms](https://docs.ashbyhq.com/application-forms)
- [Ashby custom careers page API](https://developers.ashbyhq.com/docs/creating-a-custom-careers-page)

## Harness behavior

`/demo-jobs` serves a fictional Platform Reliability Engineer role. Its application
page:

- fetches its field definition after page load;
- injects the form asynchronously;
- reveals work authorization only after country selection;
- applies server-side validation;
- records accepted applications in a separate durable table;
- exposes an authoritative lookup by stable application reference;
- can display **Application received** without writing the application when
  `mode=fake_success`.

The browser is forbidden to accept visible confirmation as proof. Every submit must
reconcile an independently queried receipt. The receipt element carries the immutable
server application ID, and that ID becomes the durable browser receipt.

## Results

| Test | Browser-visible result | Server ledger | Effect Browser result |
| --- | --- | --- | --- |
| Real dynamic submit | Application received | One application | `SUCCEEDED`; receipt ID matches ledger ID |
| Fake success | Application received | Empty | `OUTCOME_UNKNOWN`; no receipt; never success |
| Crash after real commit | Browser process lost | One application | Restart refuses retry, reconciles, zero duplicates |

All three cases run through real Microsoft Edge locally and installed Chromium in CI.

## General OpenAI planner result: failed safely

The live `gpt-5.6` planner was given only the synthetic jobs URL and a natural-language
instruction. The first call exposed an invalid root-reference layout in our strict JSON
Schema; that defect was fixed and regression-tested. The second accepted plan still
demonstrated the architectural limit:

- it proposed two generic click actions;
- it guessed nonexistent `Phone`, `Resume URL`, `Cover letter`, and `Task reference`
  fields;
- it invented a receipt URL not implemented by the target;
- it had never observed the live DOM.

The policy rejected the first ambiguous click. The task ended `FAILED`, the authoritative
application ledger remained empty, and no success was reported.

This means Effect Browser is currently a reliable executor for validated workflows, not
a general autonomous job-application agent. Calling it one would be false advertising.

## What must exist before real applications

1. An observe-decide-act loop that plans from a fresh accessibility/DOM snapshot instead
   of producing the whole plan before opening the page.
2. Site-specific handling for uploads, multi-page forms, iframes, CAPTCHAs, authentication,
   and changing custom questions.
3. A user-owned factual profile. The model must never invent work authorization,
   sponsorship, demographic, salary, or legal answers.
4. A mandatory final review before transmitting a résumé or submitting to an employer.
5. A truthful confirmation policy. If the ATS offers no authoritative receipt or
   uniquely queryable reference, the result must remain `UNCONFIRMED`, not `APPLIED`.
