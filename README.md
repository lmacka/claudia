# claudia

Self-hosted, single-tenant AI companion. One Helm release per user.

- **Adult mode** — a thinking-partner sounding board. Auditor + handover PDF
  for taking notes to your real therapist. Library + people store the model
  reads from. Gmail / calendar tools optional.
- **Kid mode** — a frontal-cortex prosthetic for autistic / ND teens. Two
  roles: kid logs in at `/`, parent admin at `/admin`. Schema-enforced
  safety floor: Haiku pre-turn classifier, write-tools disabled, no
  anthropomorphism. Therapist-handover PDF replaced by a themes-only
  precis the parent reads at `/admin/review` (no raw chat content).

Forked from [robo-therapist](https://github.com/lmacka/robo-therapist) on
2026-04-26. Same auditor / spine / tool-loop bones, packaged for Helm and
re-rolled for the kid persona.

## Status

**v0.5.x — beta.** The full v1 surface is shipped: adult + kid persona,
3-stage setup wizard, library + people stores, OCR-discard kid attachment
flow, memory-diff review, 5-theme picker, parent-admin pages, persistent
kid sessions across pod restarts. CI runs ruff, pytest, helm lint, helm
template (adult / kid / Gateway API), and three schema floor-rejection
checks via `scripts/preflight.sh`.

**Pending before v1 tag:**

- 50-scenario red-team suite + Haiku judge in CI (`docs/build-plan-v1.md`
  step 10). Crisis content categories are covered by deterministic regex
  + Haiku classifier today; the model-as-judge harness is in flight.

**Deferred to v1.5:**

- At-rest encryption for kid sessions. v1 ships kid sessions as plaintext
  JSONL on the PVC — defensible only because Helm release is single-tenant
  per family and the cluster is the parent's homelab. The kid first-chat
  banner is honest about this. See `docs/safety.md` and
  `docs/build-plan-v1.md` step 11 for the restoration plan.

## Build target

Another autistic dad with a homelab. Kubernetes, kubectl, SOPS or
sealed-secrets, NFS or block-PV. Not docker-compose, not one-click VPS.
The Helm chart is the shipping unit.

## Quickstart

### Adult mode (v0.8+)

**No Secrets need to exist before install.** All credentials are captured
in the in-app `/setup` wizard the first time you open the URL.

```bash
# 1. Pick a namespace.
kubectl create namespace claudia

# 2. Copy and edit the example values file.
cp examples/adult-values.yaml my-values.yaml
# Edit displayName, dob, ingress.host, ingress.tls.secretName.

# 3. Install.
helm install claudia oci://ghcr.io/lmacka/charts/claudia \
    --version 0.8.0 \
    --namespace claudia \
    --values my-values.yaml

# 4. Browse to https://your-host/. The first-run gate redirects to /setup,
#    which walks you through five stages:
#      Step 1: Anthropic API key (live-validated)
#      Step 2: Pick a password (or sign in with Google if creds are wired)
#      Step 3: Profile + model + custom instructions
#      Step 4: Library import (drop in PDFs, journals, etc) + auto-draft profile
#      Step 5: Theme + therapist alias → finish
```

If you want to pre-bootstrap the Anthropic key from a Helm Secret instead
of typing it in the wizard (e.g. SOPS-managed), uncomment the
`anthropicSecretRef` block in your values file. Env-mounted Secret values
always win over `/settings` edits.

### Kid mode

Kid mode is operator-managed (parent ssh + Helm). The kid never sees the
setup wizard — the parent runs it via `/admin`. Secrets are still
required pre-install:

```bash
# 1. Namespace per kid. (The chart is single-tenant — one release per kid.)
kubectl create namespace claudia-jasper
kubectl -n claudia-jasper create secret generic anthropic-api-key \
    --from-literal=api-key=sk-ant-...
kubectl -n claudia-jasper create secret generic claudia-jasper-auth \
    --from-literal=password='parent-admin-password'

# 2. Edit examples/kid-values.yaml. The basicAuth password is the
#    PARENT admin password (used at /admin). The kid sets their own
#    passphrase at first login on /.

# 3. Install.
helm install claudia-jasper oci://ghcr.io/lmacka/charts/claudia \
    --version 0.8.0 \
    --namespace claudia-jasper \
    --values my-values.yaml

# 4. Parent visits https://your-host/admin and walks the 5-stage setup
#    wizard (same as adult mode, but profile fields target the kid).
#    Then hand the URL to the kid — they hit / and set their own passphrase.
```

## First-deploy walkthrough

This is the path from `helm install` to "the kid is using it" for kid
mode. Adult is a subset (skip the kid handoff steps).

1. **Provision the cluster requirements.** Working IngressClass (or
   Gateway API — the chart supports both via `ingress.gatewayApi: true`),
   a default StorageClass, cert-manager (or pre-created TLS secret).

2. **Decide who reads the secrets.** The default examples assume plain
   `kubectl create secret`. If you GitOps via Flux + SOPS, encrypt the
   API key + admin password into your repo instead.

3. **`helm install` the chart.** The `claudia` chart deploys a single
   pod with a PVC. `kubectl -n <ns> rollout status deploy/claudia`
   should report `successfully rolled out`.

4. **Hit `/healthz`.** Returns `ok` once the FastAPI app is up. Use this
   as your liveness probe.

5. **Visit `/admin`** (kid mode). HTTP basic auth challenge — username
   is `liam` (or whatever you set as `basicAuth.user`), password is the
   one from step 1. The first-run gate redirects to `/setup`.

6. **Walk the 5-stage setup wizard.**
   - **Step 1 — Anthropic API key.** Paste your key; it's validated with
     a 1-token test call before being persisted to kv_store.
   - **Step 2 — Auth method.** Pick a password OR sign in with Google
     (if Google OAuth credentials are configured).
   - **Step 3 — Profile + model + instructions + (kid-name in kid mode).**
     Display name comes from Helm (not editable here). Set preferred
     name, DOB, country, region, model (Sonnet/Opus/Haiku), and any
     additional instructions you want appended to the system prompt.
   - **Step 4 — Library import.** Drop documents inline (PDFs,
     screenshots, chat exports, school reports, journals, IEP/EHCP/NDIS
     plans, psych assessments). Each file is extracted in a streamed
     pipeline — PDF text, DOCX, image OCR via Claude vision. The
     "auto-draft profile" button reads everything you've uploaded and
     pre-fills the four profile textareas (who you are, active stressors,
     what claudia should never do, what claudia is for). These compose
     `01_background.md` in the context pack.
   - **Step 5 — Recap + theme + therapist alias.** Sanity check, pick a
     colour scheme, optionally rename the bot, finish.

7. **Add people.** `/people` lets you seed the social map up front
   (co-parents, friends, teachers, professionals). claudia also asks
   the kid about new names that show up in OCR'd screenshots and the
   auditor records them at session end.

8. **Hand the URL to the kid.** They visit `/`. First-time login: kid
   picks their own passphrase (≥ 12 chars, Argon2id-hashed). After
   that, normal login. The kid never sees the parent admin password,
   the parent never types the kid's passphrase.

9. **The kid chats.** `/session/new` opens a chat. Send / receive,
   `+ attach` for screenshots (OCR'd then deleted, image never
   touches disk). The `···` menu in the topbar holds "real people who
   can help" (`/help` — public, unauthed by design so a locked-out
   kid can still reach hotlines), settings, and log out.

