#!/usr/bin/env bash
# Load Docker images into kind.
set -euo pipefail

IMAGE_TAG="${1:-latest}"
CLUSTER_NAME="${CLUSTER_NAME:-rag-microservices}"
IMAGES=(
  rag-api-gateway
  rag-orchestrator
  rag-ingestion-service
  rag-retrieval-service
  rag-llm-gateway
  rag-chat-ui
)

for img in "${IMAGES[@]}"; do
  echo "Loading ${img}:${IMAGE_TAG} into kind..."
  kind load docker-image "${img}:${IMAGE_TAG}" --name "${CLUSTER_NAME}"
done

echo "All images loaded."
