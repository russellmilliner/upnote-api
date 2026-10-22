#!/usr/bin/env python3
"""
UpNote Local Store & Deep-Link REST API Service.

Provides a full REST API (FastAPI + Uvicorn) for reading, searching, creating,
updating, tagging, trashing, and restoring notes, notebooks, tags, and spaces
in the local UpNote SQLite database (~/.config/UpNote/upnote.sqlite3)
while synchronizing mutations to UpNote Cloud via UpNote's x-callback-url
Electron deep-link interface.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(
    os.environ.get(
        "UPNOTE_DB_PATH",
        str(Path.home() / ".config" / "UpNote" / "upnote.sqlite3"),
    )
)
APPIMAGE_PATH = Path(
    os.environ.get(
        "UPNOTE_APPIMAGE_PATH",
        str(Path.home() / "AppImages" / "upnote.appimage"),
    )
)
LAUNCH_SCRIPT_PATH = Path(
    os.environ.get(
        "UPNOTE_LAUNCH_SCRIPT",
        str(Path.home() / ".local" / "bin" / "upnotelaunch.sh"),
    )
)
API_HOST = os.environ.get("UPNOTE_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("UPNOTE_API_PORT", "8765"))

# Lock to serialize deep-link dispatches so rapid concurrent creations can be
# matched deterministically in SQLite.
_DEEPLINK_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------


class SpaceOut(BaseModel):
    id: str = Field(..., description="Space ID ('default' for General, or UUID)")
    sqlite_space: str | None = Field(
        None, description="Value stored in SQLite 'space' column (NULL for General)"
    )
    name: str = Field(..., description="Human-readable space name (e.g. 'General', 'CHEP')")
    locked: bool = False
    notebooks_count: int = 0
    notes_count: int = 0
    tags_count: int = 0


class NotebookRef(BaseModel):
    id: str
    title: str
    path: str


class NotebookOut(BaseModel):
    id: str
    title: str
    path: str
    parent_id: str | None = None
    space_id: str
    space_name: str
    locked: bool = False
    inactive: bool = False
    synced: bool = True
    notes_count: int = 0
    note_ids: list[str] = Field(default_factory=list)
    created_at: float | None = None
    updated_at: float | None = None


class NotebookCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, description="Notebook title")
    space: str = Field(
        default="General",
        description="Space name ('General', 'CHEP') or space ID ('default' or UUID)",
    )


class TagOut(BaseModel):
    id: str
    title: str = Field(..., description="Tag title with leading '#' (e.g. '#plex')")
    name: str = Field(..., description="Normalized tag name without '#' (e.g. 'plex')")
    space_id: str
    space_name: str
    inactive: bool = False
    synced: bool = True
    notes_count: int = 0
    created_at: float | None = None
    updated_at: float | None = None


class NoteOut(BaseModel):
    id: str
    title: str
    summary: str | None = None
    space_id: str
    space_name: str
    notebooks: list[NotebookRef] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False
    bookmarked: bool = False
    trashed: bool = False
    deleted: bool = False
    synced: bool = False
    has_todo: bool = False
    is_template: bool = False
    files_count: int = 0
    created_at: float | None = None
    updated_at: float | None = None
    synced_at: float | None = None
    text: str | None = None
    html: str | None = None


class NoteCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, description="Page/note title")
    text: str = Field(default="", description="Note body (Markdown or plain text)")
    markdown: bool = Field(
        default=True,
        description="Parse 'text' as Markdown (supports tables, headings, non-wrapping code blocks)",
    )
    space: str = Field(
        default="General",
        description="Target space name ('General', 'CHEP') or ID ('default' or UUID)",
    )
    notebook: str | None = Field(
        default=None,
        description="Target notebook title (e.g. 'AI'), nested path ('Parent/Child'), or notebook UUID",
    )
    tag: str | None = Field(
        default=None,
        description="Single tag to attach (with or without leading '#', e.g. 'plex')",
    )
    tags: list[str] | None = Field(
        default=None,
        description="List of tags to attach (e.g. ['plex', 'proxmox'])",
    )
    new_window: bool = Field(
        default=False,
        description="Whether to open the newly created note in a separate UpNote window",
    )


class NoteUpdateRequest(BaseModel):
    title: str | None = Field(default=None, description="Updated note title")
    text: str | None = Field(default=None, description="Updated note body (Markdown or plain text)")
    html: str | None = Field(
        default=None,
        description="Updated raw HTML (used only when strategy='sqlite_direct')",
    )
    markdown: bool = Field(default=True, description="Interpret 'text' as Markdown")
    space: str | None = Field(default=None, description="Target space name or ID")
    notebook: str | None = Field(default=None, description="Target notebook title, path, or UUID")
    tag: str | None = Field(default=None, description="Single tag to set")
    tags: list[str] | None = Field(default=None, description="List of tags to set")
    pinned: bool | None = Field(default=None, description="Pin or unpin note")
    bookmarked: bool | None = Field(default=None, description="Bookmark or unbookmark note")
    strategy: Literal["cloud_replace", "sqlite_direct"] = Field(
        default="cloud_replace",
        description=(
            "'cloud_replace' creates a synced replacement note via UpNote x-callback-url "
            "and moves the old revision to trash so UpNote Cloud syncs immediately; "
            "'sqlite_direct' updates the row in-place in upnote.sqlite3."
        ),
    )


class DeepLinkOpenRequest(BaseModel):
    url: str | None = Field(
        default=None,
        description="Raw upnote://x-callback-url/... URL to dispatch",
    )
    note_id: str | None = Field(default=None, description="Note UUID to open in UpNote UI")
    notebook_id: str | None = Field(default=None, description="Notebook UUID to open in UpNote UI")
    tag: str | None = Field(default=None, description="Tag name to view in UpNote UI")


class FilterOut(BaseModel):
    id: str
    filter_type: str | None = None
    words: str | None = None
    space_id: str
    space_name: str
    inactive: bool = False
    synced: bool = True
    updated_at: float | None = None


class FileOut(BaseModel):
    id: str
    name: str | None = None
    version: int | None = None
    download_url: str | None = None
    tag: str | None = None
    synced: bool = True
    created_at: float | None = None
    updated_at: float | None = None


class StatusOut(BaseModel):
    ok: bool = True
    version: str = APP_VERSION
    db_path: str
    db_exists: bool
    upnote_running: bool
    upnote_pid: int | None = None
    upnote_binary: str | None = None
    counts: dict[str, int]


# ---------------------------------------------------------------------------
# Database & Process Helpers
# ---------------------------------------------------------------------------


def get_db_connection(readonly: bool = True) -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"UpNote SQLite database not found at {DB_PATH}",
        )
    if readonly:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    else:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def find_upnote_process() -> tuple[int | None, str | None, dict[str, str]]:
    """
    Locate the running UpNote AppImage / Electron process, its mounted binary,
    and its desktop session environment variables.
    """
    mounted_bins = sorted(glob.glob("/tmp/.mount_upnote*/upnote"))
    binary_path = mounted_bins[-1] if mounted_bins else (
        str(APPIMAGE_PATH) if APPIMAGE_PATH.exists() else None
    )

    found_pid: int | None = None
    session_env: dict[str, str] = {}

    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        pid = int(proc_dir.name)
        try:
            cmdline = (proc_dir / "cmdline").read_bytes().decode("utf-8", errors="ignore")
            if "upnote.appimage" in cmdline or "/.mount_upnote" in cmdline:
                env_bytes = (proc_dir / "environ").read_bytes()
                env_map: dict[str, str] = {}
                for item in env_bytes.split(b"\0"):
                    if b"=" in item:
                        k, v = item.decode("utf-8", errors="ignore").split("=", 1)
                        env_map[k] = v
                if "DISPLAY" in env_map or "WAYLAND_DISPLAY" in env_map:
                    found_pid = pid
                    session_env = env_map
                    if "upnote.appimage" in cmdline:
                        break
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue

    if not session_env:
        uid = os.getuid()
        for proc_dir in Path("/proc").iterdir():
            if not proc_dir.name.isdigit():
                continue
            try:
                if proc_dir.stat().st_uid != uid:
                    continue
                env_bytes = (proc_dir / "environ").read_bytes()
                env_map = {}
                for item in env_bytes.split(b"\0"):
                    if b"=" in item:
                        k, v = item.decode("utf-8", errors="ignore").split("=", 1)
                        env_map[k] = v
                if "WAYLAND_DISPLAY" in env_map and "XAUTHORITY" in env_map:
                    session_env = env_map
                    break
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue

    return found_pid, binary_path, session_env


def ensure_upnote_running() -> tuple[int, str, dict[str, str]]:
    """Ensure the UpNote Electron background process is running and return (pid, bin, env)."""
    pid, binary, env = find_upnote_process()
    if pid is not None and binary is not None:
        return pid, binary, env

    launch_env = os.environ.copy()
    launch_env.update(env)
    launch_env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    launch_env.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )

    if LAUNCH_SCRIPT_PATH.exists():
        subprocess.Popen(
            [str(LAUNCH_SCRIPT_PATH)],
            env=launch_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    elif APPIMAGE_PATH.exists():
        subprocess.Popen(
            [str(APPIMAGE_PATH), "--auto-start-in-background"],
            env=launch_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="UpNote is not running and no launcher was found.",
        )

    for _ in range(20):
        time.sleep(0.5)
        pid, binary, env = find_upnote_process()
        if pid is not None and binary is not None:
            return pid, binary, env

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Timed out waiting for UpNote process to start.",
    )


def dispatch_deeplink(url: str) -> None:
    """Invoke UpNote with an upnote://x-callback-url/... deep link."""
    _, binary, env = ensure_upnote_running()
    exec_env = os.environ.copy()
    exec_env.update(env)
    exec_env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    exec_env.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    try:
        subprocess.run(
            [binary, url],
            env=exec_env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except subprocess.SubprocessError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to dispatch deep link to UpNote: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Data Mapping & Resolution Helpers
# ---------------------------------------------------------------------------


def load_spaces_map(conn: sqlite3.Connection) -> dict[str | None, tuple[str, str, bool]]:
    """
    Return mapping from SQLite `space` value (None or UUID) to
    (space_id, space_name, locked).
    """
    spaces: dict[str | None, tuple[str, str, bool]] = {
        None: ("default", "General", False),
        "": ("default", "General", False),
    }
    rows = conn.execute(
        "SELECT id, name, locked FROM workspaces WHERE deleted = 0"
    ).fetchall()
    for r in rows:
        spaces[r["id"]] = (r["id"], r["name"] or r["id"], bool(r["locked"]))
    return spaces


def resolve_space(
    conn: sqlite3.Connection, space_query: str | None
) -> tuple[str, str | None, str]:
    """
    Resolve a user-supplied space identifier ('General', 'default', 'CHEP', UUID)
    into (deeplink_space_id, sqlite_space_val, space_name).
    """
    if not space_query or space_query.strip().lower() in ("general", "default", "null"):
        return ("default", None, "General")

    q = space_query.strip()
    rows = conn.execute(
        "SELECT id, name FROM workspaces WHERE deleted = 0"
    ).fetchall()
    for r in rows:
        if r["id"].lower() == q.lower() or (r["name"] and r["name"].lower() == q.lower()):
            return (r["id"], r["id"], r["name"] or r["id"])

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Workspace/space '{space_query}' not found.",
    )


