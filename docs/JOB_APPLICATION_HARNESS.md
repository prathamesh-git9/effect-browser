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
- moves from identity questions to a separate document/review step;
- attaches a synthetic résumé and submits canonicalized multipart form data;
- applies server-side validation;
- records accepted applications in a separate durable table;
- exposes an authoritative lookup by stable application reference;
- can display **Application received** without writing the application when
  `mode=fake_success`.
- can mutate a JavaScript nonce after approval (`mode=payload_drift`);
- can attempt an unreviewed upload from a file-input change handler
  (`mode=auto_upload`).

The browser is forbidden to accept visible confirmation as proof. Every submit must
reconcile an independently queried receipt. The receipt element carries the immutable
server application ID, and that ID becomes the durable browser receipt.

## Results

| Test | Browser-visible result | Server ledger | Effect Browser result |
| --- | --- | --- | --- |
| Real dynamic submit | Application received | One application | `SUCCEEDED`; receipt ID matches ledger ID |
| Fake success | Application received | Empty | `OUTCOME_UNKNOWN`; no receipt; never success |
| Crash after real commit | Browser process lost | One application | Restart refuses retry, reconciles, zero duplicates |
| Payload changes after approval | Nothing accepted | Empty | Request aborted; `FAILED` |
| File-change auto-upload | No upload received | Zero attempts | Request aborted; `FAILED` |

The cases run through real Microsoft Edge locally and installed Chromium in CI. The
generic deterministic test planner knows only field semantics and synthetic facts; it
does not contain target selectors or a fixed site action plan.

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

This one-shot baseline remains a failure. The newer reactive contract fixes observation
by selecting only fresh Scrapling candidates, but it does not make arbitrary websites
universally supported. Calling the product a universal autonomous browser would still
be false advertising.

## What must exist before real applications

1. Wire the durable user-owned factual profile into planning. The current generic
   harness planner uses only hard-coded synthetic facts; it is not authorized for a
   person's application.
2. Add explicit human-handoff states for iframes, CAPTCHAs, MFA, authentication, and
   unsupported custom widgets.
3. Add a reviewed protocol for ATS products that auto-upload files or use multiple
   writes. The MVP blocks those flows instead of weakening the one-request guarantee.
4. Define an authoritative receipt adapter per target. If the ATS offers no uniquely
   queryable evidence, the result must remain `UNCONFIRMED`, not `APPLIED`.
5. Run and publish raw live-provider evaluation counts. Passing this deterministic
   harness does not prove OpenAI or Grok chooses correct actions on arbitrary sites.
