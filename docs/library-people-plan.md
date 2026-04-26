# Unified context library + management UI

## Context

Today, ingested materials (PDFs, pastes, Gmail attachments, images) live in a
loose pile under `/data/uploads/{pdfs,pastes,images,emails,files}/` plus
`/data/context/source-material/` and `/data/archives/`. The model lists them
via an auto-generated `INDEX.md` and reads them on demand with `read_document`.
Three problems:

1. **PDFs are re-parsed on every read.** `app/tools/documents.py:69` runs
   PyPDF2 with a 20-page cap *each time* `read_document` is called. Cost
   compounds across turns and silently truncates long reports. No format
   support beyond PDF + image vision-block.
2. **No metadata, no curation, no quality gate.** A document is a filename.
   No title, tags, source provenance, supersede pointer, status, or check
   that extraction actually captured the content. `propose_promote_upload`
   records a *pending* event but no UI ever resolves it
   (`app/tools/gmail.py:428`).
3. **No UI for context management.** Liam cannot see what the model "knows
   about", cannot delete a stale paste, cannot replace an old assessment with
   a newer one, cannot see whether a screenshot was OCR'd well. Only path is
   `kubectl cp` / NFS mount.

Fix: a unified library with one folder per document, metadata sidecar,
pre-extracted text, machine-readable manifest, **per-format extraction
strategies**, **a verification pass that compares extraction against the
original**, **live single-line status feedback during processing**, and a
`/library` page in the app to browse / upload / paste / delete / supersede.

**Scope confirmed with user:** external imports only — PDF, DOC, DOCX, text
pastes, phone screenshots, WhatsApp/other chat exports. **No `.eml`** (drop
the email path entirely from the new design — Gmail attachments still arrive
via the tool, but only PDF/image/doc/docx/text payloads). Factual-spine files
(`01_background.md` … `06_interpretive_notes.md`), `05_current_state.md`,
sessions, and session-logs are **out of scope** and keep their current
handling.

## Storage layout

New top-level dir, sibling to `uploads/` (which becomes a legacy migration
source):

```
/data/library/
├── manifest.json                       # machine-readable index of all docs
├── <doc_id>/
│   ├── meta.json                       # title, kind, source, tags, status, ...
│   ├── original.<ext>                  # verbatim file
│   ├── extracted.md                    # pre-parsed text for the model
│   └── verification.json               # results of the sanity-check pass
└── <doc_id>/...
```

`<doc_id>` = UTC timestamp + short slug, e.g.
`2026-04-25T12-30-45Z_dc-diagnostic`. Predictable, sortable, no collisions,
human-debuggable on NFS.

### `meta.json` schema

```json
{
  "id": "2026-04-25T12-30-45Z_dc-diagnostic",
  "title": "DC Diagnostic — Liam Mackenzie",
  "kind": "pdf",                        // pdf | docx | doc | text | image | chat_export
  "source": "upload",                   // upload | paste | gmail_attachment
  "created_at": "2026-04-25T12:30:45Z", // upload time (when it landed in the library)
  "original_date": "2018-03-12",        // when the source was originally written/sent (see "Original date" below)
  "original_date_source": "pdf_metadata", // pdf_metadata | docx_core_props | exif | chat_first_message | user_supplied | unknown
  "date_range_end": null,               // chat exports only: last message date
  "size_bytes": 1234567,
  "mime": "application/pdf",
  "page_count": 32,                     // pdf/docx; null otherwise
  "extractor": "pdf_pypdf2",
  "extracted_chars": 45123,
  "tags": ["diagnostic", "autism"],
  "status": "active",                   // active | superseded | deleted
  "supersedes": null,
  "superseded_by": null,
  "verification": "ok",                 // ok | warn | fail
  "summary": null
}
```

### `manifest.json`

Flat array of every `meta.json` under `library/`, regenerated whenever a doc
is added/updated/deleted. The model and the UI both read this. Written
atomically (temp + rename) to a path under `.locks/` to avoid clobber from
concurrent uploads.

## Per-format extraction strategies

`app/extractors.py` exposes a small registry. Each extractor implements:

```python
class Extractor(Protocol):
    kind: str                                           # "pdf", "docx", ...
    def can_handle(self, path: Path, mime: str) -> bool: ...
    def extract(self, path: Path, emit: Emit) -> ExtractResult: ...
    def verify(self, path: Path, result: ExtractResult, emit: Emit) -> VerifyResult: ...
```

`Emit` is a callback the orchestrator passes in so the extractor can push
fine-grained status messages ("Extracting page 12/50…") to the SSE stream.

### PDF (`kind: "pdf"`)

- **Primary**: `PyPDF2` over **all pages** (no 20-page cap). Per-page failures
  recorded inline (`[page N: extraction failed]`).
- **Scan detection**: after primary extraction, compute
  `chars_per_page = extracted_chars / page_count`. If `< 100` AND
  `page_count > 0`, treat as scanned/image PDF.
