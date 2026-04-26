# claudia — CLAUDE.md

> Auto-loaded by Claude Code when you `cd` into this repo.
> READ THIS BEFORE DOING ANYTHING.

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

## Where you start (next-session bootstrap)

The fork has been done. The previous session left the repo in this state:

- `app/` — lifted unchanged from robo-therapist. 51 tests passing
  pre-fork; **run `uv sync && uv run pytest tests/ -q` to confirm they
  still pass.** If they do, the lift is clean.
- `app/prompts/` — `companion.md` renamed to `companion-adult.md`,
  `auditor.md` renamed to `auditor-adult.md`. Empty stubs created for
  `companion-kid.md`, `auditor-kid.md`, `meta-audit.md` (these get filled
  in v1 step 6).
- `chart/` — skeleton with `Chart.yaml`, `values.yaml`, `values.schema.json`
  (with the kid-mode safety floor encoded as `const: true` constraints —
  do not relax these), `README.md`. **`chart/templates/` is empty** —
  needs deployment.yaml, service.yaml, ingress/httproute.yaml, pvc.yaml.
- `.github/workflows/` — empty. Needs test.yml, image.yml, chart.yml.
- `docs/` — design doc, wireframe, library-people-plan all in place.

## What to do next (v1 build order, from `docs/design.md`)

You are at **step 1** of the build order:

1. **Repo + chart skeleton + CI** ← partially done, finish:
   - Fill `chart/templates/` (deployment, service, ingress/httproute, pvc).
     Reference `~/git/lmacka/coopernetes/kubernetes/apps/robo/app/` for the
     working robo-therapist manifests but **rewrite for generic k8s** — do
     NOT inherit the Synology/coopernetes-specific assumptions.
   - Write `.github/workflows/test.yml` (pytest on PR), `image.yml`
     (docker build + push on tag), `chart.yml` (helm package + push to
     ghcr.io as OCI artifact).
   - Confirm `helm template chart/ --values examples/adult-values.yaml`
     and `examples/kid-values.yaml` both render without errors and pass
     schema validation.
2. Adult mode parity (lift app/, ship to Liam's cluster as a second deploy
   alongside robo-therapist for parallel-running validation).
3. Three-stage setup wizard.
4. Library + people from `docs/library-people-plan.md`.
5. Memory-diff review screen.
6. Kid mode persona + safety floor + two-role auth + `/admin` routes.
7. OCR-discard kid attachment flow.
8. Theme system + settings page.
9. README + first-deploy walkthrough.
10. Ship Jasper's instance.

**~4-5 weeks of CC-time to v1** per the design doc estimate.

## Operating model

- **Single git repo.** No two-repo split like robo-therapist had with
  coopernetes. App + chart + CI all here.
- **Local git is fine for now.** Liam will decide when to push to GitHub.
- **He has authorised commits without asking** ("override the commit rule
  for this project, I want to work fluidly"). Make commits as work
  progresses; one logical change = one commit. Still ASK before
  destructive ops or pushes.
- **Commit style:** minimal messages, no emojis, no Co-Authored-By,
  no signatures, under 200 chars title, body explains the why.
- **Tests always green before commit.** `uv run pytest tests/ -q`.
- **Never push to a remote without asking.** Local git is the default.
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

- Don't add a remote without asking.
- Don't relax the kid-mode safety floor schema constraints.
- Don't put chat content in pod logs.
- Don't make claudia more engaging for engagement's sake.
- Don't rebuild what robo-therapist already proved works (auditor, spine,
  handover PDF, tool-use loop). Lift, don't re-design.
- Don't commit anything from `/data/`, `staging/`, `library/`, `people/`,
  or `.credentials/` (in `.gitignore`).
