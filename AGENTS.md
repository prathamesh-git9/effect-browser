# Repository instructions

Effect Browser treats model output as an untrusted proposal and browser commits as
fallible external effects.

- Never auto-retry an action once its dispatch may have reached the target.
- Never claim exactly-once execution without target idempotency or deterministic
  reconciliation.
- Approval must bind the exact action hash and observed page-state hash.
- State changes and audit events belong in one database transaction.
- The model may propose actions; only the executor may perform them.
- Keep secrets and page body text out of logs and durable observations.

Before finishing a change, run:

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe -m pytest
```

Use Python 3.11+, a `src/` layout, Hatchling, line length 90, and MIT licensing.
