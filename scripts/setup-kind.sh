#!/usr/bin/env bash
# Create kind cluster, install ingress-nginx, apply observability stack.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-rag-microservices}"

echo "==> Creating kind cluster '${CLUSTER_NAME}'..."
if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
  echo "    Cluster already exists, skipping create."
else
  kind create cluster --name "${CLUSTER_NAME}" --config "${ROOT}/kind/cluster.yaml"
fi

echo "==> Installing ingress-nginx..."
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
echo "    Waiting for ingress-nginx controller..."
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=180s

echo "==> Installing metrics-server (for HPA)..."
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
# kind needs insecure TLS skip for metrics-server
kubectl -n kube-system patch deployment metrics-server --type='json' \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' \
  2>/dev/null || true

echo "==> Creating namespaces..."
kubectl apply -f "${ROOT}/deploy/base/namespace.yaml"
kubectl apply -f "${ROOT}/infra/observability/namespace.yaml"

echo "==> Deploying observability stack..."
kubectl apply -k "${ROOT}/infra/observability"

echo "==> Waiting for observability pods..."
kubectl -n observability wait --for=condition=available --timeout=180s deployment --all || true

echo ""
echo "Kind cluster ready."
echo "Next:"
echo "  1. cp .env.example .env   # add OPENROUTER_API_KEY"
echo "  2. make secrets"
echo "  3. make build && make load && make deploy"
echo "  4. make wait-ready && make seed"
