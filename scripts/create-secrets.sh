#!/usr/bin/env bash
# Create Kubernetes secrets from .env file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT}/.env"

NAMESPACE="${NAMESPACE:-rag-system}"

# OpenRouter keys work with the OpenAI SDK when OPENAI_BASE_URL is set.
API_KEY="${OPENAI_API_KEY:-${OPENROUTER_API_KEY:-}}"

if [[ -z "${API_KEY}" || "${API_KEY}" == "sk-your-key-here" || "${API_KEY}" == "replace-me" ]]; then
  echo "ERROR: Set OPENROUTER_API_KEY (or OPENAI_API_KEY) in .env to a real key."
  exit 1
fi

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "${NAMESPACE}" create secret generic rag-api-keys \
  --from-literal=OPENAI_API_KEY="${API_KEY}" \
  --from-literal=ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret rag-api-keys created/updated in ${NAMESPACE}."
echo "Using OpenRouter/OpenAI-compatible key (len=${#API_KEY})."
