from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
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


BASE_URL = "http://127.0.0.1:8765"


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_file = tmp_path / "upnote.sqlite3"
    _init_test_db(db_file)
    conn = sqlite3.connect(str(db_file))
    # Same title + same space, but a DIFFERENT notebook -> must never be trashed by default.
    conn.execute(
        "INSERT INTO notebooks (id, title, parent, space, createdAt, updatedAt) "
        "VALUES ('nb-misc', 'Misc', NULL, NULL, 1000, 1000)"
    )
    conn.execute(
        "INSERT INTO lists (id, content, deleted) VALUES ('notebooks_nb-misc', ?, 0)",
        (json.dumps(["note-gen-other-nb"]),),
    )
    conn.execute(
        "INSERT INTO notes (id, title, html, space, tagLinks, trashed, deleted, createdAt, updatedAt) "
        "VALUES ('note-gen-other-nb', 'Overview', '<div>Misc overview</div>', NULL, '[]', 0, 0, 1000, 900)"
    )
    # Same title + same space + SAME notebook as note-gen-1 -> only trashed with dedupe=true.
    conn.execute(
        "UPDATE lists SET content = ? WHERE id = 'notebooks_nb-ai'",
        (json.dumps(["note-gen-1", "note-gen-wild", "note-gen-same-nb"]),),
    )
    conn.execute(
        "INSERT INTO notes (id, title, html, space, tagLinks, trashed, deleted, createdAt, updatedAt) "
        "VALUES ('note-gen-same-nb', 'Overview', '<div>Stale dup</div>', NULL, '[]', 0, 0, 1000, 800)"
    )
    # Trashed older revision of a uniquely titled note -> redirect target test.
    conn.execute(
        "INSERT INTO notes (id, title, html, space, tagLinks, trashed, deleted, createdAt, updatedAt) "
        "VALUES ('note-rev-old', 'Runbook', '<div>old</div>', NULL, '[]', 1, 0, 1000, 1000)"
    )
    conn.execute(
        "INSERT INTO notes (id, title, html, space, tagLinks, trashed, deleted, createdAt, updatedAt) "
        "VALUES ('note-rev-new', 'Runbook', '<div>new</div>', NULL, '[]', 0, 0, 2000, 2000)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(upnote_api, "DB_PATH", db_file)
    monkeypatch.setattr(upnote_api, "API_TOKEN", "")
    monkeypatch.setattr(upnote_api, "find_upnote_process", lambda: (None, None, {}))
    return db_file


@pytest.fixture()
def client(db: Path) -> TestClient:
    return TestClient(upnote_api.app, base_url=BASE_URL)


def _run_cloud_replace(db_file: Path, client: TestClient, body: dict) -> list[str]:
    """PATCH note-gen-1 with a faked UpNote create (new note lands in nb-ai); return trashed ids."""
    trashed_ids: list[str] = []

    def fake_create(req: upnote_api.NoteCreateRequest) -> upnote_api.NoteOut:
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO notes (id, title, html, space, tagLinks, trashed, deleted, createdAt, updatedAt) "
            "VALUES ('note-gen-2', ?, ?, NULL, '[\"ai\"]', 0, 0, 3000, 3000)",
            (req.title, f"<div>{req.text}</div>"),
        )
        content = json.loads(
            conn.execute("SELECT content FROM lists WHERE id = 'notebooks_nb-ai'").fetchone()[0]
        )
        conn.execute(
            "UPDATE lists SET content = ? WHERE id = 'notebooks_nb-ai'",
            (json.dumps(content + ["note-gen-2"]),),
        )
        conn.commit()
        conn.close()
        return upnote_api.get_note("note-gen-2")

    def fake_dispatch(url: str, background: bool = True) -> None:
        if "moveToTrash" in url:
            trashed_ids.append(url.split("noteId=")[1].split("&")[0])

    with patch.object(upnote_api, "create_note_via_upnote", side_effect=fake_create), patch.object(
        upnote_api, "dispatch_deeplink", side_effect=fake_dispatch
    ):
        resp = client.patch("/api/v1/notes/note-gen-1", json=body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == "note-gen-2"
    return trashed_ids


# --- Data integrity -----------------------------------------------------------------


def test_cloud_replace_trashes_only_the_updated_note_by_default(db: Path, client: TestClient) -> None:
    trashed = _run_cloud_replace(db, client, {"text": "Updated", "strategy": "cloud_replace"})
    assert trashed == ["note-gen-1"]


def test_cloud_replace_dedupe_is_limited_to_same_space_and_notebook(db: Path, client: TestClient) -> None:
    trashed = _run_cloud_replace(
        db, client, {"text": "Updated", "strategy": "cloud_replace", "dedupe": True}
    )
    assert "note-gen-1" in trashed
    assert "note-gen-same-nb" in trashed
    assert "note-gen-other-nb" not in trashed  # different notebook
    assert "note-chep-1" not in trashed  # different space


def test_match_created_note_never_falls_back_to_unrelated_rows() -> None:
    before = {"old-1"}
    rows = [
        {"id": "synced-from-phone", "title": "Groceries", "space": None},
        {"id": "wrong-space", "title": "Overview", "space": "space-chep"},
        {"id": "old-1", "title": "Overview", "space": None},
    ]
    assert upnote_api.match_created_note(rows, before, "Overview", None) is None
    rows.append({"id": "ours", "title": "Overview", "space": ""})
    assert upnote_api.match_created_note(rows, before, "Overview", None) == "ours"
    rows2 = [{"id": "amp", "title": "Q&amp;A\u00a0 notes", "space": "space-chep"}]
    assert upnote_api.match_created_note(rows2, set(), "Q&A notes", "space-chep") == "amp"


def test_sqlite_direct_and_permanent_delete_refused_while_upnote_running(
    db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upnote_api, "find_upnote_process", lambda: (4242, "/x/upnote", {}))
    r = client.patch("/api/v1/notes/note-gen-1", json={"title": "X", "strategy": "sqlite_direct"})
    assert r.status_code == 409
    r = client.delete("/api/v1/notes/note-gen-1", params={"permanent": "true"})
    assert r.status_code == 409
    r = client.patch(
        "/api/v1/notes/note-gen-1", json={"title": "Forced", "strategy": "sqlite_direct", "force": True}
    )
    assert r.status_code == 200 and r.json()["title"] == "Forced"


def test_trashed_id_redirect_is_reported(client: TestClient) -> None:
    r = client.get("/api/v1/notes/note-rev-old")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "note-rev-new"
    assert body["redirected_from"] == "note-rev-old"
    assert client.get("/api/v1/notes/note-rev-new").json()["redirected_from"] is None


# --- Regression: endpoints called internally from Python ------------------------------


def test_tag_route_and_title_lookup_work(client: TestClient) -> None:
    r = client.get("/api/v1/tags/ai/notes")
    assert r.status_code == 200, r.text
    assert [n["id"] for n in r.json()] == ["note-gen-1"]
    r = client.get("/api/v1/notes/Runbook")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "note-rev-new"


def test_wildcards_are_literal(client: TestClient) -> None:
    r = client.get("/api/v1/notes", params={"q": "100% _bg_new_window"})
    assert [n["id"] for n in r.json()] == ["note-gen-wild"]
    assert client.get("/api/v1/notes", params={"q": "1_0"}).json() == []


# --- Request security (CSRF / DNS rebinding / token) ----------------------------------


def test_check_request_security_matrix() -> None:
    chk = upnote_api.check_request_security
    ok = dict(origin=None, sec_fetch_site=None, path="/api/v1/notes", auth_header="", api_key_header="")
    assert chk("GET", "127.0.0.1:8765", **ok) is None
    assert chk("GET", "localhost:8765", **ok) is None
    assert chk("GET", "[::1]:8765", **ok) is None
    assert chk("GET", "evil.example:8765", **ok)[0] == 400  # DNS rebinding
    assert chk("DELETE", "127.0.0.1:8765", **ok) is None  # curl / CLI / MCP (no Origin)
    evil_origin = {**ok, "origin": "https://evil.example"}
    assert chk("DELETE", "127.0.0.1:8765", **evil_origin)[0] == 403
    assert chk("POST", "127.0.0.1:8765", **evil_origin)[0] == 403
    assert chk("GET", "127.0.0.1:8765", **evil_origin) is None  # reads blocked by CORS instead
    cross = {**ok, "sec_fetch_site": "cross-site"}
    assert chk("POST", "127.0.0.1:8765", **cross)[0] == 403
    same = {**ok, "origin": "http://127.0.0.1:8765", "sec_fetch_site": "same-origin"}
    assert chk("DELETE", "127.0.0.1:8765", **same) is None


def test_cross_site_delete_and_rebinding_blocked_end_to_end(client: TestClient) -> None:
    r = client.delete("/api/v1/notes/note-gen-1", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/v1/update/install", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.get("/api/v1/notes", headers={"Host": "attacker.example"})
    assert r.status_code == 400


def test_health_is_minimal_and_token_enforced(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upnote_api, "API_TOKEN", "test-secret-token")
    h = client.get("/health")
    assert h.status_code == 200 and set(h.json()) == {"ok", "version"}
    assert client.get("/api/v1/spaces").status_code == 401
    assert client.get("/api/v1/spaces", headers={"X-API-Key": "wrong"}).status_code == 401
    r = client.get("/api/v1/spaces", headers={"Authorization": "Bearer test-secret-token"})
    assert r.status_code == 200 and len(r.json()) == 2


# --- Self-updater & process discovery -------------------------------------------------


def test_verify_archive_sha256() -> None:
    data = b"release-bytes"
    good = hashlib.sha256(data).hexdigest()
    upnote_api.verify_archive_sha256(data, f"{good}  upnote-api.tar.gz\n")
    with pytest.raises(RuntimeError, match="mismatch"):
        upnote_api.verify_archive_sha256(data, "0" * 64)
    with pytest.raises(RuntimeError, match="malformed"):
        upnote_api.verify_archive_sha256(data, "not-a-hash")
    with pytest.raises(RuntimeError, match="malformed"):
        upnote_api.verify_archive_sha256(data, "")


def test_self_update_refuses_release_without_checksum(monkeypatch: pytest.MonkeyPatch) -> None:
    info = upnote_api.UpdateCheckOut(
        latest_version="99.0.0",
        update_available=True,
        download_url=upnote_api.ALLOWED_DOWNLOAD_PREFIX + "v99.0.0/upnote-api.tar.gz",
        sha256_url=None,
    )
    monkeypatch.setattr(upnote_api, "check_for_updates", lambda timeout=15.0: info)

    class _Resp(io.BytesIO):
        def __enter__(self):  # noqa: D105
            return self

        def __exit__(self, *a):  # noqa: D105
            return False

    monkeypatch.setattr(upnote_api.urllib.request, "urlopen", lambda *a, **k: _Resp(b"x"))
    monkeypatch.setattr(upnote_api, "REQUIRE_UPDATE_CHECKSUM", True)
    with pytest.raises(RuntimeError, match="missing a trusted"):
        upnote_api.install_update_from_github(restart_service=False)


def test_self_update_rejects_untrusted_download_url(monkeypatch: pytest.MonkeyPatch) -> None:
    info = upnote_api.UpdateCheckOut(
        latest_version="99.0.0", update_available=True, download_url="https://evil.example/x.tar.gz"
    )
    monkeypatch.setattr(upnote_api, "check_for_updates", lambda timeout=15.0: info)
    with pytest.raises(RuntimeError, match="untrusted"):
        upnote_api.install_update_from_github(restart_service=False)


def test_tar_data_filter_blocks_path_traversal(tmp_path: Path) -> None:
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        payload = b"pwned"
        ti = tarfile.TarInfo("../../escape.txt")
        ti.size = len(payload)
        tar.addfile(ti, io.BytesIO(payload))
    out = tmp_path / "out"
    out.mkdir()
    with tarfile.open(evil, "r:gz") as tar, pytest.raises(tarfile.FilterError):
        tar.extractall(path=out, filter="data")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_cached_process_is_revalidated(monkeypatch: pytest.MonkeyPatch) -> None:
    assert upnote_api._cached_proc_still_valid(999_999_999, "/tmp/.mount_upnoteX/upnote", 1000) is False


def test_mcp_script_shebang_is_pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPNOTE_MCP_PYTHON", "/opt/venv/bin/python")
    dst = tmp_path / "upnote-mcp"
    upnote_api._install_mcp_script(REPO_ROOT / "src" / "upnote_mcp.py", dst)
    assert dst.read_text().splitlines()[0] == "#!/opt/venv/bin/python"
    assert dst.stat().st_mode & 0o111
