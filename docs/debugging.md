# Debugging Guide

## Common commands

```bash
# Cluster overview
make status

# Pod details / events
kubectl -n rag-system get pods
kubectl -n rag-system describe pod -l app=rag-orchestrator
kubectl -n rag-system get events --sort-by=.lastTimestamp | tail -30

# Logs with trace correlation
make logs SERVICE=api-gateway
make logs SERVICE=rag-orchestrator
make logs SERVICE=llm-gateway
```

Log lines are JSON-shaped and include `trace_id`, `span_id`, and `request_id`.

## Trace a single request

```bash
# Send a request with a known ID
curl -sS -X POST http://localhost/api/v1/query \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: debug-42' \
  -d '{"query":"What is the refund policy?","collection_id":"docs-v1","top_k":3}'

# Follow logs and grab trace_id
make logs SERVICE=rag-orchestrator

# Open Jaeger and search by Trace ID or service name
make jaeger
# http://localhost:16686
```

In Jaeger you should see a tree similar to:

```
api-gateway:gateway.query
  └─ rag-orchestrator:rag.pipeline
       ├─ rag.retrieve
       │    └─ retrieval-service:retrieval.search
       │         └─ embed.texts
       └─ rag.generate
            └─ llm-gateway:llm.completions
```

Click each span and inspect **Tags** for `rag.*` attributes.

## Common failures

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Ingress 404 | ingress-nginx not ready | `kubectl -n ingress-nginx get pods` |
| 503 from gateway | Downstream CrashLoop | `kubectl describe pod` + logs |
| retrieval not ready | Weaviate still starting | Wait; check `kubectl -n rag-system logs -l app=weaviate` |
| llm-gateway 503 | Missing API key | `make secrets` after fixing `.env` |
| Empty answers / no chunks | Forgot seed | `make seed` |
| ImagePullBackOff | Image not loaded into kind | `make build && make load` |
| HPA stuck at 1 | metrics-server missing | Re-run `make kind-create` or install metrics-server |

## Port-forwards without Ingress

```bash
kubectl -n rag-system port-forward svc/api-gateway 8080:8000
curl http://localhost:8080/health
```

## Rebuild one service quickly

```bash
docker build -t rag-llm-gateway:latest -f services/llm-gateway/Dockerfile .
kind load docker-image rag-llm-gateway:latest --name rag-microservices
kubectl -n rag-system rollout restart deployment/llm-gateway
kubectl -n rag-system rollout status deployment/llm-gateway
```

## Verify Weaviate connectivity

```bash
# Readiness and server version
kubectl -n rag-system run curl --rm -it --image=curlimages/curl --restart=Never -- \
  curl -s http://weaviate:8080/v1/meta

# List collections (docs-v1 is stored as Rag_docs_v1)
kubectl -n rag-system run curl --rm -it --image=curlimages/curl --restart=Never -- \
  curl -s http://weaviate:8080/v1/schema
```
