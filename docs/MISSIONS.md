# Durable multi-search missions

## What changed

`effect-browser do` is now a mission entry point. One natural-language request is
planned into a persisted, topologically ordered graph of at most eight steps:

- `research`: one independent provider-hosted web search with required citations;
- `synthesis`: a bounded reduction over persisted dependency outputs;
- `browser`: one existing receipt-backed Effect Browser child task.

The low-level `/v1/autopilot` endpoint remains available for callers that explicitly
want one browser task. The dashboard, CLI `do` command, and MCP `do_browser_task` use
the mission path.

## Execution contract

1. The selected provider returns a strict `MissionPlan`. Step keys are unique and each
   dependency must point backward, so cycles and forward references are rejected
   before persistence. The validator also computes every ready wave and rejects a wave
   wider than four, including a narrow root that fans out later.
2. The plan, original query, authority budget, reserved browser child IDs, and every
   step state are written to the database before step execution.
3. Ready research steps run concurrently, bounded by
   `EFFECT_BROWSER_MISSION_MAX_PARALLEL_RESEARCH` (default `4`, maximum `8`).
4. Each research response must contain a completed provider `web_search_call` and at
   least one absolute HTTP(S) citation. A prose answer without tool evidence fails.
5. Step outputs receive a canonical SHA-256 and mission lifecycle events join the
   tenant's existing tamper-evident audit chain.
6. Synthesis can cite only URLs already present in its dependency outputs. Invented
   citations fail the step.
7. A mission may contain at most one browser step. A committing parent must contain
   exactly one. The child receives the original query—not planner-authored step text—
   and no more authority than that query granted.
8. An unverified, blocked, unknown, or input-waiting browser child blocks the parent.
   The mission cannot convert model confidence or visible success text into success.

The scheduler uses a mission lease with a background heartbeat during long browser
work. If a worker disappears, a later worker resets
interrupted read steps to pending and retains completed hashed outputs. A browser step
has a reserved child task UUID. If the process disappears after child creation, resume
loads that task instead of planning a second one; the existing task's effect-key,
dispatch, reconciliation, and receipt rules remain authoritative.

The four-step ready-wave limit is a plan-integrity boundary, not only a worker-pool
setting. It keeps provider-authored fan-out bounded even when an operator configures a
larger runtime pool. The existing one-browser-step rule independently prevents synthesis
text or a second branch from smuggling in another committing child.

Mission-owned child tasks cannot run through the generic task API, CLI command, MCP
tool, or polling worker. Approvals and verified facts may update the child, but browser
execution resumes through the parent mission. This prevents a failed parent from
leaving an independently runnable pre-authorized task behind.

## Authority is not multiplied

The caller's explicit grant and the original query jointly own the total
external-effect budget. A planner cannot grant authority by writing stronger child
instructions.

- Without `--commit` or `allow_external_commit: true`, every browser child has zero
  commit authority even if the query contains `apply`, `book`, `order`, or `submit`.
- A caller grant without a supported commit verb also gives zero authority.
- A caller grant plus a supported commit verb permits exactly one browser child and
  at most one reviewed external commit.
- A caller grant that contradicts `do not submit`, `prepare only`, or equivalent
  read-only language is rejected instead of silently weakening the user's words.
- Research and synthesis are always read-only.
- Browser effects still require the existing abort-first request preview, page-state
  binding, transmission guard, and authoritative receipt.

Retrieved research is deliberately **not** injected into a committing browser child's
instruction. Dependencies gate when the child may run and remain available in the
mission report, but untrusted web text cannot rewrite the target, facts, values, or
authority of an external action. Automatic evidence-driven selection of a commit
target is a later feature and will require a deterministic policy stronger than
“the model preferred this result.”

## Public surfaces

CLI:

```powershell
effect-browser do "Research official pricing, reliability, and limits, then compare them."
effect-browser mission "Research two options, then download the chosen public report."
effect-browser do "Submit the form at https://example.test/form" --commit
```

HTTP:

```http
POST /v1/missions
Content-Type: application/json

{"query":"Research official pricing, reliability, and limits, then compare them."}
```

Committing HTTP requests must add `"allow_external_commit": true`. MCP callers pass the
same optional boolean to `do_browser_task`.

Inspection and resume:

```http
GET /v1/missions
GET /v1/missions/{mission_id}
POST /v1/missions/{mission_id}/run
```

`effect-browser worker` also polls persisted `queued`/`running` missions before its
normal task queue. It does not auto-reopen a human-gated blocked mission; after the
underlying child approval, input, or recovery state changes, explicitly call the
mission run endpoint or use the dashboard's **Run / resume mission** control.

MCP:

```text
do_browser_task(query)
get_browser_mission(mission_id)
```

## Verdicts

| Verdict | Meaning |
| --- | --- |
| `running` | The persisted mission is queued or running; no terminal outcome is claimed. |
| `completed` | Every persisted step completed; research has citations and output hashes. This does not mean cited claims are independently true. |
| `verified_effect` | Every step completed and a browser submit has authoritative receipt evidence. |
| `needs_input` | A browser child stopped for a missing verified fact or human-only step. |
| `needs_authority` | A browser child reached an authority boundary. |
| `outcome_unknown` | A browser dispatch may have occurred; automatic retry remains forbidden. |
| `blocked` | A child result was unverified or policy/challenge constraints stopped progress. |
| `failed` | Planning, provider, graph, scheduler, or execution logic failed without claiming success. |

## What this does not prove

A citation proves that the provider returned a source URL for a live search. It does
not prove that the page is correct, independent, current, or free of manipulation.
Synthesis remains probabilistic. The durable value is narrower: the app can show which
searches ran, which URLs were cited, which inputs reached synthesis, which outputs were
stored, and whether a real browser effect has a receipt.

The scheduler is bounded, not an open-ended autonomous loop. It does not bypass
CAPTCHA, MFA, credentials, payments, missing user facts, website terms, or unsupported
multi-write flows.

## Acceptance harness

- `test_mission_runs_independent_searches_concurrently_and_persists_citations` proves
  real overlap, output hashes, citations, and audit-chain integrity.
- `test_resume_retries_only_interrupted_read_step_and_keeps_completed_output` proves a
  worker restart does not repeat completed research.
- `test_unverified_browser_child_blocks_parent_without_claiming_success` proves
  anti-lie propagation.
- `test_multi_search_mission_gates_one_real_browser_commit` runs two research steps,
  then drives real Chromium through one receipt-backed demo order and verifies zero
  duplicates.
- `test_dashboard_renders_multi_search_dag_and_cited_result` submits through the real
  dashboard in Chromium and inspects the rendered DAG and source links.
- `test_mission_graph_validation.py` rejects cycles, over-wide roots and later fan-out,
  two browser children, and a synthesis attempt to create a second committing branch.
