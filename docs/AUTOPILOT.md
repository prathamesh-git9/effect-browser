# One-query autopilot contract

## Blunt answer

Effect Browser now has a real one-query execution path. It does **not** and cannot
promise that every query will complete on every website without intervention.

The normal input is one natural-language string. The system resolves or validates the
target, chooses an available reactive provider, selects the configured or sole factual
profile, binds an approved document when present, infers a maximum of one external
commit from explicit verbs, creates a durable task, runs it, and derives its verdict
from stored actions and receipts.

The following remain hard stops:

- CAPTCHA, MFA, login credentials, payment-card or secret entry;
- a required personal, legal, demographic, or employment fact that has not been
  verified by the user;
- an unsupported control or multi-write submission flow;
- a target with no independent receipt lookup after a possibly committed action;
- exhausted 30-step, five-session-rollover, or one-commit budgets;
- a provider or browser failure that prevents evidence-backed completion.

Those are not missing prompt tricks. A model upgrade cannot supply user secrets,
legally attest unknown facts, bypass another party's access control, or prove a remote
commit that exposes no queryable evidence.

## Public surfaces

CLI:

```powershell
effect-browser do "Apply at https://example.test/jobs/123 using \"C:\approved\cv.pdf\""
```

HTTP:

```http
POST /v1/autopilot
Content-Type: application/json

{"query":"Check the service status at https://status.example.test"}
```

MCP:

```text
do_browser_task(query)
```

`/v1/autopilot` is the single-browser-task primitive. The dashboard, CLI `do`, and MCP
`do_browser_task` now place the durable mission scheduler above it; a simple request
still becomes one browser step, while a compound request can include parallel cited
research. See [MISSIONS.md](MISSIONS.md). The original URL, provider, profile,
document-hash, and authority controls remain under **Advanced task controls**.

## Resolution contract

1. An explicit HTTP(S) URL in the query wins. URL credentials are rejected.
2. Public targets must use HTTPS. Loopback, private, reserved, `.local`, and plain-HTTP
   targets must already be in `EFFECT_BROWSER_ALLOWED_ORIGINS`.
3. A URL-free query requires OpenAI or Grok. The resolver must call provider-hosted web
   search and cite the chosen target's origin; a URL produced without a recorded
   `web_search_call` and matching citation is rejected as an ungrounded guess.
4. OpenAI is selected when `OPENAI_API_KEY` exists, otherwise Grok is selected when
   `XAI_API_KEY` exists. `EFFECT_BROWSER_PROVIDER` can explicitly prefer either
   reactive provider.
5. Provider search finds only the start URL. The original user query remains the task
   instruction. A resolver cannot silently expand the user's intent.

The implementation follows the official provider-hosted web-search contracts:
[OpenAI Responses web search](https://developers.openai.com/api/docs/guides/tools-web-search)
and [xAI web search](https://docs.x.ai/developers/tools/web-search).

## Authority contract

The query is an authority envelope, not a universal permission bypass.

- Explicit commit verbs such as `apply`, `book`, `order`, `send`, or `submit` grant at
  most one reviewed external commit.
- Negation such as `do not submit` or `prepare only` grants no commit.
- An explicit supported document path, or `EFFECT_BROWSER_DEFAULT_DOCUMENT_PATH`,
  grants selection of only that hash-verified file. The path is replaced with
  `[approved local document]` before any provider prompt.
- `EFFECT_BROWSER_DEFAULT_PROFILE_ID` is used for profile-shaped workflows when
  configured. Without it, a saved profile is attached only when the query explicitly
  says to use the user's profile or saved details. A sole profile is not implicit
  consent to send its facts to a model provider.
- Password, OTP, API-key, PIN, CVV, and payment-card fields remain policy-blocked.

The first query-target origin is persisted in the task's bounded scope. This lets a
user-supplied public URL survive worker restarts without turning the global origin
allowlist into a wildcard.

## Verdict contract

`POST /v1/autopilot`, the CLI, and MCP return the same verdict:

| Verdict | Meaning |
| --- | --- |
| `verified_success` | A requested commit has a persisted authoritative receipt, or a requested download has a hash-verified download receipt. |
| `needs_input` | A verified fact or named human-only step is required. |
| `needs_authority` | The plan exceeded the authority inferable from the query. |
| `outcome_unknown` | Dispatch may have occurred, so automatic retry is forbidden. |
| `blocked` | Challenge, provider failure, policy, or execution budget stopped progress. |
| `unverified` | The planner stopped, but no goal-specific receipt proves the requested external effect or read-only outcome. |
| `failed` | The durable task failed before it could prove completion. |

Visible thank-you text is not evidence. A model choosing `finish` cannot produce
`verified_success` by itself. Effect queries require a succeeded `submit` plus
authoritative receipt; read-only final state is evidence-captured but remains
`unverified` unless a deterministic goal-specific contract exists.

## Crash-safe session rollover

An abort-first request preview can mutate browser-local form state even though the
network write was blocked. Bounded mode therefore persists and authorizes the exact
request, closes that browser, rehydrates the task in a fresh browser, and dispatches
only when the regenerated request fingerprint still matches. The rollover is automatic
and capped at five browser sessions.

This is the important difference between autonomy and a long prompt: the proof and
retry decisions survive browser and worker process boundaries.

## Configuration

```dotenv
EFFECT_BROWSER_PROVIDER=auto
EFFECT_BROWSER_DEFAULT_PROFILE_ID=
EFFECT_BROWSER_DEFAULT_DOCUMENT_PATH=
OPENAI_API_KEY=
XAI_API_KEY=
```

The default document still has to be absolute, exist, and fall under
`EFFECT_BROWSER_ALLOWED_UPLOAD_ROOTS`. Upload-triggered writes still require an exact
origin in `EFFECT_BROWSER_ALLOWED_UPLOAD_ORIGINS`.

## Acceptance proof

`test_one_query_autopilot_proves_real_browser_completion` starts the real FastAPI
target, submits one natural-language query, drives real Chromium, crosses the safe
session rollover, verifies one authoritative order, and asserts zero duplicate
attempts. `test_model_finish_cannot_fake_a_requested_commit` proves that a planner
cannot convert a bare `finish` into claimed success.
