# Architecture

## Request path (query)

1. Client hits Ingress (`/api/v1/query`)
2. **api-gateway** validates the body, assigns `X-Request-Id`, injects OTEL baggage
3. **rag-orchestrator** starts a parent span `rag.pipeline` with all query params
4. Calls **retrieval-service** `/search` (embed query → Weaviate `near_vector` top-k)
5. Builds a grounded prompt from retrieved chunks
6. Calls **llm-gateway** `/completions` (OpenAI or Anthropic)
7. Returns answer + citations + token usage

## Request path (ingest)

1. Client hits `/api/v1/ingest`
2. **ingestion-service** chunks documents (`chunk_size`, `chunk_overlap`)
3. Calls retrieval-service `/embed` then `/upsert`
4. Vectors land in the Weaviate collection for `collection_id`
   (`docs-v1` → `Rag_docs_v1`, since Weaviate names must start with a capital letter
   and allow only letters, digits and `_`)

## Why an LLM gateway for an external API?

This is the MLOps pattern worth learning:

- One ClusterIP service owns all model traffic
- Swap providers via ConfigMap / request field without touching orchestrator
- Central place for retries, timeouts, token metrics, and rate limits
- Same interface later if you replace OpenAI with in-cluster Ollama/vLLM

## Trace attributes (examples)

| Attribute | Where set |
|-----------|-----------|
| `rag.query` | gateway, orchestrator, retrieval |
| `rag.top_k` | gateway, orchestrator, retrieval |
| `rag.model` / `rag.temperature` / `rag.max_tokens` | gateway, orchestrator, llm-gateway |
| `rag.collection` | all pipeline services |
| `rag.chunk_size` / `rag.chunk_overlap` | gateway, ingestion |
| `rag.prompt_tokens` / `rag.completion_tokens` | llm-gateway |
| `rag.chunk_ids` | retrieval |

Propagation uses W3C `traceparent` plus baggage (`request_id`, optional `user_id` / `session_id`).

## Scaling model

```
Stateless (HPA): api-gateway, orchestrator, ingestion, retrieval, llm-gateway
Stateful (1 replica MVP): Weaviate StatefulSet with a 1Gi PersistentVolumeClaim
```

Weaviate data survives pod restarts and `make undeploy`/`make deploy` because it lives
on the PVC (`data-weaviate-0`). It is removed only by `make teardown` or by deleting the
PVC: `kubectl -n rag-system delete pvc -l app=weaviate`.

## Vector store schema (Weaviate)

Each collection uses one self-provided named vector (`default`, HNSW, cosine distance);
retrieval-service computes embeddings and Weaviate only stores and searches them.

| Property | Type | Holds |
|----------|------|-------|
| `text` | text | Chunk text |
| `chunk_index` | int | Position of the chunk in its document |
| `source` | text | Document source (filterable) |
| `metadata_json` | text | All other document metadata as JSON |

Search reports `score = 1 - cosine distance`, i.e. cosine similarity, so scores keep the
same meaning in prompts and citations.
