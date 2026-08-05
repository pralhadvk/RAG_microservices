"""RAG orchestrator: retrieve → augment → generate."""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from python_common.clients import TracedHttpClient
from python_common.schemas import (
    Citation,
    HealthResponse,
    QueryRequest,
    QueryResponse,
)
from python_common.telemetry import (
    configure_logging,
    get_meter,
    get_tracer,
    set_span_attributes,
    setup_telemetry,
)

SERVICE_NAME = "rag-orchestrator"
configure_logging(SERVICE_NAME)
logger = logging.getLogger(SERVICE_NAME)

RETRIEVAL_URL = os.getenv("RETRIEVAL_SERVICE_URL", "http://retrieval-service:8000")
LLM_URL = os.getenv("LLM_GATEWAY_URL", "http://llm-gateway:8000")

retrieval: TracedHttpClient | None = None
llm: TracedHttpClient | None = None


SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer using ONLY the provided context. "
    "If the context is insufficient, say you do not know. "
    "Cite sources by mentioning the source filename when available."
)


def build_prompt(query: str, chunks: list[dict]) -> str:
    context_blocks = []
    for i, chunk in enumerate(chunks, start=1):
        source = chunk.get("metadata", {}).get("source", "unknown")
        context_blocks.append(f"[{i}] (source={source}, score={chunk.get('score', 0):.3f})\n{chunk['text']}")
    context = "\n\n".join(context_blocks) if context_blocks else "(no context retrieved)"
    return f"Context:\n{context}\n\nQuestion: {query}\n\nAnswer:"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global retrieval, llm
    global retrieval, llm, duration_hist
    setup_telemetry(SERVICE_NAME, app)
    duration_hist = get_meter(SERVICE_NAME).create_histogram(
        "rag_query_duration_seconds",
        description="RAG query stage duration",
        unit="s",
    )
    retrieval = TracedHttpClient(RETRIEVAL_URL)
    llm = TracedHttpClient(LLM_URL, timeout=120.0)
    yield
    if retrieval:
        await retrieval.close()
    if llm:
        await llm.close()
    retrieval = None
    llm = None


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
duration_hist = None


def _tracer():
    return get_tracer(SERVICE_NAME)


@app.get("/health/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME)


@app.get("/health/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    assert retrieval is not None and llm is not None
    r = await retrieval.get("/health/ready")
    l = await llm.get("/health/live")
    if r.status_code >= 400 or l.status_code >= 400:
        raise HTTPException(status_code=503, detail="dependencies not ready")
    return HealthResponse(status="ok", service=SERVICE_NAME)


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest) -> QueryResponse:
    assert retrieval is not None and llm is not None
    with _tracer().start_as_current_span("rag.pipeline") as span:
        set_span_attributes(
            {
                "rag.query": req.query,
                "rag.collection": req.collection_id,
                "rag.top_k": req.top_k,
                "rag.model": req.model,
                "rag.temperature": req.temperature,
                "rag.max_tokens": req.max_tokens,
                "rag.provider": req.provider,
            }
        )

        t0 = time.perf_counter()
        with _tracer().start_as_current_span("rag.retrieve"):
            search_resp = await retrieval.post(
                "/search",
                json={
                    "query": req.query,
                    "collection_id": req.collection_id,
                    "top_k": req.top_k,
                },
            )
            if search_resp.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"retrieval failed: {search_resp.text}",
                )
            search_data = search_resp.json()
            chunks = search_data.get("chunks", [])
        duration_hist.record(time.perf_counter() - t0, {"stage": "retrieve"})

        t1 = time.perf_counter()
        prompt = build_prompt(req.query, chunks)
        with _tracer().start_as_current_span("rag.generate") as gen_span:
            set_span_attributes(
                {
                    "rag.context_chunks": len(chunks),
                    "rag.prompt_chars": len(prompt),
                }
            )
            completion_resp = await llm.post(
                "/completions",
                json={
                    "model": req.model,
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "provider": req.provider,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            if completion_resp.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"llm-gateway failed: {completion_resp.text}",
                )
            completion = completion_resp.json()
            gen_span.set_attribute(
                "rag.total_tokens",
                completion.get("usage", {}).get("total_tokens", 0),
            )
        duration_hist.record(time.perf_counter() - t1, {"stage": "generate"})
        duration_hist.record(time.perf_counter() - t0, {"stage": "total"})

        citations = [
            Citation(
                chunk_id=c["chunk_id"],
                text=c["text"],
                score=c["score"],
                metadata=c.get("metadata") or {},
            )
            for c in chunks
        ]
        span.set_attribute("rag.citations", len(citations))
        logger.info(
            "query answered model=%s chunks=%s tokens=%s",
            req.model,
            len(citations),
            completion.get("usage", {}).get("total_tokens", 0),
        )
        return QueryResponse(
            answer=completion.get("content", ""),
            citations=citations,
            model=completion.get("model", req.model),
            usage=completion.get("usage") or {},
        )
