from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import upnote_api  # noqa: E402


def _init_test_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            name TEXT,
            locked INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0
        );
        CREATE TABLE notebooks (
            id TEXT PRIMARY KEY,
            title TEXT,
            parent TEXT,
            space TEXT,
            locked INTEGER DEFAULT 0,
            inactive INTEGER DEFAULT 0,
            synced INTEGER DEFAULT 1,
            deleted INTEGER DEFAULT 0,
            createdAt REAL,
            updatedAt REAL
        );
        CREATE TABLE lists (
            id TEXT PRIMARY KEY,
            content TEXT,
            deleted INTEGER DEFAULT 0
        );
        CREATE TABLE organizers (
            id TEXT PRIMARY KEY,
            noteId TEXT,
            notebookId TEXT,
            deleted INTEGER DEFAULT 0
        );
        CREATE TABLE tags (
            id TEXT PRIMARY KEY,
            title TEXT,
            space TEXT,
            inactive INTEGER DEFAULT 0,
            synced INTEGER DEFAULT 1,
            deleted INTEGER DEFAULT 0,
            createdAt REAL,
            updatedAt REAL
        );
        CREATE TABLE notes (
            id TEXT PRIMARY KEY,
            title TEXT,
            summary TEXT,
            text TEXT,
            html TEXT,
            space TEXT,
            tagLinks TEXT,
            pinned INTEGER DEFAULT 0,
            bookmarked INTEGER DEFAULT 0,
            trashed INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0,
            synced INTEGER DEFAULT 1,
            hasTodo INTEGER DEFAULT 0,
            isTemplate INTEGER DEFAULT 0,
            filesCount INTEGER DEFAULT 0,
            revision INTEGER DEFAULT 1,
            createdAt REAL,
            updatedAt REAL,
            syncedAt REAL
        );
        CREATE TABLE filters (
            id TEXT PRIMARY KEY,
            filterType TEXT,
            words TEXT,
            space TEXT,
            inactive INTEGER DEFAULT 0,
            synced INTEGER DEFAULT 1,
            deleted INTEGER DEFAULT 0,
            updatedAt REAL
        );
        CREATE TABLE files (
            id TEXT PRIMARY KEY,
            name TEXT,
            version INTEGER,
            downloadURL TEXT,
            tag TEXT,
            synced INTEGER DEFAULT 1,
            deleted INTEGER DEFAULT 0,
            createdAt REAL,
            updatedAt REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO workspaces (id, name, locked, deleted) VALUES ('space-chep', 'CHEP', 0, 0)"
    )
    conn.execute(
        "INSERT INTO notebooks (id, title, parent, space, createdAt, updatedAt) "
        "VALUES ('nb-ai', 'AI', NULL, NULL, 1000, 1000)"
    )
    conn.execute(
        "INSERT INTO notebooks (id, title, parent, space, createdAt, updatedAt) "
        "VALUES ('nb-chep', 'Ops', NULL, 'space-chep', 1000, 1000)"
    )
    conn.execute(
        "INSERT INTO lists (id, content, deleted) VALUES ('notebooks_nb-ai', ?, 0)",
        (json.dumps(["note-gen-1", "note-gen-wild"]),),
    )
    conn.execute(
        "INSERT INTO lists (id, content, deleted) VALUES ('notebooks_nb-chep', ?, 0)",
        (json.dumps(["note-chep-1"]),),
    )
    conn.execute(
        "INSERT INTO notes (id, title, summary, text, html, space, tagLinks, trashed, deleted, createdAt, updatedAt) "
        "VALUES ('note-gen-1', 'Overview', 'General overview', 'General overview', '<div>General overview</div>', NULL, '[\"ai\"]', 0, 0, 1000, 1000)"
    )
    conn.execute(
        "INSERT INTO notes (id, title, summary, text, html, space, tagLinks, trashed, deleted, createdAt, updatedAt) "
        "VALUES ('note-chep-1', 'Overview', 'CHEP overview', 'CHEP overview', '<div>CHEP overview</div>', 'space-chep', '[\"chep\"]', 0, 0, 1000, 2000)"
    )
    conn.execute(
        "INSERT INTO notes (id, title, summary, text, html, space, tagLinks, trashed, deleted, createdAt, updatedAt) "
        "VALUES ('note-gen-wild', '100% _bg_new_window test', 'Wildcard test', '100% _bg_new_window', '<div>100% _bg_new_window</div>', NULL, '[]', 0, 0, 1000, 1500)"
    )
    conn.commit()
    conn.close()


