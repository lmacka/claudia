# Storage decision — files vs SQLite vs Postgres

> Status: DRAFT. Captures the v0.6.x architectural assessment that closes
> T-NEW-I. Recommends a phased move to SQLite for the structured artifacts
> while keeping the filesystem for blobs. Does not commit to a migration
> schedule; that's a v0.7.x branch decision.

## Why we're asking now

v1 chose files for "convenience and grep-ability." Three pressures have
since shown up that v1 didn't fully anticipate:

1. **Persistence-of-state surfaces are growing.** The kid-session
   in-memory dict was replaced by a file-backed store in commit `9f1ca9d`
   to survive pod restart. The new `parent_display_name.txt` (T-NEW-G)
   and the audit-sidecar JSONs (v0.5.0) are similar shapes. Each one is
   a tiny ad-hoc file with bespoke read/write code.
2. **Structured indexes are de-facto database tables.**
   `library/manifest.json` and `people/manifest.json` aggregate per-entity
   `meta.json` files into a single index. They're regenerated on
   `Library.rebuild_index()` / `People.rebuild_manifest()`. Today the
   manifest can drift from the per-entity files if a write fails halfway.
   This is the classic file-store-pretending-to-be-a-DB hazard.
3. **Personal-therapy use needs longitudinal queries.** Liam plans to
   migrate his robo-therapist data into adult mode and use claudia as
   his primary therapy tool (T-NEW-H). Questions like "when did this
   theme first appear?" or "show me all sessions tagged X" require
   scanning every JSONL transcript. Today that's `n × full-file read +
   parse`. At ~100 sessions it's slow; at ~1000 it's untenable.

## What's actually on disk

| Artifact | Path | Shape | Access pattern | Query needs |
|---|---|---|---|---|
| Session transcripts | `/data/sessions/{id}.jsonl` | Append-only event stream (header, message, event, tool_use, tool_result records) | Append-once during session, read-many on review/audit/messages-poll | Full scan to find sessions matching themes, time ranges, mood |
| Audit precis | `/data/session-logs/{id}.md` | Markdown, one per session | Write-once at audit completion, read-tail in subsequent context-loader assembles | Tail-newest-N is the hot path; full search rare |
| Audit sidecars | `/data/audit-sidecars/{id}.json` | Structured AuditorReport, one per session | Write-once at audit completion, read on `/session/{id}/review` | Lookup by session_id only |
| Mood log | `/data/context/mood-log.jsonl` | Append-only, one record per `/session/{id}/mood` POST | Append + read-all for sparkline rendering | Recent-window queries (last 30 days for sparkline) |
| Library entries | `/data/library/{doc_id}/{meta.json,verification.json,raw.{ext},extracted.txt}` | One folder per doc + root manifest.json | Insert via `create_doc`; read by `read_document` tool; index regenerated on every mutation | Title/text grep (search_documents tool); chronological list |
| People entries | `/data/people/{person_id}/meta.json` + root manifest.json | One folder per person + root manifest | Insert/update via /people routes + auditor `people_updates`; read by lookup_person/search_people tools | Name/alias lookup; full roster for context-pack rendering |
| Application feedback | `/data/app-feedback.md` | Single growing markdown file | Append at audit completion; read-tail for context | Full text the model sees |
| Profile | `/data/profile.json` | Single Pydantic-validated JSON | Read at boot, written by /admin and the wizard | None |
| Setup state | `/data/.setup_state.json` | Working state during /setup wizard, deleted on completion | Read+write during wizard | Internal only |
| Setup marker | `/data/.setup_complete` | Existence check | Boolean | Internal only |
| Parent display name | `/data/parent_display_name.txt` | Single line, mutable override of Helm default | Read every request via context_processor, written by /setup + /settings | Internal only |
| Kid passphrase | `/data/.credentials/kid_auth.json` | Argon2id hash + salt, mode 0600 | Verify on /login | Internal only |
| Kid session tokens | `/data/.credentials/kid_sessions.json` | token → display_name mapping with TTL | Read on every request, write on login/logout | Lookup by token only |
| Google OAuth tokens | `/data/.credentials/google_oauth_token.json` | OAuth refresh + access tokens | Read on every Google API call, write on refresh | Internal only |
| Document blobs | `/data/library/{doc_id}/raw.{ext}` | Original PDF, image, doc bytes | Write-once on ingest, read by extractor | None — blob storage |
| Kid attach staging | `/data/kid-attach-staging/{tmpname}` | Ephemeral upload buffer, deleted in `finally` | Write+immediate-read, then delete | None |

## The three options

### Option A — Stay with files

**Pros:** No migration. `kubectl exec; ls` works. Backup is a tarball.
JSONL is human-readable and git-grep-able. Zero new dependencies.

**Cons:** No transactional safety — half-written files on crash, manifest
drift on partial writes. Concurrency depends on filesystem semantics
(undefined across NFS / overlayfs combinations). Indexes (manifest.json)
are computed from disk scans, not maintained incrementally. Cross-record
queries require globbing every file. As the data shape grows, the
ad-hoc proliferation of single-file stores
(`parent_display_name.txt`, `.setup_state.json`, `.setup_complete`,
`.credentials/*.json`, `audit-sidecars/`, `app-feedback.md`,
`mood-log.jsonl`) becomes hard to reason about as a coherent state
surface.

### Option B — SQLite for structured + files for blobs

**Pros:** ACID writes (no half-written rows). Real queries with indexes
(SQLAlchemy + alembic for migrations). Single file on the same PVC, no
new infrastructure, no StatefulSet, no operator. Backup is still a
single file (the .db, plus the blob tree). One round-trip to read a
session vs glob+parse JSONL. Atomic transactional updates across
sessions+events+people in one shot. Schema migrations are a solved
problem.

**Cons:** Migration of existing v0.6.x JSONL data (one-shot script;
mock data is small, real data is small too). `read_document` and
`library_pipeline` are written against file paths and need a small
abstraction layer. SQLite's single-writer lock means only one writer
at a time — fine for FastAPI single-pod, brittle for multi-pod
(which is not in scope today).

### Option C — Postgres

**Pros:** Multi-pod write concurrency. Cross-deploy queries (e.g. "all
sessions across both Liam's and Jasper's instances"). Existing
operator infra at coopernetes (CNPG) makes provisioning cheap.

**Cons:** Heaviest lift. Separate StatefulSet. Backup story is
operator-mediated. No upside today — neither use-case requires
multi-pod or cross-deploy.

## Recommendation

**Move structured artifacts to SQLite. Keep filesystem for blobs.**

In phase order (each is a separate v0.7.x sub-branch):

1. **`/data/claudia.db` for sessions, events, audit sidecars, mood log,
   app feedback.** These are the highest-churn append surfaces. JSONL
   transcripts become rows in `events` keyed by session_id +
   ordinal — same content, indexed access. Audit sidecars become a
   `audit_reports` table with one row per session_id. Replaces session
   header + JSONL logic in `app/storage.py`.
2. **People + library manifests in SQLite.** Per-entity `meta.json`
   stays on disk for the human-readable / kubectl-exec story but the
   manifest moves to a `library_docs` / `people` table that's the
   authoritative index. Drop the `rebuild_index` / `rebuild_manifest`
   functions — index is now ACID-maintained.
3. **Profile, setup state, parent name, kid auth → tables.** Each
   of these tiny ad-hoc files becomes a single-row table. The
   patchwork goes away.
4. **Blobs stay on disk.** `library/{id}/raw.{ext}` and the kid-attach
   staging dir keep the filesystem layout. SQLite stores the path,
   not the bytes.

Defer Postgres to whenever (if ever) multi-pod or cross-deploy queries
become real requirements. Today neither is on the horizon.

## What this DOESN'T solve

- **Encryption at rest** (Step 11 / v1.5). SQLite supports
  `sqlite-cipher` (AES-256, transparent encryption) but the kid mode
  per-passphrase + parent break-glass envelope pattern from `docs/design.md`
  is per-record granular. SQLite-level encryption is whole-DB. Step 11
  needs a separate decision: SQLCipher with per-row encrypted blobs vs
  a key-wrap layer above plaintext SQLite. Don't pre-commit here.
- **The "read it with kubectl exec" reflex.** SQLite is `sqlite3
  /data/claudia.db ".tables"; .schema sessions; SELECT * FROM events
  WHERE session_id='X' ORDER BY ord;`. Different ergonomics than
  `cat /data/sessions/X.jsonl`. Document this in the runbook.
- **Backup strategy.** v1 backup is `tar /data`. SQLite + blobs is
  still `tar /data` (the .db is one file on the same PVC) but a hot
  backup needs `sqlite3 backup .db /backup/dest.db` to be safe under
  concurrent writes. Add to the runbook.

## Implementation sketch (deferred — v0.7.x)

The minimum viable migration:

```python
# app/db.py (new)
import sqlite3
from contextlib import contextmanager

DB_VERSION = 1

@contextmanager
def connect(data_root: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(data_root / "claudia.db")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()

def migrate(data_root: Path) -> None:
    """Idempotent. Creates tables if missing, runs forward migrations."""
    with connect(data_root) as db:
        db.executescript(SCHEMA_V1)
        # ...
```

```sql
-- Schema v1 (sketch)
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL,
  mode TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_sha TEXT,
  title TEXT
);
CREATE TABLE events (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  ord INTEGER NOT NULL,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,         -- 'message' | 'event' | 'tool_use' | 'tool_result'
  payload TEXT NOT NULL,      -- JSON
  PRIMARY KEY (session_id, ord)
);
CREATE INDEX events_kind_idx ON events(kind);
CREATE TABLE audit_reports (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  written_at TEXT NOT NULL,
  report_json TEXT NOT NULL
);
CREATE TABLE mood_log (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  ts TEXT NOT NULL,
  regulation_score INTEGER NOT NULL,
  PRIMARY KEY (session_id, ts)
);
-- (library, people, profile, setup, kv-singleton tables follow)
```

The migration plan:

1. **Land schema + dual-write phase.** Every storage write goes to
   BOTH the existing files AND SQLite. A read still goes to files. CI
   tests assert SQLite reflects file state.
2. **Switch reads to SQLite.** Files become append-only audit trail
   for one release. Tests confirm parity.
3. **Drop file writes.** Files stop being touched. They remain on
   disk indefinitely as a one-shot historical export.
4. **Re-evaluate Postgres** if multi-pod or cross-deploy queries
   become real requirements.

Cost estimate: ~4 days of CC-time per phase. Total ~2 weeks for the
full migration to land.

## Decision marker

This doc is a recommendation, not a commitment. The next step is
either (a) the user explicitly approves this approach and we open a
v0.7.x branch, or (b) an alternative is preferred (e.g. straight to
SQLCipher to avoid a second migration at v1.5; or just delete this
doc and stay with files indefinitely).
