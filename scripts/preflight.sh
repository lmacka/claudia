#!/usr/bin/env bash
# Preflight: runs every check that CI runs, locally. CI also invokes this so
# the two cannot drift. Run before pushing to main.
#
#   bash scripts/preflight.sh
#
# Exits non-zero on any check failure. Output is verbose enough that a failed
# step's error is visible in the last screen of output.

set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf "\n=== %s ===\n" "$*"; }

# System deps that extractor tests need at runtime. Missing these locally
# means PDF/.doc tests will fail — same as in CI. Warn loudly; the test step
# will surface the actual missing-binary errors.
for bin in pdfinfo libreoffice; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "WARN: ${bin} not on PATH. Some extractor tests will fail. Install:"
    echo "  sudo apt-get install -y poppler-utils libreoffice-core libreoffice-writer"
  fi
done

step "ruff lint"
uv run ruff check app tests

step "pytest"
uv run pytest tests/ -q

if ! command -v helm >/dev/null 2>&1; then
  echo "helm not found on PATH — skipping chart checks (CI will still run them)."
  exit 0
fi

step "helm lint adult values"
helm lint chart/ --values examples/adult-values.yaml

step "helm lint kid values"
helm lint chart/ --values examples/kid-values.yaml

step "helm template adult"
helm template claudia chart/ --values examples/adult-values.yaml > /tmp/claudia-adult-render.yaml

step "helm template kid"
helm template claudia chart/ --values examples/kid-values.yaml > /tmp/claudia-kid-render.yaml

step "helm template Gateway API mode"
helm template claudia chart/ --values examples/adult-values.yaml \
  --set ingress.gatewayApi=true \
  --set ingress.parentRef.name=internal-gateway > /tmp/claudia-gw-render.yaml
grep -q "kind: HTTPRoute" /tmp/claudia-gw-render.yaml

# Safety-floor schema checks: each item in this list MUST be const:true in
# values.schema.json. Setting any to false must cause helm template to fail.
# Keep this list in lockstep with chart/values.schema.json.
FLOOR_ITEMS=(
  "kid.safety.haiku_classifier"
  "kid.safety.no_anthropomorphism"
)
# kid.safety.write_tools_disabled was dropped from the schema in T-NEW-F.
# The block is now enforced at the tool registry (app/main.py:_google_enabled)
# rather than at the chart layer — kid mode physically cannot register the
# Gmail/Calendar tool specs regardless of any helm value.
for path in "${FLOOR_ITEMS[@]}"; do
  step "schema rejects ${path}=false"
  if helm template claudia chart/ --values examples/kid-values.yaml \
       --set "${path}=false" >/dev/null 2>&1; then
    echo "ERROR: schema accepted ${path}=false; safety floor broken"
    exit 1
  fi
done

# kid.encryption.enabled is intentionally NOT in the floor list — it was
# relaxed from const:true to default:true in v0.2.0 when at-rest encryption
# was deferred to Step 11. v1.5 will re-pin it. See docs/build-plan-v1.md.

echo
echo "preflight OK"