def test_html_to_markdown_preserves_inline_code_with_html_and_asterisks() -> None:
    raw_html = (
        "<div><h2>Title</h2></div>"
        "<div>Use <code>&lt;pre data-code-language=&quot;bash&quot; spellcheck=&quot;false&quot;&gt;</code> "
        "and <code>a * b * c</code> and <code>foo `bar` baz</code> in notes.</div>"
        '<pre data-code-language="python">def f(x):\n    return x &lt; 10</pre>'
    )
    md = upnote_api.html_to_markdown(raw_html, title="Title")
    assert '`<pre data-code-language="bash" spellcheck="false">`' in md
    assert "`a * b * c`" in md
    assert "`` foo `bar` baz ``" in md
    assert "```python\ndef f(x):\n    return x < 10\n```" in md
    assert not md.startswith("## Title")


def test_sanitize_and_build_deeplink_url() -> None:
    raw = (
        "upnote://x-callback-url/openNote?noteId=123"
        "&x-success=https%3A%2F%2Fevil.example%2F"
        "&x-error=javascript%3Aalert(1)"
    )
    sanitized = upnote_api.sanitize_deeplink_url(raw)
    assert "x-success" not in sanitized
    assert "x-error" not in sanitized
    assert sanitized == "upnote://x-callback-url/openNote?noteId=123"

    bg_url = upnote_api.build_deeplink_url(sanitized, background=True)
    assert bg_url.endswith("&_bg_new_window=true")

    fg_url = upnote_api.build_deeplink_url(sanitized, background=False)
    assert "_bg_new_window=true" not in fg_url

    explicit_nw = "upnote://x-callback-url/note/new?title=Hi&new_window=true"
    assert upnote_api.build_deeplink_url(explicit_nw, background=True) == explicit_nw


def test_dispatch_deeplink_rejects_oversized_url() -> None:
    huge_url = "upnote://x-callback-url/note/new?text=" + ("A" * (upnote_api.MAX_DEEPLINK_BYTES + 100))
    with pytest.raises(HTTPException) as exc_info:
        upnote_api.dispatch_deeplink(huge_url)
    assert exc_info.value.status_code == 413


def test_wildcard_escaping_and_cross_space_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_file = tmp_path / "upnote.sqlite3"
    _init_test_db(db_file)
    monkeypatch.setattr(upnote_api, "DB_PATH", db_file)
    monkeypatch.setattr(upnote_api, "API_TOKEN", "")

    client = TestClient(upnote_api.app)

    # 1. Exact literal search for '%' and '_' wildcards
    r = client.get("/api/v1/notes", params={"q": "100% _bg_new_window"})
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["id"] == "note-gen-wild"

    # 2. Updating 'Overview' in General via cloud_replace must NOT trash 'Overview' in CHEP
    trashed_ids: list[str] = []

    def fake_create(req: upnote_api.NoteCreateRequest) -> upnote_api.NoteOut:
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO notes (id, title, summary, text, html, space, tagLinks, trashed, deleted, createdAt, updatedAt) "
            "VALUES ('note-gen-2', ?, 'Updated', ?, ?, NULL, '[\"ai\"]', 0, 0, 3000, 3000)",
            (req.title, req.text, f"<div>{req.text}</div>"),
        )
        conn.commit()
        conn.close()
        return upnote_api.get_note("note-gen-2")

    def fake_dispatch(url: str, background: bool = True) -> None:
        if "moveToTrash" in url:
            nid = url.split("noteId=")[1].split("&")[0]
            trashed_ids.append(nid)

    with patch.object(upnote_api, "create_note_via_upnote", side_effect=fake_create), patch.object(
        upnote_api, "dispatch_deeplink", side_effect=fake_dispatch
    ):
        resp = client.patch(
            "/api/v1/notes/note-gen-1",
            json={"text": "Updated General overview", "strategy": "cloud_replace"},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == "note-gen-2"

    assert "note-gen-1" in trashed_ids
    assert "note-chep-1" not in trashed_ids


def test_optional_api_token_middleware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_file = tmp_path / "upnote.sqlite3"
    _init_test_db(db_file)
    monkeypatch.setattr(upnote_api, "DB_PATH", db_file)
    monkeypatch.setattr(upnote_api, "API_TOKEN", "test-secret-token")

    client = TestClient(upnote_api.app)

    # /health remains accessible
    assert client.get("/health").status_code == 200

    # /api/v1/spaces without token -> 401
    assert client.get("/api/v1/spaces").status_code == 401

    # With Bearer token -> 200
    r = client.get("/api/v1/spaces", headers={"Authorization": "Bearer test-secret-token"})
    assert r.status_code == 200
    assert len(r.json()) == 2
