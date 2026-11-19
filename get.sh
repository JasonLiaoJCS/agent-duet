#!/usr/bin/env sh
# Agent Duet: download (or update) and install in one step.
#   curl -fsSL https://raw.githubusercontent.com/JasonLiaoJCS/agent-duet/main/get.sh | sh
# Options after "sh -s --", for example:  ... | sh -s -- --default-mode work
set -e
DIR="${AGENT_DUET_DIR:-$HOME/agent-duet}"
command -v git >/dev/null 2>&1 || { echo "git is needed. Install git, then run this again." >&2; exit 1; }
if [ -d "$DIR/.git" ]; then
  echo "Updating $DIR ..."
  git -C "$DIR" pull --ff-only
else
  echo "Downloading Agent Duet to $DIR ..."
  git clone --depth 1 https://github.com/JasonLiaoJCS/agent-duet.git "$DIR"
fi
# Read the sign-in question from the terminal even though this script arrived through a pipe.
if (: < /dev/tty) 2>/dev/null; then
  sh "$DIR/agent-duet/install.sh" --auto-update on "$@" < /dev/tty
else
  sh "$DIR/agent-duet/install.sh" --auto-update on --no-login-prompt "$@"
fi
