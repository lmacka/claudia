# claudia — CLAUDE.md

> Auto-loaded by Claude Code when you `cd` into this repo.
> READ THIS BEFORE DOING ANYTHING.

## QA testing — DO NOT SKIP

Before claiming you tested anything, read [`docs/qa-protocol.md`](docs/qa-protocol.md)
in full. The 2026-04-30 session shipped v0.6.0 with five bugs that a real
browser would have surfaced in minutes (theme picker doesn't apply across
pages, hardcoded "your dad" copy that's tone-deaf in a step-parent family,
Envoy upstream-reset on session end as the kid, no Enter-to-send / Shift-
Enter-for-newline, no auto-scroll on new messages). The protocol exists
because a green status code is not a passing test. Drive the actual flow
in `gstack browse`, press the actual keys, read the actual DOM. The list
of bugs the next session inherits is in the protocol file under "Open
bugs from the v0.6.0 ship" — start there.

## What you're looking at

`claudia` is a **fork of robo-therapist** (`~/git/lmacka/robo-therapist`),
re-rolled into a self-contained Helm-deployable companion app for autistic
adults and parents of neurodivergent kids. Forked 2026-04-26 after a
`/office-hours` session that produced:

- **`docs/design.md`** — the canonical design doc (~770 lines, spec-reviewed,
  status APPROVED with reviewer concerns addressed inline). **Read this
  first.** Everything else flows from it.
- **`docs/wireframe/`** — clickable HTML mockup of every screen (12 pages +
  shared CSS). `index.html` is the map. Reflects locked UX decisions: dark
  + pastel themes, 3-stage setup wizard, mood at start AND end, memory
  diffs, OCR-discard kid attachments, two-role admin model.
- **`docs/library-people-plan.md`** — the ~900-line library/people/extractors
  spec. Lifted from a parallel design doc; retargeted to claudia per
  Premise 5.

## Where you are (current state, v0.8.6)

Both deploys live on `ghcr.io/lmacka/claudia:0.8.6`:
- `claudia.coopernetes.com` — adult mode (Liam)
- `claudia-jasper.coopernetes.com` — kid mode (Jasper)

Repo skeleton is fully fleshed out:
- `app/` — lifted from robo-therapist + extensively built out. 416 tests
  passing, 52 skipped (libreoffice-gated extractor tests + redteam suite
  gated on `RUN_REDTEAM=1`). Run `bash scripts/preflight.sh` for the full
  CI-equivalent check.
- `app/prompts/` — `companion-adult.md`, `companion-kid.md`,
  `auditor-adult.md`, `auditor-kid.md`, `meta-audit.md`, `handover.md` all
  written (no stubs left).
- `chart/templates/` — deployment, service, ingress, httproute, pvc,
  _helpers all in place. `examples/adult-values.yaml` and
  `examples/kid-values.yaml` both render and pass schema validation.
- `.github/workflows/` — `test.yml`, `image.yml`, `chart.yml`,
  `redteam.yml` all wired up. Tag `vX.Y.Z` triggers image + chart push.
- `docs/` — `design.md`, `build-plan-v1.md`, `library-people-plan.md`,
  `safety.md`, `qa-protocol.md`, `storage-decision.md`, `wireframe/`.

## What to do next

**v1 build sequence** — see `docs/build-plan-v1.md` for the canonical
status table. Steps 1–8 shipped. Currently outstanding:

- **Step 9** — README + first-deploy walkthrough (not started; gated by
  step 10c green)
- **Step 10** — Red-team CI suite (ship-blocker)
  - 10a/b — `tests/test_redteam.py` exists but the scenario file may not
    have all 50 hand-curated cases yet; verify before claiming done
  - 10c — workflow wired (`.github/workflows/redteam.yml`); confirm it
    actually fails on regression, not just runs green
- **Step 11** — Encryption restore from `crypto-snapshot-pre-defer` tag
  (end of queue, multi-step 11a–11e)
- **Step 12** — Original "Ship Jasper's instance" — already happened at
  v0.5.0; the spec entry is left in the plan as a milestone marker

**Day-to-day mode (since v0.5.0):** debugging-passes. User exercises a
release in the browser as Liam or Jasper, reports bugs inline mid-chat,
loop is `/investigate or direct fix → preflight → push → wait CI → tag
v0.X.Y → bump coopernetes deployments → /healthz`. The `## QA testing`
section above is load-bearing here — green status code is not a passing
test, drive the actual flow.

## Active TODOs (deferred from v1)

See `TODOS.md` for full context. Captured during /plan-eng-review:
- T1 — people-prompt threshold tuning
- T2 — adult attachment toggle
- T3 — OCI vs Pages chart channel
- T4 — v1.5 longitudinal harm patterns
- T5 — OSS governance / handoff
- T6 — wireframe-implementation parity rule

## Operating model

- **Single git repo.** No two-repo split like robo-therapist had with
  coopernetes. App + chart + CI all here.
- **GitHub remote is configured.** `origin =
  git@github.com:lmacka/claudia.git` (PUBLIC). `gh auth status` is
  authenticated as `lmacka`. Don't ask to `gh repo create`.