def load_notebooks_index(
    conn: sqlite3.Connection,
) -> tuple[dict[str, NotebookOut], dict[str, list[NotebookRef]]]:
    """
    Build:
      1. `notebooks_by_id`: map of notebook UUID -> NotebookOut (with full slash path & note_ids)
      2. `note_to_notebooks`: map of note UUID -> list[NotebookRef]
    """
    spaces_map = load_spaces_map(conn)
    nb_rows = conn.execute(
        "SELECT * FROM notebooks WHERE deleted = 0 ORDER BY title COLLATE NOCASE ASC"
    ).fetchall()

    raw_nbs: dict[str, sqlite3.Row] = {r["id"]: r for r in nb_rows}

    def compute_path(nb_id: str, visited: set[str] | None = None) -> str:
        if visited is None:
            visited = set()
        if nb_id in visited or nb_id not in raw_nbs:
            return ""
        visited.add(nb_id)
        row = raw_nbs[nb_id]
        parent_id = row["parent"]
        if parent_id and parent_id in raw_nbs:
            parent_path = compute_path(parent_id, visited)
            return f"{parent_path}/{row['title']}" if parent_path else row["title"]
        return row["title"] or ""

    list_rows = conn.execute(
        "SELECT id, content FROM lists WHERE deleted = 0 AND id LIKE 'notebooks_%'"
    ).fetchall()
    nb_notes_map: dict[str, list[str]] = {}
    for lr in list_rows:
        nb_id = lr["id"][len("notebooks_") :]
        try:
            note_ids = json.loads(lr["content"] or "[]")
            if isinstance(note_ids, list):
                nb_notes_map[nb_id] = [str(x) for x in note_ids]
        except json.JSONDecodeError:
            nb_notes_map[nb_id] = []

    org_rows = conn.execute(
        "SELECT noteId, notebookId FROM organizers WHERE deleted = 0"
    ).fetchall()
    for orow in org_rows:
        nb_id = orow["notebookId"]
        note_id = orow["noteId"]
        if nb_id and note_id:
            lst = nb_notes_map.setdefault(nb_id, [])
            if note_id not in lst:
                lst.append(note_id)

    active_note_ids = {
        r["id"]
        for r in conn.execute(
            "SELECT id FROM notes WHERE deleted = 0 AND trashed = 0"
        ).fetchall()
    }

    notebooks_by_id: dict[str, NotebookOut] = {}
    note_to_notebooks: dict[str, list[NotebookRef]] = {}

    for nb_id, r in raw_nbs.items():
        sp_id, sp_name, _ = spaces_map.get(
            r["space"], (r["space"] or "default", r["space"] or "General", False)
        )
        path = compute_path(nb_id)
        all_nids = nb_notes_map.get(nb_id, [])
        active_nids = [nid for nid in all_nids if nid in active_note_ids]
        nb_out = NotebookOut(
            id=nb_id,
            title=r["title"] or "",
            path=path,
            parent_id=r["parent"] or None,
            space_id=sp_id,
            space_name=sp_name,
            locked=bool(r["locked"]),
            inactive=bool(r["inactive"]),
            synced=bool(r["synced"]),
            notes_count=len(active_nids),
            note_ids=active_nids,
            created_at=r["createdAt"],
            updated_at=r["updatedAt"],
        )
        notebooks_by_id[nb_id] = nb_out
        nb_ref = NotebookRef(id=nb_id, title=nb_out.title, path=nb_out.path)
        for nid in all_nids:
            note_to_notebooks.setdefault(nid, []).append(nb_ref)

    return notebooks_by_id, note_to_notebooks


