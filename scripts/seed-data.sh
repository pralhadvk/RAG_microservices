#!/usr/bin/env bash
# Seed sample documents via the public ingest API.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${BASE_URL:-http://localhost}"

echo "Waiting for ingress to respond..."
for i in $(seq 1 30); do
  if curl -sf "${BASE_URL}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "Ingesting sample documents..."
curl -sS -X POST "${BASE_URL}/api/v1/ingest" \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: seed-$(date +%s)" \
  -d @"${ROOT}/samples/seed.json" | python3 -m json.tool

echo ""
echo "Seed complete. Try a query:"
echo "  curl -X POST ${BASE_URL}/api/v1/query -H 'Content-Type: application/json' \\"
echo "    -d '{\"query\":\"What is the refund policy?\",\"collection_id\":\"docs-v1\",\"top_k\":3,\"model\":\"openai/gpt-5.6-luna\"}'"
