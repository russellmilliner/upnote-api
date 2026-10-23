# UpNote Local Store & Deep-Link REST API — OpenAPI / Swagger Reference

- **Version:** `v0.1.1`
- **OpenAPI Version:** `3.1.0`
- **Interactive Swagger UI:** `http://127.0.0.1:8765/docs`
- **Interactive ReDoc UI:** `http://127.0.0.1:8765/redoc`
- **Raw OpenAPI Spec:** [`docs/openapi.json`](./openapi.json) (`http://127.0.0.1:8765/openapi.json`)

Full REST API for querying, searching, creating, updating, tagging, and managing notes, notebooks, tags, and workspaces in UpNote on Linux.

- **Read Operations** query UpNote's local SQLite store (`~/.config/UpNote/upnote.sqlite3`) in read-only mode.
- **Write Operations** dispatch `upnote://x-callback-url/...` commands to the running UpNote Electron instance so new notes, notebooks, tags, and trash/restore actions automatically synchronize with **UpNote Cloud**.

---

## Endpoints Summary

| Tag | Method | Path | Summary |
| :--- | :--- | :--- | :--- |
| **System** | `GET` | `/api/v1/status` | Get UpNote service and store status |
| **System** | `GET` | `/health` | Health check |
| **System** | `POST` | `/api/v1/app/launch` | Ensure UpNote desktop app is running |
| **System** | `POST` | `/api/v1/app/open` | Open a note, notebook, tag, or deep link in UpNote UI |
| **Spaces** | `GET` | `/api/v1/spaces` | List all workspaces/spaces |
| **Spaces** | `GET` | `/api/v1/spaces/{space}` | Get space details by name or ID |
| **Notebooks** | `GET` | `/api/v1/notebooks` | List or search notebooks |
| **Notebooks** | `POST` | `/api/v1/notebooks` | Create a new notebook |
| **Notebooks** | `GET` | `/api/v1/notebooks/{notebook}` | Get notebook by UUID, title, or path |
| **Notebooks** | `GET` | `/api/v1/notebooks/{notebook}/notes` | List all active notes inside a notebook |
| **Tags** | `GET` | `/api/v1/tags` | List all tags with note counts |
| **Tags** | `GET` | `/api/v1/tags/{tag}/notes` | List notes carrying a specific tag |
| **Notes** | `GET` | `/api/v1/notes` | Search and filter notes |
| **Notes** | `POST` | `/api/v1/notes` | Create a new note in UpNote |
| **Notes** | `GET` | `/api/v1/notes/{note_id_or_title}` | Get a single note by UUID or exact title |
| **Notes** | `PATCH` | `/api/v1/notes/{note_id}` | Partially update an existing note |
| **Notes** | `PUT` | `/api/v1/notes/{note_id}` | Replace or update an existing note |
| **Notes** | `DELETE` | `/api/v1/notes/{note_id}` | Move a note to Trash or permanently delete |
| **Notes** | `POST` | `/api/v1/notes/{note_id}/restore` | Restore a trashed note |
| **Filters & Files** | `GET` | `/api/v1/filters` | List saved UpNote filters |
| **Filters & Files** | `GET` | `/api/v1/files` | List file attachments |

---

## Detailed Endpoint Reference

### `GET /api/v1/status` — Get UpNote service and store status

- **Category:** `System`

Return service health, UpNote process status, and store item counts.

### `GET /health` — Health check

- **Category:** `System`

Return service health, UpNote process status, and store item counts.

### `POST /api/v1/app/launch` — Ensure UpNote desktop app is running

- **Category:** `System`

Ensure the UpNote desktop Electron process is running in the background.

### `POST /api/v1/app/open` — Open a note, notebook, tag, or deep link in UpNote UI

- **Category:** `System`

Open a note, notebook, tag, or custom x-callback-url in the UpNote UI.