def resolve_notebook(
    conn: sqlite3.Connection,
    notebook_query: str,
    space_id: str | None = None,
) -> NotebookOut | None:
    """Find a notebook by UUID, exact title, or slash-separated path."""
    notebooks_by_id, _ = load_notebooks_index(conn)
    q = notebook_query.strip()
    if q in notebooks_by_id:
        return notebooks_by_id[q]

    q_lower = q.lower()
    for nb in notebooks_by_id.values():
        if space_id and nb.space_id.lower() != space_id.lower():
            continue
        if nb.path.lower() == q_lower or nb.title.lower() == q_lower:
            return nb
    return None


def row_to_note_out(
    row: sqlite3.Row,
    spaces_map: dict[str | None, tuple[str, str, bool]],
    note_to_notebooks: dict[str, list[NotebookRef]],
    include_content: bool = True,
) -> NoteOut:
    sp_id, sp_name, _ = spaces_map.get(
        row["space"], (row["space"] or "default", row["space"] or "General", False)
    )
    try:
        raw_tags = json.loads(row["tagLinks"] or "[]")
        tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
    except json.JSONDecodeError:
        tags = []

    return NoteOut(
        id=row["id"],
        title=row["title"] or "",
        summary=row["summary"],
        space_id=sp_id,
        space_name=sp_name,
        notebooks=note_to_notebooks.get(row["id"], []),
        tags=tags,
        pinned=bool(row["pinned"]),
        bookmarked=bool(row["bookmarked"]),
        trashed=bool(row["trashed"]),
        deleted=bool(row["deleted"]),
        synced=bool(row["synced"]),
        has_todo=bool(row["hasTodo"]),
        is_template=bool(row["isTemplate"]),
        files_count=int(row["filesCount"] or 0),
        created_at=row["createdAt"],
        updated_at=row["updatedAt"],
        synced_at=row["syncedAt"],
        text=row["text"] if include_content else None,
        html=row["html"] if include_content else None,
    )


def normalize_tag_name(tag: str) -> str:
    """Return tag name without leading '#' or whitespace."""
    cleaned = tag.strip().lstrip("#＃")
    cleaned = re.sub(r"[\s~!@#＃$%^&*(),.?\":;/\\+='{}[\]|<>]+", "-", cleaned)
    return cleaned.strip("-")


def build_hashtag_anchor_html(tag_name: str) -> str:
    """Build an UpNote native hashtag anchor element that DOMPurify & tagLinks index."""
    norm = normalize_tag_name(tag_name)
    encoded = urllib.parse.quote(norm)
    return (
        f'<a title="#{norm}" data-upnote-tag="#{norm}" '
        f'spellcheck="false" data-non-editable="true" '
        f'href="upnote://x-callback-url/tag/view?tag={encoded}">#{norm}</a>'
    )


