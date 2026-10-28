#!/usr/bin/env python3
"""UpNote Model Context Protocol (MCP) Server (`upnote-mcp`).

Provides native MCP tools wrapping the local `upnote-api` FastAPI service
(http://127.0.0.1:8765) for AI agents (Google Antigravity CLI/IDE, OpenCode,
Claude Code, and local Ollama agents).
"""

from __future__ import annotations

import os
from typing import Any

import requests
from mcp.server.mcpserver import MCPServer

APP_VERSION = "0.2.4"
UPNOTE_API_URL = os.getenv("UPNOTE_API_URL", "http://127.0.0.1:8765").rstrip("/")
UPNOTE_API_TOKEN = os.getenv("UPNOTE_API_TOKEN", "").strip()

_SESSION = requests.Session()
if UPNOTE_API_TOKEN:
    _SESSION.headers["Authorization"] = f"Bearer {UPNOTE_API_TOKEN}"

mcp = MCPServer("upnote")


def _clean_note_summary(n: dict[str, Any], include_content: bool = False) -> dict[str, Any]:
    out = {
        "id": n.get("id"),
        "title": n.get("title"),
        "summary": n.get("summary"),
        "space_name": n.get("space_name"),
        "notebooks": [nb.get("path") or nb.get("title") for nb in (n.get("notebooks") or [])],
        "tags": n.get("tags") or [],
        "pinned": n.get("pinned", False),
        "synced": n.get("synced", False),
        "updated_at": n.get("updated_at"),
    }
    if n.get("redirected_from"):
        out["redirected_from"] = n["redirected_from"]
    if include_content:
        out["text"] = n.get("markdown") or n.get("text")
    return out


@mcp.tool()
def upnote_status() -> dict[str, Any]:
    """Get UpNote API service status, running UpNote Electron PID, and SQLite database counts."""
    r = _SESSION.get(f"{UPNOTE_API_URL}/api/v1/status", timeout=10)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def upnote_list_spaces() -> list[dict[str, Any]]:
    """List all UpNote workspaces (e.g., 'General', 'CHEP') and their note/notebook counts."""
    r = _SESSION.get(f"{UPNOTE_API_URL}/api/v1/spaces", timeout=10)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def upnote_list_notebooks(space: str = "General", query: str = "") -> list[dict[str, Any]]:
    """List notebooks in an UpNote space (default 'General'), optionally filtered by query."""
    params: dict[str, Any] = {"space": space}
    if query:
        params["q"] = query
    r = _SESSION.get(f"{UPNOTE_API_URL}/api/v1/notebooks", params=params, timeout=10)
    r.raise_for_status()
    return [
        {
            "id": nb.get("id"),
            "title": nb.get("title"),
            "path": nb.get("path"),
            "notes_count": nb.get("notes_count"),
        }
        for nb in r.json()
    ]