- **Request Body (`application/json`):** [`DeepLinkOpenRequest`](#schema-deeplinkopenrequest)

### `GET /api/v1/spaces` — List all workspaces/spaces

- **Category:** `Spaces`

List all UpNote spaces (including the default 'General' space).

### `GET /api/v1/spaces/{space}` — Get space details by name or ID

- **Category:** `Spaces`

Get a single space by name ('General', 'CHEP') or ID ('default', UUID).

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `space` | `path` | `string` | Yes |  |

### `GET /api/v1/notebooks` — List or search notebooks

- **Category:** `Notebooks`

List notebooks with their nested paths and note counts.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `space` | `query` | `string / null` | No | Filter by space name ('General', 'CHEP') or ID ('default', UUID) |
| `parent_id` | `query` | `string / null` | No | Filter by parent notebook UUID |
| `q` | `query` | `string / null` | No | Case-insensitive search by notebook title/path |

### `POST /api/v1/notebooks` — Create a new notebook

- **Category:** `Notebooks`

Create a new notebook in the target space via UpNote x-callback-url.

- **Request Body (`application/json`):** [`NotebookCreateRequest`](#schema-notebookcreaterequest)

### `GET /api/v1/notebooks/{notebook}` — Get notebook by UUID, title, or path

- **Category:** `Notebooks`

Get a notebook by UUID, title (e.g. 'AI'), or slash-separated path.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `notebook` | `path` | `string` | Yes |  |
| `space` | `query` | `string / null` | No | Optional space name or ID |

### `GET /api/v1/notebooks/{notebook}/notes` — List all active notes inside a notebook

- **Category:** `Notebooks`

List all active notes inside a specific notebook.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `notebook` | `path` | `string` | Yes |  |
| `space` | `query` | `string / null` | No | Optional space name or ID |
| `include_content` | `query` | `boolean` | No | Include full 'text' and 'html' fields |

### `GET /api/v1/tags` — List all tags with note counts

- **Category:** `Tags`

List all tags along with active note counts.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `space` | `query` | `string / null` | No | Filter by space name or ID |
| `q` | `query` | `string / null` | No | Search tag name |
| `include_inactive` | `query` | `boolean` | No | Include inactive tags |

### `GET /api/v1/tags/{tag}/notes` — List notes carrying a specific tag

- **Category:** `Tags`

List all active notes tagged with the given tag name.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `tag` | `path` | `string` | Yes |  |
| `space` | `query` | `string / null` | No | Optional space filter |
| `include_content` | `query` | `boolean` | No | Include full 'text' and 'html' |

### `GET /api/v1/notes` — Search and filter notes

- **Category:** `Notes`

Search and filter notes across spaces, notebooks, and tags.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `space` | `query` | `string / null` | No | Filter by space name ('General', 'CHEP') or ID |
| `notebook` | `query` | `string / null` | No | Filter by notebook title (e.g. 'AI'), path, or UUID |
| `tag` | `query` | `string / null` | No | Filter by tag name (with or without '#', e.g. 'plex') |
| `q` | `query` | `string / null` | No | Full-text search query across note title and text |
| `trashed` | `query` | `boolean` | No | Whether to return trashed notes |
| `pinned` | `query` | `boolean / null` | No | Filter by pinned status |
| `bookmarked` | `query` | `boolean / null` | No | Filter by bookmarked status |
| `has_todo` | `query` | `boolean / null` | No | Filter notes containing todos |
| `is_template` | `query` | `boolean / null` | No | Filter template notes |
| `unsynced` | `query` | `boolean / null` | No | Filter unsynced notes |
| `include_content` | `query` | `boolean` | No | Include full 'text' and 'html' body in each returned note |
| `limit` | `query` | `integer` | No | Maximum notes to return |
| `offset` | `query` | `integer` | No | Pagination offset |

### `POST /api/v1/notes` — Create a new note in UpNote

- **Category:** `Notes`

Create a new note in UpNote (with space, notebook, tags, and Markdown formatting)
and automatically sync to UpNote Cloud.

- **Request Body (`application/json`):** [`NoteCreateRequest`](#schema-notecreaterequest)

### `GET /api/v1/notes/{note_id_or_title}` — Get a single note by UUID or exact title

- **Category:** `Notes`

Get a single note (including full text and HTML) by UUID or exact title.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `note_id_or_title` | `path` | `string` | Yes |  |
| `space` | `query` | `string / null` | No | Optional space filter when looking up by title |
| `notebook` | `query` | `string / null` | No | Optional notebook filter when looking up by title |

### `PATCH /api/v1/notes/{note_id}` — Partially update an existing note

- **Category:** `Notes`

Update an existing note.

- `strategy='cloud_replace'` (default): Creates the updated note via UpNote's
  `x-callback-url/note/new` in the target space/notebook/tags and moves the
  previous revision to Trash so UpNote Cloud syncs the change immediately.
- `strategy='sqlite_direct'`: Updates the SQLite row in place in `upnote.sqlite3`.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `note_id` | `path` | `string` | Yes |  |

- **Request Body (`application/json`):** [`NoteUpdateRequest`](#schema-noteupdaterequest)

### `PUT /api/v1/notes/{note_id}` — Replace or update an existing note

- **Category:** `Notes`

Update an existing note.

- `strategy='cloud_replace'` (default): Creates the updated note via UpNote's
  `x-callback-url/note/new` in the target space/notebook/tags and moves the
  previous revision to Trash so UpNote Cloud syncs the change immediately.
- `strategy='sqlite_direct'`: Updates the SQLite row in place in `upnote.sqlite3`.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `note_id` | `path` | `string` | Yes |  |

- **Request Body (`application/json`):** [`NoteUpdateRequest`](#schema-noteupdaterequest)

### `DELETE /api/v1/notes/{note_id}` — Move a note to Trash or permanently delete

- **Category:** `Notes`

Move a note to Trash (synced to UpNote Cloud) or permanently mark it deleted.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `note_id` | `path` | `string` | Yes |  |
| `permanent` | `query` | `boolean` | No | If false (default), moves the note to UpNote Trash via x-callback-url; if true, marks deleted=1 in SQLite |

### `POST /api/v1/notes/{note_id}/restore` — Restore a trashed note

- **Category:** `Notes`

Restore a trashed note via `upnote://x-callback-url/note/restore`.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `note_id` | `path` | `string` | Yes |  |

### `GET /api/v1/filters` — List saved UpNote filters

- **Category:** `Filters & Files`

List saved filters in the UpNote store.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `space` | `query` | `string / null` | No | Optional space filter |

### `GET /api/v1/files` — List file attachments

- **Category:** `Filters & Files`

List file attachments tracked in the UpNote store.

#### Parameters

| Name | In | Type | Required | Description |
| :--- | :--- | :--- | :---: | :--- |
| `q` | `query` | `string / null` | No | Search attachment filename |
| `limit` | `query` | `integer` | No |  |

---

## Data Schemas (Component Models)

### Schema: `DeepLinkOpenRequest`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `url` | `string / null` | No | — | Raw upnote://x-callback-url/... URL to dispatch |
| `note_id` | `string / null` | No | — | Note UUID to open in UpNote UI |
| `notebook_id` | `string / null` | No | — | Notebook UUID to open in UpNote UI |
| `tag` | `string / null` | No | — | Tag name to view in UpNote UI |

### Schema: `FileOut`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `id` | `string` | Yes | — |  |
| `name` | `string / null` | No | — |  |
| `version` | `integer / null` | No | — |  |
| `download_url` | `string / null` | No | — |  |
| `tag` | `string / null` | No | — |  |
| `synced` | `boolean` | No | `true` |  |
| `created_at` | `number / null` | No | — |  |
| `updated_at` | `number / null` | No | — |  |

### Schema: `FilterOut`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `id` | `string` | Yes | — |  |
| `filter_type` | `string / null` | No | — |  |
| `words` | `string / null` | No | — |  |
| `space_id` | `string` | Yes | — |  |
| `space_name` | `string` | Yes | — |  |
| `inactive` | `boolean` | No | `false` |  |
| `synced` | `boolean` | No | `true` |  |
| `updated_at` | `number / null` | No | — |  |

### Schema: `HTTPValidationError`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `detail` | `array` | No | — |  |

### Schema: `NoteCreateRequest`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `title` | `string` | Yes | — | Page/note title |
| `text` | `string` | No | `""` | Note body (Markdown or plain text) |
| `markdown` | `boolean` | No | `true` | Parse 'text' as Markdown (supports tables, headings, non-wrapping code blocks) |
| `space` | `string` | No | `"General"` | Target space name ('General', 'CHEP') or ID ('default' or UUID) |
| `notebook` | `string / null` | No | — | Target notebook title (e.g. 'AI'), nested path ('Parent/Child'), or notebook UUID |
| `tag` | `string / null` | No | — | Single tag to attach (with or without leading '#', e.g. 'plex') |
| `tags` | `array / null` | No | — | List of tags to attach (e.g. ['plex', 'proxmox']) |
| `new_window` | `boolean` | No | `false` | Whether to open the newly created note in a separate UpNote window |

### Schema: `NoteOut`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `id` | `string` | Yes | — |  |
| `title` | `string` | Yes | — |  |
| `summary` | `string / null` | No | — |  |
| `space_id` | `string` | Yes | — |  |
| `space_name` | `string` | Yes | — |  |
| `notebooks` | `array` | No | — |  |
| `tags` | `array` | No | — |  |
| `pinned` | `boolean` | No | `false` |  |
| `bookmarked` | `boolean` | No | `false` |  |
| `trashed` | `boolean` | No | `false` |  |
| `deleted` | `boolean` | No | `false` |  |
| `synced` | `boolean` | No | `false` |  |
| `has_todo` | `boolean` | No | `false` |  |
| `is_template` | `boolean` | No | `false` |  |
| `files_count` | `integer` | No | `0` |  |
| `created_at` | `number / null` | No | — |  |
| `updated_at` | `number / null` | No | — |  |
| `synced_at` | `number / null` | No | — |  |
| `text` | `string / null` | No | — |  |
| `html` | `string / null` | No | — |  |

### Schema: `NoteUpdateRequest`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `title` | `string / null` | No | — | Updated note title |
| `text` | `string / null` | No | — | Updated note body (Markdown or plain text) |
| `html` | `string / null` | No | — | Updated raw HTML (used only when strategy='sqlite_direct') |
| `markdown` | `boolean` | No | `true` | Interpret 'text' as Markdown |
| `space` | `string / null` | No | — | Target space name or ID |
| `notebook` | `string / null` | No | — | Target notebook title, path, or UUID |
| `tag` | `string / null` | No | — | Single tag to set |
| `tags` | `array / null` | No | — | List of tags to set |
| `pinned` | `boolean / null` | No | — | Pin or unpin note |
| `bookmarked` | `boolean / null` | No | — | Bookmark or unbookmark note |
| `strategy` | `string` | No | `"cloud_replace"` | 'cloud_replace' creates a synced replacement note via UpNote x-callback-url and moves the old revision to trash so UpNote Cloud syncs immediately; 'sqlite_direct' updates the row in-place in upnote.sqlite3. |

### Schema: `NotebookCreateRequest`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `title` | `string` | Yes | — | Notebook title |
| `space` | `string` | No | `"General"` | Space name ('General', 'CHEP') or space ID ('default' or UUID) |

### Schema: `NotebookOut`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `id` | `string` | Yes | — |  |
| `title` | `string` | Yes | — |  |
| `path` | `string` | Yes | — |  |
| `parent_id` | `string / null` | No | — |  |
| `space_id` | `string` | Yes | — |  |
| `space_name` | `string` | Yes | — |  |
| `locked` | `boolean` | No | `false` |  |
| `inactive` | `boolean` | No | `false` |  |
| `synced` | `boolean` | No | `true` |  |
| `notes_count` | `integer` | No | `0` |  |
| `note_ids` | `array` | No | — |  |
| `created_at` | `number / null` | No | — |  |
| `updated_at` | `number / null` | No | — |  |

### Schema: `NotebookRef`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `id` | `string` | Yes | — |  |
| `title` | `string` | Yes | — |  |
| `path` | `string` | Yes | — |  |

### Schema: `SpaceOut`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `id` | `string` | Yes | — | Space ID ('default' for General, or UUID) |
| `sqlite_space` | `string / null` | No | — | Value stored in SQLite 'space' column (NULL for General) |
| `name` | `string` | Yes | — | Human-readable space name (e.g. 'General', 'CHEP') |
| `locked` | `boolean` | No | `false` |  |
| `notebooks_count` | `integer` | No | `0` |  |
| `notes_count` | `integer` | No | `0` |  |
| `tags_count` | `integer` | No | `0` |  |

### Schema: `StatusOut`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `ok` | `boolean` | No | `true` |  |
| `version` | `string` | No | `"0.1.1"` |  |
| `db_path` | `string` | Yes | — |  |
| `db_exists` | `boolean` | Yes | — |  |
| `upnote_running` | `boolean` | Yes | — |  |
| `upnote_pid` | `integer / null` | No | — |  |
| `upnote_binary` | `string / null` | No | — |  |
| `counts` | `object` | Yes | — |  |

### Schema: `TagOut`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `id` | `string` | Yes | — |  |
| `title` | `string` | Yes | — | Tag title with leading '#' (e.g. '#plex') |
| `name` | `string` | Yes | — | Normalized tag name without '#' (e.g. 'plex') |
| `space_id` | `string` | Yes | — |  |
| `space_name` | `string` | Yes | — |  |
| `inactive` | `boolean` | No | `false` |  |
| `synced` | `boolean` | No | `true` |  |
| `notes_count` | `integer` | No | `0` |  |
| `created_at` | `number / null` | No | — |  |
| `updated_at` | `number / null` | No | — |  |

### Schema: `ValidationError`

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :--- | :--- |
| `loc` | `array` | Yes | — |  |
| `msg` | `string` | Yes | — |  |
| `type` | `string` | Yes | — |  |
| `input` | `any` | No | — |  |
| `ctx` | `object` | No | — |  |

