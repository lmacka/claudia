# claudia — CLAUDE.md

> Auto-loaded by Claude Code when you `cd` into this repo.

## QA testing — DO NOT SKIP

Before claiming you tested anything, read [`docs/qa-protocol.md`](docs/qa-protocol.md)
in full. A green status code is not a passing test. Drive the actual flow
in `gstack browse`, press the actual keys, read the actual DOM.

## What you're looking at

`claudia` is a self-contained Helm-deployable companion app — one Helm
release per user, single-tenant. The user customises it via the in-app
`/setup` wizard on first visit.

- **`docs/qa-protocol.md`** — QA testing protocol (required reading).
- **`docs/storage-decision.md`** — files vs SQLite rationale.
- **`docs/library-people-plan.md`** — library/people/extractors spec.

## Where you are (current state)

Source is at v0.9.x (`pyproject.toml` + `chart/Chart.yaml`). Live deploy
runs at `claudia.coopernetes.com`. Bump the image tag in
`~/git/lmacka/coopernetes/kubernetes/apps/claudia/app/deployment.yaml`
to ship a release.

Repo skeleton:
- `app/` — FastAPI + Jinja + HTMX. Run `bash scripts/preflight.sh` for
  the full CI-equivalent check.
- `app/prompts/` — `companion-adult.md`, `auditor-adult.md`, `handover.md`.
- `chart/templates/` — deployment, service, ingress, httproute, pvc,
  _helpers all in place. `examples/adult-values.yaml` renders and passes
  schema validation.
- `.github/workflows/` — `test.yml`, `image.yml`, `chart.yml` wired up.
  Tag `vX.Y.Z` triggers image + chart push.
- `docs/` — `library-people-plan.md`, `qa-protocol.md`, `storage-decision.md`.

## What to do next

**Day-to-day mode:** debugging-passes. User exercises a release in the
browser, reports bugs inline mid-chat. Loop is `/investigate or direct
fix → preflight → push → wait CI → tag v0.X.Y → bump coopernetes
deployments → /healthz`.

## Active TODOs

See `TODOS.md` for full context.

## Operating model

- **Single git repo.** App + chart + CI all here.
- **GitHub remote is configured.** `origin = git@github.com:lmacka/claudia.git`
  (PUBLIC). `gh auth status` is authenticated as `lmacka`.
- **Commits AND pushes authorised without asking.** Make commits as work
  progresses; push to `origin/main` after every landed batch. Tag a
  release (`v0.X.Y`) when the batch is worth user testing — `image.yml`
  and `chart.yml` workflows trigger on `v*.*.*` and publish to ghcr.io.
- **Still ASK before destructive ops.** Force-push, branch delete, tag
  delete, force-push to main never OK without explicit instruction.
- **Commit style:** minimal messages, no emojis, no Co-Authored-By,
  no signatures, under 200 chars title, body explains the why.
- **Run preflight before push.** `bash scripts/preflight.sh` runs the same
  checks CI runs. Do not push to main without a green preflight.
- **Verify CI before tagging a release.** `gh run list --limit 5` after
  push — the `test` workflow on `main` must be green before you tag
  `vX.Y.Z`.
- **Bridge-tool ethos.** Don't make claudia more engaging for engagement's
  sake. Removed UI ceremony; the principle stands.

## When you're stuck

- **Unclear architecture choice?** Read `docs/library-people-plan.md`
  if it's about extractors / library / people store. Otherwise read the
  code — there is no separate design doc anymore.
- **Want to see what robo-therapist did?** `~/git/lmacka/robo-therapist/`
  is still there.

## Don'ts

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
- helm-lint: `helm lint chart/ --values examples/adult-values.yaml`
- helm-template: `helm template claudia chart/ --values examples/adult-values.yaml`
- helm-template-gateway: `helm template claudia chart/ --values examples/adult-values.yaml --set ingress.gatewayApi=true --set ingress.parentRef.name=internal-gateway` (must contain `kind: HTTPRoute`)
- typecheck: SKIP (no mypy/pyright in pyproject)
- deadcode: SKIP (no vulture/knip configured)
- shell: SKIP unless shellcheck installed locally
