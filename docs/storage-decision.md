# Storage decision — files vs SQLite vs Postgres

> Status: shipped. Captures the architectural rationale for the SQLite +
> filesystem split now in use.

## Why we moved off pure files

v1 chose files for "convenience and grep-ability." Three pressures showed
up that v1 didn't fully anticipate:

1. **Persistence-of-state surfaces are growing.** The session token
   in-memory dict was replaced by a file-backed store to survive pod
   restart. Audit-sidecar JSONs, parent-display-name overrides, and the
   setup-wizard working state all became their own ad-hoc files with
   bespoke read/write code.
2. **Structured indexes are de-facto database tables.**
   `library/manifest.json` and `people/manifest.json` aggregated
   per-entity `meta.json` files into a single index. Manifests could
   drift from the per-entity files if a write failed halfway. Classic
   file-store-pretending-to-be-a-DB hazard.
3. **Personal use needs longitudinal queries.** Questions like "when did
   this theme first appear?" or "show me all sessions tagged X" require
   scanning every JSONL transcript. At ~100 sessions it's slow; at ~1000
   untenable.

## What's on disk

| Artifact | Path | Shape | Access pattern |
|---|---|---|---|
| Sessions + events + messages | `/data/claudia.db` (SQLite tables) | Append-only event stream | Append during session, read on review/audit |
| Audit reports | `/data/claudia.db` (`audit_reports` table) | Structured AuditorReport per session | Write at audit completion, read on `/session/{id}/review` |
| Mood log | `/data/claudia.db` (`mood_log` table) | One record per `/session/{id}/mood` POST | Append + read-recent for sparkline |
| App feedback | `/data/claudia.db` (`app_feedback` table) | Auditor-extracted notes | Append at audit completion, read-tail for context |
| Library entries | `/data/claudia.db` (`library_docs`) + `/data/library/{doc_id}/{raw.ext,extracted.txt,verification.json}` | Metadata in DB; bytes + extracted text on disk | Insert via `create_doc`; read by tools; tag/search via DB |
| People entries | `/data/claudia.db` (`people` table) + `/data/people/{person_id}/notes.md` | Metadata in DB; freeform notes on disk | Insert/update via /people routes + auditor; tools query via DB |
| Singletons | `/data/claudia.db` (`kv_store` table) | parent_display_name, google_enabled, setup state, runtime credential overrides | Tiny key-value reads/writes |
| Auth + sessions | `/data/.credentials/{auth.json,sessions.json}` | Argon2id hash + token map, mode 0600 | Verify on /login; cookie lookup per request |
| Google OAuth tokens | `/data/.credentials/google_oauth_token.json` | OAuth refresh + access tokens, mode 0600 | Read on every Google API call, write on refresh |
| Document blobs | `/data/library/{doc_id}/raw.{ext}` | Original PDF, image, doc bytes | Write-once on ingest, read by extractor |

## The three options considered

### Option A — Stay with files (rejected)

**Pros:** No migration. `kubectl exec; ls` works. Backup is a tarball.
JSONL is human-readable and git-grep-able. Zero new dependencies.

**Cons:** No transactional safety — half-written files on crash, manifest
drift on partial writes. Concurrency depends on filesystem semantics
(undefined across NFS / overlayfs combinations). Indexes computed from
disk scans, not maintained incrementally. Cross-record queries require
globbing every file. The ad-hoc proliferation of single-file stores
becomes hard to reason about as a coherent state surface.

### Option B — SQLite for structured + files for blobs (chosen)

**Pros:** ACID writes (no half-written rows). Real queries with indexes.
Single file on the same PVC, no new infrastructure, no StatefulSet, no
operator. Backup is still a single file (the .db, plus the blob tree).
One round-trip to read a session vs glob+parse JSONL. Atomic
transactional updates across sessions+events+people in one shot. Schema
migrations idempotent forward-only via versioned blocks tracked in a
`_schema_version` row.

**Cons:** Migration of existing JSONL data (one-shot script; data is
small). SQLite's single-writer lock means only one writer at a time —
fine for FastAPI single-pod, brittle for multi-pod (not in scope).

### Option C — Postgres (deferred)

**Pros:** Multi-pod write concurrency. Existing operator infra at
coopernetes (CNPG) makes provisioning cheap.

**Cons:** Heaviest lift. Separate StatefulSet. Backup story is
operator-mediated. No upside today — single-pod single-tenant doesn't
need it.

## What stayed on the filesystem

- **Document blobs.** `library/{id}/raw.{ext}` keeps original bytes;
  SQLite stores the path, not the bytes. Same for extracted text and
  verification JSON.
- **People notes.** `people/{id}/notes.md` is freeform markdown; metadata
  is in the DB. Kept on disk so the user can hand-edit via `kubectl exec`
  if needed.
- **Credentials.** Auth hash, session token map, OAuth refresh tokens
  stay in `.credentials/*.json` with mode 0600. Could move into the DB
  later behind a key-wrap layer; not blocking.

## Future: encryption at rest

SQLite supports `sqlite-cipher` (AES-256, transparent whole-DB
encryption). If at-rest encryption becomes a requirement, that's the
shape: SQLCipher with a per-deploy KEK derived from a Helm Secret.
Per-record encryption is more flexible but more work; not justified for
a single-tenant family-tool.

## Backup story

`tar /data` captures everything (the .db + blobs + credentials). For
hot backup under concurrent writes, use
`sqlite3 /data/claudia.db ".backup /backup/dest.db"` then tar the
result + the blob tree.
