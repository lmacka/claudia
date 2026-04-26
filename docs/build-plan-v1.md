# claudia v1 build plan

> Status: APPROVED — produced by `/plan-design-review` + `/plan-eng-review`
> on 2026-04-26 against the kid-chat screen. Captures the v1-shape decisions
> that came out of those reviews, including the major architectural deferral
> of encryption to the end of the queue. Encryption deferral executed in
> commits `0050be3` (delete) → `f912165` (schema) → `7196aa6` (copy) →
> this commit (docs).
>
> This supersedes the build-order section in `CLAUDE.md` for v1.

## What changed from the design doc

### 1. Action prompts removed from kid-chat

The 7-button "what's going on?" panel (am I about to make this worse, etc)
is **dropped entirely** for v1. Reason: user override — "they're annoying."

Kid-chat opens with a varied greeting only. Companion-kid prompt holds the
greeting variation table (~5 greetings, picked per request).

**Departure from design doc.** The doc treats action prompts as a core
autism-affordance for conversation-starting. Bridge-tool ethos wins:
let Jasper drive.

### 2. Mood widget removed (start AND end of session)

No 1–10 slider anywhere in kid-mode. Mood is felt, not measured.

Auditor doesn't emit a `mood_signal` field in v1. Greeting just rotates plain.

**Departure from design doc.** Doc specified mood-at-start-and-end as a
ritual. Removed for the same "stop widgetising feelings" reason as #1.

### 3. Crisis banner: always-visible footer REMOVED

The persistent bottom banner with AU hotlines is dropped from steady-state
UI. Reason: user override — "too much."

**Replaced by two separate behaviours:**

- **Reactive (when classifier fires)**: Claudia replies inline with named
  hotlines, encourages the kid to call one, and recommends one based on
  what's known about Jasper (age, AU location, recent themes from precis-stack).
- **Discoverable (always available)**: `···` menu in topbar gains a
  "real people who can help" item. Two taps from anywhere. Lists all four
  AU hotlines + one-line context.

**Departure from design doc.** Doc specified persistent crisis footer as a
safety floor element. Replaced with the two-channel model above. Safety
floor narrowed but not removed: the safety classifier (Haiku) is still
non-disableable, AU hotlines still hardcoded, crisis is still always
reachable — just not always shouting.

### 4. Encryption feature set DEFERRED to end of queue

This is the big one. All kid-mode AES-GCM / KEK / break-glass / encrypted-
session-log work is **stood down for v1**. Sessions store as plaintext
JSONL on `/data` like adult mode.

**Already-shipped code DELETED** (commits 6c, 6d): `app/crypto.py`,
`app/session_keys.py`, `app/templates/break_glass_envelope.html`,
`tests/test_crypto.py`. Pre-deletion SHA tagged as
`crypto-snapshot-pre-defer`. Step 11 restores via:

```bash
git checkout crypto-snapshot-pre-defer -- \
  app/crypto.py \
  app/session_keys.py \
  app/templates/break_glass_envelope.html \
  tests/test_crypto.py
```

…then re-wires the call sites in `app/main.py` (the diff is at the
deletion commit), restores Argon2 + cryptography deps in `pyproject.toml`,
re-pins the schema to const, and adds the plaintext→encrypted migration
for sessions accumulated during v1.

**Cascade of simplifications this enables for v1:**

- **D2 reverses**: kid-mode auditor goes back to BackgroundTask (non-blocking).
  No cookie-keyed DEK access constraint.
- **Session-end UX**: no "wrapping up..." wait screen. Kid taps "end session"
  → immediate redirect → auditor runs server-side.
- **Premise 4 collapses**: encrypted full session-log + unencrypted precis
  split goes away. Both are plaintext. `/admin/review` reads either directly.
- **Two-role auth STAYS** (`/` for kid, `/admin` for parent) but as access
  control only — no cryptographic meaning. Parent can read sessions
  directly without a break-glass envelope.

**New step at end of queue (was not on roadmap):**
Step 11 restores encryption. See "v1 Build Sequence" below.

### 5. Other kid-chat decisions (mechanical)

- **Empty state**: rotating greeting + rotating composer placeholder
  (placeholder IS the prompt). Falls quiet on first keystroke. No widget chrome.
- **OCR failure**: same dashed tool-card as success, with retry/describe
  inline buttons. Original image still discarded post-OCR (safety floor unchanged).
- **Send / attach / paste failures**: one default pattern — muted "didn't
  go through — try again?" chip directly under the failed item, tap-to-retry.
- **Topbar**: just "claudia" wordmark + `···` menu. No session #, no time,
  no "last chat: yesterday" metadata (engagement-bait risk).