def create_note_via_upnote(req: NoteCreateRequest) -> NoteOut:
    """
    Create a note via UpNote's `upnote://x-callback-url/note/new` handler and wait
    for it to be persisted in `upnote.sqlite3`.
    """
    with _DEEPLINK_LOCK:
        with get_db_connection(readonly=True) as conn:
            deeplink_space_id, _, _ = resolve_space(conn, req.space)
            notebook_param: str | None = None
            if req.notebook:
                resolved_nb = resolve_notebook(conn, req.notebook, space_id=deeplink_space_id)
                notebook_param = resolved_nb.path if resolved_nb else req.notebook.strip()

            before_ids = {
                r["id"]
                for r in conn.execute("SELECT id FROM notes").fetchall()
            }

        all_tags: list[str] = []
        if req.tag and req.tag.strip():
            norm = normalize_tag_name(req.tag)
            if norm and norm not in all_tags:
                all_tags.append(norm)
        if req.tags:
            for t in req.tags:
                norm = normalize_tag_name(t)
                if norm and norm not in all_tags:
                    all_tags.append(norm)

        primary_tag = all_tags[0] if all_tags else None
        extra_tags = all_tags[1:] if len(all_tags) > 1 else []

        body_text = req.text or ""
        if extra_tags and req.markdown:
            anchors = " ".join(build_hashtag_anchor_html(t) for t in extra_tags)
            body_text = f"{body_text}\n\n{anchors}" if body_text else anchors

        query_params: dict[str, str] = {
            "spaceId": deeplink_space_id,
            "title": req.title,
            "markdown": "true" if req.markdown else "false",
        }
        if body_text:
            query_params["text"] = body_text
        if notebook_param:
            query_params["notebook"] = notebook_param
        if primary_tag:
            query_params["tag"] = primary_tag
        if req.new_window:
            query_params["new_window"] = "true"

        url = "upnote://x-callback-url/note/new?" + urllib.parse.urlencode(
            query_params, quote_via=urllib.parse.quote
        )
        dispatch_deeplink(url)

        created_id: str | None = None
        for _ in range(30):
            time.sleep(0.25)
            with get_db_connection(readonly=True) as conn:
                rows = conn.execute(
                    "SELECT id FROM notes WHERE title = ? ORDER BY createdAt DESC LIMIT 5",
                    (req.title,),
                ).fetchall()
                for r in rows:
                    if r["id"] not in before_ids:
                        created_id = r["id"]
                        break
                if created_id:
                    break

        if not created_id:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Dispatched note/new to UpNote, but the new note did not appear in SQLite within 7.5s.",
            )

        time.sleep(0.3)
        with get_db_connection(readonly=True) as conn:
            spaces_map = load_spaces_map(conn)
            _, note_to_notebooks = load_notebooks_index(conn)
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ?", (created_id,)
            ).fetchone()
            return row_to_note_out(row, spaces_map, note_to_notebooks, include_content=True)


# ---------------------------------------------------------------------------
# FastAPI Application & OpenAPI Metadata
# ---------------------------------------------------------------------------

OPENAPI_TAGS = [
    {
        "name": "System",
        "description": "Service health, UpNote Electron process status, and UI deep-link triggers.",
    },
    {
        "name": "Spaces",
        "description": "Query UpNote workspaces (`General` default space and custom spaces like `CHEP`).",
    },
    {
        "name": "Notebooks",
        "description": "List, search, inspect, and create notebooks across UpNote spaces.",
    },
    {
        "name": "Tags",
        "description": "List hashtags (`#tag`), counts, and retrieve notes associated with a tag.",
    },
    {
        "name": "Notes",
        "description": "Full CRUD for UpNote pages/notes with Markdown rendering, non-wrapping code blocks, notebooks, tags, Trash, and cloud sync.",
    },
    {
        "name": "Filters & Files",
        "description": "Inspect saved UpNote filters and file attachments.",
    },
]

app = FastAPI(
    title="UpNote Local Store & Deep-Link REST API",
    description=(
        "Full REST API for querying, searching, creating, updating, tagging, and "
        "managing notes, notebooks, tags, and workspaces in UpNote on Linux.\n\n"
        "- **Read Operations** query UpNote's local SQLite store (`~/.config/UpNote/upnote.sqlite3`) in read-only mode.\n"
        "- **Write Operations** dispatch `upnote://x-callback-url/...` commands to the running UpNote Electron instance "
        "so new notes, notebooks, tags, and trash/restore actions automatically synchronize with **UpNote Cloud**."
    ),
    version=APP_VERSION,
    openapi_tags=OPENAPI_TAGS,
    contact={
        "name": "Russell Milliner",
        "email": "russell.milliner@gmail.com",
        "url": "https://github.com/russellmilliner/upnote-api",
    },
    license_info={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
    },
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=StatusOut, tags=["System"], summary="Health check")
@app.get("/api/v1/status", response_model=StatusOut, tags=["System"], summary="Get UpNote service and store status")
def get_status() -> StatusOut:
    """Return service health, UpNote process status, and store item counts."""
    pid, binary, _ = find_upnote_process()
    counts: dict[str, int] = {}
    if DB_PATH.exists():
        with get_db_connection(readonly=True) as conn:
            counts["spaces"] = (
                conn.execute("SELECT COUNT(*) FROM workspaces WHERE deleted = 0").fetchone()[0]
                + 1
            )
            counts["notebooks"] = conn.execute(
                "SELECT COUNT(*) FROM notebooks WHERE deleted = 0"
            ).fetchone()[0]
            counts["notes_active"] = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE deleted = 0 AND trashed = 0"
            ).fetchone()[0]
            counts["notes_trashed"] = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE deleted = 0 AND trashed = 1"
            ).fetchone()[0]
            counts["notes_unsynced"] = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE deleted = 0 AND synced = 0"
            ).fetchone()[0]
            counts["tags"] = conn.execute(
                "SELECT COUNT(*) FROM tags WHERE deleted = 0"
            ).fetchone()[0]
            counts["filters"] = conn.execute(
                "SELECT COUNT(*) FROM filters WHERE deleted = 0"
            ).fetchone()[0]
            counts["files"] = conn.execute(
                "SELECT COUNT(*) FROM files WHERE deleted = 0"
            ).fetchone()[0]

    return StatusOut(
        ok=True,
        version=APP_VERSION,
        db_path=str(DB_PATH),
        db_exists=DB_PATH.exists(),
        upnote_running=pid is not None,
        upnote_pid=pid,
        upnote_binary=binary,
        counts=counts,
    )


