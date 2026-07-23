# Research decision: do not build another browser agent

Research date: 2026-07-23. Sources are current primary product documentation.

## Decision

Generic model-driven browser control is commodity. Do not compete on “tell an agent what
to click.” Build the missing commit-control layer around browser automation instead.

**Selected product:** Effect Browser, a standalone browser-operations service that
persists plans, binds approval to an exact action and page observation, fences external
submits before execution, refuses to replay ambiguous outcomes, and reconciles receipts
when the target offers a stable business key.

This is an operations/reliability product, not a prompt-injection product. A stronger
model may choose better elements. It still cannot infer from a crashed TCP connection
whether a remote order, email, refund, or admin change committed.

## What is already commodity

- OpenAI's Computer use API already returns UI actions for a harness to execute, and its
  official guidance supports Playwright, custom tool harnesses, and code-execution
  harnesses. It specifically points mature systems toward their own execution,
  observability, retry, allow-list, and confirmation controls. The action-selection
  layer is therefore not a defensible standalone project.
  [OpenAI Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use)
- Browser Use sells natural-language extraction, form filling, multi-step workflows,
  monitoring, testing, scheduling, sessions, and browser profiles.
  [Browser Use cloud agent](https://docs.browser-use.com/cloud/agent/quickstart)
- Stagehand provides autonomous DOM/vision/hybrid agents, action history, reusable
  observed actions, custom tools, and runtime substitution for secrets.
  [Stagehand Agent](https://docs.stagehand.dev/v3/basics/agent)
- Microsoft's Playwright MCP gives models deterministic browser tools over accessibility
  snapshots, persistent or isolated profiles, and Docker execution. Its own README says
  it is not a security boundary.
  [Playwright MCP](https://github.com/microsoft/playwright-mcp)
- Guida already offers a local Windows browser with MCP, approvals, domain controls,
  queues, audit logs, and durable workspace data. Rebuilding that feature checklist
  would be pointless.
  [Guida](https://guida-app.dev/)

## The serious adjacent competitors

Cloudflare is the closest threat to this project. Its browser runtime can retain tabs,
cookies, and browser sessions across hibernation and human-approval pauses. That kills
“durable browser session” as a moat by itself.
[Cloudflare Agents browser runtime](https://developers.cloudflare.com/agents/tools/browser/)

Browserbase records sessions for debugging, while Playwright already supplies isolated
contexts, resilient locators, storage-state snapshots, and detailed traces.
[Browserbase session recording](https://docs.browserbase.com/platform/browser/observability/session-recording),
[Playwright isolation](https://playwright.dev/python/docs/browser-contexts),
[Playwright trace viewer](https://playwright.dev/python/docs/trace-viewer-intro)

These are useful substrates. Their public documentation does not define a transactional
contract for the interval after a remote submit may have committed but before the worker
durably records success. That interval is the product boundary.

## The real failure

A worker clicks “Place order.” The target commits the order. Before the success page is
captured, the browser crashes or the process loses its lease. A normal retry clicks the
button again. Better visual reasoning is irrelevant: the missing fact lives in remote
state, not in the model context.

The same failure appears in portals that send messages, book appointments, file claims,
change account settings, approve invoices, or create shipments. Teams use browsers
because those systems have no suitable API. That also means they usually lack native
idempotency keys.

## Honest guarantees

Effect Browser must not market fake exactly-once execution.

| Failure point | Guarantee |
| --- | --- |
| Before durable `DISPATCHING` | Safe to retry; no browser commit was attempted. |
| After `DISPATCHING`, before browser action | Conservative `OUTCOME_UNKNOWN`; no automatic retry. |
| Target confirms a stable idempotency/business key | Reconciliation can converge to one observable effect. |
| Target provides no key or queryable receipt | At-most-one dispatch plus explicit manual resolution only. |
| Page changes after approval | Approval is invalidated; the changed action must be reviewed again. |

The non-commodity value is the negative guarantee: after a crash, the system will not
quietly repeat an action that might already have succeeded.

## Provider boundary

OpenAI and Grok are interchangeable planners. OpenAI's computer-use guidance explicitly
keeps execution in the developer's harness. xAI's Responses-compatible function-calling
flow likewise returns client-side calls for local execution.
[xAI function calling](https://docs.x.ai/developers/tools/function-calling)

No provider receives authority to execute browser effects. Provider upgrades improve
planning quality without changing the durability contract.

## Kill criteria

Stop investing if one of these becomes true:

1. Design partners do not run browser workflows with costly duplicate-submit risk.
2. Target portals never expose a stable reference or receipt that can reconcile outcomes.
3. Cloudflare, Browserbase, or another substrate ships action-level commit fencing,
   approval hash binding, and ambiguous-outcome recovery as a general primitive.
4. Users only want personal browsing convenience. Consumer browser assistants already
   serve that market better.
