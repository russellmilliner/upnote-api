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

import argparse
import hashlib
import html as html_mod
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

APP_VERSION = "0.2.4"
GITHUB_REPO = "russellmilliner/upnote-api"
GITHUB_LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases"
ALLOWED_DOWNLOAD_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/download/"
UPDATE_CHECK_INTERVAL_SECONDS = 6 * 3600
MAX_DEEPLINK_BYTES = 120_000

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
API_TOKEN = os.environ.get("UPNOTE_API_TOKEN", "").strip()
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "UPNOTE_CORS_ORIGINS",
        f"http://127.0.0.1:{API_PORT},http://localhost:{API_PORT}",
    ).split(",")
    if o.strip()
]
ALLOWED_HOSTS = frozenset(
    h.strip().lower()
    for h in os.environ.get(
        "UPNOTE_ALLOWED_HOSTS",
        "127.0.0.1,localhost,::1",
    ).split(",")
    if h.strip()
)
# Allow skipping the release checksum only by explicit opt-out (not recommended).
REQUIRE_UPDATE_CHECKSUM = os.environ.get("UPNOTE_REQUIRE_UPDATE_CHECKSUM", "1") != "0"

logger = logging.getLogger("upnote_api")

# Lock to serialize deep-link dispatches so rapid concurrent creations can be
# matched deterministically in SQLite.
_DEEPLINK_LOCK = threading.RLock()


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
    markdown: str | None = None
    html: str | None = None
    redirected_from: str | None = Field(
        default=None,
        description=(
            "Set when the requested id was a trashed revision and the newest active note "
            "with the same title in the same space was returned instead."
        ),
    )


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
            "'sqlite_direct' updates the row in-place in upnote.sqlite3 (offline-only)."
        ),
    )
    dedupe: bool = Field(
        default=False,
        description=(
            "cloud_replace only: also trash other active notes with the same title in the "
            "same space AND same notebook(s). Off by default so unrelated notes are never touched."
        ),
    )
    force: bool = Field(
        default=False,
        description="sqlite_direct only: allow writing while UpNote is running (unsafe).",
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


class UpdateCheckOut(BaseModel):
    current_version: str = APP_VERSION
    latest_version: str = APP_VERSION
    update_available: bool = False
    release_url: str = GITHUB_RELEASES_PAGE_URL
    download_url: str | None = None
    sha256_url: str | None = None
    release_notes: str = ""
    checked_at: float | None = None


class UpdateInstallOut(BaseModel):
    ok: bool = True
    previous_version: str = APP_VERSION
    installed_version: str
    release_url: str
    message: str


class StatusOut(BaseModel):
    ok: bool = True
    version: str = APP_VERSION
    latest_version: str = APP_VERSION
    update_available: bool = False
    release_url: str | None = None
    db_path: str
    db_exists: bool
    upnote_running: bool
    upnote_pid: int | None = None
    upnote_binary: str | None = None
    counts: dict[str, int]


# ---------------------------------------------------------------------------
# GitHub Release Update Checker & Self-Updater
# ---------------------------------------------------------------------------

_UPDATE_CACHE_LOCK = threading.Lock()
_CACHED_UPDATE_INFO: UpdateCheckOut = UpdateCheckOut()


def _parse_semver(version: str) -> tuple[int, int, int]:
    cleaned = version.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = [int(p) if p.isdigit() else 0 for p in cleaned.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def is_newer_version(current: str, candidate: str) -> bool:
    return _parse_semver(candidate) > _parse_semver(current)


def check_for_updates(timeout: float = 10.0) -> UpdateCheckOut:
    """Query GitHub Releases API for the latest UpNote REST API version."""
    global _CACHED_UPDATE_INFO
    req = urllib.request.Request(
        GITHUB_LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"upnote-api/{APP_VERSION}",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    tag_name = str(data.get("tag_name") or "").strip()
    latest_version = tag_name.lstrip("vV") or APP_VERSION
    html_url = str(data.get("html_url") or GITHUB_RELEASES_PAGE_URL)
    body = str(data.get("body") or "")
    download_url: str | None = None
    sha256_url: str | None = None
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        burl = str(asset.get("browser_download_url") or "")
        if name.endswith(".tar.gz") and burl:
            download_url = burl
        elif name.endswith(".tar.gz.sha256") and burl:
            sha256_url = burl

    info = UpdateCheckOut(
        current_version=APP_VERSION,
        latest_version=latest_version,
        update_available=is_newer_version(APP_VERSION, latest_version),
        release_url=html_url,
        download_url=download_url,
        sha256_url=sha256_url,
        release_notes=body,
        checked_at=time.time(),
    )
    with _UPDATE_CACHE_LOCK:
        _CACHED_UPDATE_INFO = info
    return info


def get_cached_update_info() -> UpdateCheckOut:
    with _UPDATE_CACHE_LOCK:
        return _CACHED_UPDATE_INFO


def _background_update_check_loop() -> None:
    time.sleep(3.0)
    while True:
        try:
            check_for_updates(timeout=10.0)
        except Exception:
            pass
        time.sleep(UPDATE_CHECK_INTERVAL_SECONDS)


def verify_archive_sha256(archive_bytes: bytes, sha_text: str) -> None:
    """Raise RuntimeError unless `sha_text` (sha256sum format) matches `archive_bytes`."""
    parts = sha_text.strip().split()
    expected = parts[0].lower() if parts else ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("Release checksum file is malformed.")
    actual = hashlib.sha256(archive_bytes).hexdigest().lower()
    if not secrets.compare_digest(expected, actual):
        raise RuntimeError(
            f"SHA-256 checksum mismatch for release archive (expected {expected}, got {actual})"
        )



def _mcp_python() -> str:
    """Interpreter for the MCP server script (needs the `mcp` package; system python may lack it)."""
    env_py = os.environ.get("UPNOTE_MCP_PYTHON", "").strip()
    if env_py:
        return env_py
    venv_py = Path.home() / ".local" / "share" / "agy-mcp" / ".venv" / "bin" / "python"
    return str(venv_py) if venv_py.exists() else "/usr/bin/env python3"


def _install_mcp_script(src: Path, dst: Path) -> None:
    """Copy upnote_mcp.py to `dst`, pinning its shebang to an interpreter that has `mcp`."""
    lines = src.read_text(encoding="utf-8").splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        lines[0] = f"#!{_mcp_python()}\n"
    dst.write_text("".join(lines), encoding="utf-8")
    dst.chmod(0o755)


def install_update_from_github(restart_service: bool = True) -> UpdateInstallOut:
    """Download, verify, and install the latest GitHub release archive."""
    info = check_for_updates(timeout=15.0)
    if not info.update_available:
        return UpdateInstallOut(
            ok=True,
            previous_version=APP_VERSION,
            installed_version=APP_VERSION,
            release_url=info.release_url,
            message=f"Already up to date (v{APP_VERSION}).",
        )
    if not info.download_url:
        raise RuntimeError(f"No .tar.gz release asset found at {info.release_url}")
    if not info.download_url.startswith(ALLOWED_DOWNLOAD_PREFIX):
        raise RuntimeError(
            f"Refusing untrusted download URL outside {ALLOWED_DOWNLOAD_PREFIX}: {info.download_url}"
        )

    with tempfile.TemporaryDirectory(prefix="upnote-api-update-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        archive_path = tmp_path / "upnote-api-release.tar.gz"
        req = urllib.request.Request(
            info.download_url,
            headers={"User-Agent": f"upnote-api/{APP_VERSION}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=60.0) as resp:
            archive_bytes = resp.read()
            archive_path.write_bytes(archive_bytes)

        sha_url_ok = bool(info.sha256_url) and str(info.sha256_url).startswith(ALLOWED_DOWNLOAD_PREFIX)
        if not sha_url_ok:
            if REQUIRE_UPDATE_CHECKSUM:
                raise RuntimeError(
                    "Release is missing a trusted .tar.gz.sha256 asset; refusing to install "
                    "(set UPNOTE_REQUIRE_UPDATE_CHECKSUM=0 to override)."
                )
            logger.warning("Installing release without checksum verification (override enabled).")
        else:
            sha_req = urllib.request.Request(
                str(info.sha256_url),
                headers={"User-Agent": f"upnote-api/{APP_VERSION}"},
                method="GET",
            )
            with urllib.request.urlopen(sha_req, timeout=15.0) as sha_resp:
                sha_text = sha_resp.read().decode("utf-8")
            verify_archive_sha256(archive_bytes, sha_text)

        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=tmp_path, filter="data")

        install_dir = Path.home() / ".local" / "share" / "upnote-api"
        mcp_dir = Path.home() / ".local" / "share" / "agy-mcp"
        bin_dir = Path.home() / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        mcp_dir.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)

        extracted_api = list(tmp_path.glob("**/src/upnote_api.py"))
        extracted_cli = list(tmp_path.glob("**/src/upnote_cli.py"))
        extracted_mcp = list(tmp_path.glob("**/src/upnote_mcp.py"))
        if not extracted_api or not extracted_cli:
            raise RuntimeError("Release archive is missing src/upnote_api.py or src/upnote_cli.py")

        target_api = install_dir / "upnote_api.py"
        target_cli = bin_dir / "upnote-cli"
        shutil.copy2(extracted_api[0], target_api)
        target_api.chmod(0o755)
        shutil.copy2(extracted_cli[0], target_cli)
        target_cli.chmod(0o755)

        if extracted_mcp:
            for mcp_target in (
                install_dir / "upnote_mcp.py",
                mcp_dir / "upnote_mcp.py",
                bin_dir / "upnote-mcp",
            ):
                _install_mcp_script(extracted_mcp[0], mcp_target)

        if restart_service:
            subprocess.Popen(
                ["sh", "-c", "sleep 1 && systemctl --user restart upnote-api.service"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

    return UpdateInstallOut(
        ok=True,
        previous_version=APP_VERSION,
        installed_version=info.latest_version,
        release_url=info.release_url,
        message=f"Installed UpNote REST API v{info.latest_version}"
        + (" and scheduled upnote-api.service restart." if restart_service else "."),
    )


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


_CACHED_UPNOTE_PROC: tuple[int, str, dict[str, str]] | None = None


def _cached_proc_still_valid(cpid: int, cbin: str, uid: int) -> bool:
    """A cached (pid, binary) is valid only if the pid is ours and still runs that binary."""
    proc_path = Path(f"/proc/{cpid}")
    try:
        if not proc_path.exists() or proc_path.stat().st_uid != uid:
            return False
        if cbin == str(APPIMAGE_PATH):
            return APPIMAGE_PATH.exists()
        return os.readlink(proc_path / "exe") == cbin and Path(cbin).exists()
    except OSError:
        return False


def find_upnote_process() -> tuple[int | None, str | None, dict[str, str]]:
    """
    Locate the running UpNote AppImage / Electron process owned by the current UID,
    resolve its mounted binary directly from `/proc/<pid>/exe` (never from a `/tmp`
    glob, which another local user could plant; SquashFS FUSE mounts report uid=0 for
    their inodes so ownership of the mount itself cannot be checked), and extract its
    desktop session environment variables. Falls back only to the configured
    `UPNOTE_APPIMAGE_PATH`.
    """
    global _CACHED_UPNOTE_PROC
    uid = os.getuid()
    if _CACHED_UPNOTE_PROC is not None:
        cpid, cbin, cenv = _CACHED_UPNOTE_PROC
        if _cached_proc_still_valid(cpid, cbin, uid):
            return cpid, cbin, cenv
        _CACHED_UPNOTE_PROC = None

    found_pid: int | None = None
    proc_binary_path: str | None = None
    session_env: dict[str, str] = {}

    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        pid = int(proc_dir.name)
        try:
            if proc_dir.stat().st_uid != uid:
                continue
            cmdline = (proc_dir / "cmdline").read_bytes().decode("utf-8", errors="ignore")
            if "upnote.appimage" in cmdline or "/.mount_upnote" in cmdline:
                try:
                    exe_target = os.readlink(proc_dir / "exe")
                    if "/.mount_upnote" in exe_target and Path(exe_target).exists():
                        proc_binary_path = exe_target
                except OSError:
                    pass
                env_bytes = (proc_dir / "environ").read_bytes()
                env_map: dict[str, str] = {}
                for item in env_bytes.split(b"\0"):
                    if b"=" in item:
                        k, v = item.decode("utf-8", errors="ignore").split("=", 1)
                        env_map[k] = v
                if "DISPLAY" in env_map or "WAYLAND_DISPLAY" in env_map:
                    found_pid = pid
                    session_env = env_map
                    if proc_binary_path:
                        break
        except (PermissionError, FileNotFoundError, ProcessLookupError, OSError):
            continue

    binary_path = proc_binary_path
    if not binary_path and APPIMAGE_PATH.exists():
        binary_path = str(APPIMAGE_PATH)

    if not session_env:
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
            except (PermissionError, FileNotFoundError, ProcessLookupError, OSError):
                continue

    if found_pid is not None and binary_path is not None:
        _CACHED_UPNOTE_PROC = (found_pid, binary_path, session_env)
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
            [str(LAUNCH_SCRIPT_PATH), "--auto-start-in-background"],
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


def sanitize_deeplink_url(url: str) -> str:
    """
    Validate that `url` uses the `upnote://` scheme and strip `x-success` / `x-error`
    callback parameters so callers cannot trigger Electron `shell.openExternal` on
    external URLs.
    """
    if not url.startswith("upnote://"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Deep-link URL must start with 'upnote://'.",
        )
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [
        (k, v)
        for k, v in pairs
        if k.lower() not in ("x-success", "x-error", "x-cancel")
    ]
    if len(filtered) == len(pairs):
        return url
    new_query = urllib.parse.urlencode(filtered, quote_via=urllib.parse.quote)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment)
    )


def build_deeplink_url(url: str, background: bool = True) -> str:
    """Sanitize an `upnote://` URL and append `_bg_new_window=true` when `background=True`."""
    clean_url = sanitize_deeplink_url(url)
    if background and "new_window=true" not in clean_url.lower():
        sep = "&" if "?" in clean_url else "?"
        clean_url = f"{clean_url}{sep}_bg_new_window=true"
    return clean_url


def dispatch_deeplink(url: str, background: bool = True) -> None:
    """
    Invoke UpNote with an upnote://x-callback-url/... deep link.

    When `background=True` (default for all API write/CRUD operations), appends
    `_bg_new_window=true` to the query string if `new_window=true` is not already
    present. UpNote's Electron main process (`dist/electron/main.js`) checks
    `!url.toLowerCase().includes('new_window=true')` before calling `mainWindow.show()`,
    while the renderer (`dist/electron/renderer.js`) checks
    `searchParams.get('new_window')?.toLowerCase() === 'true'` before opening a
    detail window. Passing `_bg_new_window=true` suppresses `mainWindow.show()`
    (preventing UpNote from popping up or stealing desktop focus) without opening
    a secondary note window.
    """
    final_url = build_deeplink_url(url, background=background)
    encoded_len = len(final_url.encode("utf-8"))
    if encoded_len > MAX_DEEPLINK_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"Encoded deep-link URL size ({encoded_len} bytes) exceeds Linux "
                f"MAX_ARG_STRLEN safe limit ({MAX_DEEPLINK_BYTES} bytes)."
            ),
        )

    _, binary, env = ensure_upnote_running()
    exec_env = os.environ.copy()
    exec_env.update(env)
    exec_env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    exec_env.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    )
    try:
        subprocess.run(
            [binary, final_url],
            env=exec_env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.exception("Deep-link dispatch failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to dispatch deep link to UpNote (see upnote-api.service logs).",
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
    notebooks_by_id: dict[str, NotebookOut] | None = None,
) -> NotebookOut | None:
    """Find a notebook by UUID, exact title, or slash-separated path."""
    if notebooks_by_id is None:
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


def _escape_like(term: str) -> str:
    """Escape SQLite LIKE wildcards ('%', '_', '\\') for literal substring searches."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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

    title_val = row["title"] or ""
    md_content = html_to_markdown(row["html"], title_val) if include_content else None

    return NoteOut(
        id=row["id"],
        title=title_val,
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
        text=(md_content or row["text"]) if include_content else None,
        markdown=md_content,
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


def strip_duplicate_leading_title(body: str, title: str) -> str:
    """Strip leading `# Title` or `## Title` lines if they duplicate the note's title."""
    if not body or not title:
        return body
    norm_title = re.sub(r"[\s\-–—:]+", " ", html_mod.unescape(title).strip().lower())
    s = body.strip()
    for _ in range(2):
        lines = s.splitlines()
        if not lines:
            break
        first = lines[0].strip()
        first_clean = re.sub(r"^#{1,6}\s*", "", first)
        norm_first = re.sub(r"[\s\-–—:`*]+", " ", html_mod.unescape(first_clean).strip().lower())
        if norm_first == norm_title:
            s = "\n".join(lines[1:]).strip()
        else:
            break
    return s


def html_to_markdown(raw_html: str | None, title: str = "") -> str:
    """Reconstruct clean, human-readable Markdown from UpNote's stored HTML column."""
    if not raw_html:
        return ""
    s = raw_html
    # Strip UpNote native tag links (<a ... data-upnote-tag="#..." ...>#...</a>)
    s = re.sub(r"<a\b[^>]*\bdata-upnote-tag=[^>]*>.*?</a>", "", s, flags=re.I | re.S)

    # Convert <pre ...>...</pre> blocks first with placeholders so inner HTML/newlines are preserved
    pre_blocks: list[str] = []

    def _replace_pre(m: re.Match[str]) -> str:
        attrs = m.group(1) or ""
        body = m.group(2) or ""
        lang_m = re.search(r"data-code-language=[\"\x27]([^\"\x27]+)[\"\x27]", attrs, re.I)
        lang = lang_m.group(1).strip() if lang_m else ""
        body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
        body = re.sub(r"<[^>]+>", "", body)
        body = html_mod.unescape(body).rstrip("\n")
        idx = len(pre_blocks)
        pre_blocks.append(f"\n```{lang}\n{body}\n```\n")
        return f"\n@@PRE_BLOCK_{idx}@@\n"

    s = re.sub(r"<pre(\s[^>]*)?>(.*?)</pre>", _replace_pre, s, flags=re.I | re.S)

    # Tokenize inline <code>...</code> spans before tag stripping or HTML unescaping so
    # escaped HTML tags (e.g. `<code>&lt;div&gt;</code>`) or `*` / `_` inside inline code
    # are never stripped or turned into bold/italic Markdown.
    code_spans: list[str] = []

    def _replace_code_span(m: re.Match[str]) -> str:
        inner = re.sub(r"<[^>]+>", "", m.group(1) or "")
        inner_unescaped = html_mod.unescape(inner).replace("\xa0", " ")
        idx = len(code_spans)
        if "`" in inner_unescaped:
            code_spans.append(f"`` {inner_unescaped} ``")
        else:
            code_spans.append(f"`{inner_unescaped}`")
        return f"@@CODE_SPAN_{idx}@@"

    s = re.sub(r"<code\b[^>]*>(.*?)</code>", _replace_code_span, s, flags=re.I | re.S)

    def _clean_inline(fragment: str) -> str:
        f = fragment
        f = re.sub(r"<br\s*/?>", " ", f, flags=re.I)
        f = re.sub(r"<(b|strong)\b[^>]*>(.*?)</\1>", r"**\2**", f, flags=re.I | re.S)
        f = re.sub(r"<(i|em)\b[^>]*>(.*?)</\1>", r"*\2*", f, flags=re.I | re.S)
        f = re.sub(r"<(strike|s)\b[^>]*>(.*?)</\1>", r"~~\2~~", f, flags=re.I | re.S)

        def _link_sub(m: re.Match[str]) -> str:
            attrs, inner = m.group(1), m.group(2)
            hm = re.search(r"href=[\"\x27]([^\"\x27]+)[\"\x27]", attrs, re.I)
            inner_clean = re.sub(r"<[^>]+>", "", inner).strip()
            if hm:
                href = hm.group(1)
                if inner_clean == href:
                    return href
                return f"[{inner_clean}]({href})"
            return inner_clean

        f = re.sub(r"<a\b([^>]*)>(.*?)</a>", _link_sub, f, flags=re.I | re.S)
        f = re.sub(r"<[^>]+>", "", f)
        f = html_mod.unescape(f).replace("\xa0", " ").strip()
        return f

    def _replace_table(m: re.Match[str]) -> str:
        tbody = m.group(0)
        rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", tbody, flags=re.I | re.S)
        if not rows:
            return ""
        md_rows: list[str] = []
        for r_idx, r_html in enumerate(rows):
            cells = re.findall(r"<(th|td)\b[^>]*>(.*?)</\1>", r_html, flags=re.I | re.S)
            if not cells:
                continue
            cleaned_cells = [_clean_inline(c[1]).replace("|", "\\|") for c in cells]
            md_rows.append("| " + " | ".join(cleaned_cells) + " |")
            if r_idx == 0:
                md_rows.append("| " + " | ".join(["---"] * len(cleaned_cells)) + " |")
        return "\n\n" + "\n".join(md_rows) + "\n\n"

    s = re.sub(r"<table\b[^>]*>.*?</table>", _replace_table, s, flags=re.I | re.S)

    for level in range(1, 7):
        hashes = "#" * level
        s = re.sub(
            rf"<h{level}\b[^>]*>(.*?)</h{level}>",
            lambda m, h=hashes: f"\n\n{h} {_clean_inline(m.group(1))}\n\n",
            s,
            flags=re.I | re.S,
        )

    s = re.sub(r"<hr\s*/?>", "\n\n---\n\n", s, flags=re.I)

    def _replace_blockquote(m: re.Match[str]) -> str:
        inner = m.group(1)
        inner = re.sub(r"</?(div|p)\b[^>]*>", "\n", inner, flags=re.I)
        inner = re.sub(r"<br\s*/?>", "\n", inner, flags=re.I)
        lines = [f"> {_clean_inline(ln)}" for ln in inner.splitlines() if _clean_inline(ln)]
        return "\n\n" + "\n".join(lines) + "\n\n"

    s = re.sub(r"<blockquote\b[^>]*>(.*?)</blockquote>", _replace_blockquote, s, flags=re.I | re.S)

    def _replace_ul(m: re.Match[str]) -> str:
        items = re.findall(r"<li(\s[^>]*)?>(.*?)</li>", m.group(1), flags=re.I | re.S)
        out: list[str] = []
        for attrs, item_html in items:
            checked_m = re.search(r"data-checked=[\"\x27](true|false)[\"\x27]", attrs or "", re.I)
            prefix = "- "
            if checked_m:
                prefix = "- [x] " if checked_m.group(1).lower() == "true" else "- [ ] "
            out.append(f"{prefix}{_clean_inline(item_html)}")
        return "\n\n" + "\n".join(out) + "\n\n"

    def _replace_ol(m: re.Match[str]) -> str:
        items = re.findall(r"<li\b[^>]*>(.*?)</li>", m.group(1), flags=re.I | re.S)
        out = [f"{i + 1}. {_clean_inline(item_html)}" for i, item_html in enumerate(items)]
        return "\n\n" + "\n".join(out) + "\n\n"

    for _ in range(2):
        s = re.sub(r"<ul\b[^>]*>(.*?)</ul>", _replace_ul, s, flags=re.I | re.S)
        s = re.sub(r"<ol\b[^>]*>(.*?)</ol>", _replace_ol, s, flags=re.I | re.S)

    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(div|p)>", "\n\n", s, flags=re.I)
    s = re.sub(r"<(div|p)\b[^>]*>", "", s, flags=re.I)

    s = re.sub(r"<(b|strong)\b[^>]*>(.*?)</\1>", r"**\2**", f"{s}", flags=re.I | re.S)
    s = re.sub(r"<(i|em)\b[^>]*>(.*?)</\1>", r"*\2*", s, flags=re.I | re.S)
    s = re.sub(r"<(strike|s)\b[^>]*>(.*?)</\1>", r"~~\2~~", s, flags=re.I | re.S)

    def _link_sub2(m: re.Match[str]) -> str:
        attrs, inner = m.group(1), m.group(2)
        hm = re.search(r"href=[\"\x27]([^\"\x27]+)[\"\x27]", attrs, re.I)
        inner_clean = re.sub(r"<[^>]+>", "", inner).strip()
        if hm:
            href = hm.group(1)
            if inner_clean == href:
                return href
            return f"[{inner_clean}]({href})"
        return inner_clean

    s = re.sub(r"<a\b([^>]*)>(.*?)</a>", _link_sub2, s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_mod.unescape(s).replace("\xa0", " ")

    for idx, span in enumerate(code_spans):
        s = s.replace(f"@@CODE_SPAN_{idx}@@", span)
    for idx, block in enumerate(pre_blocks):
        s = s.replace(f"@@PRE_BLOCK_{idx}@@", block)

    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return strip_duplicate_leading_title(s, title)


def _normalize_title(title: str | None) -> str:
    """Normalize a note title for equality checks (HTML entities, NBSP, whitespace runs)."""
    t = html_mod.unescape(title or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()


def match_created_note(
    rows: list[sqlite3.Row] | list[dict[str, Any]],
    before_ids: set[str],
    title: str,
    sqlite_space: str | None,
) -> str | None:
    """
    Return the id of a row that is new (not in `before_ids`) AND matches `title` and
    `sqlite_space`. Never falls back to an arbitrary new row: a concurrently cloud-synced
    note from another device must not be mistaken for the note we just created, because
    `update_note` trashes the original once a replacement is confirmed.
    """
    want_title = _normalize_title(title)
    want_space = sqlite_space or ""
    for r in rows:
        if r["id"] in before_ids:
            continue
        if _normalize_title(r["title"]) != want_title:
            continue
        if (r["space"] or "") != want_space:
            continue
        return str(r["id"])
    return None


def create_note_via_upnote(req: NoteCreateRequest) -> NoteOut:
    """
    Create a note via UpNote's `upnote://x-callback-url/note/new` handler and wait
    for it to be persisted in `upnote.sqlite3`.
    """
    with _DEEPLINK_LOCK:
        with get_db_connection(readonly=True) as conn:
            deeplink_space_id, sqlite_space_val, _ = resolve_space(conn, req.space)
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

        clean_title = req.title.replace("\xa0", " ").strip()
        body_text = strip_duplicate_leading_title(req.text or "", clean_title)
        if extra_tags and req.markdown:
            anchors = " ".join(build_hashtag_anchor_html(t) for t in extra_tags)
            body_text = f"{body_text}\n\n{anchors}" if body_text else anchors

        query_params: dict[str, str] = {
            "spaceId": deeplink_space_id,
            "title": clean_title,
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
        for _ in range(100):
            time.sleep(0.05)
            with get_db_connection(readonly=True) as conn:
                rows = conn.execute(
                    "SELECT id, title, space FROM notes ORDER BY createdAt DESC LIMIT 25"
                ).fetchall()
                created_id = match_created_note(rows, before_ids, clean_title, sqlite_space_val)
                if created_id:
                    break

        if not created_id:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=(
                    "Dispatched note/new to UpNote, but no new note with a matching title and "
                    "space appeared in SQLite within 5.0s. No existing notes were modified."
                ),
            )

        time.sleep(0.05)
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

_UPDATE_THREAD_STARTED = False


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _UPDATE_THREAD_STARTED
    if not _UPDATE_THREAD_STARTED:
        _UPDATE_THREAD_STARTED = True
        threading.Thread(
            target=_background_update_check_loop,
            name="upnote-api-update-checker",
            daemon=True,
        ).start()
    yield


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
    lifespan=_lifespan,
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
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_AUTH_EXEMPT_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json"})


def _request_hostname(host_header: str) -> str:
    """Extract the lowercase hostname from a Host header (strips port, handles [IPv6])."""
    h = host_header.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end != -1 else h
    if h.count(":") == 1:
        return h.split(":", 1)[0]
    return h


def check_request_security(
    method: str,
    host_header: str,
    origin: str | None,
    sec_fetch_site: str | None,
    path: str,
    auth_header: str,
    api_key_header: str,
) -> tuple[int, str] | None:
    """
    Return `(status_code, detail)` if the request must be rejected, else `None`.

    1. Host allowlist  -> blocks DNS-rebinding attacks (a hostile domain re-pointed at 127.0.0.1).
    2. Origin / Sec-Fetch-Site on unsafe methods -> blocks cross-site "simple" requests
       (e.g. `fetch(url, {method: "DELETE", mode: "no-cors"})`) that CORS alone never prevents.
       Browsers always attach `Origin` to non-GET/HEAD requests; non-browser clients
       (curl, upnote-cli, upnote-mcp) normally omit it and are unaffected.
    3. Optional bearer / X-API-Key token when `UPNOTE_API_TOKEN` is set.
    """
    if _request_hostname(host_header) not in ALLOWED_HOSTS:
        return (status.HTTP_400_BAD_REQUEST, "Invalid Host header.")

    if method.upper() not in _SAFE_METHODS:
        if sec_fetch_site and sec_fetch_site.lower() == "cross-site":
            return (status.HTTP_403_FORBIDDEN, "Cross-site requests are not allowed.")
        if origin and origin.rstrip("/") not in CORS_ORIGINS:
            return (status.HTTP_403_FORBIDDEN, "Request origin is not allowed.")

    if API_TOKEN and path not in _AUTH_EXEMPT_PATHS:
        candidate = ""
        if auth_header.lower().startswith("bearer "):
            candidate = auth_header[7:].strip()
        else:
            candidate = api_key_header.strip()
        if not candidate or not secrets.compare_digest(candidate, API_TOKEN):
            return (status.HTTP_401_UNAUTHORIZED, "Invalid or missing API token.")
    return None


@app.middleware("http")
async def _security_middleware(request: Request, call_next: Any) -> Any:
    rejection = check_request_security(
        method=request.method,
        host_header=request.headers.get("host", ""),
        origin=request.headers.get("origin"),
        sec_fetch_site=request.headers.get("sec-fetch-site"),
        path=request.url.path,
        auth_header=request.headers.get("authorization", ""),
        api_key_header=request.headers.get("x-api-key", ""),
    )
    if rejection is not None:
        code, detail = rejection
        return JSONResponse(status_code=code, content={"detail": detail})
    return await call_next(request)


@app.get("/health", tags=["System"], summary="Minimal unauthenticated liveness check")
def health() -> dict[str, Any]:
    """Return a minimal liveness payload (no paths, PIDs, or store counts)."""
    return {"ok": True, "version": APP_VERSION}


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

    upd = get_cached_update_info()
    return StatusOut(
        ok=True,
        version=APP_VERSION,
        latest_version=upd.latest_version,
        update_available=upd.update_available,
        release_url=upd.release_url,
        db_path=str(DB_PATH),
        db_exists=DB_PATH.exists(),
        upnote_running=pid is not None,
        upnote_pid=pid,
        upnote_binary=binary,
        counts=counts,
    )


@app.get(
    "/api/v1/update",
    response_model=UpdateCheckOut,
    tags=["System"],
    summary="Check GitHub Releases for a newer UpNote REST API version",
)
def get_update_status(
    refresh: bool = Query(
        default=True,
        description="Query GitHub Releases API immediately (if false, returns cached check)",
    ),
) -> UpdateCheckOut:
    """Check whether a newer version of UpNote REST API is available on GitHub."""
    if not refresh and get_cached_update_info().checked_at is not None:
        return get_cached_update_info()
    try:
        return check_for_updates(timeout=10.0)
    except Exception as exc:
        logger.exception("GitHub update check failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub update check failed (see upnote-api.service logs).",
        ) from exc


@app.post(
    "/api/v1/update/install",
    response_model=UpdateInstallOut,
    tags=["System"],
    summary="Download and install the latest UpNote REST API release from GitHub",
)
def install_update() -> UpdateInstallOut:
    """Download the latest GitHub release archive, update local files, and restart upnote-api.service."""
    try:
        return install_update_from_github(restart_service=True)
    except Exception as exc:
        logger.exception("Self-update installation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Self-update installation failed (see upnote-api.service logs).",
        ) from exc


@app.post("/api/v1/app/launch", response_model=StatusOut, tags=["System"], summary="Ensure UpNote desktop app is running")
def launch_upnote_app() -> StatusOut:
    """Ensure the UpNote desktop Electron process is running in the background."""
    ensure_upnote_running()
    return get_status()


@app.post("/api/v1/app/open", tags=["System"], summary="Open a note, notebook, tag, or deep link in UpNote UI")
def open_in_upnote_ui(req: DeepLinkOpenRequest) -> dict[str, Any]:
    """Open a note, notebook, tag, or custom x-callback-url in the UpNote UI."""
    if req.url:
        url = sanitize_deeplink_url(req.url)
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
    dispatch_deeplink(url, background=False)
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
        notebooks_by_id, note_to_notebooks = load_notebooks_index(conn)
        nb = resolve_notebook(
            conn, notebook, space_id=target_space_id, notebooks_by_id=notebooks_by_id
        )
        if not nb:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Notebook '{notebook}' not found.",
            )
        if not nb.note_ids:
            return []
        spaces_map = load_spaces_map(conn)
        placeholders = ",".join("?" for _ in nb.note_ids)
        rows = conn.execute(
            f"SELECT * FROM notes WHERE id IN ({placeholders}) AND deleted = 0 AND trashed = 0",
            nb.note_ids,
        ).fetchall()
        rows_by_id = {r["id"]: r for r in rows}
        return [
            row_to_note_out(
                rows_by_id[nid],
                spaces_map,
                note_to_notebooks,
                include_content=include_content,
            )
            for nid in nb.note_ids
            if nid in rows_by_id
        ]


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
            deeplink_space_id, sqlite_space_val, _ = resolve_space(conn, req.space)
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


# NOTE: `list_notes` and `get_note` are also called directly from Python (by
# `list_tag_notes`, `get_note`, `update_note`, `delete_note`, ...). Their parameters use
# `Annotated[..., Query(...)] = <plain default>` so that direct calls receive real
# defaults. With the older `param = Query(default=...)` style, omitted arguments are
# FastAPI `Query` objects, which are truthy and non-None, and silently broke those calls
# (e.g. `/api/v1/tags/{tag}/notes` and title lookups returned HTTP 500).
@app.get("/api/v1/notes", response_model=list[NoteOut], tags=["Notes"], summary="Search and filter notes")
def list_notes(
    space: Annotated[
        str | None, Query(description="Filter by space name ('General', 'CHEP') or ID")
    ] = None,
    notebook: Annotated[
        str | None, Query(description="Filter by notebook title (e.g. 'AI'), path, or UUID")
    ] = None,
    tag: Annotated[
        str | None, Query(description="Filter by tag name (with or without '#', e.g. 'plex')")
    ] = None,
    q: Annotated[
        str | None, Query(description="Full-text search query across note title and text")
    ] = None,
    trashed: Annotated[bool, Query(description="Whether to return trashed notes")] = False,
    pinned: Annotated[bool | None, Query(description="Filter by pinned status")] = None,
    bookmarked: Annotated[bool | None, Query(description="Filter by bookmarked status")] = None,
    has_todo: Annotated[bool | None, Query(description="Filter notes containing todos")] = None,
    is_template: Annotated[bool | None, Query(description="Filter template notes")] = None,
    unsynced: Annotated[bool | None, Query(description="Filter unsynced notes")] = None,
    include_content: Annotated[
        bool, Query(description="Include full 'text' and 'html' body in each returned note")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=500, description="Maximum notes to return")] = 50,
    offset: Annotated[int, Query(ge=0, description="Pagination offset")] = 0,
) -> list[NoteOut]:
    """Search and filter notes across spaces, notebooks, and tags."""
    with get_db_connection(readonly=True) as conn:
        spaces_map = load_spaces_map(conn)
        notebooks_by_id, note_to_notebooks = load_notebooks_index(conn)

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
            nb = resolve_notebook(
                conn, notebook, space_id=target_space_id, notebooks_by_id=notebooks_by_id
            )
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
            clauses.append("(title LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\')")
            like_q = f"%{_escape_like(q)}%"
            params.extend([like_q, like_q])

        sql = f"SELECT * FROM notes WHERE {' AND '.join(clauses)} ORDER BY pinned DESC, updatedAt DESC"
        rows = conn.execute(sql, params).fetchall()

        norm_tag = normalize_tag_name(tag).lower() if tag else None
        if norm_tag:
            filtered_rows: list[sqlite3.Row] = []
            for r in rows:
                try:
                    raw_tags = json.loads(r["tagLinks"] or "[]")
                    tags_lower = [str(t).lower() for t in raw_tags] if isinstance(raw_tags, list) else []
                except json.JSONDecodeError:
                    tags_lower = []
                if norm_tag in tags_lower:
                    filtered_rows.append(r)
            rows = filtered_rows

        sliced_rows = rows[offset : offset + limit]
        return [
            row_to_note_out(
                r, spaces_map, note_to_notebooks, include_content=include_content
            )
            for r in sliced_rows
        ]


@app.get("/api/v1/notes/{note_id_or_title}", response_model=NoteOut, tags=["Notes"], summary="Get a single note by UUID or exact title")
def get_note(
    note_id_or_title: str,
    space: Annotated[
        str | None, Query(description="Optional space filter when looking up by title")
    ] = None,
    notebook: Annotated[
        str | None, Query(description="Optional notebook filter when looking up by title")
    ] = None,
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
            redirected_from: str | None = None
            if row["trashed"] == 1 and row["title"]:
                active_replacement = conn.execute(
                    "SELECT * FROM notes WHERE title = ? AND COALESCE(space, '') = COALESCE(?, '') "
                    "AND trashed = 0 AND deleted = 0 ORDER BY updatedAt DESC LIMIT 1",
                    (row["title"], row["space"]),
                ).fetchone()
                if active_replacement:
                    redirected_from = row["id"]
                    row = active_replacement
            out = row_to_note_out(row, spaces_map, note_to_notebooks, include_content=True)
            out.redirected_from = redirected_from
            return out

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
        _require_upnote_stopped(req.force, "strategy='sqlite_direct'")
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
        target_text = strip_duplicate_leading_title(req.text, target_title)
    else:
        raw_text = existing.markdown or html_to_markdown(existing.html, existing.title) or existing.text or ""
        target_text = strip_duplicate_leading_title(raw_text, target_title)

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

    # Hold the (re-entrant) deep-link lock across create -> trash so no concurrent
    # request can interleave between confirming the replacement and trashing the original.
    with _DEEPLINK_LOCK:
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

        # By default only the note being updated is trashed. Other notes that merely share
        # its title are left alone unless the caller explicitly opts in with `dedupe=true`,
        # which is further limited to the same space AND the same notebook.
        ids_to_trash: list[str] = [existing.id]
        if req.dedupe:
            with get_db_connection(readonly=True) as conn:
                _, target_sqlite_space, _ = resolve_space(conn, target_space)
                _, note_to_notebooks = load_notebooks_index(conn)
                new_nb_ids = {nb.id for nb in note_to_notebooks.get(new_note.id, [])}
                dup_rows = conn.execute(
                    "SELECT id FROM notes WHERE title = ? AND COALESCE(space, '') = COALESCE(?, '') "
                    "AND trashed = 0 AND deleted = 0 AND id != ?",
                    (target_title, target_sqlite_space, new_note.id),
                ).fetchall()
                for dr in dup_rows:
                    dup_nb_ids = {nb.id for nb in note_to_notebooks.get(dr["id"], [])}
                    if dup_nb_ids == new_nb_ids and dr["id"] not in ids_to_trash:
                        ids_to_trash.append(dr["id"])

        for tid in ids_to_trash:
            if tid != new_note.id:
                dispatch_deeplink(
                    f"upnote://x-callback-url/note/moveToTrash?noteId={urllib.parse.quote(tid)}"
                )
                time.sleep(0.05)
    return new_note


def _require_upnote_stopped(force: bool, operation: str) -> None:
    """
    Direct SQLite writes bypass UpNote's in-memory store and Cloud sync queue; while the
    Electron app is running they can be silently overwritten or never synced. Refuse
    unless UpNote is stopped or the caller passes `force=true`.
    """
    if force:
        return
    pid, _, _ = find_upnote_process()
    if pid is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{operation} writes directly to upnote.sqlite3, which is unsafe while UpNote "
                f"(pid {pid}) is running: changes may be overwritten or never reach UpNote Cloud. "
                "Quit UpNote first, or pass force=true to proceed anyway."
            ),
        )


@app.delete("/api/v1/notes/{note_id}", tags=["Notes"], summary="Move a note to Trash or permanently delete")
def delete_note(
    note_id: str,
    permanent: bool = Query(
        default=False,
        description=(
            "If false (default), moves the note to UpNote Trash via x-callback-url; if true, "
            "marks deleted=1 directly in SQLite (offline-only; requires UpNote to be stopped or force=true)"
        ),
    ),
    force: bool = Query(
        default=False,
        description="Allow permanent=true while UpNote is running (unsafe: may be overwritten / not synced)",
    ),
) -> dict[str, Any]:
    """Move a note to Trash (synced to UpNote Cloud) or permanently mark it deleted."""
    if permanent:
        _require_upnote_stopped(force, "permanent=true")
    with get_db_connection(readonly=True) as conn:
        spaces_map = load_spaces_map(conn)
        _, note_to_notebooks = load_notebooks_index(conn)
        row = conn.execute(
            "SELECT * FROM notes WHERE id = ? AND deleted = 0",
            (note_id,),
        ).fetchone()
        if row:
            existing = row_to_note_out(row, spaces_map, note_to_notebooks, include_content=False)
        else:
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
                "SELECT * FROM files WHERE deleted = 0 AND name LIKE ? ESCAPE '\\' ORDER BY updatedAt DESC LIMIT ?",
                (f"%{_escape_like(q)}%", limit),
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
    parser = argparse.ArgumentParser(
        prog="upnote-api",
        description="UpNote Local Store & Deep-Link REST API Service",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"upnote-api v{APP_VERSION}",
    )
    parser.add_argument(
        "-u",
        "--check-update",
        action="store_true",
        help="Check GitHub Releases for a newer UpNote REST API version and exit",
    )
    parser.add_argument(
        "--self-update",
        action="store_true",
        help="Download and install the latest UpNote REST API release from GitHub and exit",
    )
    args = parser.parse_args()

    if args.check_update:
        try:
            info = check_for_updates(timeout=15.0)
            if info.update_available:
                print(
                    f"Update available: v{info.current_version} -> v{info.latest_version}\n"
                    f"Release URL: {info.release_url}"
                )
            else:
                print(f"UpNote Local REST API v{info.current_version} is up to date.")
            return
        except Exception as exc:
            print(f"Update check failed: {exc}", file=sys.stderr)
            sys.exit(1)

    if args.self_update:
        try:
            res = install_update_from_github(restart_service=True)
            print(res.message)
            return
        except Exception as exc:
            print(f"Self-update failed: {exc}", file=sys.stderr)
            sys.exit(1)

    uvicorn.run(
        app,
        host=API_HOST,
        port=API_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