- **Scan fallback**: rasterize each page via `pdf2image` (poppler) →
  feed each page image to Sonnet 4.6 vision with a transcription prompt
  ("transcribe all text verbatim, preserve headings and lists, use
  markdown"). Concatenate results into `extracted.md`. Tag
  `extractor: "pdf_vision_ocr"` so the user can see this PDF was OCR'd.
- **Page-by-page emit**: orchestrator pushes `"Extracting page N/M…"`
  through the SSE stream every page (or every 5 pages for big PDFs).

### DOCX (`kind: "docx"`)

- Library: `python-docx`.
- Walk paragraphs → markdown (preserve heading levels via style names).
- Tables → markdown tables.
- Headers/footers → appended in a `## Headers/footers` section.
- Inline images → counted, listed under a `## Embedded images (N)` section
  with no extraction (out of scope to also save them as separate docs).
- `page_count` is unreliable for `.docx` (Word computes it at render
  time); store paragraph count instead, label the field accordingly in
  the UI.

### DOC (legacy `.doc`, `kind: "doc"`)

- Convert via `libreoffice --headless --convert-to docx` into a temp dir,
  then run the DOCX extractor.
- Requires `libreoffice-core` in the container — add to `Dockerfile`.
- Original `.doc` preserved as `original.doc`; converted DOCX is discarded
  after extraction (not kept — user can re-export if needed).
- If LibreOffice fails (corrupt file, etc.), set
  `verification: "fail"` with the conversion error.

### Text paste (`kind: "text"`)

- Verbatim copy from `POST /library/paste` body → `extracted.md`.
- Title heuristic: first non-empty line, max 80 chars. Falls back to
  user-supplied label.
- WhatsApp-format detection (see chat export below) auto-routes the paste
  to the chat extractor instead.
- The 4000-char inline-vs-save threshold in `session_chat.html:158` is
  preserved.

### Phone screenshot / image (`kind: "image"`)

- Original kept as `original.png` / `.jpg` / `.webp`.
- Extraction via Sonnet 4.6 vision: send image + transcription prompt
  ("transcribe all visible text verbatim. Note any UI chrome, sender
  names, timestamps. Use markdown."). Result → `extracted.md`.
- For image-heavy sessions this matters: a phone screenshot of a
  WhatsApp thread or a calendar invite is the common case, and tesseract
  butchers those layouts.
- `read_document` still returns the original as a vision block when the
  model needs to *see* the image (UI layout, not just text). The
  extracted text feeds the cheap path (`list_documents`,
  `search_documents`, system-prompt injection if ever).

### Chat export (`kind: "chat_export"`)

- **Detection**: paste or `.txt` upload whose first ~20 non-empty lines
  match WhatsApp's `^\[\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}` (date,
  time, sender, body) more than half the time. Same trigger for iMessage
  or Telegram exports if a similar regex hits.
- **Normalisation**: parse into `(timestamp, sender, body)` tuples.
  Render as markdown:
  ```
  ## WhatsApp chat — Rhiannon O'Hara — 2025-09-12 → 2026-04-20

  **2025-09-12 14:32 — Liam**: …
  **2025-09-12 14:33 — Rhiannon**: …
  ```
- Multi-line bodies preserved; system messages
  ("Messages and calls are end-to-end encrypted…") stripped to a single
  marker.
- Persists original verbatim as `original.txt`.
- `extracted.md` gets the normalised version; the parser also writes
  `participants`, `message_count`, `date_range` into `meta.json` for the
  UI. These pop in the table view ("WhatsApp · 1,243 msgs · 8 months").

### Default fallback

Anything else (`.txt`, `.md`, unknown MIME) → text extractor, verbatim.

## Original date (when the source was written, not when it was uploaded)

A 2018 diagnostic PDF and a screenshot from yesterday carry very different
weight in a session. The companion needs to know "this is from X years ago"
without Liam having to say it every time. Capture an `original_date` field
per doc, with a clear provenance label, and prompt the user when we can't
detect it automatically.

### Per-format detection

| Kind | How `original_date` is sourced |
|------|---------------------------------|
| pdf  | `PyPDF2 reader.metadata.creation_date` first; fall back to `/CreationDate` raw; fall back to a regex sweep of the first page text for date patterns ("Date of Report:", "Issued:", `dd Month yyyy` etc.). Source label: `pdf_metadata` or `pdf_text_pattern`. |
| docx | `python-docx`'s `core_properties.created`. Source: `docx_core_props`. |
| doc  | After LibreOffice → DOCX conversion, same as DOCX. |
| image | Pillow EXIF `DateTimeOriginal` (tag 36867). Phone screenshots carry this reliably. Source: `exif`. |
| text | No automatic detection (rarely meaningful for free-text pastes). Always prompt. |
| chat_export | First message timestamp = `original_date`; last = `date_range_end`. Source: `chat_first_message`. |

### "Ask the user" fallback

If detection returns nothing (or returns a date that looks suspicious — e.g.
PDF creation_date in the future, or 1970-01-01), the SSE pipeline pauses
between the **Detecting date…** and **Building index…** stages and the
status line transforms into a date-input affordance:

```
Couldn't detect the original date. When was this from?
[ date picker ]   [ Use upload date ]   [ Unknown ]
```

User selects → POST `/library/{doc_id}/date` (date in ISO, or `unknown`,
or `use_upload_date`) → orchestrator resumes the pipeline. `original_date_source`
becomes `user_supplied` or `unknown`. The doc never reaches `active` until
the date question is answered or skipped.

### Where the model sees it

- `list_documents` output: `DC Diagnostic — Liam Mackenzie (2018-03-12)`
  for known dates, `(date unknown)` otherwise.
- `read_document` prepends a single header line to the returned text:
  `> Original date: 2018-03-12 (from PDF metadata)` or
  `> Original date: unknown — uploaded 2026-04-25`.
- Companion prompt gets a one-line nudge to *use* the date when reasoning
  about how recent something is.

### Editing later

The doc-detail panel in `/library` shows `original_date` and lets the user
edit it post-hoc — handy if a wrong date was detected or a "Use upload date"
shortcut needs correcting.

## Verification (the double-check)

After each extraction, the same extractor's `verify()` runs and writes
`verification.json`:

```json
{
  "status": "ok",                       // ok | warn | fail
  "checks": [
    {"name": "size_ratio", "ok": true,  "detail": "extracted=45123 chars / original=1.2MB → 0.038, within range"},
    {"name": "page_coverage", "ok": true, "detail": "32/32 pages produced text"},
    {"name": "spot_check", "ok": true, "detail": "model audit: no obvious gaps"}
  ],
  "checked_at": "2026-04-25T12:30:48Z"
}
```

### Per-format checks

| Kind | Heuristic checks (fast, deterministic) | Spot-check (model audit, optional) |
|------|----------------------------------------|------------------------------------|
| pdf | (a) every page produced ≥1 char OR was flagged. (b) `extracted_chars / page_count >= 50` (else `warn`). (c) text-extracted PDFs: re-extract first + last + middle page via a *second* method (pdfplumber) and confirm overlap. | OCR'd PDFs only: send original page-1 image + extracted page-1 text to Sonnet, ask "anything missing? respond `ok` or list gaps." |
| docx | (a) paragraph_count > 0. (b) re-open with `python-docx` after write and confirm round-trip. | Skip — extraction is lossless. |
| doc  | (a) LibreOffice exit code == 0. (b) downstream DOCX checks. | Skip. |
| text | (a) `len(extracted) == len(original)` byte-for-byte. | Skip. |
| image | (a) `extracted_chars > 0` (else `warn` — possibly an image with no text, like a meme). | Default-on for images: send original + transcript to Sonnet, "anything missing or misread? respond `ok` or list issues." |
| chat_export | (a) parsed `message_count` ≥ 0.9 × original line count after stripping system messages. (b) every detected sender appears in the normalised output. | Skip. |

Heuristics are cheap and run always. Spot-checks are gated:
- Always for OCR'd PDFs and images.
- Never for text/docx (lossless).
- Configurable via env: `ROBO_LIBRARY_SPOT_CHECK=auto|always|never`.

`status: "warn"` is non-blocking — the doc is active and usable, but the
UI shows an amber dot. `status: "fail"` blocks promotion to `active` (doc
lands as `status: "deleted"` from the start so it doesn't pollute the
model's view); the UI shows a red dot with the failure reason and a
"Retry extraction" button.

## Live status feedback (single updating line)

The user wants one line that morphs through the lifecycle. Implementation:
**SSE** via `sse-starlette` (small dep) consumed by HTMX's SSE extension.

### Wire flow

1. UI submits the upload form via `hx-post="/library/upload"` to a hidden
   iframe (or fetch + `EventSource`). Server saves `original.<ext>`,
   mints `<doc_id>`, then **redirects** the response to
   `GET /library/{doc_id}/stream` which is the SSE endpoint.
2. The status line is a single `<span id="upload-status">` bound via
   `hx-ext="sse"` `sse-connect="/library/{doc_id}/stream"` and
   `sse-swap="message"`. Each event replaces the span's contents.
3. Server-side, the orchestrator runs in a `BackgroundTask`-equivalent
   (an `asyncio.Task`) and pushes events onto an `asyncio.Queue` keyed
   by `doc_id`. The SSE endpoint awaits the queue.

### Stages emitted

```
Uploaded — 1.2MB, application/pdf
Detecting type…
Extracting page 1/32…
Extracting page 17/32…
Extracting page 32/32…
Detecting date…
Date: 2018-03-12 (from PDF metadata)
Building index…
Sanity checking…
Sanity check: ok
Done — DC Diagnostic — Liam Mackenzie
```

If the date can't be detected the pipeline pauses on a "Couldn't detect
date" stage that exposes a date picker (see "Original date" above), then
resumes once the user answers.

(Per the project rule: no emoji unless the user uses them first. Use
plain markers like `ok` / `warn` / `fail` instead of ✓✗⚠ in the actual
strings — I included the tick above for clarity in this plan only.)

Final event includes a `hx-trigger` payload that swaps the new row into
the library table. If `verification: "warn"` or `"fail"`, the final
line stays visible (with a "View details" link) until the user
dismisses.

### Reconnect / refresh safety

- Status events also written to an in-memory ring buffer per doc (last
  20 messages). On reconnect within 60s the SSE endpoint replays the
  buffer first.
- If the user navigates away mid-extraction the work continues in the
  background; the doc shows up under "Active" with a yellow "processing"
  badge and the row's own SSE connection picks up where it left off
  when the user returns to `/library`.

## UI: `/library`

New page, linked from the home nav. Three sections:

1. **Active documents** — table: title, kind icon, created, size, tags,
   verification dot (green/amber/red), actions (View, Tag, Supersede,
   Delete-soft).
2. **Add new** — drag-and-drop zone (multi-file ok; each file gets its
   own status line) + paste textarea with title field. Live status lines
   stack while uploads are in flight, collapse to row entries when done.
3. **Archived** — collapsible; `status=superseded` and `status=deleted`.
   Each row: Restore, Permanently delete (hard).

Single Jinja template, HTMX + SSE extension, Pico.css.

### Routes (in `app/main.py`, all `Depends(require_auth)`)

| Route | Method | Purpose |
|-------|--------|---------|
| `/library` | GET | Renders `library.html` with manifest data |
| `/library/upload` | POST | Multipart; saves original, kicks off background processor, returns `{doc_id, stream_url}` (or HTMX redirect to stream) |
| `/library/paste` | POST | Same shape, body is text |
| `/library/{doc_id}/stream` | GET | SSE — emits processing stages until `done` |
| `/library/{doc_id}` | GET | JSON or HTMX fragment with full meta + extracted preview (first ~5KB) |
| `/library/{doc_id}/tags` | POST | Update tags |
| `/library/{doc_id}/supersede` | POST | Multipart upload; marks current as superseded, creates new doc with `supersedes` pointer |
| `/library/{doc_id}/delete` | POST | Soft delete (status=deleted) |
| `/library/{doc_id}/restore` | POST | status → active |
| `/library/{doc_id}/purge` | POST | Hard delete: rm-rf doc folder, regen manifest |
| `/library/{doc_id}/retry` | POST | Re-run extraction (e.g. after upgrading an extractor) |

Old `POST /upload` and `POST /session/{id}/paste` become thin shims that
forward to `/library/upload` and `/library/paste`. They preserve:
- The 25MB upload cap (`_MAX_UPLOAD_BYTES` in `app/main.py:1117`).
- The `paste_saved` session-event append (so the auditor's session log
  still records that a paste happened mid-session).
- The JSON response shape **but** with `path` being the `doc_id` instead
  of an `uploads/...` filesystem path (compatible because `read_document`
  accepts both).
- The `[uploaded: <doc_id>]` / `[pasted: <doc_id>]` marker the JS injects
  into the textarea (`session_chat.html:150,168,186`). Update those lines
  to read the new `doc_id` field. Update `companion.md:30` so the model
  knows the marker now contains an ID, not a path.

## People system

Therapy is about people. Liam's sessions revolve around a small cast —
co-parents, family, professionals, friends — and the companion needs fast,
consistent context on each. The factual spine (`04_relationship_map.md`)
captures this today as a single hand-edited markdown file, but it's a flat
narrative: there's no easy way for the companion to look up just "Rhiannon"
when she comes up mid-session, no link from a person to the documents
about them, and no way for the auditor to propose updates after a
conversation reveals new context.

Add a sibling structure to the library, modelled the same way (one folder
per entity, manifest, atomic writes), with a `/people` UI and three new
tools.

### Storage

```
/data/people/
├── manifest.json                    # flat array of all meta.json
├── <person_id>/
│   ├── meta.json                    # structured fields (see below)
│   └── notes.md                     # free-form markdown, append-friendly
```

`<person_id>` = name slug, lowercase + hyphens (e.g. `rhiannon-ohara`,
`dr-tanya-collins`). Collisions get a numeric suffix (`-2`).

### `meta.json` schema

```json
{
  "id": "rhiannon-ohara",
  "name": "Rhiannon O'Hara",
  "aliases": ["Bri", "Rhi"],
  "category": "co-parent",            // co-parent | family | partner | friend | professional | child | colleague | other
  "relationship": "Ex-partner, mother of Jasper",  // one-liner shown in tables
  "summary": "Co-parents Jasper. Lives in Dayboro. Primary channel: WhatsApp.",
  "important_context": [              // bullet points the companion sees when looking up
    "Strong views on schooling; flashpoint topic",
    "Emotional topics often misfire over text"
  ],
  "tags": ["co-parent", "high-conflict-history"],
  "linked_documents": ["2026-04-25T12-30-45Z_whatsapp-rhiannon"],  // doc_ids
  "first_seen": "2026-01-15",
  "last_mentioned": "2026-04-20T08:14:00Z",  // updated when companion looks them up
  "status": "active",                 // active | archived
  "created_at": "2026-04-25T12:30:45Z",
  "updated_at": "2026-04-25T12:30:45Z"
}
```

`notes.md` is free-form markdown — therapy context, history, recurring
patterns. The auditor can append; the user can edit in full.

### Seeding from `04_relationship_map.md`

Don't try to auto-parse the spine file at runtime — the format is
narrative, not structured. Instead, the `/people` UI exposes an "Import
from 04_relationship_map.md" button (admin one-shot):

1. Click → server sends 04's text + a strict extraction prompt to Sonnet
   ("extract a JSON list of person records with name, aliases,
   relationship, summary, important_context").
2. Result rendered as a checklist: each proposed person with editable
   fields.
3. User ticks/edits/discards, clicks "Commit" → records written to
   `/data/people/`.

`04_relationship_map.md` itself stays untouched (still part of the
factual spine that feeds handover; the people system is a parallel
index, not a replacement).

### Auditor integration

`app/summariser.py` auditor's tool-use schema gains a new field:

```json
"people_updates": [
  {"action": "add",    "name": "Marcus Chen", "category": "friend", "summary": "..."},
  {"action": "update", "id": "rhiannon-ohara", "append_note": "Mentioned the school enrolment letter is due Friday."},
  {"action": "touch",  "id": "jasper-ohara"}   // just bump last_mentioned
]
```

Applied automatically post-session (single-user, low blast radius). Each
applied update is logged in the session-log so Liam can see what
changed. `add` proposals for someone with a near-name-match to an
existing record (Levenshtein < 3) are turned into `update` proposals
instead, with the alias merged.

### How the companion uses it

Cached **block 2** of the system prompt (`app/context.py`) gets a new
section beneath INDEX.md: a rendered `people.md` (derived from
`people/manifest.json`) listing every active person as
`- **Name** (aliases) — category — one-line summary`. Cheap, ~50–100
people max realistically, fits in a few hundred tokens. This means the
companion *always knows who exists*; it doesn't have to discover them.

Three new tools (registered in `app/tools/people.py`):

| Tool | Purpose |
|------|---------|
| `list_people()` | Returns the same rendered list as block 2; included for completeness so the model can re-fetch if needed. |
| `lookup_person(id_or_name)` | Full meta + `notes.md` + titles of every `linked_documents` entry. Bumps `last_mentioned`. The model calls this when a person becomes the focus of the conversation. |
| `search_people(query)` | Substring match across name, aliases, tags, summary, notes. Returns ids + match snippets. Useful when Liam refers to someone obliquely ("the woman from the school assessment"). |

Companion prompt update: a short paragraph teaching it that people exist
as first-class records, that block 2 carries the roster, and that
`lookup_person` is the right call when a conversation shifts to a
specific person — same pattern as `read_document` for documents.

### UI: `/people`

Sibling page to `/library`, linked from home nav.

1. **Active people** — table: name (with aliases as small text), category
   chip, relationship, last mentioned, linked-doc count, actions
   (View, Edit, Archive).
2. **Add new** — form: name, aliases (comma-sep), category dropdown,
   relationship, summary, important_context (one per line), tags.
3. **Person detail** (modal or own page): full meta, editable notes.md
   in a textarea, list of linked documents (each a link back to
   `/library/<doc_id>`), "Link a document" button (multi-select from
   library), "Archive" / "Delete" actions.
4. **Archived** — collapsible.

Routes (all `Depends(require_auth)`):

| Route | Method | Purpose |
|-------|--------|---------|
| `/people` | GET | Renders `people.html` |
| `/people/new` | POST | Create person |
| `/people/{id}` | GET | Detail (HTMX fragment or full page) |
| `/people/{id}` | POST | Update meta |
| `/people/{id}/notes` | POST | Replace `notes.md` |
| `/people/{id}/link` | POST | Add doc_id to `linked_documents` |
| `/people/{id}/unlink` | POST | Remove doc_id |
| `/people/{id}/archive` | POST | status → archived |
| `/people/{id}/restore` | POST | status → active |
| `/people/{id}/delete` | POST | Hard delete (no soft tier here — people records are small and archive is enough soft-delete) |
| `/people/import-from-spine` | POST | Triggers the 04_relationship_map.md import flow |

### Library ↔ People cross-link

- Library doc upload form gains an optional "Linked people" multi-select
  (autocompletes against people manifest). Selections write through
  to both `library/<doc_id>/meta.json::linked_people` and each
  person's `linked_documents` (kept in sync; one is the source of
  truth — let's say `linked_documents` on the person side, mirrored on
  the doc for convenience).
- Chat-export extractor auto-links: every detected `participant` is
  matched against people manifest (name + aliases). Matches → linked.
  Unmatched → status line in the upload pipeline offers "Add as new
  person?" before finishing.
- Doc detail panel in `/library` shows linked people; person detail
  shows linked docs. Each is a clickable cross-link.

## Tools rework

Three existing tools, same names, new internals:

| Tool | New behaviour |
|------|---------------|
| `list_documents` | Render manifest entries with `status=active` as markdown (id, title with `original_date` in parens, kind, size, verification, tags, linked people). Same shape so prompt block 2 stays compatible. |
| `read_document(id_or_path)` | Accepts `<doc_id>` or a legacy `uploads/...` path. Returns `extracted.md` (with the `> Original date: …` header line) for text/PDF/DOCX/DOC/chat. Returns vision block from `original.<ext>` for images. **Read-time cap raised from 200KB → 1MB**, with a clear truncation note. New optional arg `pages: "N-M"` for paginating big extracts. |
| `search_documents(query)` | Greps `library/*/extracted.md`, returns `doc_id` + snippet. Drops `uploads/`, `archives/`, and `context/source-material/` from scan (they're empty / migrated). |
| `list_people` | Returns the same rendered roster as block 2 (id, name, aliases, category, one-line summary). |
| `lookup_person(id_or_name)` | Full meta + `notes.md` + titles of every linked document. Bumps `last_mentioned`. |
| `search_people(query)` | Substring across name, aliases, tags, summary, notes. Returns ids + snippets. |

`propose_promote_upload` is removed from the tool registry and from
`companion.md` — the upload/paste flow now lands docs as first-class
library entries directly.

`INDEX.md` remains a derived artifact, regenerated from `manifest.json`
into `context/INDEX.md` on every change. `app/context.py` block 2 stays
unchanged.

## Files to add / modify

### New
- `app/library.py` — `Library` class: `add_upload`, `add_paste`, `get`,
  `list_active`, `list_archived`, `supersede`, `soft_delete`, `restore`,
  `hard_delete`, `retry`, `rebuild_manifest`, `render_index_md`. Owns
  every read/write under `library/`. Uses temp-file + rename for
  manifest writes; per-doc work serialised behind a `.locks/<doc_id>`
  file lock.
- `app/extractors.py` — registry + concrete extractors: `PdfExtractor`,
  `DocxExtractor`, `DocExtractor`, `TextExtractor`, `ImageExtractor`,
  `ChatExportExtractor`. Each implements `extract`, `verify`, and
  `detect_date` (returns `(iso_date | None, source_label)`).
- `app/library_stream.py` — `StatusBus`: per-doc `asyncio.Queue` +
  ring buffer; `emit(doc_id, msg)` and `subscribe(doc_id)`.
- `app/people.py` — `People` class: `add`, `get`, `update`,
  `replace_notes`, `link_doc`, `unlink_doc`, `archive`, `restore`,
  `delete`, `list_active`, `rebuild_manifest`, `render_people_md`,
  `import_from_spine`. Same atomic-write pattern as `Library`.
- `app/tools/people.py` — `list_people_spec`, `lookup_person_spec`,
  `search_people_spec`. Registered in `app/tools/registry.py`.
- `app/templates/people.html` — management page.
- `app/templates/fragments/person_card.html`,
  `app/templates/fragments/person_row.html` — HTMX fragments.
- `tests/test_people.py` — CRUD, slug collision, alias merge, link
  bookkeeping (set difference between doc.linked_people and
  person.linked_documents stays consistent).
- `app/templates/library.html` — management page.
- `app/templates/fragments/library_row.html` — single-row HTMX fragment.
- `app/templates/fragments/library_status.html` — single-line status
  span used by SSE.
- `tests/test_library.py` — add/supersede/delete/restore/purge,
  manifest regen, slug collisions, path-traversal safety, concurrency.
- `tests/test_extractors.py` — per-format: tiny fixtures for PDF (text +
  scanned), DOCX, plain text, WhatsApp export, mocked vision call.
- `tests/test_library_stream.py` — SSE event ordering, ring buffer
  replay on reconnect.

### Modified
- `app/tools/documents.py` — rewrite handlers against `Library`. Drop
  `rebuild_index` (replaced by `Library.render_index_md`). Keep
  `_safe_resolve` for legacy path support during the migration window.
  **Update existing tests**: `tests/test_tools.py::test_rebuild_index_*`,
  `test_list_documents_generates_index`, etc. need to assert the new
  manifest-backed behaviour.
- `app/tools/registry.py` — drop `propose_promote_upload`; register the
  three new people tools.
- `app/tools/gmail.py` — `save_gmail_attachment` writes via
  `Library.add_upload(source="gmail_attachment")`. **PDFs and images
  only** (the supported kinds); attachments of other types are saved as
  text-fallback library docs with a clear `verification: warn`. Remove
  `propose_promote_upload`.
- `app/main.py` — add `/library/*` and `/people/*` routes; convert
  `/upload` and `/session/{id}/paste` to forwarders; add `library` and
  `people` to the startup mkdir list at line 158; add `/library` and
  `/people` nav links.
- `app/templates/session_chat.html` — change marker injection to use
  `doc_id` (lines 150, 168, 186).
- `app/templates/home.html`, `base.html` — nav link to `/library`.
- `app/context.py` — block 2 now concatenates INDEX.md + a blank line +
  rendered `people.md`. Both are stable enough to share the same cache
  layer. No other logic change.
- `app/summariser.py` — auditor tool-use schema gains `people_updates`;
  applier walks the list and calls `People.add` / `People.update` /
  `People.append_note` / `last_mentioned` bumps. Each applied update
  recorded in the session-log section the auditor writes.
- `app/prompts/companion.md` — drop `propose_promote_upload`; update
  marker line to `[uploaded: <doc_id>]` / `[pasted: <doc_id>]`; add a
  short paragraph teaching the model that block 2 carries a roster of
  known people, that `lookup_person` is the right call when a
  conversation focuses on someone, and that `original_date` on
  documents is meaningful context (a 2018 assessment ≠ a recent one).
- `app/prompts/auditor.md` — extend the schema doc to cover
  `people_updates` and explain when to add vs update vs touch.
- `pyproject.toml` — add deps: `python-docx`, `pdf2image`,
  `pdfplumber` (verification cross-check), `sse-starlette`. **`pypdf2`
  stays.** No `pytesseract` — image OCR uses Sonnet vision.
- `Dockerfile` — install system packages: `libreoffice-core` (for .doc),
  `poppler-utils` (for `pdf2image`).

### Deployment (coopernetes — separate repo, gitops, ask before pushing)

The current setup uses a static NFS PV
(`coopernetes/kubernetes/apps/robo/app/pv.yaml`, 50Gi, RWX, pointing at
`192.168.200.3:/volume1/robo`) bound 1:1 by `pvc.yaml`. Switch to a
dynamically-provisioned 10Gi PVC.

**`coopernetes/kubernetes/apps/robo/app/pv.yaml`** — delete (no longer needed;
let the storage class provision the PV).

**`coopernetes/kubernetes/apps/robo/app/pvc.yaml`** — rewrite as:

```yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: robo-data
spec:
  accessModes:
    - ReadWriteOnce            # single-replica deployment, RWX no longer needed
  resources:
    requests:
      storage: 10Gi
  storageClassName: longhorn-ssd-single   # recommend; alternatives: longhorn-nvme, synology-nfs
```

`longhorn-ssd-single` is the right default here: distributed (survives node
loss), single replica (10Gi footprint matches the single-user data volume),
no NVMe burn for a low-IO workload. Switch to `longhorn-nvme` if you want
faster PDF processing on large files; switch to `synology-nfs` if you'd
rather keep data on the NAS while still moving off the static-PV pattern.

Also drop the kustomization entry for `pv.yaml` in
`coopernetes/kubernetes/apps/robo/app/kustomization.yaml`.

The deployment.yaml itself doesn't need volume changes — the PVC name
`robo-data` and the mount path `/data` stay the same. The pod transparently
sees an empty filesystem on first boot of the new PVC.

**Cutover steps** (Liam-driven, gitops):
1. Land app code changes here, build + push image with new tag.
2. In coopernetes: bump image tag, replace pv.yaml/pvc.yaml as above,
   commit, push. Flux reconciles; Longhorn provisions; pod restarts on
   the empty PVC. Old NFS PV data remains on the Synology share, untouched.
3. Open `/library` in the browser, start uploading. Each upload is a live
   test of the new pipeline.

### Data preservation: seed the new PVC from the old NFS

The therapist must not lose continuity. Sessions, mood log, the factual
spine, session-logs, and Google OAuth credentials all migrate
automatically. Only the *document-like* content (PDFs, screenshots, chat
exports) is left for Liam to re-upload via the new UI — that doubles as
the hands-on acceptance test of the upload/extraction/verification flow.

**Migrated automatically (preserved on first boot of the new PVC):**

| Path | Why it must survive |
|------|---------------------|
| `sessions/*.jsonl` | Therapy session transcripts. Source of truth for everything the auditor and handover ever read. |
| `session-logs/*.md` | Auditor summaries. Feed system-prompt block 3. |
| `session-exports/*.pdf` | Past therapist-handover PDFs. |
| `context/01_background.md` … `06_interpretive_notes.md` | The factual spine. |
| `context/SKILL.md`, `context/PACK_README.md`, `context/commitments.md` | Hand-curated context. |
| `context/mood-log.jsonl` | Mood history; renders the home-page sparkline. |
| `app-feedback.md` | The feedback loop the auditor appends to. |
| `.credentials/` | Google OAuth refresh tokens (else Liam re-OAuths from scratch). |

`people/` does **not** migrate (it doesn't exist on the old PVC). After
cutover, Liam opens `/people` and clicks "Import from
04_relationship_map.md" to seed it from the existing spine, reviews the
proposed records, and commits. Then re-uploads documents through
`/library`, linking each to the relevant people.

**Skipped (Liam re-uploads through `/library`):**

- `uploads/` — old loose ingestion area, replaced by `library/`.
- `context/source-material/` — three PDFs (DC Diagnostic, MIGDAS, Gmail
  draft). Re-upload to test the PDF + date-detection path.
- `archives/` — `2026-04-19_first-claude-session-raw.txt` and a WhatsApp
  export. Re-upload to test the chat-export path.
- `context/INDEX.md` — auto-regenerated post-cutover anyway.

**Mechanism: one-shot k8s Job that mounts both volumes and rsyncs.**

In coopernetes, add `migration/` subdir under
`kubernetes/apps/robo/` with two files:

`legacy-pvc.yaml` — keep the existing NFS PV/PVC reachable under a new
name `robo-data-legacy` (rename `robo-data` → `robo-data-legacy` in the
existing `pv.yaml` and `pvc.yaml`, *do not delete yet*).

`seed-job.yaml`:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: robo-seed-pvc
spec:
  ttlSecondsAfterFinished: 86400
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: rsync
          image: alpine:3.20
          command: ["sh", "-c"]
          args:
            - |
              set -euo pipefail
              apk add --no-cache rsync
              rsync -av \
                --exclude='uploads/' \
                --exclude='archives/' \
                --exclude='context/source-material/' \
                --exclude='context/INDEX.md' \
                --exclude='library/' \
                --exclude='.locks/' \
                /old/ /new/
              echo "Seed complete."
              ls -la /new/
              ls -la /new/context/
          volumeMounts:
            - { name: old, mountPath: /old, readOnly: true }
            - { name: new, mountPath: /new }
      volumes:
        - name: old
          persistentVolumeClaim:
            claimName: robo-data-legacy
        - name: new
          persistentVolumeClaim:
            claimName: robo-data
```

**Cutover sequence (Liam-driven, gitops, ask before each push):**

1. Land app code changes in this repo. Build and push the new image.
2. In coopernetes, in one PR:
   - Rename the existing `robo-data` PV/PVC to `robo-data-legacy`
     (edit `pv.yaml` and `pvc.yaml` `metadata.name` and the PVC's
     `volumeName` reference; update `kustomization.yaml`).
   - Add the new `robo-data` PVC (10Gi Longhorn) — see "Deployment"
     section below.
   - Add `migration/seed-job.yaml`.
   - Bump the deployment image tag, scale `replicas: 0` so the pod
     releases the volume before the Job runs.
3. Push, wait for Flux to reconcile.
4. Wait for the seed Job to finish (`kubectl -n robo logs job/robo-seed-pvc`).
   Confirm `/new/sessions/`, `/new/context/01_background.md`,
   `/new/context/mood-log.jsonl`, `/new/.credentials/` exist.
5. Scale deployment back to `replicas: 1`. Pod boots on the seeded PVC.
6. Smoke: `/` shows the mood sparkline (proof mood-log copied), past
   sessions are listed, `/connect-gmail` doesn't ask for re-auth (proof
   `.credentials/` copied), starting a session loads the spine into the
   system prompt (proof `context/01-06.md` copied).
7. Open `/library` (empty manifest) and start uploading documents.
8. After a verification window (a week or two), in coopernetes drop
   `pv.yaml`, `pvc.yaml` (legacy), and `migration/`. The legacy NFS
   share stays on the Synology untouched — `persistentVolumeReclaimPolicy:
   Retain` means the data survives PV deletion as a cold backup.

**Failure recovery:** if anything is missing post-cutover, scale to 0,
re-run the Job (delete + reapply), or pull individual files via
`kubectl cp` from a temporary pod that mounts `robo-data-legacy`.

## Verification (project-level)

1. **Unit tests:** `uv run pytest tests/ -q` stays green. Existing
   `test_tools.py` tests updated; new `test_library.py`,
   `test_extractors.py`, `test_library_stream.py` cover new modules.
2. **Local smoke (`ROBO_MODE=local`, mocks Anthropic):** start uvicorn,
   hit `/library`, upload a multi-page text PDF; confirm
   `library/<id>/extracted.md` contains text from page > 20; status
   line in browser walks through Uploaded → Extracting → Building →
   Sanity → Done; row appears with `verification: ok` (green dot).
3. **Scanned-PDF path (`ROBO_MODE=dev`):** upload a scanned PDF (or
   force the OCR branch via env override). Confirm vision OCR runs and
   `extractor: pdf_vision_ocr` appears in `meta.json`.
4. **DOCX:** upload a docx with a table and a heading; confirm markdown
   table preserved.
5. **DOC:** upload a `.doc` (LibreOffice in Docker); confirm conversion
   succeeds and downstream DOCX extractor produces the same content.
6. **Phone screenshot:** upload a WhatsApp screenshot; confirm vision
   OCR transcribes the messages; spot-check produces `verification:
   ok`.
7. **WhatsApp export:** paste a chunk of WhatsApp `_chat.txt`; confirm
   `kind: chat_export`, `participants` populated, normalised markdown
   in `extracted.md`.
8. **Supersede:** upload PDF v1, supersede with v2; v1 → archived, v2
   → active, manifest cross-pointers correct.
9. **Soft → hard delete:** delete; doc disappears from
   `list_documents` but `library/<id>/` remains. Purge; folder gone,
   manifest updated.
10. **Tool integration (`ROBO_MODE=dev`):** start a session, ask the
    model to list documents and read one; verify `extracted.md` path
    in logs (no PyPDF2 in the request path).
11. **Date detection:** upload a PDF with known `CreationDate` metadata
    → confirm `original_date` populated, `original_date_source:
    pdf_metadata`. Upload a phone screenshot with EXIF → confirm
    `exif`. Upload a free-text paste → confirm pipeline pauses on
    "Couldn't detect date" with the picker, answer it, confirm
    `user_supplied`.
12. **Migration acceptance (coopernetes cutover):** after the PV/PVC
    rename + Job + image bump:
    - `kubectl -n robo logs job/robo-seed-pvc` shows non-empty rsync
      output and "Seed complete."
    - Pod boots cleanly on the new PVC.
    - Home page shows the mood sparkline (proves
      `context/mood-log.jsonl` carried over).
    - Past session list is intact (proves `sessions/`).
    - Starting a new session, the system prompt shows tokens from
      `01_background.md` etc. (proves spine carried).
    - `/connect-gmail` shows already-connected (proves `.credentials/`).
    - `/library` renders an empty manifest.
    - `/people` renders an empty manifest with a visible "Import from
      04_relationship_map.md" button.
    - First manual upload of a `source-material` PDF walks the full
      status pipeline and ends `verification: ok` with a populated
      `original_date`.
13. **People system:** import from spine; review proposed records;
    commit a few. Confirm `people/manifest.json` populated and
    `people.md` rendered. Start a session, mention one of the imported
    people by name; confirm the model can describe them (block 2 has
    the roster) and that calling `lookup_person` returns full notes.
    Run a session that introduces a new person → confirm the auditor
    proposes an `add` in `people_updates` and it lands as an active
    record. Upload a document, link it to a person, confirm the link
    is visible from both `/library/<doc_id>` and `/people/<id>`.

## Out of scope (deferred)

- `.eml` ingestion — Liam confirmed not needed; Gmail attachments still
  flow through but only as PDF/image/doc/docx/text.
- Folding spine files (01–04, 06) and `05_current_state.md` into the
  library UI.
- Browsing sessions / session-logs in the same UI.
- Tag autocomplete, full-text semantic search, embeddings.
- Rich-text PDF preview (we show `extracted.md`, not rendered pages).
- Inline-image extraction from DOCX as separate library docs.