@app.post("/api/v1/app/launch", response_model=StatusOut, tags=["System"], summary="Ensure UpNote desktop app is running")
def launch_upnote_app() -> StatusOut:
    """Ensure the UpNote desktop Electron process is running in the background."""
    ensure_upnote_running()
    return get_status()


@app.post("/api/v1/app/open", tags=["System"], summary="Open a note, notebook, tag, or deep link in UpNote UI")
def open_in_upnote_ui(req: DeepLinkOpenRequest) -> dict[str, Any]:
    """Open a note, notebook, tag, or custom x-callback-url in the UpNote UI."""
    if req.url:
        if not req.url.startswith("upnote://"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="URL must start with 'upnote://'",
            )
        url = req.url
    elif req.note_id:
        url = f"upnote://x-callback-url/openNote?noteId={urllib.parse.quote(req.note_id)}"
    elif req.notebook_id:
        url = f"upnote://x-callback-url/openNotebook?notebookId={urllib.parse.quote(req.notebook_id)}"
    elif req.tag:
        norm = normalize_tag_name(req.tag)
        url = f"upnote://x-callback-url/tag/view?tag={urllib.parse.quote(norm)}"
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide one of 'url', 'note_id', 'notebook_id', or 'tag'.",
        )
    dispatch_deeplink(url)
    return {"dispatched": True, "url": url}


# ---------------------------------------------------------------------------
# Spaces / Workspaces Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/v1/spaces", response_model=list[SpaceOut], tags=["Spaces"], summary="List all workspaces/spaces")
def list_spaces() -> list[SpaceOut]:
    """List all UpNote spaces (including the default 'General' space)."""
    with get_db_connection(readonly=True) as conn:
        spaces_map = load_spaces_map(conn)
        result: list[SpaceOut] = []

        gen_nb = conn.execute(
            "SELECT COUNT(*) FROM notebooks WHERE deleted = 0 AND (space IS NULL OR space = '')"
        ).fetchone()[0]
        gen_notes = conn.execute(
            "SELECT COUNT(*) FROM notes WHERE deleted = 0 AND trashed = 0 AND (space IS NULL OR space = '')"
        ).fetchone()[0]
        gen_tags = conn.execute(
            "SELECT COUNT(*) FROM tags WHERE deleted = 0 AND (space IS NULL OR space = '')"
        ).fetchone()[0]
        result.append(
            SpaceOut(
                id="default",
                sqlite_space=None,
                name="General",
                locked=False,
                notebooks_count=gen_nb,
                notes_count=gen_notes,
                tags_count=gen_tags,
            )
        )

        for sp_key, (sp_id, sp_name, locked) in spaces_map.items():
            if not sp_key:
                continue
            nb_c = conn.execute(
                "SELECT COUNT(*) FROM notebooks WHERE deleted = 0 AND space = ?",
                (sp_key,),
            ).fetchone()[0]
            nt_c = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE deleted = 0 AND trashed = 0 AND space = ?",
                (sp_key,),
            ).fetchone()[0]
            tg_c = conn.execute(
                "SELECT COUNT(*) FROM tags WHERE deleted = 0 AND space = ?",
                (sp_key,),
            ).fetchone()[0]
            result.append(
                SpaceOut(
                    id=sp_id,
                    sqlite_space=sp_key,
                    name=sp_name,
                    locked=locked,
                    notebooks_count=nb_c,
                    notes_count=nt_c,
                    tags_count=tg_c,
                )
            )
        return result


@app.get("/api/v1/spaces/{space}", response_model=SpaceOut, tags=["Spaces"], summary="Get space details by name or ID")
def get_space(space: str) -> SpaceOut:
    """Get a single space by name ('General', 'CHEP') or ID ('default', UUID)."""
    with get_db_connection(readonly=True) as conn:
        sp_id, _, _ = resolve_space(conn, space)
    for item in list_spaces():
        if item.id == sp_id:
            return item
    raise HTTPException(status_code=404, detail=f"Space '{space}' not found.")


# ---------------------------------------------------------------------------
# Notebooks Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/v1/notebooks", response_model=list[NotebookOut], tags=["Notebooks"], summary="List or search notebooks")
def list_notebooks(
    space: str | None = Query(
        default=None,
        description="Filter by space name ('General', 'CHEP') or ID ('default', UUID)",
    ),
    parent_id: str | None = Query(default=None, description="Filter by parent notebook UUID"),
    q: str | None = Query(default=None, description="Case-insensitive search by notebook title/path"),
) -> list[NotebookOut]:
    """List notebooks with their nested paths and note counts."""
    with get_db_connection(readonly=True) as conn:
        target_space_id: str | None = None
        if space:
            target_space_id, _, _ = resolve_space(conn, space)
        notebooks_by_id, _ = load_notebooks_index(conn)
        items = list(notebooks_by_id.values())
        if target_space_id:
            items = [nb for nb in items if nb.space_id == target_space_id]
        if parent_id is not None:
            items = [nb for nb in items if nb.parent_id == parent_id]
        if q:
            q_low = q.lower()
            items = [
                nb
                for nb in items
                if q_low in nb.title.lower() or q_low in nb.path.lower()
            ]
        return sorted(items, key=lambda x: (x.space_name.lower(), x.path.lower()))


@app.get("/api/v1/notebooks/{notebook}", response_model=NotebookOut, tags=["Notebooks"], summary="Get notebook by UUID, title, or path")
def get_notebook(
    notebook: str,
    space: str | None = Query(default=None, description="Optional space name or ID"),
) -> NotebookOut:
    """Get a notebook by UUID, title (e.g. 'AI'), or slash-separated path."""
    with get_db_connection(readonly=True) as conn:
        target_space_id: str | None = None
        if space:
            target_space_id, _, _ = resolve_space(conn, space)
        nb = resolve_notebook(conn, notebook, space_id=target_space_id)
        if not nb:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Notebook '{notebook}' not found.",
            )
        return nb


