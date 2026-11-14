# Current-model routing

Policy verified on 2026-11-07; verify official family roles again at the start of a later collaboration.

User-preferred standard tier: newest Sol and newest Opus. Hard tier: newest Astra and newest Fable. As of verification, examples are GPT-6.1 Sol / GPT-6 Astra and Claude Opus 5.5 / Fable 5.1. Versions are examples, not execution constants.

Codex queries `codex app-server` with `initialize`, `initialized`, and paginated `model/list`. It chooses the highest numeric version of the exact requested family among visible returned models and validates the requested effort against the returned list. It fails clearly if the family or effort is unavailable. This refreshes account availability on each call; it cannot discover a future renamed family by itself. The result records the requested model and live catalog provenance. Codex CLI events may omit actual server-resolved model identity; the bridge leaves `actual_models` empty in that case rather than inventing evidence.

Claude uses `opus` or `fable`, avoids a fixed model ID, and returns the `modelUsage` keys from the CLI result. Compare those keys to the current documented family version. The helper rejects inherited `ANTHROPIC_DEFAULT_OPUS_MODEL` / `ANTHROPIC_DEFAULT_FABLE_MODEL` overrides and flags a response whose actual model family mismatches the requested tier. Full work mode inherits normal settings/tools; optional review mode uses `--safe-mode` with restricted read tools. Do not substitute `--bare`: that mode skips subscription credential discovery. Provider/managed/settings-based mappings can still resolve older versions within the same family; the script cannot infer release chronology from an arbitrary provider ID. Inspect actual model evidence against current official docs. If alias resolution is old, investigate the installed CLI/provider/settings and report it; do not automatically run an additional expensive call.

Defaults: `high`; only raise to supported `xhigh` or `max` for a stated reason. Two tier changes are distinct: a harder reasoning level is not automatically a more expensive model family. Do not use `best`, an account default, or a silent fallback as a substitute for this user's explicit tier preference.

Official references:

- [OpenAI model guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [Codex app-server and model/list](https://learn.chatgpt.com/docs/app-server)
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Claude model aliases and effort](https://code.claude.com/docs/en/model-config)
- [Claude programmatic CLI](https://code.claude.com/docs/en/headless)

Never use old recipes that assume `codex mcp-server` exists. The installed Codex 0.164.0 uses app-server/exec instead.
