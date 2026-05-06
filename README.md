# claudia

Self-hosted, single-tenant AI companion. One Helm release per user.
A thinking-partner sounding board with a session auditor + handover PDF
for taking notes to your real therapist. Library + people store the
model reads from. Gmail / calendar tools optional.

## Status

**v0.9.x — beta.** Single-user surface: 5-step setup wizard, library +
people stores, memory-diff session review, theme picker, persistent
sessions across pod restarts. CI runs ruff, pytest, helm lint, and helm
template via `scripts/preflight.sh`.

## Build target

Another autistic dad with a homelab. Kubernetes, kubectl, SOPS or
sealed-secrets, NFS or block-PV. Not docker-compose, not one-click VPS.
The Helm chart is the shipping unit.

## Quickstart

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
    --version 0.9.0 \
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

## First-deploy walkthrough

1. **Provision the cluster requirements.** Working IngressClass (or
   Gateway API — the chart supports both via `ingress.gatewayApi: true`),
   a default StorageClass, cert-manager (or pre-created TLS secret).

2. **Decide who reads the secrets.** The default examples assume plain
   `kubectl create secret`. If you GitOps via Flux + SOPS, encrypt the
   API key + admin password into your repo instead.

3. **`helm install` the chart.** The `claudia` chart deploys a single
   pod with a PVC. `kubectl -n <ns> rollout status deploy/claudia`
   should report `successfully rolled out`.

4. **Hit `/healthz`.** Returns `ok` once the FastAPI app is up.

5. **Visit `/`.** The first-run gate redirects to `/setup`.

6. **Walk the 5-stage setup wizard.**
   - **Step 1 — Anthropic API key.** Paste your key; it's validated with
     a 1-token test call before being persisted to kv_store.
   - **Step 2 — Auth method.** Pick a password OR sign in with Google
     (if Google OAuth credentials are configured).
   - **Step 3 — Profile + model + instructions.** Display name comes
     from Helm (not editable here). Set preferred name, DOB, country,
     region, model (Sonnet/Opus/Haiku), and any additional instructions
     you want appended to the system prompt.
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
   (co-parents, friends, family, professionals). The auditor also
   records new names mentioned in chats.

8. **Chat.** `/session/new` opens a chat. Send / receive, `+ attach`
   for PDFs / screenshots / pasted text. End the session and the
   auditor produces a session-log + memory-diff cards at
   `/session/{id}/review`.

9. **Theme it.** `/settings` swaps between sage / blush / lavender /
   amber / high-contrast. Cookie-persisted, 1-year max-age.

## Architecture

- **App.** FastAPI + Jinja2 + HTMX + hand-rolled CSS. No SPA, no React,
  no build step. Stores sessions in SQLite + filesystem on `/data`.
- **Chart.** `chart/` — single-deployment + service + ingress (or HTTPRoute)
  + PVC. Gmail/Calendar tools are opt-in via
  `integrations.google.enabled`.
- **CI.** `.github/workflows/test.yml` runs `scripts/preflight.sh` —
  ruff, pytest, helm lint, helm template (default + Gateway).
  `image.yml` builds multi-arch `linux/amd64,linux/arm64` on tag push
  and pushes to `ghcr.io`. `chart.yml` packages and pushes the Helm
  chart as an OCI artifact on tag push.
- **Models.** Sonnet 4.6 for chat + auditor; Sonnet vision for image
  OCR.

## Development

```bash
# One-time setup.
mise install            # picks up python 3.13 + uv
uv sync                 # installs deps from uv.lock

# Run the app locally (no auth, mock chat replies — see app/main.py).
CLAUDIA_OPS_MODE=local \
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
(or it can be entered in `/setup/1`).

## Project docs

- [`docs/qa-protocol.md`](docs/qa-protocol.md) — QA testing protocol; required reading before claiming a UI change is tested.
- [`docs/storage-decision.md`](docs/storage-decision.md) — files vs SQLite vs Postgres rationale.
- [`docs/library-people-plan.md`](docs/library-people-plan.md) — library + people + extractors spec.

## License

MIT. Personal use is the build target. Commercial use not considered or
supported.