## Safety-floor implications

Documented in `docs/safety.md` (v1 dev-mode section, top of file).

- Kid session content sits plaintext on the parent's cluster PVC. Defensible
  because Helm release is single-tenant per family and the cluster is the
  parent's homelab; not defensible for any other threat model.
- First-chat banner copy already updated to "this is yours, but not secret"
  in `app/templates/kid_login.html`.
- `chart/values.schema.json` const constraints stay for haiku_classifier,
  write_tools_disabled, no_anthropomorphism. `kid.encryption.enabled`
  relaxed to `default: true` (warning in description) — re-pinned to const
  at v1.5.

## v1 Build Sequence

| # | Step | Status | Notes |
|---|------|--------|-------|
| 1 | Repo + chart skeleton + CI + examples | ✓ done | commits up to `b0eae8e` |
| 2 | Adult mode parity | ✓ done | `ff84e0c` |
| 3 | Library + people + extractors | **next** | per `docs/library-people-plan.md`. Substrate. |
| 4 | Three-stage setup wizard (adult + kid) | depends on 3 | |
| 5 | Memory-diff review screen | depends on 3 | |
| 6 | Kid-mode persona + safety + two-role auth + admin routes | ✓ done (encryption parts dormant) | 6a–6f shipped |
| 7a | Port wireframe `style.css` + 5-theme system into `app/static/` | new | single PR; unifies tokens |
| 7b | Re-skin existing templates against variant-C language | new | home, chat, login, admin/home, admin/review |
| 7c | Build missing templates | new | `/library`, `/people`, `/memory-diff`, `/settings`, `/setup`x2 |
| 7d | A11y baseline (44px targets, ARIA, focus-visible, contrast) | inline with 7b/7c | |
| 8 | OCR-discard kid attachment flow + people inline-prompt | depends on 3 | |
| 9 | README + first-deploy walkthrough | | |
| 10a | Red-team scenario fixture format | ship-blocker | YAML schema for {input, expected_classifier_fire, expected_judge_score, category} |
| 10b | Write 50 scenarios | ship-blocker | 10 each across 5 categories from `docs/safety.md` |
| 10c | CI pytest harness | ship-blocker | runs scenarios on PRs touching `app/prompts/*` / `app/safety.py`, fails on regression, ~$0.50/run |
| 11a | Restore crypto code from `crypto-snapshot-pre-defer` tag | end of queue | one `git checkout` |
| 11b | Re-wire call sites in `main.py` + restore deps in `pyproject.toml` | depends on 11a | diff is the inverse of commit `0050be3` |
| 11c | Sync auditor (revert from BackgroundTask), 'wrapping up...' UI | depends on 11b | reverts the cascade noted in section 4 above |
| 11d | Plaintext → encrypted session migration | depends on 11b | reads accumulated v1 plaintext JSONL, derives DEK from new passphrase, re-writes encrypted |
| 11e | Re-pin schema (`kid.encryption.enabled: const: true`) + remove dev-mode warnings from chart README + first-chat banner | depends on 11b–11d | schema migration; bumps chart minor version |
| 12 | Ship Jasper's instance | | |

## Wireframe debt (to clean up under T6)

These wireframes are now invalid and will mislead future readers:

- `docs/wireframe/chat-kid.html` — has action chips, mood slider, crisis banner that all got removed
- `docs/wireframe/kid-firstchat.html` — likely also references chips
- `docs/wireframe/setup-kid-*.html` — verify the kid setup flow doesn't promise encryption-mediated confidentiality
- The "fully confidential" framing wherever it appears

Either regenerate (when AI designer + verification ready) or annotate inline
with `<!-- v1: this element removed, see docs/build-plan-v1.md -->`.

## Open TODOs introduced by this plan

Captured in `TODOS.md`:

- **T-NEW-A** — v1.5 mood_signal classification + conditional check-in.
  Add once Jasper-shaped data is available to tune thresholds.
- **T-NEW-B** — Wireframe regeneration for `chat-kid.html`, `kid-firstchat.html`,
  and any setup wireframes that reference removed elements.
- **T-NEW-C** — ✅ done in this commit batch (v1 dev-mode section in
  `docs/safety.md`, banner copy in `kid_login.html`, prompt updates in
  `auditor-kid.md` + `companion-kid.md`).

## Source

- Mockup variants: `~/.gstack/projects/claudia/designs/kid-chat-20260426/`
- Approved variant: C — "Tool with Chrome"
- Detailed kid-chat decisions: `~/.gstack/projects/claudia/designs/kid-chat-20260426/plan.md`
