# RAG Microservices MVP

A learning-focused RAG platform on Kubernetes (kind) with five microservices,
OpenTelemetry tracing of every request parameter, and an in-cluster LLM gateway
that routes to external providers (OpenAI / Anthropic).

## Architecture

```
Client → Ingress → api-gateway → rag-orchestrator → retrieval-service → Qdrant
                                 └───────────────→ llm-gateway → OpenAI/Anthropic
                 └────────────→ ingestion-service → retrieval-service → Qdrant

All services → OTEL Collector → Jaeger (traces) + Prometheus (metrics) → Grafana
```

## Prerequisites

- Docker Desktop (8GB+ RAM recommended)
- [kind](https://kind.sigs.k8s.io/), kubectl, kustomize (kubectl has built-in `-k`)
- An OpenAI API key (optional Anthropic key)

## Quickstart

```bash
# 1. Configure secrets
cp .env.example .env
# edit .env and set OPENROUTER_API_KEY=...

# 2. Create cluster + observability + ingress
make kind-create

# 3. Create K8s secrets from .env
make secrets

# 4. Build, load into kind, deploy
make build
make load
make deploy
make wait-ready

# 5. Seed sample docs and open the chat UI
make seed

# Chat UI (same Ingress as the API)
open http://localhost/

# Or query via curl
curl -sS -X POST http://localhost/api/v1/query \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: demo-1' \
  -d '{
    "query": "What is the refund policy?",
    "collection_id": "docs-v1",
    "top_k": 3,
    "model": "openai/gpt-4o-mini",
    "temperature": 0.2,
    "max_tokens": 256
  }' | python3 -m json.tool
```

## Observability

| Tool | Access | Purpose |
|------|--------|---------|
| Jaeger | `make jaeger` → http://localhost:16686 | Distributed traces with RAG params |
| Grafana | `make grafana` → http://localhost:3000 | Dashboards (admin/admin) |
| Prometheus | `make prometheus` → http://localhost:9090 | Raw metrics |

Every span records attributes such as `rag.query`, `rag.top_k`, `rag.model`,
`rag.temperature`, `rag.collection`, token counts, and chunk IDs.

Debugging flow:

1. `make logs SERVICE=rag-orchestrator`
2. Copy `trace_id` from the JSON log line
3. Search that ID in Jaeger

See [docs/debugging.md](docs/debugging.md) for more.

## Services

| Service | Public? | Role |
|---------|---------|------|
| api-gateway | Yes (Ingress) | Validation, request IDs, baggage |
| rag-orchestrator | Internal | Retrieve → augment → generate |
| ingestion-service | Via gateway | Chunk + embed + upsert |
| retrieval-service | Internal | Embeddings + Qdrant search |
| llm-gateway | Internal only | Provider routing, retries, token metrics |
| chat-ui | Yes (`/`) | Browser chat UI with citations |
| qdrant | Internal | Vector store |

## Scaling

Horizontal Pod Autoscalers (CPU 70%, min 1, max 5) are defined for all
stateless services. After seeding data:

```bash
make load-test   # requires: go install github.com/rakyll/hey@latest
kubectl -n rag-system get hpa -w
```

## Project layout

```
services/          # FastAPI microservices
shared/python_common/  # Schemas, OTEL, HTTP clients
deploy/            # Kustomize base + kind overlay
infra/observability/   # OTEL, Jaeger, Prometheus, Grafana
kind/              # kind cluster config
scripts/           # Bootstrap, seed, load-test
docs/              # Learning guides
```

## Learning docs

- [Architecture](docs/architecture.md)
- [Debugging](docs/debugging.md)
- [Kubernetes concepts practiced here](docs/k8s-concepts.md)

## Teardown

```bash
make teardown
```

## Adding a feature (dev loop)

1. Edit a service under `services/<name>/app/`
2. `make build && make load && make deploy`
3. Hit the API and inspect Jaeger for new span attributes
4. Prefer changing ConfigMaps for routing/provider defaults before code changes
