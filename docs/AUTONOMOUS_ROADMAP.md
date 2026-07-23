# Autonomous browser completion contract

This file is the stop condition for both interactive and scheduled development.
The product is not "done" because a demo looks convincing. It is done only when
the acceptance criteria below are implemented, tested, documented, and green in
CI.

## Product contract

Given a natural-language instruction and a starting URL, Effect Browser opens a
real browser, repeatedly observes the current page, chooses one action from
freshly discovered candidates, executes it through deterministic policy, and
continues until it completes, pauses for user authority, or reports an honest
blocker.

For a job application, the user supplies a factual profile and résumé. The
system may prepare every answer automatically, but it must show the exact final
payload before transmitting personal data. It never invents legal, demographic,
work-authorization, sponsorship, salary, or identity answers.

## Non-negotiable truth states

- `PREPARED`: the form is filled but nothing has been submitted.
- `AWAITING_APPROVAL`: the exact action and current page hash need user approval.
- `SUBMITTED_VERIFIED`: independent target evidence identifies the stored effect.
- `SUBMITTED_UNCONFIRMED`: dispatch happened but persistence cannot be proved.
- `BLOCKED`: CAPTCHA, MFA, missing factual data, unsupported UI, or policy stopped
  the task.

Visible success text alone can never produce `SUBMITTED_VERIFIED`.

## Acceptance criteria

- [x] A reactive provider observes the page before every planned action.
- [x] The model can select only candidates present in the fresh snapshot.
- [x] Scrapling generates candidates and relocates a saved element after DOM drift.
- [x] Links, reversible controls, ambiguous clicks, and commits receive different
      deterministic risk treatment.
- [ ] The engine supports navigation, click, fill, select, file upload, and submit.
- [ ] A factual profile stores answer value, source, sensitivity, and user
      verification state.
- [ ] Missing or unverified consequential answers stop the workflow.
- [ ] Résumé bytes stay outside prompts and logs; only approved local paths reach
      the browser executor.
- [ ] A final review displays every outgoing field and document hash before submit.
- [ ] Approval binds the final action, outgoing payload hash, and page-state hash.
- [ ] The dynamic ATS harness includes hydration, conditional fields, file upload,
      an iframe or multi-step transition, duplicate protection, and fake success.
- [ ] A generic reactive planner completes the harness without a site-specific plan.
- [ ] Fake success never becomes verified success.
- [ ] A crash after target commit never causes an automatic second submit.
- [ ] CAPTCHA and MFA produce an explicit human-handoff state.
- [ ] Task, action, approval, profile, receipt, and audit APIs are tenant-isolated.
- [ ] CLI, dashboard, MCP, Docker, and operations documentation expose the workflow.
- [ ] Python 3.11 and 3.13 CI, Ruff, full tests, wheel build, and Docker build pass.
- [ ] A live-model evaluation report includes raw pass/fail counts and known limits.

## Ordered work queue

- [x] R1: Rendered-page snapshot and Scrapling candidate extraction.
- [x] R2: Single-step provider contract and provider implementations.
- [x] R3: Durable dynamic action append and reactive engine loop.
- [x] R4: Candidate-bound click policy and selector drift recovery.
- [ ] R5: Factual profile and answer provenance.
- [ ] R6: File upload with path allowlist and content hash.
- [ ] R7: Final outgoing-payload review gate.
- [ ] R8: Multi-page adversarial ATS harness.
- [ ] R9: Generic deterministic reactive-agent tests.
- [ ] R10: Live OpenAI and Grok evaluations.
- [ ] R11: Dashboard, CLI, MCP, operations, packaging, and release hardening.

## Scheduled-run rules

Each unattended run takes one unchecked `R` item or one failing acceptance
criterion. It must:

1. Work only in the dedicated automation worktree.
2. Read `AGENTS.md` and this file before editing.
3. Never use personal data or contact a real external service.
4. Never weaken approval, reconciliation, tenant isolation, or audit behavior.
5. Run focused tests, then the repository verification commands.
6. Commit only a green, coherent change to the automation branch.
7. Never push, merge, rewrite history, or edit this completion contract.
8. Stop when `automation/AUTOMATION_COMPLETE` exists.

Only a human-reviewed merge may check the final acceptance criterion and create
the completion marker.
