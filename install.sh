#!/bin/sh
# One-line installer for agent-bridge:
#   curl -LsSf https://raw.githubusercontent.com/merlinh0/agent-bridge/main/install.sh | sh
# Installs uv if needed, installs the `agent-bridge` command, registers it in Claude Desktop and
# Claude Code, then runs `agent-bridge doctor`. Extra arguments go to `agent-bridge install`
# (e.g. `sh -s -- --no-claude-code`). Set AGENT_BRIDGE_SOURCE to install a tag or a local checkout.
set -eu

# Everything runs from main(), called on the last line, so a download cut short runs nothing.
main() {
  source="${AGENT_BRIDGE_SOURCE:-git+https://github.com/merlinh0/agent-bridge}"

  if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv (Python package manager)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
    export PATH
  fi

  echo "Installing agent-bridge from ${source}…"
  uv tool install --force "$source"
  PATH="$(uv tool dir --bin):$PATH"
  export PATH

  agent-bridge install "$@"
  echo
  agent-bridge doctor || true
  echo
  echo "Done. Quit and reopen Claude Desktop, then ask it to use the agent-bridge tools."
}

main "$@"
