#!/usr/bin/env bash
# Optional load-test helper for observing HPA scaling (requires hey).
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost}"
DURATION="${DURATION:-60s}"
CONCURRENCY="${CONCURRENCY:-20}"

if ! command -v hey >/dev/null 2>&1; then
  echo "Install hey: go install github.com/rakyll/hey@latest"
  echo "Or use curl in a loop for a lighter test."
  exit 1
fi

echo "Load testing ${BASE_URL}/api/v1/query for ${DURATION} (c=${CONCURRENCY})..."
hey -z "${DURATION}" -c "${CONCURRENCY}" -m POST \
  -H "Content-Type: application/json" \
  -d '{"query":"What is the refund policy?","collection_id":"docs-v1","top_k":3,"model":"openai/gpt-4o-mini","max_tokens":128}' \
  "${BASE_URL}/api/v1/query"

echo ""
echo "Watch HPA: kubectl -n rag-system get hpa -w"