@app.get(
    "/api/v1/notebooks/{notebook}/notes",
    response_model=list[NoteOut],
    tags=["Notebooks"],
    summary="List all active notes inside a notebook",
)
def list_notebook_notes(
    notebook: str,
    space: str | None = Query(default=None, description="Optional space name or ID"),
    include_content: bool = Query(
        default=False, description="Include full 'text' and 'html' fields"
    ),
) -> list[NoteOut]:
    """List all active notes inside a specific notebook."""
    with get_db_connection(readonly=True) as conn:
        target_space_id: str | None = None
        if space:
            target_space_id, _, _ = resolve_space(conn, space)
        nb = resolve_notebook(conn, notebook, space_id=target_space_id)
        if not nb:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Notebook '{notebook}' not found.",
            )
        spaces_map = load_spaces_map(conn)
        _, note_to_notebooks = load_notebooks_index(conn)
        notes_out: list[NoteOut] = []
        for nid in nb.note_ids:
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ? AND deleted = 0 AND trashed = 0",
                (nid,),
            ).fetchone()
            if row:
                notes_out.append(
                    row_to_note_out(
                        row, spaces_map, note_to_notebooks, include_content=include_content
                    )
                )
        return notes_out


@app.post(
    "/api/v1/notebooks",
    response_model=NotebookOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Notebooks"],
    summary="Create a new notebook",
)
def create_notebook(req: NotebookCreateRequest) -> NotebookOut:
    """Create a new notebook in the target space via UpNote x-callback-url."""
    with _DEEPLINK_LOCK:
        with get_db_connection(readonly=True) as conn:
            deeplink_space_id, _, _ = resolve_space(conn, req.space)
            existing = resolve_notebook(conn, req.title, space_id=deeplink_space_id)
            if existing:
                return existing

        url = "upnote://x-callback-url/notebook/new?" + urllib.parse.urlencode(
            {"spaceId": deeplink_space_id, "title": req.title.strip()},
            quote_via=urllib.parse.quote,
        )
        dispatch_deeplink(url)

        for _ in range(25):
            time.sleep(0.25)
            with get_db_connection(readonly=True) as conn:
                nb = resolve_notebook(conn, req.title, space_id=deeplink_space_id)
                if nb:
                    return nb

        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Dispatched notebook/new for '{req.title}', but it did not appear in SQLite.",
        )


# ---------------------------------------------------------------------------
# Tags Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/v1/tags", response_model=list[TagOut], tags=["Tags"], summary="List all tags with note counts")
def list_tags(
    space: str | None = Query(default=None, description="Filter by space name or ID"),
    q: str | None = Query(default=None, description="Search tag name"),
    include_inactive: bool = Query(default=True, description="Include inactive tags"),
) -> list[TagOut]:
    """List all tags along with active note counts."""
    with get_db_connection(readonly=True) as conn:
        spaces_map = load_spaces_map(conn)
        target_space_id: str | None = None
        if space:
            target_space_id, _, _ = resolve_space(conn, space)

        tag_counts: dict[tuple[str, str], int] = {}
        note_rows = conn.execute(
            "SELECT space, tagLinks FROM notes WHERE deleted = 0 AND trashed = 0 AND tagLinks IS NOT NULL AND tagLinks != '[]'"
        ).fetchall()
        for nr in note_rows:
            sp_id, _, _ = spaces_map.get(nr["space"], (nr["space"] or "default", "General", False))
            try:
                tlinks = json.loads(nr["tagLinks"] or "[]")
                if isinstance(tlinks, list):
                    for t in tlinks:
                        key = (sp_id, str(t).lower())
                        tag_counts[key] = tag_counts.get(key, 0) + 1
            except json.JSONDecodeError:
                pass

        tag_rows = conn.execute(
            "SELECT * FROM tags WHERE deleted = 0 ORDER BY title COLLATE NOCASE ASC"
        ).fetchall()
        results: list[TagOut] = []
        for r in tag_rows:
            if not include_inactive and r["inactive"]:
                continue
            sp_id, sp_name, _ = spaces_map.get(
                r["space"], (r["space"] or "default", "General", False)
            )
            if target_space_id and sp_id != target_space_id:
                continue
            title = r["title"] or ""
            norm = normalize_tag_name(title)
            if q and q.lower().lstrip("#") not in norm.lower():
                continue
            results.append(
                TagOut(
                    id=r["id"],
                    title=title,
                    name=norm,
                    space_id=sp_id,
                    space_name=sp_name,
                    inactive=bool(r["inactive"]),
                    synced=bool(r["synced"]),
                    notes_count=tag_counts.get((sp_id, norm.lower()), 0),
                    created_at=r["createdAt"],
                    updated_at=r["updatedAt"],
                )
            )
        return results


@app.get("/api/v1/tags/{tag}/notes", response_model=list[NoteOut], tags=["Tags"], summary="List notes carrying a specific tag")
def list_tag_notes(
    tag: str,
    space: str | None = Query(default=None, description="Optional space filter"),
    include_content: bool = Query(default=False, description="Include full 'text' and 'html'"),
) -> list[NoteOut]:
    """List all active notes tagged with the given tag name."""
    norm = normalize_tag_name(tag).lower()
    return list_notes(
        space=space,
        tag=norm,
        include_content=include_content,
        limit=500,
        offset=0,
    )


