# When something is in the way

Read the part that matches. In every case: repair what you can, then tell the user in one line only what is theirs to do.

## After a reboot or an app restart

Nothing has to be paired again. The project ID, the participant names, the mailbox and the pairing are on disk, and a Claude conversation that joined with its session UUID is found by that UUID at delivery time even though its `ListAgents` address changed. The only requirement is that both conversations are open again.

1. `duet_native_status check=true` reports, without any model usage, whether each UUID-bound Claude conversation is open right now and what to do if it is not (usually: open it). For Codex it reports `reachable: null`: a queued message is accepted either way and runs when the thread is open in the app, so only a reply proves delivery.
2. Read `duet_inbox` and continue from `duet_status.latest_checkpoint`. `awaiting_peer` shows your own handoffs still waiting; do not resend them.
3. An active schedule restarts its timer by itself at the first bridge call.

A fresh participant name and a new nonce check are needed only when the session UUID itself changed (a different conversation), or when a Claude conversation was joined by address without a UUID and that address changed. Joining with the session UUID avoids both.

Check that no helper process from before the restart (screen capture, viewers, monitors, test runs) is still running, and close it.

## A CLI is signed out

The desktop apps keep their own login. The `claude` or `codex` CLI can be signed out while the app works perfectly; every wake and consultation through that CLI then fails, which looks like the pairing "keeps disconnecting". `duet_doctor`, `duet_join`, `duet_native_wake`, `duet_consult` and `duet_native_status check=true` all detect it without spending anything.

The bridge does not fail quietly. When a wake or a consultation is refused for this reason it tries to show a desktop notification and to open that CLI's own sign-in in a terminal window, at most once per half hour per CLI. The result says what really happened in `tell_the_user`; on a machine with no desktop (SSH, a server) neither can be shown and it returns the command to run instead.

- Pass `tell_the_user` on in one line, in the user's language.
- If no window was opened, open it with `duet_doctor login=claude` or `login=codex`.
- Never ask for, type or handle credentials yourself.
- When the user says it is done, repeat the wake or consultation that was refused. A refused wake did not count against any limit and nothing has to be paired again.

## A wake did not go through

| Result | Meaning | What to do |
|---|---|---|
| `queued` / `attempted` | handed to the transport | wait for `read_by_peer` or a reply; this alone is not proof |
| `cooldown` | another wake to that side less than 20 s ago | batch, then repeat the same message |
| `limit_reached` | the wake rate limit for this window | nothing; the message is in the mailbox and the peer reads it at its next work boundary. Raise `max_wakes` with `duet_native_start` only if the task needs it |
| `courier_budget_reached` | the Claude courier estimate for this window is used | same as above |
| `not_sent` | refused before any transport call (signed out, CLI missing, paused) | fix the stated cause and repeat; it is safe to repeat |
| `unknown` | the transport may have run | check `duet_native_status` before waking that message again |

A pairing that says `paused`, `needs_user` or `completed` was stopped on purpose; `duet_native_start` (or setting a schedule) makes it active again.

## A consultation failed

- `error` plus `hint`: an expired sign-in; pass the hint on, do not retry.
- `routing.fallback`: the account could not use the requested model and the next best one ran; tell the user once.
- `routing.warning`: the model was resolved from a stale catalog; `duet_models update=true` when updates are allowed.
- `tool_access_issue`: a Codex shell command was refused by local policy; send the material as `files` instead, or use simpler commands.
- `timed_out`: the default is 20 minutes; give a long job `timeout_seconds` (up to 3600) and a narrower task.
- Anything else: one retry after fixing the stated cause, never a loop.

## A vendor's account is out of usage

A session, daily or weekly cap ("You've hit your 5-hour limit · resets 11pm") applies to every model of that vendor, so the bridge does not step down a model. The job result carries `account_limit` and a `hint` with the reset time; for the next 30 minutes further consultations and Claude courier wakes for that vendor are refused without a call, and `duet_doctor` lists the vendor as not ready. Tell the user once, in one line, when it resets; meanwhile ask the other vendor (`provider=codex` or `provider=claude`) or leave the work in the mailbox. A wake that was refused this way was never sent and can simply be repeated later. Only the vendor's own error text counts; a task or a reply that merely mentions a limit never triggers this.

## After the skill is upgraded

Nothing has to be taught or paired again. Every project keeps its ID, mailbox, checkpoints and pairing in its own `.agent-duet/` folder, and older state is migrated in place the first time the new version touches it. From 2.4 on, a conversation that is already open hands its calls to the newly installed bridge by itself and is told to fetch the new tool definitions; a conversation still running an older bridge needs its MCP tools reloaded once, and so does any conversation where a tool still looks old. Re-read SKILL.md when you next use the skill, because the rules may have changed; `duet_doctor` shows the installed version.

`duet_doctor upgrade=true` fetches the newest version into the checkout it was installed from and installs it. Use it when the user asks (更新 agent-duet), not on your own.

## The machine

At most three consultation CLIs (the workers behind `duet_consult` and background runs) run at once on the computer across all projects (`max_workers` in settings.json, or `AGENT_DUET_MAX_WORKERS`); a job beyond that waits for a free slot. The few-second model lookup and the short wake courier are outside that limit. Do not start jobs just to have them queued, and end a finished project with `duet_finish` so nothing lingers.