- **He has authorised commits AND pushes without asking.** Both rules
  overridden ("override the commit rule for this project, I want to
  work fluidly" and "each iteration I want you to publish for me to
  test"). Make commits as work progresses; push to `origin/main` after
  every landed batch (a step or sub-step). Tag a release (`v0.X.Y`)
  when the batch is worth user testing — `image.yml` and `chart.yml`
  workflows trigger on `v*.*.*` and publish to ghcr.io.
- **Still ASK before destructive ops.** Force-push, branch delete, tag
  delete, force-push to main never OK without explicit instruction.
- **Commit style:** minimal messages, no emojis, no Co-Authored-By,
  no signatures, under 200 chars title, body explains the why.
- **Run preflight before push.** `bash scripts/preflight.sh` runs the same
  checks CI runs (ruff, pytest, helm lint, helm template adult+kid+gateway,
  schema-floor rejections). Both CI and local sessions invoke this single
  script so they cannot drift. **Do not push to main without a green
  preflight** — `uv run pytest` alone is not sufficient. CI failed silently
  for v0.2.0 → v0.3.0 because no one ran the helm checks locally; this
  policy exists to prevent that recurring.
- **Verify CI before tagging a release.** `gh run list --limit 5` after
  push — the `test` workflow on `main` must be green before you tag
  `vX.Y.Z`. Image + chart workflows fire on tag, but a broken test job
  means the tag is shipping un-verified code.
- **Bridge-tool ethos still applies.** Don't make claudia more engaging for
  engagement's sake. Removed UI ceremony (per robo-therapist iterations 7-8)
  but the principle stands.

## Things that are different from robo-therapist

- **Two modes.** `mode: adult|kid` Helm value, immutable post-install.
- **Two-role auth in kid mode.** Kid logs into `/`, parent-admin into
  `/admin` with separate password. See `docs/design.md` "Two-role auth".
- **Encryption in kid mode.** AES-GCM with kid passphrase + parent
  break-glass envelope. See `docs/design.md` "Encryption (kid mode only)"
  for the double-wrapped DEK pattern.
- **Auditor runs synchronously at kid session end** (regression from
  robo-therapist iter-5 non-blocking pattern) because background tasks
  have no key access after cookie expiry. Adult mode keeps the
  non-blocking pattern.
- **Auditor writes two outputs** (encrypted full session-log + unencrypted
  audit-precis-with-themes-only) per Premise 4 to make `/admin/review`
  meta-audit possible without violating the kid's confidentiality
  contract. See design doc.
- **Single shared `/people` store** between roles in kid mode (parent
  manages at `/admin/people`, kid proposes via inline `remember X?`
  prompt). Per the user's correction mid-design — classmate names
  aren't confidential.
- **Crisis tripwire returns** for kid mode (was removed from robo-therapist
  in iteration 7 because Liam was the only user). AU hotlines hardcoded.
- **Kid-mode safety floor is non-disableable** via `chart/values.schema.json`
  schema constraints. Do not relax these.
- **CI red-team suite is a v1 ship-blocker** (50 hand-curated scenarios).
  See `docs/design.md` OQ8 for pass criteria. Build this before ship,
  not after. Document results in `docs/safety.md` (file doesn't exist
  yet — create when red-team work starts).

## Things that stay the same

- Sonnet 4.6 default, Haiku for the safety classifier.
- File-based context pack as the system prompt foundation.
- ReportLab for handover PDF (adult mode only).
- JSONL session storage on `/data` (no database).
- HTMX + Jinja + Pico.css (or hand-rolled CSS — wireframe uses no framework).
- `uv` for deps, `pytest` for tests.
- Background tasks for slow work (adult mode auditor still BackgroundTask;
  kid mode is the exception).

## When you're stuck

- **Unclear architecture choice?** Read `docs/design.md` first. Then
  `docs/wireframe/` if it's UX. Then `docs/library-people-plan.md` if it's
  about extractors / library / people store.
- **Want to see what robo-therapist did?** `~/git/lmacka/robo-therapist/`
  is still there (frozen by Premise 6 once claudia adult-mode hits parity,
  but readable indefinitely). Lift code, lift patterns.
- **Don't know how to do the encryption right?** This is one of the two
  load-bearing things the spec reviewer flagged. Read design doc
  "Encryption (kid mode only)" carefully. Don't improvise; the
  double-wrapped-DEK pattern is what was approved.
- **Anything safety-related in kid mode?** Default to the more cautious
  option. The framing is "good parent due diligence" not "commercial
  defensibility" but the floor still has to hold.

## Don'ts

- Don't relax the kid-mode safety floor schema constraints.
- Don't put chat content in pod logs.
- Don't make claudia more engaging for engagement's sake.
- Don't rebuild what robo-therapist already proved works (auditor, spine,
  handover PDF, tool-use loop). Lift, don't re-design.
- Don't commit anything from `/data/`, `staging/`, `library/`, `people/`,
  or `.credentials/` (in `.gitignore`).
- Don't force-push or delete branches/tags without explicit instruction.

## Health Stack

What `/health` runs. Mirrors `scripts/preflight.sh` so CI and local dashboards
agree on what "healthy" means.

- lint: `uv run ruff check app tests`
- test: `uv run pytest tests/ -q`
- helm-lint-adult: `helm lint chart/ --values examples/adult-values.yaml`
- helm-lint-kid: `helm lint chart/ --values examples/kid-values.yaml`
- helm-template-adult: `helm template claudia chart/ --values examples/adult-values.yaml`
- helm-template-kid: `helm template claudia chart/ --values examples/kid-values.yaml`
- helm-template-gateway: `helm template claudia chart/ --values examples/adult-values.yaml --set ingress.gatewayApi=true --set ingress.parentRef.name=internal-gateway` (must contain `kind: HTTPRoute`)
- schema-floor: `kid.safety.haiku_classifier=false` and `kid.safety.no_anthropomorphism=false` must both be rejected by `helm template`
- typecheck: SKIP (no mypy/pyright in pyproject)
- deadcode: SKIP (no vulture/knip configured)
- shell: SKIP unless shellcheck installed locally
