# Background cooperation and idle wake

For the user's **original desktop conversations**, use [native-conversations.md](native-conversations.md) first. This page describes separate background workers and the older experimental MCP channel route; neither substitutes for native original-context pairing.

## Task-scoped autonomous mode (works without native GUI wake)

`duet_autopilot_start` takes root/project ID/requester/goal/acceptance and optional workdir, first_provider, max_turns, max_hours, job_timeout_seconds. It starts a detached Python supervisor. A single project cannot have two active supervisors, and existing CLI jobs must finish or be cancelled before the supervisor takes ownership. The workers always use `mode=work` (full CLI permissions) regardless of the installation's consult default, because an autonomous run must be able to edit and test; starting autopilot is the explicit opt-in. They use the ordinary tier and high effort; a reasoned escalation request in a handoff may select the hard tier next.

Each worker ends with a `DUET_RESULT:` JSON record. `continue` schedules the counterpart; `ready` schedules independent review; only a reviewer with observed evidence may return `complete` for final completion. The opposite provider receives the prior handoff and must inspect actual artifacts. This is model-driven verification, not a mathematical guarantee; the coordinator should request concrete tests and evidence suited to the task.

`needs_user`, failure, stop and exhausted limits are separate from completion. A stale supervisor is identified through recorded process creation identity. Resume preserves finished handoffs and recovers a launched job rather than blindly repeating its effects. An unsuccessful/invalid-report job can be retired on explicit resume, carrying its partial-work context into the next assignment. Supply `instructions` on resume to answer a blocker. For a materially different goal/acceptance definition, stop the current run and create a new one.

Defaults: 40 worker turns, 8 hours total, 15 minutes per worker. User/agent may set appropriate bounds (2–500 turns, up to 168 hours, at most 30 minutes per worker). A limit does not mean the goal was achieved. No operating-system scheduled task, login service, external relay or automatic reboot restart is installed. The machine must remain awake and online. Sleep pauses progress; if the wall-clock deadline passes during sleep, the run stops on wake.

The supervisor persists state in the project's SQLite database, writes `.agent-duet/AUTOPILOT.md`, and sends a result message to the initiating participant's mailbox. This mailbox message is not an automatic desktop toast or proof a GUI chat saw it. Poll `duet_autopilot_status` when the user asks for progress; there is no need for repeated user prompts to advance work. Do not change or close unrelated chats.

## Claude Code native push

The MCP server advertises `experimental['claude/channel']` and `duet_watch` binds that particular MCP connection to one registered project participant. It emits `notifications/claude/channel` for unacknowledged messages with project ID, root, sender and message ID. Acknowledgement remains explicit. The binding cannot silently switch to another project, and no network listener is opened.

For a custom local channel, current Claude Code docs require starting the session with:

```text
claude --dangerously-load-development-channels server:agent-duet
```

To retain an existing Claude Code conversation, use its exact `--resume <session-id>` when starting that session with the channel flag, after confirming the original is not running concurrently. Do not guess an ID or resume an active conversation in a second writer. The development-channel confirmation belongs to Claude Code; follow its prompt rather than suppressing organizational policy. After startup, join the project and call `duet_watch`. The session must remain open. Ordinary Claude Desktop Chat and a Code session in an isolated VM are different surfaces and are not automatically covered.

Official sources: [Channels](https://code.claude.com/docs/en/channels), [channel capability and notification protocol](https://code.claude.com/docs/en/channels-reference). Preview flags and protocol can change; refresh these docs if startup behavior differs. A notification written to stdout can still be dropped by a client that did not opt in. Only a response/ack proves the event reached the agent.

Local validation status (2026-11-07): notification format, project binding and acknowledgement behavior passed tests. A real Claude Code streaming/print session joined, called watch and finished its initial turn, but did not reply to the subsequent idle event within 120 seconds. Native idle wake is therefore experimental and NOT verified on this installation. Do not claim it is enabled for the user's existing session, and do not make unattended progress depend on it. Use the tested autonomous supervisor. Interactive-session opt-in may behave differently; verify a real roundtrip if setting that up later.

## Codex original-session wake

Official [app-server](https://learn.chatgpt.com/docs/app-server) supports starting/steering a turn on an owned thread; [developer commands](https://learn.chatgpt.com/docs/developer-commands) describe shared daemon transports. The app also exposes host messaging tools to authorized running chats. These capabilities are not interchangeable with launching a fresh `codex exec`.

On this machine the earlier shared daemon probe failed to connect to the control socket on 2026-11-07. A later supported `codex queue --thread UUID --message TEXT` call queued the ADQ-20261107 probe while the original skill-building Desktop chat was busy. After that turn completed, the probe automatically started another turn in the same thread with its original context; host read_thread confirmed this. This proves queued continuation in the loaded Desktop chat, not resurrection of a closed/unloaded chat. Version 2.1 adds this queue transport, but queue acceptance alone remains insufficient for other targets: use the actual pair handshake in [native-conversations.md](native-conversations.md). No private desktop endpoints, access tokens or UI typing hacks are used.
