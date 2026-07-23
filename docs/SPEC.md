# Effect Browser specification

## Pitch

Effect Browser is a self-hosted control plane for AI-driven browser operations against
portals with no usable API. Models propose typed actions. A durable executor performs
safe navigation automatically, pauses on external commits, binds approval to the exact
action and observed page, and never silently retries a submit whose outcome is unknown.
Where a stable business reference exists, deterministic reconciliation produces a
receipt; otherwise the run stops for an operator.

## Users and problem

Target users are operations, finance, implementation, and support teams automating
repetitive vendor or customer portals. Their expensive failure is not a bad summary. It
is a duplicate shipment, duplicate booking, repeated message, or conflicting account
change after a timeout, worker restart, or expired browser session.

A stronger model does not solve distributed uncertainty between the local journal and a
remote web application. Only a state machine, commit fence, and target-side evidence can.

## Architecture

```text
Instruction -> Planner (deterministic/OpenAI/Grok) -> typed action proposals
                                                        |
                                                        v
Dashboard/API/MCP -> Policy -> durable PREPARED / APPROVAL_REQUIRED
                                                        |
                                                        v
                                              Playwright executor
                                                        |
                         +------------------------------+------------------+
                         |                                                 |
                  receipt recorded                                OUTCOME_UNKNOWN
                         |                                                 |
                         v                                                 v
                    SUCCEEDED                                  deterministic reconciler
```

Package layout:

```text
src/effect_browser/
  api.py                 FastAPI service and dashboard routes
  cli.py                 Typer operator and worker commands
  config.py              environment settings
  domain.py              frozen contracts and state machines
  engine.py              durable execution and recovery semantics
  policy.py              deterministic action classification
  store.py               SQLAlchemy persistence and hash-chained events
  browser/base.py        driver and reconciliation protocols
  browser/playwright.py  real browser adapter
  providers/             deterministic, OpenAI, and Grok planners
  demo_target.py         local order portal used by the killer demo
  mcp_server.py          safe MCP surface; no model-accessible approval tool
  web/                   operator dashboard
```

## Core contracts

```python
class Planner(Protocol):
    def plan(self, request: PlanRequest) -> tuple[ProposedAction, ...]: ...


class BrowserDriver(Protocol):
    def observe(self) -> Observation: ...
    def execute(self, action: BrowserAction) -> BrowserReceipt: ...
    def reconcile(self, spec: ReconciliationSpec) -> BrowserReceipt | None: ...


class ActionState(StrEnum):
    PENDING = "pending"
    APPROVAL_REQUIRED = "approval_required"
    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
```

Approval records contain `action_sha256`, `observation_sha256`, actor, and decision
time. Commands supply an expected action version and mutation locks the database row.
The
executor writes `DISPATCHING` and its audit event before calling Playwright. A worker
that encounters a previously dispatching action converts it to `OUTCOME_UNKNOWN` and
does not call the browser.

## Data guarantees

- One active worker lease per task.
- State mutation and hash-chained audit append share one transaction.
- Optimistic versions protect approval and resolution commands.
- Approval becomes invalid if action bytes or pre-dispatch observation change.
- Commit actions have an immutable effect key.
- `OUTCOME_UNKNOWN` is terminal until deterministic reconciliation or an explicit
  operator resolution.
- Exactly-once is claimed only when the target accepts a stable idempotency key or its
  state can be uniquely reconciled. Otherwise the guarantee is at-most-one dispatch.

## Killer demo

The included order portal accepts a stable customer reference. The agent fills an order
and pauses immediately before “Place order.” After approval, the demo injects a process
failure after the portal commits but before Effect Browser records success. On restart,
the worker does not click again. It reports `OUTCOME_UNKNOWN`, queries the order by the
stable reference, records the existing receipt, and completes with exactly one order.

A plain LLM call cannot reproduce this guarantee because the relevant state spans two
process lifetimes and a remote commit. A generic Playwright retry can reproduce the
workflow but not safely decide whether to retry the submit.

## MVP

- Typed plans and deterministic/OpenAI/Grok providers.
- Playwright driver with role/label locators and isolated contexts.
- Origin allow list and hard approval for every external commit.
- Approval hash binding and drift invalidation.
- SQLite/PostgreSQL-compatible durable store and hash-chained events.
- Explicit unknown outcomes, reconciliation, and manual resolution.
- Typer CLI, FastAPI API/dashboard, safe MCP server, Prometheus metrics.
- Local order-portal demo, unit tests, real-browser end-to-end test, Docker, and CI.

Later scope: encrypted browser profiles, distributed worker queues, remote browser
providers, OIDC/RBAC, multi-reviewer thresholds, site-specific reconciliation plugins,
OpenTelemetry, and signed external audit anchors.

## Top risks

1. **Impossible generic exactly-once.** State the weaker guarantee prominently and make
   reconciliation capabilities explicit per workflow.
2. **Commodity overlap.** Do not grow a general browsing feature checklist; keep the
   product centered on commit correctness and proof.
3. **DOM drift.** Use accessible locators, observation hashes, and invalidate approval
   rather than self-healing across a commit boundary.
4. **Credential handling.** MVP has no credential vault. Use isolated demo/public sites;
   add encrypted, scoped secrets only with a dedicated threat model.
5. **Long-lived browser fragility.** Persist logical checkpoints, not a claim that a live
   browser process survives every failure.
6. **Auto-saving fields.** The MVP treats form fills in a validated workflow as
   preparation. A portal that commits on input/change needs a site-specific effect
   adapter; do not onboard it as a generic form workflow. Ambiguous generic clicks are
   rejected outright.

## Build order

1. Domain states, canonical hashes, and policy.
2. Transactional store, CAS transitions, leases, and audit verification.
3. Engine with crash injection and unknown-outcome tests.
4. Deterministic planner and fake browser for exhaustive state-machine tests.
5. Playwright driver and local order target.
6. HTTP API, CLI, worker, MCP, and dashboard.
7. Provider adapters with mocked contract tests.
8. Real-browser crash/reconciliation demo, packaging, Docker, and CI.
