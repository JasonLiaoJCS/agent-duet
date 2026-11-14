# Practical ideas adapted from existing projects

Research checked 2026-11-07. The implementation and wording here were independently written; no third-party source files were bundled. Links credit the workflow ideas, not a claim that these packages were installed or fully audited.

| Source | Useful idea | Agent Duet implementation |
|---|---|---|
| [PAL clink](https://github.com/BeehiveInnovations/pal-mcp-server/blob/main/docs/tools/clink.md) | Cross-CLI questions with distinct roles and follow-ups | `consult` supports planner/reviewer/debugger/general and bounded `previous_job_id` context forwarding |
| [codex-collab](https://github.com/Kevin7Qi/codex-collab) | Detached work, status inspection and model discovery | Asynchronous jobs, cancellation, live Codex model catalog and clear account/model evidence |
| [local-agent-bridge](https://github.com/normcrandall/local-agent-bridge) | Visible handoffs, durable state, bounded discussion and recursion control | Project-bound mailboxes, checkpoints, explicit progress/results and nested-spawn rejection |
| [Clodex](https://github.com/9thLevelSoftware/Clodex) | Persistent handoff and review of the same code revision | Saved verification evidence and a rule to recheck changes made after review |
| [Claude–Codex collaboration skill](https://github.com/mzopedia/claude-code-codex-collaboration-skill/blob/main/SKILL.md) | Verify returned work, continue corrections, distinguish failures from completion | Acceptance checks remain with the coordinator; diagnostics and focused follow-ups stay visible |

Agent Duet adapts these ideas to this user's preferences: symmetric Codex/Claude participation, explicit project identity, latest Sol/Opus for normal work, Astra/Fable only for difficult work, and high effort by default. It does not inherit other projects' fixed vendor leadership, fixed model versions or unbounded loops. Full CLI permissions in work mode were added because this user explicitly requested them, with an optional read-only review mode retained.

The durable handoff is deliberately small: completed outcome, evidence, remaining work, next action. Use it to resume work without dragging a whole transcript into a fresh model context. Human-facing output remains natural prose; structured transport should stay behind the scenes.
