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
- If the worktree is dirty before you start, report that and stop.

When the slice is green, commit it on the current automation branch with a
specific message. If blocked, leave the worktree clean and explain the exact
blocker in the final response.
