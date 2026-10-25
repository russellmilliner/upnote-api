#!/usr/bin/env python3
"""
CLI client for the local UpNote REST API service (http://127.0.0.1:8765).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

APP_VERSION = "0.2.0"
BASE_URL = os.environ.get("UPNOTE_API_URL", "http://127.0.0.1:8765")
GITHUB_REPO = "russellmilliner/upnote-api"
GITHUB_LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases"


def _parse_semver(version: str) -> tuple[int, int, int]:
    cleaned = version.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = [int(p) if p.isdigit() else 0 for p in cleaned.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def is_newer_version(current: str, candidate: str) -> bool:
    return _parse_semver(candidate) > _parse_semver(current)


def check_github_release(timeout: float = 15.0) -> dict[str, Any]:
    req = urllib.request.Request(
        GITHUB_LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"upnote-cli/{APP_VERSION}",
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
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        burl = str(asset.get("browser_download_url") or "")
        if name.endswith(".tar.gz") and burl:
            download_url = burl
            break

    return {
        "current_version": APP_VERSION,
        "latest_version": latest_version,
        "update_available": is_newer_version(APP_VERSION, latest_version),
        "release_url": html_url,
        "download_url": download_url,
        "release_notes": body,
    }


def self_update_from_github() -> dict[str, Any]:
    info = check_github_release(timeout=15.0)
    if not info["update_available"]:
        return {
            "ok": True,
            "previous_version": APP_VERSION,
            "installed_version": APP_VERSION,
            "release_url": info["release_url"],
            "message": f"Already up to date (v{APP_VERSION}).",
        }
    download_url = info.get("download_url")
    if not download_url:
        raise RuntimeError(f"No .tar.gz release asset found at {info['release_url']}")

    with tempfile.TemporaryDirectory(prefix="upnote-cli-update-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        archive_path = tmp_path / "upnote-api-release.tar.gz"
        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": f"upnote-cli/{APP_VERSION}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=60.0) as resp:
            archive_path.write_bytes(resp.read())

        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=tmp_path)

        install_dir = Path.home() / ".local" / "share" / "upnote-api"
        bin_dir = Path.home() / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)

        extracted_api = list(tmp_path.glob("**/src/upnote_api.py"))
        extracted_cli = list(tmp_path.glob("**/src/upnote_cli.py"))
        if not extracted_api or not extracted_cli:
            raise RuntimeError("Release archive is missing src/upnote_api.py or src/upnote_cli.py")

        target_api = install_dir / "upnote_api.py"
        target_cli = bin_dir / "upnote-cli"
        shutil.copy2(extracted_api[0], target_api)
        target_api.chmod(0o755)
        shutil.copy2(extracted_cli[0], target_cli)
        target_cli.chmod(0o755)

        subprocess.run(
            ["systemctl", "--user", "restart", "upnote-api.service"],
            check=False,
        )

    return {
        "ok": True,
        "previous_version": APP_VERSION,
        "installed_version": info["latest_version"],
        "release_url": info["release_url"],
        "message": f"Installed UpNote REST API & CLI v{info['latest_version']} and restarted upnote-api.service.",
    }


def api_request(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    url = f"{BASE_URL}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean, doseq=True)

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Connection error to {BASE_URL}: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="upnote-cli",
        description="UpNote Local Store & Deep-Link REST API CLI",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"upnote-cli v{APP_VERSION}",
    )
    parser.add_argument(
        "-u",
        "--check-update",
        action="store_true",
        help="Check GitHub Releases for a newer UpNote REST API version",
    )
    parser.add_argument(
        "--self-update",
        action="store_true",
        help="Download and install the latest UpNote REST API release from GitHub",
    )
    sub = parser.add_subparsers(dest="cmd", required=False)

    sub.add_parser("status", help="Show UpNote service and database status")
    sub.add_parser("check-update", help="Check GitHub Releases for a newer version")
    sub.add_parser("self-update", help="Download and install the latest release from GitHub")
    sub.add_parser("spaces", help="List all spaces/workspaces")

    nb_list = sub.add_parser("notebooks", help="List notebooks")
    nb_list.add_argument("--space", help="Filter by space name or ID")
    nb_list.add_argument("--q", help="Search notebook title/path")

    nb_new = sub.add_parser("create-notebook", help="Create a notebook")
    nb_new.add_argument("title", help="Notebook title")
    nb_new.add_argument("--space", default="General", help="Space name or ID")

    tag_list = sub.add_parser("tags", help="List tags")
    tag_list.add_argument("--space", help="Filter by space name or ID")
    tag_list.add_argument("--q", help="Search tag name")

    notes_p = sub.add_parser("notes", help="List/search notes")
    notes_p.add_argument("--space", help="Space name ('General', 'CHEP') or ID")
    notes_p.add_argument("--notebook", help="Notebook title, path, or UUID")
    notes_p.add_argument("--tag", help="Tag name (e.g. 'plex')")
    notes_p.add_argument("--q", help="Full-text search query")
    notes_p.add_argument("--trashed", action="store_true", help="List trashed notes")
    notes_p.add_argument("--content", action="store_true", help="Include full text/html")
    notes_p.add_argument("--limit", type=int, default=25, help="Max results")

    get_p = sub.add_parser("get", help="Get a single note by ID or exact title")
    get_p.add_argument("note", help="Note UUID or exact title")
    get_p.add_argument("--space", help="Optional space filter")
    get_p.add_argument("--notebook", help="Optional notebook filter")

    create_p = sub.add_parser("create", help="Create a new note in UpNote")
    create_p.add_argument("--title", required=True, help="Note title")
    create_p.add_argument("--space", default="General", help="Space name or ID (default: General)")
    create_p.add_argument("--notebook", help="Notebook title, path, or UUID")
    create_p.add_argument("--tag", action="append", dest="tags", help="Tag to attach (repeatable)")
    create_p.add_argument("--text", help="Note Markdown/text body")
    create_p.add_argument("--file", help="Read note Markdown/text body from file ('-' for stdin)")
    create_p.add_argument("--plain", action="store_true", help="Treat text as plain text instead of Markdown")

    del_p = sub.add_parser("trash", help="Move a note to UpNote Trash")
    del_p.add_argument("note_id", help="Note UUID or title")

    rest_p = sub.add_parser("restore", help="Restore a note from UpNote Trash")
    rest_p.add_argument("note_id", help="Note UUID")

    args = parser.parse_args()

    if args.check_update or args.cmd == "check-update":
        try:
            res = check_github_release(timeout=15.0)
        except Exception as exc:
            print(f"Update check failed: {exc}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return

    if args.self_update or args.cmd == "self-update":
        try:
            res = self_update_from_github()
        except Exception as exc:
            print(f"Self-update failed: {exc}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return

    if args.cmd == "status":
        res = api_request("GET", "/api/v1/status")
    elif args.cmd == "spaces":
        res = api_request("GET", "/api/v1/spaces")
    elif args.cmd == "notebooks":
        res = api_request("GET", "/api/v1/notebooks", {"space": args.space, "q": args.q})
    elif args.cmd == "create-notebook":
        res = api_request("POST", "/api/v1/notebooks", payload={"title": args.title, "space": args.space})
    elif args.cmd == "tags":
        res = api_request("GET", "/api/v1/tags", {"space": args.space, "q": args.q})
    elif args.cmd == "notes":
        res = api_request(
            "GET",
            "/api/v1/notes",
            {
                "space": args.space,
                "notebook": args.notebook,
                "tag": args.tag,
                "q": args.q,
                "trashed": "true" if args.trashed else "false",
                "include_content": "true" if args.content else "false",
                "limit": args.limit,
            },
        )
    elif args.cmd == "get":
        encoded = urllib.parse.quote(args.note, safe="")
        res = api_request("GET", f"/api/v1/notes/{encoded}", {"space": args.space, "notebook": args.notebook})
    elif args.cmd == "create":
        body = args.text or ""
        if args.file:
            body = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        res = api_request(
            "POST",
            "/api/v1/notes",
            payload={
                "title": args.title,
                "space": args.space,
                "notebook": args.notebook,
                "tags": args.tags or [],
                "text": body,
                "markdown": not args.plain,
            },
        )
    elif args.cmd == "trash":
        encoded = urllib.parse.quote(args.note_id, safe="")
        res = api_request("DELETE", f"/api/v1/notes/{encoded}")
    elif args.cmd == "restore":
        encoded = urllib.parse.quote(args.note_id, safe="")
        res = api_request("POST", f"/api/v1/notes/{encoded}/restore")
    else:
        parser.print_help()
        return

    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
