# Model routing that maintains itself

The user never edits a model name. Two roles are fixed; the model that fills each role is resolved when it is needed.

| Tier | Role | Codex family | Claude alias |
|---|---|---|---|
| `standard` | Everyday work; the default | `sol` | `opus` |
| `hard` | The flagship, only for genuinely hard tasks, with a recorded reason | `astra` | `fable` |

Versions are examples only and go stale by design. When this page was last revised (2026-11-19) the roles resolved to GPT-6.2 Sol / Claude Opus 5.5 and GPT-6.2 Astra / Claude Fable 5.2. Effort is `high`; raise it to a supported `xhigh` or `max` only for a stated reason. A harder reasoning level and a more expensive family are two separate decisions.

## How resolution works

- **Codex.** Each consultation asks the installed CLI for its live catalog (`codex app-server`: `initialize`, `initialized`, paginated `model/list`) and takes the highest version number of the tier's family among visible models. It fails clearly when the family or the effort is unavailable; there is no silent fallback to a different family.
- **Claude.** The CLI is called with the rolling alias, never a fixed model ID, so Claude Code picks the newest member itself. The job result lists the `modelUsage` keys as `actual_models`, and the bridge fails a job whose actual model is from a different family. An inherited `ANTHROPIC_DEFAULT_<ALIAS>_MODEL` override is refused, because it pins the alias to something the user did not choose here.
- **Families** live in one per-user file, `~/.agent-duet/models.json` (or `$AGENT_DUET_HOME`), shared by the Codex and Claude copies of the skill. With no file the table above applies.

## `duet_models`: the one call that keeps this current

`duet_models` resolves both tiers immediately and returns:

- `codex.standard` / `codex.hard`: the exact models a consultation would use now, plus `catalog`, the newest visible model of every family with its description.
- `claude.last_observed`: the actual models seen in recent Claude jobs, per alias.
- `actions`: what is out of date and the exact fix. Empty means `up_to_date`.
- `checked_at`: stored so that later calls know when a recheck is due (seven days).

Options: `refresh=true` also fetches the newest published CLI versions from the npm registry and compares them with the installed ones. `families={"codex":{"standard":"sol","hard":"astra"},"claude":{...}}` records a changed family.

Call it at the first use in a collaboration, when the user says a model was released, and when any result says `models_check_due`.

## The failure this prevents

A Codex CLI only lists models that existed when it was built. Left un-updated, it keeps resolving the newest model *it knows*, and its catalog then describes that model as a "previous generation" or "legacy" model. Consultations ran one version behind for two weeks this way before anyone looked. The bridge now treats those words on the selected model as evidence of a stale CLI: `duet_models` puts the update command in `actions`, and each job records `routing.warning`.

Fix: `npm install -g @openai/codex@latest` (and `claude update` for Claude Code), while no bridge job is running, then rerun `duet_models` and report the new resolution in one line.

## When a family changes

Vendors occasionally rename a line or put a new one on top. The catalog cannot tell which family is the flagship, so this needs one check by an agent, not by the user: read the official pages below, decide which family is the everyday line and which is the flagship, and store them with `families=`. `catalog` in the report shows every family the CLI can see, which makes a new or renamed one easy to spot. Never guess a family from its name.

- [OpenAI model guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [Codex app-server and model/list](https://learn.chatgpt.com/docs/app-server)
- [Claude model aliases and effort](https://code.claude.com/docs/en/model-config)
- [Claude programmatic CLI](https://code.claude.com/docs/en/headless)

## Limits worth knowing

- Codex CLI events may omit the server-resolved model identity; `actual_models` is then left empty instead of invented, and `routing.model` is the requested model.
- Provider, managed or settings-based mappings can still point a Claude alias at an older version of the same family. If `actual_models` looks old, inspect the installed CLI, provider and settings and report it; do not automatically run a second expensive call.
- A wake does not change the model of an original conversation; that is selected in each app.
- Do not use `best`, an account default, or `--bare` (it skips subscription credential discovery) as a substitute for the tiers.
- Old recipes that assume `codex mcp-server` exists are wrong for current CLIs, which use `app-server` and `exec`.
