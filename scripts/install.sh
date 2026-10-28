#!/usr/bin/env bash
# Install UpNote Local REST API Service, MCP Server (upnote-mcp), and CLI (upnote-cli) into ~/.local and systemd --user
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The MCP server needs the `mcp` package, which the system python usually lacks.
# Pin its shebang to UPNOTE_MCP_PYTHON, else the agy-mcp venv, else `env python3`.
MCP_PYTHON="${UPNOTE_MCP_PYTHON:-}"
if [[ -z "$MCP_PYTHON" ]]; then
  if [[ -x "$HOME/.local/share/agy-mcp/.venv/bin/python" ]]; then
    MCP_PYTHON="$HOME/.local/share/agy-mcp/.venv/bin/python"
  else
    MCP_PYTHON="/usr/bin/env python3"
  fi
fi

install_mcp() {
  local dst="$1"
  { printf '#!%s\n' "$MCP_PYTHON"; tail -n +2 "$REPO_ROOT/src/upnote_mcp.py"; } > "$dst"
  chmod 755 "$dst"
}

mkdir -p "$HOME/.local/share/upnote-api"
mkdir -p "$HOME/.local/share/agy-mcp"
mkdir -p "$HOME/.local/bin"
mkdir -p "$HOME/.config/systemd/user"

install -m 755 "$REPO_ROOT/src/upnote_api.py" "$HOME/.local/share/upnote-api/upnote_api.py"
install_mcp "$HOME/.local/share/upnote-api/upnote_mcp.py"
install_mcp "$HOME/.local/share/agy-mcp/upnote_mcp.py"
install_mcp "$HOME/.local/bin/upnote-mcp"
install -m 755 "$REPO_ROOT/src/upnote_cli.py" "$HOME/.local/bin/upnote-cli"
install -m 644 "$REPO_ROOT/systemd/upnote-api.service" "$HOME/.config/systemd/user/upnote-api.service"

systemctl --user daemon-reload
systemctl --user enable --now upnote-api.service
systemctl --user restart upnote-api.service

echo "[OK] Installed upnote-api.service, ~/.local/bin/upnote-mcp (python: $MCP_PYTHON), and ~/.local/bin/upnote-cli"
