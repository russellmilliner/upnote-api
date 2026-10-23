#!/usr/bin/env python3
"""
CLI client for the local UpNote REST API service (http://127.0.0.1:8765).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

APP_VERSION = "0.1.1"
BASE_URL = os.environ.get("UPNOTE_API_URL", "http://127.0.0.1:8765")


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
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show UpNote service and database status")
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
