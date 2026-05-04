# claudia — TODOS

Captured during /plan-eng-review on 2026-04-26. These items were considered and
deliberately deferred from v1.

## T1 — People-prompt threshold tuning (was OQ5)

**What:** "≥2 occurrences in OCR text" as the trigger for the inline `remember X?`
prompt is a guess based on no real-world data.

**Why:** Real Jasper-shaped use will tell us whether the threshold should be 1, 2,
3, or context-aware.

**Pros:** Calibrate to actual behaviour; reduce false positives or false negatives.
**Cons:** Trivially small parameter; not blocking.

**Context:** First v1 ship will populate enough data over a few weeks to tune. Look
for "names that triggered the prompt but the kid declined to add" vs "names the kid
mentioned 5+ times that never triggered" patterns.

**Depends on:** v1 shipped, ≥2 weeks of kid-mode use.

## T2 — Adult attachment toggle (was OQ6)

**What:** Per-attachment "discard original after OCR extraction" toggle in adult
mode. Currently adult mode keeps originals on the PV; kid mode discards.

**Why:** Some adult-mode attachments are sensitive (medical reports, legal docs,
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

## T4 — v1.5 longitudinal harm patterns

**What:** Detect dependency-formation patterns in kid mode: repeated
reassurance-seeking, compulsive return, theme repetition over multiple sessions,
late-night use frequency.

**Why:** Per-turn safety gates miss the slow failure mode (Codex flagged in
plan-review outside voice, /plan-eng-review D13). The "frontal-cortex prosthetic"
becoming the safest-feeling confidant is the Replika failure mode in family-tool
shape. v1 ships with nudges-only by user-decided risk acceptance; this TODO captures
the v1.5 follow-up.

**Pros:** Most-likely-to-mature kid-mode safety concern.
**Cons:** Requires months of real-world data to validate detection thresholds; risks
being either over-eager (false positives = paternalistic) or under-tuned.

**Context:** Sketch — a daily-cron Sonnet pass over recent precis-stack that flags
to /admin/review when patterns hit a threshold. Schema: detection rules in
`docs/safety.md` v1.5 section. Each pattern needs a CI red-team scenario.

**Depends on:** v1 shipped + ≥3 months of Jasper-shaped use.

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

## T7 — DESIGN.md formalisation (post-v1)

**What:** Run `/design-consultation` to formalise the existing intuitive design
system into a checked-in `DESIGN.md`.

**Why:** v1 ships with token shapes (`--bg`, `--panel`, `--accent`, etc.) defined in
`docs/wireframe/style.css` but no documented system. Future contributors and
implementers will guess at spacing scale, motion guidelines, typography
hierarchy. Captured during /plan-design-review Pass 5.

**Pros:** Onboarding aid; future-developer clarity; aligns with the wireframe-first
posture's spirit.

**Cons:** Doesn't ship code. v1 is the priority and inline `style.css` comments
buy the same effect at 1% the cost.

**Context:** The existing five themes already share token shapes consistently — the
work is description, not invention. Sections to capture: spacing scale (px-based
likely), typography hierarchy (post-D5 Inter at body / Inter Semibold at headings),
motion guidelines (prefers-reduced-motion respect, default transition timings),
component vocabulary (`.panel`, `.diff-card`, `.envelope`, `.crisis-banner`,
`.action-row`, `.mood-row`, `.tile`, `.btn`).

**Depends on:** v1 shipped; some real production styling done so we can write
about what works.

## T8 — Wireframe-deployment pipeline (mobile review)

**What:** Static-serve `docs/wireframe/` at `claudia.coopernetes.com` (or whichever
domain you prefer) so the test team can review wireframes on their phones.

**Why:** User-add during /plan-design-review D1 focus question. Test-team review
on phones is the right shape for this product (kid mode is overwhelmingly mobile).
Local file:// previews don't work for that audience; needing them to set up a
local Python server doesn't either.

**Pros:** Test team reviews on the device kids will actually use; auto-updates on
merge; cheap (static-file serving, ~5 MB).

**Cons:** Yet-another-deploy-target; needs ingress + certs; bikeshed-prone choice
of hosting (Helm chart? GitHub Pages? Existing nginx in coopernetes?).

**Context:** Two viable shapes:
- (a) Sub-chart in this repo: `chart/wireframe-preview/` deploys an nginx pod that
  serves `/docs/wireframe/`. Reuses claudia's chart infra. Auto-updates via image
  rebuild on tag.
- (b) GitHub Pages on the claudia repo. Auto-deploys on merge via Actions. Free,
  no cluster involvement, public URL.

(b) is simpler if the repo is going public; (a) is right if the repo stays private
and wireframes shouldn't leak. Decide pre-ship.

**Depends on:** Step 1 CI workflows landing.

## T6 — Wireframe-implementation parity check

**What:** Establish the rule: if a route changes meaningfully, the corresponding
`docs/wireframe/*.html` page is updated or removed.

**Why:** HTML wireframes will drift as Jinja templates evolve. Stale wireframes
mislead future reviewers and contributors. The wireframe was load-bearing for the
plan review; it should not become a misleading artifact.

**Pros:** Cheap process discipline; one rule.
**Cons:** Easy to forget without enforcement.

**Context:** Lightweight enforcement options:
- README rule + reviewer checklist
- CI step that diffs route-list-of-app vs file-list-in-docs/wireframe/ and warns
- Auto-generated wireframe-from-templates pass (more work; v2)

Start with the README rule and reviewer checklist. Escalate if it bites.

**Depends on:** v1 shipped (so we have the route surface stabilised).

## T-NEW-A — v1.5 mood_signal classification + conditional check-in

**What:** Auditor-kid emits a `mood_signal: positive | neutral | negative`
field. Companion-kid reads it from the precis-stack at session-start and
adds a one-line conversational check-in ("Hey. Last time it sounded rough
— how's today going?") only when the previous session was negative.

**Why:** Captured during /plan-design-review on 2026-04-26. v1 ships
greetings as a flat rotation with no conditional behaviour because we
have zero Jasper-shaped data to tune the negative-classification
threshold against. The "we noticed last time was rough" empathy moment
is real but premature.

**Pros:** One of the few empathy moments we kept after stripping mood
widgets and action chips; restores some of the design-doc intent without
re-adding UI chrome.

**Cons:** Threshold tuning needs real data; over-eager classification is
patronising; under-eager misses real bad days. v1 ships without it.

**Context:** Implementation sketch in `~/.gstack/projects/claudia/designs/kid-chat-20260426/plan.md`
under Pass 7A. Auditor prompt addition (`mood_signal` field) and
companion-kid prompt logic (read precis_stack[-1].mood_signal on session
open) are both small. The hard part is the classification heuristic in
the auditor prompt.

**Depends on:** v1 shipped + ≥4 weeks of Jasper-shaped use.

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

## T-NEW-F — Remove Gmail/Calendar integration from kid mode entirely; opt-in in adult mode

**What:** Stop registering `create_gmail_draft`, `create_calendar_event`,
`update_calendar_event`, `read_email`, `search_emails`, `list_calendar_events`,
`save_gmail_attachment` (and any future Google-write tools) in kid mode at the
registry level — not at the prompt level. In adult mode, default these to OFF
behind a Helm value (`adult.integrations.google.enabled: false`); the user
opts in per-deploy.

**Why:** Today `chart/values.schema.json:48-52` declares
`write_tools_disabled: const true` in kid mode, but `app/main.py:153-172`
registers Gmail/Calendar tools unconditionally. The schema is a lie. A
prompt-jailbreak in kid mode could send email or create calendar events on
the parent's account. Adult mode also shouldn't enable Google integrations
by default — most adult-therapy users don't want Claudia touching their
calendar.

**Pros:** Closes the schema/code gap. Removes a real safety footgun. Aligns
the default with what a new adult user actually wants. Lets adult and kid
mode evolve in parallel without "did you remember to gate this for kid?"
review burden — tools are gated at the registry, once.

**Cons:** Requires Helm value rename + migration note for existing adult
deploys (Liam's, Jasper's). Existing users who relied on Gmail/Calendar
in adult mode will need to flip the new flag.

**Context:** Implementation:
1. `app/main.py:_build_tool_registry`: branch on `cfg.is_adult AND cfg.adult_integrations_google_enabled` before registering each Google tool spec.
2. `app/config.py`: add `adult_integrations_google_enabled: bool = False`.
3. `chart/values.yaml`: add `adult.integrations.google.enabled: false` (default off).
4. `chart/values.schema.json`: drop the lying `write_tools_disabled: const true` block, OR keep it and have the registry actually honour it.
5. `companion-kid.md`: remove the lines listing Gmail/Calendar tools as available (lines 28-35) — they're not, and listing them invites attempts.
6. `examples/adult-values.yaml`: leave default off; document the opt-in path in README.
7. Tests: add `test_kid_mode_no_google_tools_registered` asserting the registry has no Google tools when `mode: kid`.

**Depends on:** Nothing. Should land before adult-mode testing starts so the
testing baseline reflects the secured posture.

## T-NEW-G — Fix the schema lies + dead config

**What:** Three small cleanups surfaced by the kid-vs-adult audit:
1. `no_anthropomorphism: const true` in `chart/values.schema.json` — prompt-only, not code-enforced. Either add a code gate (post-process companion-kid responses through a regex/classifier that rejects "I missed you" / "we"-as-relationship language) or drop the schema lie and document it as prompt-mediated.
2. `session_nudge_minutes` in `chart/values.schema.json` and `app/config.py` — defined, never read by any code. Delete or implement.
3. Audit all template `{% if claudia_mode == "kid" %}` checks (~10 templates: home, session_chat, setup/step{1,2,3}, settings, library, people, message_pair, messages_list) for the same shape — consolidate where possible.

**Why:** The kid-vs-adult audit found these as parallel-development risks. A
schema constraint that doesn't reflect code is worse than no constraint —
it gives false confidence to reviewers and sets a trap for future maintainers.

**Pros:** Removes false-confidence surfaces. Makes the schema actually trustworthy.
**Cons:** Anthropomorphism gate is non-trivial if implemented properly.
Probably ship as "drop the schema claim, document as prompt-mediated" for v1.

**Depends on:** T-NEW-F (which closes the biggest schema-lie); these are smaller follow-ups.

## T-NEW-H — Full adult-mode test pass before personal-therapy migration

**What:** Drive the full adult-mode surface in a real browser per
`docs/qa-protocol.md`. Cover: setup wizard, chat composer (Enter / Shift+Enter
/ auto-scroll), session end → memory-diff /review cards, library upload + extract
flow, people add/edit, /report PDF generation, theme persistence across pages,
auditor BackgroundTask completion (no Envoy resets). Fix every bug found
atomically; ship as v0.6.x patches.

**Why:** Liam plans to migrate his personal robo-therapist data into adult
mode and use claudia as his primary therapy tool. Adult mode needs to work
reliably *before* that migration starts — not after. Adult is also the
larger surface (library/people/report) so testing it first surfaces more
shared-surface bugs (theme, auth, setup) that kid mode also depends on.

**Pros:** Bugs found in adult shake out shared-surface issues that kid mode
inherits. Faster iteration loop than kid mode (no passphrase ceremony,
fewer safety gates to step around).

**Cons:** Doesn't directly verify kid-mode safety floor. Kid testing still
needed before any production hand to Jasper.

**Context:** Use the qa-protocol checklist. Run mode = LIVE against
`claudia.coopernetes.com`. Before testing starts, confirm T-NEW-F has
landed so the security posture being tested is the production-shape posture.

**Depends on:** T-NEW-F (Gmail/Calendar gating) + the v0.6.0 inherited bug
list (theme picker, kid_parent_display_name, Envoy reset, keyboard shortcuts,
auto-scroll) — fix those first so testing isn't constantly re-finding them.

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
- The kid-session persistence fix (commit `9f1ca9d`) was needed because in-memory dict didn't survive pod restart — a sign the file/in-memory line is moving.
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

## T-NEW-B — Wireframe regeneration after v1 design departures

**What:** Update or regenerate `docs/wireframe/chat-kid.html`,
`docs/wireframe/kid-firstchat.html`, and any setup wireframes that
reference removed elements (action chips, mood slider, persistent
crisis banner) or imply encryption-mediated confidentiality.

**Why:** Captured during /plan-design-review on 2026-04-26. The v1
build plan dropped the action prompts panel, the mood widget, and the
always-visible crisis banner. Stale wireframes will mislead future
implementers and reviewers. Per T6 (wireframe-implementation parity).

**Pros:** Wireframes stop lying; future plan reviews work against
accurate visual specs.

**Cons:** AI designer (gpt-image-1) needed for clean regeneration; or
hand-edit via inline annotations. Hand-edit is cheaper but uglier.

**Context:** Two viable shapes:
- (a) Inline `<!-- v1: this element removed, see docs/build-plan-v1.md -->`
  comments next to dropped elements. Cheap, ugly, easy to miss.
- (b) Regenerate the affected pages via the gstack designer (now
  unblocked, OpenAI org verified) using variant-C styling as the
  reference. Cleaner, longer.

Recommended (b) once Step 7b lands so the regenerated wireframes can
match the live app's visual language.

**Depends on:** Step 7b (template re-skin) — so wireframe and live app
use the same design language.

## T-NEW-J — Split `app/main.py` into per-feature route modules

**What:** `app/main.py` is 3442 lines / 127KB / 70 route handlers. Split
into per-feature modules (`auth_routes.py`, `session_routes.py`,
`library_routes.py`, `people_routes.py`, `settings_routes.py`,
`setup_routes.py`, `admin_routes.py`) using FastAPI APIRouters mounted
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
