Continue building Effect Browser toward the contract in
docs/AUTONOMOUS_ROADMAP.md.

Work on exactly one highest-priority unchecked R-item or one concrete failing
acceptance criterion. Read AGENTS.md and all directly relevant code first.
Implement a coherent production-quality slice, add regression tests, and run
the required verification commands.

Hard boundaries:

- Work only in this repository/worktree.
- Never browse to, authenticate to, or submit data to a real employer or other
  external transactional site.
- Never invent user facts or add personal data.
- Never weaken action approval, target reconciliation, tenant isolation,
  crash behavior, or audit integrity.
- Never auto-retry an ambiguous external commit.
- Never modify docs/AUTONOMOUS_ROADMAP.md or create
  automation/AUTOMATION_COMPLETE.
- Never push, merge, rebase, reset, or force-update Git history.
- This is a dedicated automation worktree. If it contains unfinished changes
  from a previous time-limited run, inspect the diff first and continue that
  same roadmap slice only when the intent is unambiguous. Never discard or
  overwrite unfinished changes.

Keep the slice small enough to verify within this run. Stop expanding scope
after six minutes, run the most relevant focused checks, and commit only if
the slice is green. When the slice is green, commit it on the current
automation branch with a specific message. If a safe continuation cannot be
established, do not alter the existing changes and explain the exact blocker
in the final response.
