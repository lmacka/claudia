#!/usr/bin/env bash
# Preflight: runs every check that CI runs, locally. CI also invokes this so
# the two cannot drift. Run before pushing to main.
#
#   bash scripts/preflight.sh
#
# Exits non-zero on any check failure.

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

step "helm lint"
helm lint chart/ --values examples/adult-values.yaml

step "helm template"
helm template claudia chart/ --values examples/adult-values.yaml > /tmp/claudia-render.yaml

step "helm template Gateway API mode"
helm template claudia chart/ --values examples/adult-values.yaml \
  --set ingress.gatewayApi=true \
  --set ingress.parentRef.name=internal-gateway > /tmp/claudia-gw-render.yaml
grep -q "kind: HTTPRoute" /tmp/claudia-gw-render.yaml

echo
echo "preflight OK"
