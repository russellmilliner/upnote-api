# Changelog — upnote-api

All notable changes and bumped dependencies for `russellmilliner/upnote-api` are documented in this file.

## [v0.2.5-deps](https://github.com/russellmilliner/upnote-api/releases/tag/v0.2.5-deps) — 2026-10-31

### 📦 Bumped Dependencies (1 package)

| Package | Previous | Updated | Scope |
| :--- | :--- | :--- | :--- |
| `pydantic` | `2.14.1` | **`2.14.2`** | Direct (`pyproject.toml`) |

## [v0.2.5](https://github.com/russellmilliner/upnote-api/releases/tag/v0.2.5) — 2026-10-28

### Highlights
- html_to_markdown: linear time on all inputs; fixes silent loss of text after escaped '<' (e.g. table rows with '(<61)', '<X.Y.Z>', 'Vec<T>') that updates were baking into notes; correct nested lists; no double-decoding of entities. Releases are now signed (OpenSSH Ed25519, namespace upnote-api-release) and self-update requires a valid signature.

### Commits
- fix(converter,security): linear-time html_to_markdown, stop destroying text after escaped '<', correct nested lists, sign releases (OpenSSH Ed25519) and require signatures on self-update (8d16ea1)

## [v0.2.4](https://github.com/russellmilliner/upnote-api/releases/tag/v0.2.4) — 2026-10-28

### Highlights
- Block cross-site writes (Origin/Sec-Fetch-Site) and DNS rebinding (Host allowlist), require release checksums, remove /tmp glob fallback, strict new-note matching (no wrong-note trashing), cloud_replace trashes only the updated note (opt-in dedupe), 409 guard on offline SQLite writes, fix HTTP 500 on tag route and title lookups, minimal /health, 19 tests

### Commits
- fix(security,integrity): block cross-site writes and DNS rebinding, require update checksums, drop /tmp glob, strict new-note matching, opt-in dedupe, guard offline SQLite writes, fix Query-default 500s, expand tests to 19 (97e63b7)

## [v0.2.3](https://github.com/russellmilliner/upnote-api/releases/tag/v0.2.3) — 2026-10-28

## [v0.2.2](https://github.com/russellmilliner/upnote-api/releases/tag/v0.2.2) — 2026-10-28

### Highlights
- Prevent UpNote Electron window from popping up or stealing desktop focus during background API/MCP write operations (_bg_new_window=true and --auto-start-in-background)

### Commits
- fix(deeplink): prevent UpNote from stealing desktop focus during background API writes (fcb2118)

## [v0.2.1](https://github.com/russellmilliner/upnote-api/releases/tag/v0.2.1) — 2026-10-25

## [v0.2.0](https://github.com/russellmilliner/upnote-api/releases/tag/v0.2.0) — 2026-10-25

### Changes
- feat(mcp): add native Model Context Protocol server (upnote-mcp) and bump to v0.2.0 (dd57dfb)
- chore(deps): update dependencies (fastapi 0.162.0) (a8d4bf1)

### 📦 Bumped Dependencies (1 package)

| Package | Previous | Updated | Scope |
| :--- | :--- | :--- | :--- |
| `fastapi` | `0.161.1` | **`0.162.0`** | Direct (`pyproject.toml`) |

## [v0.1.2-deps](https://github.com/russellmilliner/upnote-api/releases/tag/v0.1.2-deps) — 2026-10-24

### 📦 Bumped Dependencies (1 package)

| Package | Previous | Updated | Scope |
| :--- | :--- | :--- | :--- |
| `fastapi` | `0.161.1` | **`0.162.0`** | Direct (`pyproject.toml`) |

## [v0.1.2](https://github.com/russellmilliner/upnote-api/releases/tag/v0.1.2) — 2026-10-22

### Changes
- docs(changelog): add v0.1.1 dependency changelog (7d2d5ad)

## [v0.1.1](https://github.com/russellmilliner/upnote-api/releases/tag/v0.1.1) — 2026-10-22

### 📦 Bumped Dependencies (3 packages)

| Package | Previous | Updated | Scope |
| :--- | :--- | :--- | :--- |
| `fastapi` | `0.110.0` | **`0.161.1`** | Direct (`pyproject.toml`) |
| `pydantic` | `2.6.0` | **`2.14.1`** | Direct (`pyproject.toml`) |
| `uvicorn` | `0.27.0` | **`0.54.0`** | Direct (`pyproject.toml`) |
