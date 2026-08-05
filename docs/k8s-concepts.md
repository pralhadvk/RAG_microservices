# Kubernetes Concepts Practiced in This Project

This document maps each K8s primitive to a concrete file so you can learn by reading YAML that actually runs.

| Concept | Where | What to notice |
|---------|-------|----------------|
| Namespace | `deploy/base/namespace.yaml`, `infra/observability/namespace.yaml` | Isolate app vs observability |
| Deployment | `deploy/base/*/deployment.yaml` | Replicas, labels, rolling updates |
| Service (ClusterIP) | Same files | DNS name = Service name (`http://llm-gateway:8000`) |
| Ingress | `deploy/base/ingress.yaml` | External HTTP routing via ingress-nginx |
| ConfigMap | `deploy/base/configmap.yaml` | Non-secret config (URLs, model defaults, OTEL endpoint) |
| Secret | `scripts/create-secrets.sh` | API keys from `.env` |
| Probes | Each Deployment | `/health/live` and `/health/ready` |
| Resources | Each Deployment | Requests/limits sized for kind laptops |
| HPA | Each service Deployment YAML | CPU target 70%, min 1, max 5 |
| Kustomize | `deploy/base` + `deploy/overlays/kind` | Base vs environment overlay |
| kind | `kind/cluster.yaml` | Local multi-node cluster, host ports 80/443 |
| metrics-server | `scripts/setup-kind.sh` | Required for HPA metrics |
| Stateful vs stateless | Qdrant vs app services | Only Qdrant holds durable state (MVP: emptyDir) |

## Mental model: how a request is routed

1. Packet arrives on kind node port 80 (mapped in `kind/cluster.yaml`)
2. ingress-nginx matches path `/api` → Service `api-gateway:8000`
3. kube-proxy / iptables routes to a ready Pod selected by `app=api-gateway`
4. That Pod calls other Services by DNS inside the cluster network

## MLOps concepts practiced

| Concept | Implementation |
|---------|----------------|
| Model routing | `llm-gateway` + `DEFAULT_LLM_PROVIDER` / request `provider` |
| Pipeline observability | OTEL spans around retrieve / generate stages |
| Token accounting | `llm_tokens_total` metric |
| Config-driven behavior | ConfigMap for endpoints and embedding model |
| Safe external boundary | llm-gateway is ClusterIP-only (not on Ingress) |

## Exercises

1. Change default model in the ConfigMap and restart orchestrator/gateway pods.
2. Scale manually: `kubectl -n rag-system scale deploy/rag-orchestrator --replicas=3`
3. Break readiness: edit retrieval probe path, apply, watch Service endpoints drop.
4. Add a new span attribute (e.g. `rag.user_tier`) and find it in Jaeger.
5. Point `DEFAULT_LLM_PROVIDER` to `anthropic` (with a valid key) without changing orchestrator code.
