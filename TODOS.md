# claudia — TODOS

Captured during /plan-eng-review on 2026-04-26. These items were considered and
deliberately deferred from v1.

## T2 — Per-attachment discard toggle (was OQ6)

**What:** Per-attachment "discard original after OCR extraction" toggle.
Currently uploaded originals are kept on the PV.

**Why:** Some attachments are sensitive (medical reports, legal docs,
therapist notes) and the user may want to discard the original after extraction.

**Pros:** User-controllable privacy posture per upload.
**Cons:** Easy enough to remember; not blocking v1.

**Context:** Wireframe `library-adult.html` already shows a "Keep original" checkbox
slot — wire it up post-v1.

**Depends on:** v1 shipped.

## T3 — OCI vs Pages chart distribution channel (was OQ7)

**What:** Choose chart distribution channel before first ship: OCI artifact on
ghcr.io (default per design) or fallback to GitHub Pages helm repo if Liam keeps
the GitHub repo private longer.

**Why:** OCI requires public ghcr.io image; private repo means fallback path.

**Pros:** One-flag toggle; low-friction to swap.
**Cons:** Decision deferred to pre-ship moment.

**Context:** `chart.yml` workflow defaults to `helm push oci://ghcr.io/lmacka/charts/claudia`.
If Liam decides repo stays private, swap to `gh-pages` branch with `helm package` +
manual `index.yaml` update.

**Depends on:** Liam's GitHub repo visibility decision.

## T5 — OSS governance / handoff

**What:** "What this is NOT" doc + maintainer succession plan + provider-fallback
plan + migration/backup story.

**Why:** Plan is shaped around lmacka, AU defaults, Sonnet 4.6, k8s literacy, single-
family trust model. If Liam disappears (hit by a bus / loses interest / Anthropic
has a 6-month outage), there is no maintainer path, no provider fallback, no clear
"what this does NOT guarantee" safety boundary doc for parents inheriting the
project. Codex flagged in plan-review outside voice.

**Pros:** Serious for OSS positioning; "good parent due diligence" only holds if
there's an inheriting parent's path.

**Cons:** Large meta-work that doesn't ship code. v1 ship is the priority.

**Context:** Five sub-items:
1. `docs/SAFETY-BOUNDARY.md` — explicit "what claudia does NOT guarantee" + when to
   not use it (severe MH crisis = real human, not the bot).
2. `docs/MAINTAINER.md` — succession plan, contributor onboarding.
3. Provider abstraction: factor out `app/claude.py` into a generic LLM interface so
   Gemini/local-llama can be drop-in if Anthropic is down or pricing changes.
4. Backup/export: `kubectl exec` script that exports `/data` to a tarball + key
   material instructions.
5. Migration: schema_version on profile.json (already in v1 per D8) extended to all
   on-disk artifacts so v1 → v1.5 migrations can run cleanly.

**Depends on:** v1 shipped.

## T-NEW-D — `/people/import-from-spine` route + UI

**What:** Add the admin-one-shot route that reads
`context/04_relationship_map.md`, posts it through Sonnet with a strict
extraction prompt, and renders the proposed `{name, aliases, category,
relationship, summary, important_context}` records as a checklist for
review and commit.

**Why:** Captured during commit H (Step 3 / library-people-plan). Dead
code at first deploy because new users have no spine. Existing
robo-therapist users (i.e. Liam himself) have one and need this to seed
`/data/people/` cheaply.

**Pros:** One-click migration for the only user with a pre-populated
spine file. Saves manually re-typing ~12 records.

**Cons:** Pure migration helper; useless for new users. Adds Sonnet API
cost on a one-off operation that could be done via direct file editing.

**Context:** Pseudocode in `docs/library-people-plan.md` "Seeding from
04_relationship_map.md". Minimal v1 shape: read 04, POST to Sonnet,
return JSON of proposed records, render as checklist via HTMX, on
submit call `People.add` for each ticked entry.

**Depends on:** v1 shipped (or whenever Liam's instance migrates from
robo-therapist).

## T-NEW-E — Migrate `save_gmail_attachment` to library pipeline

**What:** Rewrite `app/tools/gmail.py:save_gmail_attachment_spec` to land
attachments as first-class library docs with `source: gmail_attachment`
via `library_pipeline.process_doc_creation`, instead of writing to the
legacy `uploads/` filesystem path.

**Why:** Captured during commit H. The library plan calls for this
explicitly: "save_gmail_attachment writes via Library.add_upload(
source='gmail_attachment')". Today the function still writes
plaintext-only to `uploads/`; `read_document` accepts the legacy path
via the fallback branch, so it still works.

**Pros:** Gmail attachments get extracted text + verification + people-
linking + supersede semantics like every other library doc.

**Cons:** ~30 LoC change but introduces a Library dependency in
gmail.py. Not blocking v1.