@mcp.tool()
def upnote_list_tags(space: str = "General", query: str = "") -> list[dict[str, Any]]:
    """List all hashtags (#tag) in an UpNote space with active note counts."""
    params: dict[str, Any] = {"space": space}
    if query:
        params["q"] = query
    r = _SESSION.get(f"{UPNOTE_API_URL}/api/v1/tags", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def upnote_search_notes(
    query: str = "",
    notebook: str = "",
    tag: str = "",
    space: str = "General",
    include_content: bool = False,
) -> list[dict[str, Any]]:
    """Search and filter UpNote notes by keyword query, notebook path/title, or hashtag."""
    params: dict[str, Any] = {
        "space": space,
        "include_content": str(include_content).lower(),
    }
    if query:
        params["q"] = query
    if notebook:
        params["notebook"] = notebook
    if tag:
        params["tag"] = tag.lstrip("#")
    r = _SESSION.get(f"{UPNOTE_API_URL}/api/v1/notes", params=params, timeout=15)
    r.raise_for_status()
    return [_clean_note_summary(n, include_content=include_content) for n in r.json()]


@mcp.tool()
def upnote_get_note(note_id_or_title: str, space: str = "General") -> dict[str, Any]:
    """Retrieve the full Markdown text and metadata of a single UpNote note by UUID or exact title."""
    r = _SESSION.get(
        f"{UPNOTE_API_URL}/api/v1/notes/{requests.utils.quote(note_id_or_title, safe='')}",
        params={"space": space},
        timeout=15,
    )
    r.raise_for_status()
    return _clean_note_summary(r.json(), include_content=True)


@mcp.tool()
def upnote_create_note(
    title: str,
    text: str,
    notebook: str = "AI",
    tags: list[str] | None = None,
    space: str = "General",
) -> dict[str, Any]:
    """Create a new Markdown note in UpNote and sync it to UpNote Cloud."""
    payload = {
        "title": title,
        "space": space,
        "notebook": notebook,
        "tags": [t.lstrip("#") for t in (tags or [])],
        "markdown": True,
        "text": text,
    }
    r = _SESSION.post(f"{UPNOTE_API_URL}/api/v1/notes", json=payload, timeout=30)
    r.raise_for_status()
    return _clean_note_summary(r.json(), include_content=False)


@mcp.tool()
def upnote_update_note_safe(
    note_id: str,
    append_markdown: str = "",
    full_replacement_markdown: str = "",
    title: str = "",
    notebook: str = "",
    tags: list[str] | None = None,
    space: str = "",
) -> dict[str, Any]:
    """Non-destructively update an existing UpNote note via cloud_replace.

    Prefer passing `append_markdown` to append a new section while preserving all existing content.
    If passing `full_replacement_markdown`, ensure you fetched the existing note first with `upnote_get_note`
    and merged all prior sections.
    """
    curr_resp = _SESSION.get(
        f"{UPNOTE_API_URL}/api/v1/notes/{requests.utils.quote(note_id, safe='')}",
        params={"space": space} if space else None,
        timeout=15,
    )
    curr_resp.raise_for_status()
    curr = curr_resp.json()
    existing_text = (curr.get("markdown") or curr.get("text") or "").strip()
    existing_title = curr.get("title") or ""

    lines = existing_text.splitlines()
    if lines and lines[0].strip() == existing_title.strip():
        existing_body = "\n".join(lines[1:]).strip()
    else:
        existing_body = existing_text

    if append_markdown:
        merged_text = f"{existing_body}\n\n---\n\n{append_markdown.strip()}".strip()
    elif full_replacement_markdown:
        merged_text = full_replacement_markdown.strip()
    else:
        merged_text = existing_body

    notebooks_list = curr.get("notebooks") or []
    target_notebook = notebook or (notebooks_list[0].get("path") if notebooks_list else "AI")
    target_tags = [t.lstrip("#") for t in tags] if tags is not None else (curr.get("tags") or [])
    target_space = space or curr.get("space_name") or "General"

    payload = {
        "title": title or existing_title,
        "space": target_space,
        "notebook": target_notebook,
        "tags": target_tags,
        "strategy": "cloud_replace",
        "text": merged_text,
    }
    r = _SESSION.patch(
        f"{UPNOTE_API_URL}/api/v1/notes/{requests.utils.quote(curr['id'], safe='')}",
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return _clean_note_summary(r.json(), include_content=False)


@mcp.tool()
def upnote_trash_note(note_id: str) -> dict[str, Any]:
    """Move an UpNote note to Trash by UUID."""
    r = _SESSION.delete(
        f"{UPNOTE_API_URL}/api/v1/notes/{requests.utils.quote(note_id, safe='')}",
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
def upnote_restore_note(note_id: str) -> dict[str, Any]:
    """Restore a trashed UpNote note by UUID."""
    r = _SESSION.post(
        f"{UPNOTE_API_URL}/api/v1/notes/{requests.utils.quote(note_id, safe='')}/restore",
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