# ---------------------------------------------------------------------------
# Notes CRUD Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/v1/notes", response_model=list[NoteOut], tags=["Notes"], summary="Search and filter notes")
def list_notes(
    space: str | None = Query(
        default=None, description="Filter by space name ('General', 'CHEP') or ID"
    ),
    notebook: str | None = Query(
        default=None, description="Filter by notebook title (e.g. 'AI'), path, or UUID"
    ),
    tag: str | None = Query(
        default=None, description="Filter by tag name (with or without '#', e.g. 'plex')"
    ),
    q: str | None = Query(
        default=None, description="Full-text search query across note title and text"
    ),
    trashed: bool = Query(default=False, description="Whether to return trashed notes"),
    pinned: bool | None = Query(default=None, description="Filter by pinned status"),
    bookmarked: bool | None = Query(default=None, description="Filter by bookmarked status"),
    has_todo: bool | None = Query(default=None, description="Filter notes containing todos"),
    is_template: bool | None = Query(default=None, description="Filter template notes"),
    unsynced: bool | None = Query(default=None, description="Filter unsynced notes"),
    include_content: bool = Query(
        default=False,
        description="Include full 'text' and 'html' body in each returned note",
    ),
    limit: int = Query(default=50, ge=1, le=500, description="Maximum notes to return"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
) -> list[NoteOut]:
    """Search and filter notes across spaces, notebooks, and tags."""
    with get_db_connection(readonly=True) as conn:
        spaces_map = load_spaces_map(conn)
        _, note_to_notebooks = load_notebooks_index(conn)

        clauses = ["deleted = 0", "trashed = ?"]
        params: list[Any] = [1 if trashed else 0]

        target_space_id: str | None = None
        if space:
            target_space_id, sqlite_space_val, _ = resolve_space(conn, space)
            if sqlite_space_val is None:
                clauses.append("(space IS NULL OR space = '')")
            else:
                clauses.append("space = ?")
                params.append(sqlite_space_val)

        if notebook:
            nb = resolve_notebook(conn, notebook, space_id=target_space_id)
            if not nb or not nb.note_ids:
                return []
            placeholders = ",".join("?" for _ in nb.note_ids)
            clauses.append(f"id IN ({placeholders})")
            params.extend(nb.note_ids)

        if pinned is not None:
            clauses.append("pinned = ?")
            params.append(1 if pinned else 0)
        if bookmarked is not None:
            clauses.append("bookmarked = ?")
            params.append(1 if bookmarked else 0)
        if has_todo is not None:
            clauses.append("hasTodo = ?")
            params.append(1 if has_todo else 0)
        if is_template is not None:
            clauses.append("isTemplate = ?")
            params.append(1 if is_template else 0)
        if unsynced is not None:
            clauses.append("synced = ?")
            params.append(0 if unsynced else 1)

        if q:
            clauses.append("(title LIKE ? OR text LIKE ?)")
            like_q = f"%{q}%"
            params.extend([like_q, like_q])

        sql = f"SELECT * FROM notes WHERE {' AND '.join(clauses)} ORDER BY pinned DESC, updatedAt DESC"
        rows = conn.execute(sql, params).fetchall()

        norm_tag = normalize_tag_name(tag).lower() if tag else None
        results: list[NoteOut] = []
        for r in rows:
            note_out = row_to_note_out(
                r, spaces_map, note_to_notebooks, include_content=include_content
            )
            if norm_tag and norm_tag not in [t.lower() for t in note_out.tags]:
                continue
            results.append(note_out)

        return results[offset : offset + limit]


@app.get("/api/v1/notes/{note_id_or_title}", response_model=NoteOut, tags=["Notes"], summary="Get a single note by UUID or exact title")
def get_note(
    note_id_or_title: str,
    space: str | None = Query(default=None, description="Optional space filter when looking up by title"),
    notebook: str | None = Query(default=None, description="Optional notebook filter when looking up by title"),
) -> NoteOut:
    """Get a single note (including full text and HTML) by UUID or exact title."""
    with get_db_connection(readonly=True) as conn:
        spaces_map = load_spaces_map(conn)
        _, note_to_notebooks = load_notebooks_index(conn)

        row = conn.execute(
            "SELECT * FROM notes WHERE id = ? AND deleted = 0",
            (note_id_or_title,),
        ).fetchone()
        if row:
            return row_to_note_out(row, spaces_map, note_to_notebooks, include_content=True)

        matches = list_notes(
            space=space,
            notebook=notebook,
            q=note_id_or_title,
            include_content=True,
            limit=50,
        )
        for m in matches:
            if m.title.lower() == note_id_or_title.strip().lower():
                return m

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Note '{note_id_or_title}' not found.",
        )


@app.post(
    "/api/v1/notes",
    response_model=NoteOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Notes"],
    summary="Create a new note in UpNote",
)
def create_note(req: NoteCreateRequest) -> NoteOut:
    """
    Create a new note in UpNote (with space, notebook, tags, and Markdown formatting)
    and automatically sync to UpNote Cloud.
    """
    return create_note_via_upnote(req)


