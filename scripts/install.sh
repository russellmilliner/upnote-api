#!/usr/bin/env bash
# Install UpNote Local REST API Service, MCP Server (upnote-mcp), and CLI (upnote-cli) into ~/.local and systemd --user
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "$HOME/.local/share/upnote-api"
mkdir -p "$HOME/.local/share/agy-mcp"
mkdir -p "$HOME/.local/bin"
mkdir -p "$HOME/.config/systemd/user"

install -m 755 "$REPO_ROOT/src/upnote_api.py" "$HOME/.local/share/upnote-api/upnote_api.py"
install -m 755 "$REPO_ROOT/src/upnote_mcp.py" "$HOME/.local/share/upnote-api/upnote_mcp.py"
install -m 755 "$REPO_ROOT/src/upnote_mcp.py" "$HOME/.local/share/agy-mcp/upnote_mcp.py"
install -m 755 "$REPO_ROOT/src/upnote_mcp.py" "$HOME/.local/bin/upnote-mcp"
install -m 755 "$REPO_ROOT/src/upnote_cli.py" "$HOME/.local/bin/upnote-cli"
install -m 644 "$REPO_ROOT/systemd/upnote-api.service" "$HOME/.config/systemd/user/upnote-api.service"

systemctl --user daemon-reload
systemctl --user enable --now upnote-api.service
systemctl --user restart upnote-api.service

echo "[OK] Installed upnote-api.service, ~/.local/bin/upnote-mcp, and ~/.local/bin/upnote-cli"
