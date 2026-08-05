"""Retrieval service: embeddings + Qdrant vector search."""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from openai import AsyncOpenAI
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from python_common.schemas import (
    EmbedRequest,
    EmbedResponse,
    HealthResponse,
    RetrievedChunk,
    SearchRequest,
    SearchResponse,
    UpsertRequest,
    UpsertResponse,
)
from python_common.telemetry import (
    configure_logging,
    get_meter,
    get_tracer,
    set_span_attributes,
    setup_telemetry,
)

SERVICE_NAME = "retrieval-service"
configure_logging(SERVICE_NAME)
logger = logging.getLogger(SERVICE_NAME)

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip() or None

qdrant: QdrantClient | None = None
openai_client: AsyncOpenAI | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global qdrant, openai_client, chunks_counter
    setup_telemetry(SERVICE_NAME, app)
    chunks_counter = get_meter(SERVICE_NAME).create_counter(
        "rag_retrieval_chunks_returned",
        description="Number of chunks returned by retrieval",
    )
    qdrant = QdrantClient(url=QDRANT_URL, timeout=30)
    openai_client = AsyncOpenAI(
        api_key=OPENAI_API_KEY or "missing",
        base_url=OPENAI_BASE_URL,
    )
    logger.info(
        "Connected to Qdrant at %s (embeddings via %s)",
        QDRANT_URL,
        OPENAI_BASE_URL or "https://api.openai.com/v1",
    )
    yield
    qdrant = None
    openai_client = None


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
chunks_counter = None


def _tracer():
    return get_tracer(SERVICE_NAME)


def _ensure_collection(collection_id: str) -> None:
    assert qdrant is not None
    existing = {c.name for c in qdrant.get_collections().collections}
    if collection_id not in existing:
        qdrant.create_collection(
            collection_name=collection_id,
            vectors_config=qm.VectorParams(size=EMBEDDING_DIM, distance=qm.Distance.COSINE),
        )
        logger.info("Created collection %s", collection_id)


async def _embed(texts: list[str]) -> list[list[float]]:
    assert openai_client is not None
    with _tracer().start_as_current_span("embed.texts") as span:
        set_span_attributes(
            {
                "rag.embedding_model": EMBEDDING_MODEL,
                "rag.embed_count": len(texts),
            }
        )
        resp = await openai_client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
        vectors = [item.embedding for item in resp.data]
        span.set_attribute("rag.embedding_dim", len(vectors[0]) if vectors else 0)
        return vectors


@app.get("/health/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME)


@app.get("/health/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    try:
        assert qdrant is not None
        qdrant.get_collections()
        return HealthResponse(status="ok", service=SERVICE_NAME)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Qdrant not ready: {exc}") from exc


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest) -> EmbedResponse:
    vectors = await _embed(req.texts)
    return EmbedResponse(
        embeddings=vectors,
        model=EMBEDDING_MODEL,
        dimensions=len(vectors[0]) if vectors else EMBEDDING_DIM,
    )


@app.post("/upsert", response_model=UpsertResponse)
async def upsert(req: UpsertRequest) -> UpsertResponse:
    assert qdrant is not None
    with _tracer().start_as_current_span("qdrant.upsert") as span:
        set_span_attributes(
            {
                "rag.collection": req.collection_id,
                "rag.upsert_count": len(req.points),
            }
        )
        _ensure_collection(req.collection_id)
        points: list[qm.PointStruct] = []
        for p in req.points:
            vector = p.get("vector")
            if not vector:
                raise HTTPException(status_code=400, detail="Each point needs a vector")
            points.append(
                qm.PointStruct(
                    id=p.get("id") or str(uuid.uuid4()),
                    vector=vector,
                    payload=p.get("payload") or {},
                )
            )
        qdrant.upsert(collection_name=req.collection_id, points=points)
        span.set_attribute("rag.upserted_count", len(points))
        return UpsertResponse(collection_id=req.collection_id, upserted_count=len(points))


@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    assert qdrant is not None
    with _tracer().start_as_current_span("retrieval.search") as span:
        set_span_attributes(
            {
                "rag.query": req.query,
                "rag.collection": req.collection_id,
                "rag.top_k": req.top_k,
            }
        )
        _ensure_collection(req.collection_id)
        query_vec = (await _embed([req.query]))[0]
        # Prefer query_points (newer clients); fall back to search for older servers.
        try:
            hits = qdrant.query_points(
                collection_name=req.collection_id,
                query=query_vec,
                limit=req.top_k,
                with_payload=True,
            ).points
        except Exception:  # noqa: BLE001
            hits = qdrant.search(
                collection_name=req.collection_id,
                query_vector=query_vec,
                limit=req.top_k,
                with_payload=True,
            )
        chunks: list[RetrievedChunk] = []
        for hit in hits:
            payload: dict[str, Any] = hit.payload or {}
            chunks.append(
                RetrievedChunk(
                    chunk_id=str(hit.id),
                    text=str(payload.get("text", "")),
                    score=float(getattr(hit, "score", 0.0) or 0.0),
                    metadata={k: v for k, v in payload.items() if k != "text"},
                )
            )
        chunks_counter.add(len(chunks), {"collection": req.collection_id})
        span.set_attribute("rag.chunks_returned", len(chunks))
        span.set_attribute(
            "rag.chunk_ids",
            ",".join(c.chunk_id for c in chunks),
        )
        logger.info(
            "search collection=%s top_k=%s returned=%s",
            req.collection_id,
            req.top_k,
            len(chunks),
        )
        return SearchResponse(chunks=chunks, collection_id=req.collection_id, query=req.query)