10. **The auditor runs at session end.** Two outputs:
    - **Full session log** — plaintext on the PVC (v1 dev mode; v1.5
      will encrypt at-rest).
    - **Themes-only precis** — what the parent reads at `/admin/review`.
      No quotes, no specifics, no raw chat content. The deliberate
      fallback: parent gets enough to know if something's wrong without
      reading what the kid actually said.

11. **Theme it.** `/settings` swaps between sage / blush / lavender /
    amber / high-contrast. Cookie-persisted, 1-year max-age. Same UI
    in adult and kid mode (kid reaches it via the kebab).

## Architecture

- **App.** FastAPI + Jinja2 + HTMX + hand-rolled CSS. No SPA, no React,
  no build step. ~3K LOC across `app/`. Stores sessions as JSONL on
  `/data` (no DB).
- **Chart.** `chart/` — single-deployment + service + ingress (or HTTPRoute)
  + PVC. `values.schema.json` enforces the kid-mode safety floor at
  `helm install` time (haiku_classifier, no_anthropomorphism — both
  `const: true`). Gmail/Calendar tools are gated at the app's tool
  registry rather than the schema: kid mode physically cannot register
  them; adult mode opts in via `adult.integrations.google.enabled`.
- **CI.** `.github/workflows/test.yml` runs `scripts/preflight.sh` —
  ruff, pytest, helm lint, helm template (adult / kid / Gateway), and
  schema floor-rejection checks. `image.yml` builds multi-arch
  `linux/amd64,linux/arm64` on tag push and pushes to `ghcr.io`.
  `chart.yml` packages and pushes the Helm chart as an OCI artifact
  on tag push.
- **Models.** Sonnet 4.6 for chat + auditor, Haiku 4.5 for the safety
  pre-classifier (kid mode only) and image OCR transcription.

## Development

```bash
# One-time setup.
mise install            # picks up python 3.13 + uv
uv sync                 # installs deps from uv.lock

# Run the app locally (no auth, mock chat replies — see app/main.py).
CLAUDIA_OPS_MODE=local CLAUDIA_MODE=adult \
    CLAUDIA_DATA_ROOT=/tmp/claudia-dev \
    CLAUDIA_DISPLAY_NAME="Dev" \
    uv run uvicorn app.main:app --port 8081

# Run the full preflight before pushing — same script CI runs.
bash scripts/preflight.sh

# Tests only.
uv run pytest tests/ -q
```

`CLAUDIA_OPS_MODE=local` skips auth, uses an in-memory session store, and
returns mock chat replies. `dev` and `prod` both require `ANTHROPIC_API_KEY`
+ `BASIC_AUTH_PASSWORD`.

## Project docs

- [`docs/design.md`](docs/design.md) — canonical v1 design doc (~1000 lines, spec-reviewed).
- [`docs/build-plan-v1.md`](docs/build-plan-v1.md) — v1 build sequence + v1.5 deferrals.
- [`docs/library-people-plan.md`](docs/library-people-plan.md) — library + people + extractors spec.
- [`docs/safety.md`](docs/safety.md) — kid-mode safety floor + red-team criteria.
- [`docs/wireframe/`](docs/wireframe/) — clickable mockups (`index.html` is the map).

## License

MIT. Personal / family use is the build target. Commercial use not
considered or supported. The bus-factor-one trust model assumes the
deployer (the parent for kid mode, the user themselves for adult mode)
owns the cluster and is the one safety net that matters.
