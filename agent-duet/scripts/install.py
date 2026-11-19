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
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        raise SystemExit("Agent Duet needs Python 3.10+ (3.11+ recommended).")
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
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
