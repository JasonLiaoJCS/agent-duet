#!/usr/bin/env python3
"""Install Agent Duet into local Codex/Claude skills and register MCP clients."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from duet import cli, FLAGS

def backup(path):
    if path.exists():
        target = path.with_name(path.name + ".agent-duet-backup-" + time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns() % 1000000))
        shutil.copy2(path, target)
        return str(target)
    return None

def copy_skill(source, destination):
    if source.resolve() == destination.resolve():
        return
    if destination.exists():
        marker = destination / "SKILL.md"
        if not marker.is_file() or "name: agent-duet" not in marker.read_text(encoding="utf-8"):
            raise RuntimeError(f"Refusing to overwrite unrelated directory: {destination}")
    backup_root = destination.parent.parent / "agent-duet-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    staged = backup_root / ("staging-" + str(time.time_ns()))
    shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".agent-duet", "settings.json"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    old = None
    if destination.exists():
        old = backup_root / ("skill-" + str(time.time_ns()))
        destination.rename(old)
    try:
        staged.rename(destination)
    except Exception:
        if old is not None:
            old.rename(destination)
        raise

def run(args, env=None):
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=45, creationflags=FLAGS)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()

def read_settings(*folders):
    for folder in folders:
        try:
            data = json.loads((folder/"settings.json").read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return {}

def write_settings(folder, data):
    tmp = folder/"settings.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    os.replace(tmp, folder/"settings.json")

def optional_cli(name):
    try:
        return cli(name), None
    except Exception as exc:
        return None, str(exc)

def set_codex_tool_timeout(config, seconds=600):
    """Codex stops an MCP tool call after 60 s by default, which is shorter than waiting for a
    consultation or updating a CLI. Give this one server a longer limit; leave everything else alone.
    The edit is written only if re-reading the file proves that nothing else changed."""
    try:
        import tomllib
    except ImportError:
        return "not changed (needs Python 3.11+ to edit config.toml safely); set tool_timeout_sec = %d under [mcp_servers.agent-duet] yourself" % seconds
    try:
        raw = config.read_bytes()
        text = raw.decode("utf-8")
        before = tomllib.loads(text)
    except (OSError, ValueError) as exc:
        return "not changed (%s)" % exc
    server = before.get("mcp_servers", {}).get("agent-duet")
    if not isinstance(server, dict):
        return "agent-duet server entry not found"
    if "tool_timeout_sec" in server:
        return "already set to %s s" % server["tool_timeout_sec"]
    lines = text.splitlines(keepends=True)
    header = next((i for i, line in enumerate(lines)
                   if line.split("#")[0].strip() in ("[mcp_servers.agent-duet]", '[mcp_servers."agent-duet"]', "[mcp_servers.'agent-duet']")), None)
    if header is None:
        return "not changed (the entry is not a plain [mcp_servers.agent-duet] table); set tool_timeout_sec = %d there yourself" % seconds
    ending = "\r\n" if lines[header].endswith("\r\n") else "\n"
    if not lines[header].endswith(("\n", "\r")):
        lines[header] += ending
    # Directly under the table header is always outside any multi-line value.
    lines.insert(header + 1, "tool_timeout_sec = %d" % seconds + ending)
    updated = "".join(lines)
    try:
        after = tomllib.loads(updated)
    except ValueError as exc:
        return "not changed (edit would not parse: %s)" % exc
    expected = json.loads(json.dumps(before, default=str))
    expected["mcp_servers"]["agent-duet"]["tool_timeout_sec"] = seconds
    if json.loads(json.dumps(after, default=str)) != expected:
        return "not changed (the edit would have altered other settings)"
    tmp = config.with_name(config.name + ".agent-duet-tmp")
    tmp.write_bytes(updated.encode("utf-8"))
    try:
        shutil.copymode(config, tmp)
    except OSError:
        pass
    os.replace(tmp, config)
    return "set to %d s" % seconds

def upgrade_source(source):
    """Fast-forward a git checkout of the skill before installing from it."""
    repo = source.parent
    git = shutil.which("git")
    if not git or not (repo/".git").exists():
        raise SystemExit("--upgrade needs a git checkout of Agent Duet, and this copy is not one. Nothing was changed.\n"
                         "Download the new version from https://github.com/JasonLiaoJCS/agent-duet and run its installer.")
    done = subprocess.run([git, "-C", str(repo), "pull", "--ff-only"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180, creationflags=FLAGS)
    if done.returncode:
        raise SystemExit("Could not update the checkout:\n" + (done.stderr or done.stdout).strip())
    return (done.stdout or "").strip().splitlines()[-1] if (done.stdout or "").strip() else "up to date"

def offer_login(result, args):
    """In an interactive terminal, sign a signed-out CLI in right away instead of leaving it for later."""
    if args.json or args.no_login_prompt or not (sys.stdin and sys.stdin.isatty() and sys.stdout.isatty()):
        return
    import duet
    for provider, state in result.get("signed_in", {}).items():
        if state is not False:
            continue
        try:
            answer = input("Sign the %s CLI in now? A browser window will open. [Y/n] " % provider).strip().lower()
        except EOFError:
            return
        if answer in ("", "y", "yes"):
            subprocess.run(duet.login_command(provider))
            print("%s CLI: %s" % (provider, "signed in" if duet.logged_in(provider) else "still signed out; run the command again later"))

def summary(result, missing):
    """What a person needs to know after installing, in plain words."""
    lines = ["", "Agent Duet %s is installed." % result["version"], ""]
    lines.append("  Codex          : " + ("tools registered" if result["codex_mcp"] and not str(result["codex_mcp"]).startswith("skipped") else "not found on this computer"))
    lines.append("  Claude Code    : " + ("tools registered" if result["claude_mcp"] and not str(result["claude_mcp"]).startswith("skipped") else "not found on this computer"))
    lines.append("  Claude Desktop : " + ("tools registered" if str(result["claude_desktop"]).startswith("registered") else "not present"))
    for provider, label in (("codex", "Codex CLI sign-in  "), ("claude", "Claude CLI sign-in ")):
        state = result.get("signed_in", {}).get(provider)
        if state is not None or provider not in missing:
            lines.append("  %s: %s" % (label, "ok" if state else "SIGNED OUT" if state is False else "could not check"))
    lines.append("  Consult workers default to: " + ("read-only (review)" if result["default_consult_mode"] == "review" else "full permissions (work)"))
    lines.append("  Update outdated CLIs automatically: " + ("yes" if result["auto_update_cli"] else "no (enable with --auto-update on)"))
    if result.get("upgrade"):
        lines.append("  Source: " + result["upgrade"])
    lines += ["", "Next:"]
    signed_out = [p for p, state in result.get("signed_in", {}).items() if state is False]
    for provider in signed_out:
        lines.append("  * Sign the %s CLI in: run `%s` and finish in the browser." % (provider, "claude auth login" if provider == "claude" else "codex login"))
        lines.append("    (The desktop app has its own login; this one is separate, and waking or reviewing needs it.)")
    if missing:
        lines.append("  1. Install and log in to %s, then run this installer again to enable pairing." % " and ".join(missing))
    else:
        lines.append("  1. Reopen Codex and Claude Code once so they load the tools.")
        lines.append("     (Conversations already running Agent Duet 2.4 or later switch to the new version by themselves;")
        lines.append("      coming from 2.3 or earlier, this one reopen is needed.)")
        lines.append("  2. In one conversation on each side, paste:")
        lines.append("       用 agent-duet 配對這個專案：<project folder>。目標：<what you want done>。")
    lines += ["", "Existing projects keep their mailbox, checkpoints and pairing; nothing has to be set up again.",
              "Details as JSON: run with --json", ""]
    return chr(10).join(lines)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex"))))
    parser.add_argument("--claude-home", type=Path, help="Explicit custom Claude config directory; omitted preserves normal CLI discovery")
    parser.add_argument("--skip-desktop", action="store_true", help="Do not register the MCP server in Claude Desktop")
    parser.add_argument("--default-mode", choices=["review", "work"],
                        help="Permissions for consultant CLI workers when a call omits mode: review=read-only (safe default), "
                             "work=full CLI permissions (--dangerously-* flags). Omitted keeps the previous choice, else review.")
    parser.add_argument("--auto-update", choices=["on", "off"],
                        help="Let Agent Duet update an outdated codex/claude CLI by itself so consultations never run on an "
                             "old model. Omitted keeps the previous choice (off on a fresh install).")
    parser.add_argument("--upgrade", action="store_true",
                        help="Fetch the newest version first (git pull in this checkout), then install it.")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON instead of a summary.")
    parser.add_argument("--no-login-prompt", action="store_true", help="Do not offer to sign in a signed-out CLI at the end.")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        raise SystemExit("Agent Duet needs Python 3.10+ (3.11+ recommended).")
    if args.upgrade and os.environ.get("AGENT_DUET_UPGRADED") != "1":
        note = upgrade_source(Path(__file__).resolve().parents[1])
        # Install with the code that was just fetched, not the copy already loaded in this process.
        forwarded = [a for a in sys.argv[1:] if a != "--upgrade"]
        env = dict(os.environ, AGENT_DUET_UPGRADED="1", AGENT_DUET_UPGRADE_NOTE=note)
        raise SystemExit(subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve())] + forwarded, env=env).returncode)
    custom_claude_home = args.claude_home
    args.claude_home = args.claude_home or Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home()/".claude")))
    source = Path(__file__).resolve().parents[1]
    codex_dest, claude_dest = args.codex_home/"skills/agent-duet", args.claude_home/"skills/agent-duet"
    # Resolve prerequisites before modifying anything. One CLI is enough to install; both are needed for pairing.
    codex, codex_error = optional_cli("codex")
    claude, claude_error = optional_cli("claude")
    if os.name == "nt":
        desktop = Path(os.environ.get("APPDATA", str(Path.home()/"AppData/Roaming")))/"Claude/claude_desktop_config.json"
    elif sys.platform == "darwin":
        desktop = Path.home()/"Library/Application Support/Claude/claude_desktop_config.json"
    else:
        desktop = Path.home()/".config/Claude/claude_desktop_config.json"
    use_desktop = not args.skip_desktop and desktop.parent.is_dir()
    if not codex and not claude and not use_desktop:
        raise SystemExit(" | ".join(["Neither the codex nor the claude CLI was found",
                                     "codex: %s" % codex_error, "claude: %s" % claude_error,
                                     "Install at least one (and log in), then rerun this installer."]))
    previous = read_settings(codex_dest, claude_dest)
    mode = args.default_mode or previous.get("default_mode") or "review"
    settings = dict(previous, default_mode=mode)
    result = {"skills": [], "backups": [], "default_consult_mode": mode}
    for dest in (codex_dest, claude_dest):
        copy_skill(source, dest)
        write_settings(dest, settings)
        result["skills"].append(str(dest))
    codex_env = dict(os.environ, CODEX_HOME=str(args.codex_home))
    claude_env = dict(os.environ)
    if custom_claude_home is not None:
        claude_env["CLAUDE_CONFIG_DIR"] = str(custom_claude_home)
    for path in (args.codex_home/"config.toml", args.claude_home/".claude.json", Path.home()/".claude.json"):
        saved = backup(path)
        if saved:
            result["backups"].append(saved)
    if codex:
        # Re-adding replaces only this server's own entry.
        subprocess.run(codex + ["mcp", "remove", "agent-duet"], capture_output=True, env=codex_env, timeout=30, creationflags=FLAGS)
        result["codex_mcp"] = run(codex + ["mcp", "add", "agent-duet", "--", sys.executable, str(codex_dest/"scripts/duet.py"), "mcp"], codex_env) or "registered"
        result["codex_tool_timeout"] = set_codex_tool_timeout(args.codex_home/"config.toml")
    else:
        result["codex_mcp"] = "skipped: " + codex_error
    if claude:
        # Updating an existing same-name registration is explicit and limited to this bridge.
        # Scope-specific remove cannot touch a similarly named project registration.
        subprocess.run(claude + ["mcp", "remove", "--scope", "user", "agent-duet"], capture_output=True,
                       env=claude_env, timeout=30, creationflags=FLAGS)
        result["claude_mcp"] = run(claude + ["mcp", "add", "--scope", "user", "--transport", "stdio", "agent-duet", "--", sys.executable, str(claude_dest/"scripts/duet.py"), "mcp"], claude_env) or "registered"
    else:
        result["claude_mcp"] = "skipped: " + claude_error
    if use_desktop:
        config = json.loads(desktop.read_text(encoding="utf-8-sig")) if desktop.exists() else {}
        servers = config.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise RuntimeError("Desktop mcpServers must be an object; configuration left intact")
        saved = backup(desktop)
        if saved:
            result["backups"].append(saved)
        servers["agent-duet"] = {"command": sys.executable, "args": [str(claude_dest/"scripts/duet.py"), "mcp"]}
        tmp = desktop.with_name(desktop.name + ".agent-duet-tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
        os.replace(tmp, desktop)
        result["claude_desktop"] = "registered; restart/reload Desktop to load tools"
    else:
        result["claude_desktop"] = "not configured (directory absent or skipped)"
    if args.auto_update:
        import duet
        duet.change_models_state(lambda state: state.update(auto_update=args.auto_update == "on"))
    import duet as _duet
    result["auto_update_cli"] = _duet.auto_update_enabled()
    missing = [name for name, found in (("codex", codex), ("claude", claude)) if not found]
    result["next"] = ("Reload Codex and Claude once. Then paste the same one-line pairing request into one conversation on each side."
                      if not missing else
                      "Installed for the available client. Install and log in to %s, then rerun this installer to enable native pairing." % " and ".join(missing))
    result["version"] = _duet.VERSION
    result["signed_in"] = {name: _duet.logged_in(name) for name, found in (("codex", codex), ("claude", claude)) if found}
    if os.environ.get("AGENT_DUET_UPGRADE_NOTE"):
        result["upgrade"] = os.environ["AGENT_DUET_UPGRADE_NOTE"]
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else summary(result, missing))
    offer_login(result, args)

if __name__ == "__main__":
    main()