@app.put("/api/v1/notes/{note_id}", response_model=NoteOut, tags=["Notes"], summary="Replace or update an existing note")
@app.patch("/api/v1/notes/{note_id}", response_model=NoteOut, tags=["Notes"], summary="Partially update an existing note")
def update_note(note_id: str, req: NoteUpdateRequest) -> NoteOut:
    """
    Update an existing note.

    - `strategy='cloud_replace'` (default): Creates the updated note via UpNote's
      `x-callback-url/note/new` in the target space/notebook/tags and moves the
      previous revision to Trash so UpNote Cloud syncs the change immediately.
    - `strategy='sqlite_direct'`: Updates the SQLite row in place in `upnote.sqlite3`.
    """
    existing = get_note(note_id)

    if req.strategy == "sqlite_direct":
        now_ms = float(int(time.time() * 1000))
        with get_db_connection(readonly=False) as conn:
            updates = ["updatedAt = ?", "synced = 0", "revision = COALESCE(revision, 0) + 1"]
            params: list[Any] = [now_ms]

            if req.title is not None:
                updates.append("title = ?")
                params.append(req.title)
            if req.text is not None:
                updates.append("text = ?")
                params.append(req.text)
                updates.append("summary = ?")
                params.append(req.text[:200])
            if req.html is not None:
                updates.append("html = ?")
                params.append(req.html)
            if req.pinned is not None:
                updates.append("pinned = ?")
                params.append(1 if req.pinned else 0)
            if req.bookmarked is not None:
                updates.append("bookmarked = ?")
                params.append(1 if req.bookmarked else 0)
            if req.tags is not None or req.tag is not None:
                new_tags: list[str] = []
                if req.tag:
                    new_tags.append(normalize_tag_name(req.tag).lower())
                if req.tags:
                    for t in req.tags:
                        norm = normalize_tag_name(t).lower()
                        if norm and norm not in new_tags:
                            new_tags.append(norm)
                updates.append("tagLinks = ?")
                params.append(json.dumps(new_tags))

            params.append(existing.id)
            conn.execute(
                f"UPDATE notes SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()
        return get_note(existing.id)

    target_title = req.title if req.title is not None else existing.title
    if req.text is not None:
        target_text = req.text
    else:
        raw_text = existing.text or ""
        if raw_text.startswith(existing.title + "\n"):
            raw_text = raw_text[len(existing.title) + 1 :]
        target_text = raw_text

    target_space = req.space if req.space is not None else existing.space_id
    target_notebook = (
        req.notebook
        if req.notebook is not None
        else (existing.notebooks[0].path if existing.notebooks else None)
    )
    target_tags = (
        req.tags
        if req.tags is not None
        else ([req.tag] if req.tag is not None else existing.tags)
    )

    new_note = create_note_via_upnote(
        NoteCreateRequest(
            title=target_title,
            text=target_text,
            markdown=req.markdown,
            space=target_space,
            notebook=target_notebook,
            tags=target_tags,
        )
    )

    dispatch_deeplink(
        f"upnote://x-callback-url/note/moveToTrash?noteId={urllib.parse.quote(existing.id)}"
    )
    time.sleep(0.3)
    return new_note


@app.delete("/api/v1/notes/{note_id}", tags=["Notes"], summary="Move a note to Trash or permanently delete")
def delete_note(
    note_id: str,
    permanent: bool = Query(
        default=False,
        description="If false (default), moves the note to UpNote Trash via x-callback-url; if true, marks deleted=1 in SQLite",
    ),
) -> dict[str, Any]:
    """Move a note to Trash (synced to UpNote Cloud) or permanently mark it deleted."""
    existing = get_note(note_id)
    if permanent:
        with get_db_connection(readonly=False) as conn:
            conn.execute(
                "UPDATE notes SET trashed = 1, deleted = 1, updatedAt = ? WHERE id = ?",
                (float(int(time.time() * 1000)), existing.id),
            )
            conn.commit()
        return {"id": existing.id, "title": existing.title, "trashed": True, "deleted": True}

    with _DEEPLINK_LOCK:
        dispatch_deeplink(
            f"upnote://x-callback-url/note/moveToTrash?noteId={urllib.parse.quote(existing.id)}"
        )
        for _ in range(20):
            time.sleep(0.2)
            with get_db_connection(readonly=True) as conn:
                r = conn.execute(
                    "SELECT trashed FROM notes WHERE id = ?", (existing.id,)
                ).fetchone()
                if r and r["trashed"] == 1:
                    break

    return {"id": existing.id, "title": existing.title, "trashed": True, "deleted": False}


@app.post("/api/v1/notes/{note_id}/restore", response_model=NoteOut, tags=["Notes"], summary="Restore a trashed note")
def restore_note(note_id: str) -> NoteOut:
    """Restore a trashed note via `upnote://x-callback-url/note/restore`."""
    with _DEEPLINK_LOCK:
        dispatch_deeplink(
            f"upnote://x-callback-url/note/restore?noteId={urllib.parse.quote(note_id)}"
        )
        for _ in range(20):
            time.sleep(0.2)
            with get_db_connection(readonly=True) as conn:
                r = conn.execute(
                    "SELECT trashed FROM notes WHERE id = ?", (note_id,)
                ).fetchone()
                if r and r["trashed"] == 0:
                    break
    return get_note(note_id)


# ---------------------------------------------------------------------------
# Filters & Files Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/v1/filters", response_model=list[FilterOut], tags=["Filters & Files"], summary="List saved UpNote filters")
def list_filters(
    space: str | None = Query(default=None, description="Optional space filter"),
) -> list[FilterOut]:
    """List saved filters in the UpNote store."""
    with get_db_connection(readonly=True) as conn:
        spaces_map = load_spaces_map(conn)
        target_space_id: str | None = None
        if space:
            target_space_id, _, _ = resolve_space(conn, space)
        rows = conn.execute("SELECT * FROM filters WHERE deleted = 0").fetchall()
        out: list[FilterOut] = []
        for r in rows:
            sp_id, sp_name, _ = spaces_map.get(
                r["space"], (r["space"] or "default", "General", False)
            )
            if target_space_id and sp_id != target_space_id:
                continue
            out.append(
                FilterOut(
                    id=r["id"],
                    filter_type=r["filterType"],
                    words=r["words"],
                    space_id=sp_id,
                    space_name=sp_name,
                    inactive=bool(r["inactive"]),
                    synced=bool(r["synced"]),
                    updated_at=r["updatedAt"],
                )
            )
        return out


@app.get("/api/v1/files", response_model=list[FileOut], tags=["Filters & Files"], summary="List file attachments")
def list_files(
    q: str | None = Query(default=None, description="Search attachment filename"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[FileOut]:
    """List file attachments tracked in the UpNote store."""
    with get_db_connection(readonly=True) as conn:
        if q:
            rows = conn.execute(
                "SELECT * FROM files WHERE deleted = 0 AND name LIKE ? ORDER BY updatedAt DESC LIMIT ?",
                (f"%{q}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM files WHERE deleted = 0 ORDER BY updatedAt DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            FileOut(
                id=r["id"],
                name=r["name"],
                version=r["version"],
                download_url=r["downloadURL"],
                tag=r["tag"],
                synced=bool(r["synced"]),
                created_at=r["createdAt"],
                updated_at=r["updatedAt"],
            )
            for r in rows
        ]


def main() -> None:
    uvicorn.run(
        app,
        host=API_HOST,
        port=API_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
