# Active-session protocol

## Pairing

The join response contains the canonical root, project ID, stored goal and registered participants. Both sides must use that same collaboration root. The project ID belongs to that root, not to a folder's short name or Git remote. Two clones are different coordination roots unless the user explicitly chooses one shared root. Different hosts do not share this local transport.

Send a first `message` containing your exact project ID/root, goal, actual checkout/worktree, participant/session identity, proposed task ownership, and one acceptance test. The receiver checks these against its own task and replies with agreement or a specific correction. Start independent planning before the reply; hold edits that would conflict until ownership is agreed. Do not infer a live connection from a registration or a recently updated timestamp. Report pairing established only after receiving a real reply.

If an old collaboration has a different goal, name the new goal and get peer agreement before `set_goal`; pass the old goal as `expected_goal`. Its compare-and-set rejects stale updates and retains the prior goal as a checkpoint. The project ID stays stable. A user's direct change of direction authorizes the coordinator to notify peers and update the goal. For an entirely new project or intentionally separate collaboration, use the intended separate root. Never edit the state database manually to force an ID match.

## Message patterns

- `task`: outcome, exact scope, owner, constraints, inputs and acceptance test.
- `critique`: disputed claim, evidence/counterexample and proposed correction.
- `decision`: accepted approach, reasoning and remaining uncertainty.
- `result`: changed files or artifact, tests actually run, unresolved items.
- `cancel`: request to stop the named task; the recipient stops its own work and replies.
- `done`: no further work requested. Reply only when information or confirmation is needed.

Messages are durable until acknowledged. `inbox` does not consume them, but it records that the recipient's conversation has read them, which the sender sees as `read_by_peer`. A response sent with `reply_to` and kind `result`, `decision` or `done` closes its original; any other kind leaves it open. Mail follows the conversation: one that rejoined under a new name with the same session UUID still receives what was addressed to its earlier names (a Claude address used in place of a UUID never links two names). Use a stable unique dedupe key for a retry of the SAME message. No blind auto-replies to acknowledgements or completed discussions.

## Lead, deputy and change log

On long projects one agent leads (direction, priorities, final decisions, integration) and the other is deputy (assigned implementation, independent review, debugging). Write the split, the project ID and the wake rules into the project's own instructions or project skill so later sessions inherit them. The lead answers the deputy's objections with evidence, hears each objection once, decides, and records the reason in the shared plan. After every work burst that changed anything, send one batched change-log message: what changed, where, the commit, and what the peer should do differently. Change logs go to the mailbox only; they never justify a wake.

## Active loop and idle limits

Read inbox at task boundaries and before conclusions. While a peer is working on a blocking dependency, check every 15–30 seconds if the host permits a continuing turn. Continue independent tasks meanwhile. A skill cannot schedule its own model inference after the host has ended the turn; do not promise background awareness. A separate CLI consultation is the on-demand alternative and contains only the context explicitly supplied to it.

`claim` takes an exact shared resource key (`file:src/core.py` or an agreed `task:solver`). A different key or a parent directory does not conflict automatically. Use normalized relative slash-separated paths and agree on granularity. Leases expire by default after 15 minutes and can be renewed; check again before writing after a long pause. They are workflow signals, not a security boundary. Integration is owned by one participant.

## CLI consultations

`consult` returns immediately with a job ID. The worker chooses the model, starts a subprocess, and records its response and usage. `job` reports queued/resolving/running/completed/failed/cancelled/timed_out, plus interrupted/orphaned after an unexpected exit. Workers refresh their heartbeat. Reconciliation checks recorded process creation identities, not only PIDs: a confirmed dead worker and child become interrupted; a surviving/unknown child keeps its capacity reservation as orphaned. `cancel` can stop a verified surviving child. Unknown identity is reported for inspection, never treated as proof it stopped. At most two nonterminal jobs per project are admitted concurrently.

Choose a role appropriate to the question. When a call omits `mode`, the installation default applies: `settings.json` next to SKILL.md (written by the installer's `--default-mode`), overridable by the `AGENT_DUET_DEFAULT_MODE` environment variable, and `review` when neither exists. `mode=work` uses the full-permission CLI flags: Codex `--dangerously-bypass-approvals-and-sandbox`, Claude `--dangerously-skip-permissions`. Each inherits its normal configured tools; project/task scope still applies. This is not OS administrator elevation, account access creation, or an override of host/organization policy. Existing Desktop/CLI sessions retain their own host permissions; these flags configure bridge-launched workers.

For a user-requested restricted review, `mode=review` uses Claude restricted Read/Grep/Glob tools and Codex's read-only sandbox. Codex read-only is not path-confined reading. Neither mode is a general-purpose secure container. Use the caller's permissions as the upper bound.

For follow-up, `previous_job_id` imports a completed same-provider job's task and answer; the bridge refuses an oversized continuation instead of silently truncating it. There is no preservation of the old worker's hidden state. Supply `workdir` for a linked worktree or project subdirectory; it is validated against the collaboration root before launch.

Use `files` for explicit UTF-8 text snapshots within the project or validated worktree: at most 20 files, 200 KB each, 60000 characters combined. Relative paths resolve from `workdir`. Symlink escapes are rejected. The selection is sent to the chosen provider, just like the prompt; select only material relevant and authorized for this collaboration. The helper labels these snapshots as reference data. On this Windows installation, Codex shell reads can be denied by local policy; explicit snapshots allow a content review without asking the consultant to perform that shell action. `tool_access_issue` reports observed Codex policy errors; always inspect the answer before treating the task as successful.

Consultants inherit CLI account authentication, not a copy of the other desktop chat. Default runtime is 20 minutes, configurable from 10 to 3600 seconds. Collect a job with `job wait_seconds=...` (up to 540) rather than a polling loop. Escalating model or effort needs a concrete reason. The script refuses recursive `consult` calls via its depth marker; prompts also instruct consultants to return follow-up questions to the parent. These safeguards are cooperative and are not a hostile-code sandbox.
