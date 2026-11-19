# Setup, fallback and troubleshooting

## Install

Python 3.10+ and logged-in `codex` / `claude` CLIs are prerequisites; Python 3.11+ is recommended. One CLI is enough to install (mailbox, checkpoints and that provider's consultations work); native pairing needs both on the same machine. No runtime pip packages or API keys are needed by the bridge itself. It uses each installed CLI's own login and billing settings. Run `install-windows.cmd` (Windows) or `sh install.sh` (macOS/Linux) from the skill folder; both forward arguments to `scripts/install.py`. `--default-mode review|work` sets the consult default stored in `settings.json` (review if never chosen; a reinstall without the flag keeps the previous choice). `--auto-update on|off` stores whether Agent Duet may update an outdated `codex`/`claude` CLI by itself (off on a fresh install; `duet_models auto_update=true` sets the same preference later). Updates run in the background with the CLI's own updater (`npm install -g <package>@latest` for an npm install, `claude update` for the native Claude installer, `brew upgrade codex` for Homebrew); a CLI that is busy with a job is skipped and retried at the next check. The installer prints a short plain-language summary (`--json` for the full result). It also gives the `agent-duet` server a 600-second tool timeout in Codex's `config.toml`, because Codex's 60-second default is shorter than waiting for a consultation. `--upgrade` fast-forwards a git checkout and installs the fetched version; on a copy that is not a git checkout it stops without changing anything. An upgrade never requires re-pairing: project state is migrated in place. From 2.4 on, a bridge process that is already running hands its calls to the newly installed code. Coming from 2.3 or earlier, whose installer has no `--upgrade` and whose bridge has no hand-off, do it by hand once: `git pull --ff-only` in the checkout, run the installer, and reload the MCP tools in open conversations. The timeout edit to `config.toml` is made only when re-reading the file proves nothing else changed (it needs Python 3.11+); otherwise the installer says so and leaves the file alone. The summary also says whether each CLI is signed in. The desktop apps keep a separate login, so a CLI can be signed out while the app works; in an interactive terminal the installer offers to run the sign-in right away (`--no-login-prompt` skips that). Later, when a wake or consultation hits a signed-out CLI, the bridge sends a desktop notification and opens the sign-in window itself, at most once per half hour per CLI (`AGENT_DUET_NO_LOGIN_PROMPT=1` turns that off); `duet_doctor login=claude|codex` opens it on request. After installing, run `python scripts/duet.py call doctor --pretty`: it lists each CLI, its version, whether Codex has `queue`, whether Claude is new enough (2.1.234+) for native messaging, and a fix hint for every problem. It copies the skill into Codex and Claude user skill directories, registers the local stdio MCP server with both CLIs, and adds it to Claude Desktop when its configuration directory exists. Existing configuration is backed up before modification. An identical installation is safe to repeat. Restart/reload clients to discover the new tools; do not quit the user's active apps automatically.

For Claude Desktop, this installs the MCP tools; a local `.claude/skills` folder is a Claude Code skill location, not a guarantee that ordinary Desktop Chat imports the skill automatically. Upload the distributable skill ZIP in the Desktop skill interface if supported, or attach/paste SKILL.md into the conversation and tell it to use `duet_*` tools. Claude Desktop Code uses its Code environment. If Desktop runs Code in a VM rather than the host, confirm it can access the same root and local MCP endpoint; a host path alone does not connect an isolated VM.

## Background processes and resources

Nothing runs permanently. Three kinds of process exist, each tied to work the user asked for, and `duet_finish` ends all of them for a project:

- **Consultation workers** (`duet_consult`): one Python worker plus one model CLI per job, at most two per project and three at once on the computer (`max_workers` in `settings.json`, or `AGENT_DUET_MAX_WORKERS`, 1 to 8). A job beyond the limit waits for a free slot; a slot held by a process that died is reclaimed.
- **The schedule timer** (`duet_schedule`): one small Python process per project with an active schedule. It sleeps between checks and uses no model. It exits when the schedule ends, and any later bridge call restarts it if it died while the schedule is still active.
- **The autopilot supervisor** (`duet_autopilot_start`): one process per run.

The MCP server itself is started by each client and ends with it. No scheduled task, login item or service is installed.

## CLI fallback

Except connection-bound `watch`, MCP operations have an equivalent command:

`python <absolute-skill-path>/scripts/duet.py call <operation> --file <arguments.json>`

Use UTF-8 JSON files or `--file -` with stdin. Do not interpolate prompt text into a shell command. Operations and schemas are discoverable from `DEFS` in the helper or MCP `tools/list`. Example join arguments:

```json
{"root":"C:/projects/example","project_name":"Example","goal":"Implement and test CSV import","name":"codex-session123","provider":"codex","session":"actual-session-id"}
```

Keep the returned project ID, then include it in sends and consultations:

```json
{"root":"C:/projects/example","project_id":"ID_FROM_JOIN","requester":"codex-session123","provider":"claude","prompt":"Review the supplied algorithm and find a counterexample...","tier":"standard","effort":"high"}
```

A new machine can override CLI executable discovery through `AGENT_DUET_CODEX_BIN` and `AGENT_DUET_CLAUDE_BIN`. Windows npm shims (`codex.cmd`, `claude.cmd`) are resolved to Node plus the package entrypoint, or to a bundled `.exe`, without passing task text through cmd.exe.

## Project state

The shared root gets `.agent-duet/state.sqlite3`, consultation logs under `.agent-duet/jobs/`, and autonomous progress in `.agent-duet/AUTOPILOT.md`. These can contain project content and should remain local; add `.agent-duet/` to that project's local Git exclude if appropriate. Do not commit or upload the database by default. No network listener, public tunnel, scheduled task or login service is installed. Autonomous mode creates a task-scoped background supervisor that exits when finished/stopped/blocked/limited; it does not resume after a reboot by itself.

Copied/moved state is rejected because its recorded root differs. For a legitimate move, finish/cancel old jobs and explicitly set up a fresh collaboration at the new root; preserve the old state as an archive rather than silently rebinding it. Participant names bind to one provider/session; use a new suffix for a new conversation.

`doctor` checks executable versions without inference. `models` discovers Codex's live catalog. `status` lists participants, jobs and the latest handoff checkpoint. `job` exposes model/usage evidence, errors and overdue state. Process identity reconciliation recovers confirmed interrupted jobs; orphaned live children retain capacity until a verified cancellation. Authentication failures remain failures; the helper does not switch to a different billing provider. A failed job can be retried once after fixing the cause. Don't restart unrelated sessions or kill a process solely because a stored PID matches.

## Existing alternatives considered

- [PAL clink](https://github.com/BeehiveInnovations/pal-mcp-server/blob/main/docs/tools/clink.md): multi-CLI delegation; inspect permission defaults before adoption.
- [codex-collab](https://github.com/Kevin7Qi/codex-collab): Claude Code skill driving Codex app-server.
- [local-agent-bridge](https://github.com/normcrandall/local-agent-bridge): cross-CLI MCP collaboration with bundled skills.

Agent Duet is an independent small implementation tailored to explicit project identity, symmetric existing-session mailboxes, the user's model tier policy, and bounded consultations. These repositories were researched, not installed or copied.