**Context:** TODO comment is in place at `app/tools/gmail.py:328`.
Wiring is straightforward: pass `state.library` + `state.extractor_registry`
into `save_gmail_attachment_spec` (mirror the pattern in `READ_DOCUMENT_SPEC`).

**Depends on:** Step 3 commit H landed (done).

## T-NEW-H — Full surface test pass before personal-therapy migration

**What:** Drive the full app surface in a real browser per
`docs/qa-protocol.md`. Cover: setup wizard, chat composer (Enter / Shift+Enter
/ auto-scroll), session end → memory-diff /review cards, library upload + extract
flow, people add/edit, /report PDF generation, theme persistence across pages,
auditor BackgroundTask completion (no Envoy resets). Fix every bug found
atomically.

**Why:** Liam plans to migrate his personal robo-therapist data into
claudia and use it as his primary therapy tool. Needs to work reliably
*before* that migration starts — not after.

**Context:** Use the qa-protocol checklist. Run mode = LIVE against
`claudia.coopernetes.com`.

## T-NEW-I — Reconsider files-vs-database for /data backing store

**What:** Decide whether the JSONL + filesystem-tree backing store
(`/data/sessions/*.jsonl`, `/data/library/*`, `/data/people/*.json`,
`/data/session-logs/*.md`, `/data/.credentials/*`) should migrate to
something more structured. Three candidate end-states:
1. **Stay with files** — accept the trade-offs, harden what's there.
2. **SQLite on the same PVC** — single-file embedded DB, ACID, queryable, no extra infra. Sessions/people/library-manifest as tables; raw documents stay on disk.
3. **Postgres** — separate StatefulSet, biggest lift, only justified if multi-pod write concurrency or cross-deploy queries are wanted.

**Why:** Files were chosen for v1 convenience. Three pressures push toward
reconsideration:
- The session persistence fix (commit `9f1ca9d`) was needed because in-memory dict didn't survive pod restart — a sign the file/in-memory line is moving.
- People + library now have indexes (`library/manifest.json`, `people/*.json`) that are de-facto DB tables, manually maintained.
- Adult-mode personal-therapy use (T-NEW-H goal) means longitudinal queries ("when did this theme first appear?", "show me all sessions tagged X") that are awkward against JSONL.

**Pros of moving to SQLite:** ACID writes (no half-written JSONL on crash),
real queries, schema migrations are a solved problem (alembic), single file
on the same PVC = no infra change, transactional precis-stack updates,
one round-trip to read a session instead of glob+parse.

**Cons:** Migration of existing v0.6.x JSONL data. `read_document` /
auditor-sidecar / library-extractor are all written against file paths;
some refactor. SQLite single-writer locks could be an issue if multiple
async tasks write concurrently (though FastAPI single-pod doesn't really
have that problem).

**Pros of staying with files:** No migration. `kubectl exec; ls` works.
JSONL is git-grep-able and human-readable. Backup is a tarball.

**Cons of staying:** Concurrency safety relies on filesystem semantics
that aren't guaranteed across NFS / overlayfs. Indexes drift. Queries
require full scans.

**Recommendation (initial):** SQLite for sessions + people + library manifest
+ session-keys + audit sidecars (the structured stuff). Keep filesystem for
raw documents (PDFs, images, originals) since they're blobs that don't need
DB-level transactionality. Defer Postgres unless multi-pod write contention
shows up.

**Depends on:** A focused design pass — write `docs/storage-decision.md`
listing the on-disk artifacts, their access pattern (read-many vs append-once),
their query needs, and the proposed SQLite/file split. Then a v0.7.x branch
to do the migration with shadow-write + back-fill before cutover.

## T-NEW-J — Split `app/main.py` into per-feature route modules

**What:** `app/main.py` is large (multi-thousand lines, ~50 route handlers). Split
into per-feature modules (`auth_routes.py`, `session_routes.py`,
`library_routes.py`, `people_routes.py`, `settings_routes.py`,
`setup_routes.py`) using FastAPI APIRouters mounted
from a slim `main.py` that handles lifespan + middleware + router
registration.

**Why:** Captured during /health tidy-up on 2026-05-04. The file is at
the size where every grep, every IDE jump, every code review pays a
tax. New developers (and Claude sessions) struggle to map the surface
area. Refactor cost amortises across every future read.

**Pros:** Easier navigation; per-feature test placement becomes obvious;
git blame stays meaningful per file; reduces merge conflicts when two
batches of work touch unrelated routes.

**Cons:** Pure refactor with risk — easy to drop a route, easy to break
a test that imports something from `app.main`. No user-visible benefit;
takes 2–4 hours of focused work to do safely.

**Context:** Best done in one sitting with the test suite as the
verification gate. Suggested order: extract the smallest groups first
(`/healthz` + `/metrics` + `/readyz` → health_routes; `/help` →
public_routes), confirm tests pass, then move bigger groups
(library, people, sessions, setup) one PR at a time.

**Depends on:** Nothing technical. Schedule when no other work is
in-flight against `main.py` so rebase pain stays bounded.
