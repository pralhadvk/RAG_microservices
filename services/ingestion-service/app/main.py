"""Ingestion service: chunk documents, embed via retrieval-service, upsert."""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from python_common.clients import TracedHttpClient
from python_common.schemas import HealthResponse, IngestRequest, IngestResponse
from python_common.telemetry import (
    configure_logging,
    get_tracer,
    set_span_attributes,
    setup_telemetry,
)

SERVICE_NAME = "ingestion-service"
configure_logging(SERVICE_NAME)
logger = logging.getLogger(SERVICE_NAME)

RETRIEVAL_URL = os.getenv("RETRIEVAL_SERVICE_URL", "http://retrieval-service:8000")

retrieval: TracedHttpClient | None = None


def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    if chunk_overlap >= chunk_size:
        chunk_overlap = max(0, chunk_size // 4)
    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + chunk_size, length)
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        start = end - chunk_overlap
    return chunks or [text]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global retrieval
    setup_telemetry(SERVICE_NAME, app)
    retrieval = TracedHttpClient(RETRIEVAL_URL)
    yield
    if retrieval:
        await retrieval.close()
    retrieval = None


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)


def _tracer():
    return get_tracer(SERVICE_NAME)


@app.get("/health/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME)


@app.get("/health/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    assert retrieval is not None
    resp = await retrieval.get("/health/ready")
    if resp.status_code >= 400:
        raise HTTPException(status_code=503, detail="retrieval-service not ready")
    return HealthResponse(status="ok", service=SERVICE_NAME)


@app.post("/ingest", response_model=IngestResponse)
async def ingest(req: IngestRequest) -> IngestResponse:
    assert retrieval is not None
    with _tracer().start_as_current_span("ingestion.pipeline") as span:
        set_span_attributes(
            {
                "rag.collection": req.collection_id,
                "rag.document_count": len(req.documents),
                "rag.chunk_size": req.chunk_size,
                "rag.chunk_overlap": req.chunk_overlap,
            }
        )
        all_chunks: list[dict] = []
        for doc in req.documents:
            pieces = chunk_text(doc.text, req.chunk_size, req.chunk_overlap)
            for idx, piece in enumerate(pieces):
                all_chunks.append(
                    {
                        "id": str(uuid.uuid4()),
                        "text": piece,
                        "payload": {
                            "text": piece,
                            "chunk_index": idx,
                            **doc.metadata,
                        },
                    }
                )

        if not all_chunks:
            raise HTTPException(status_code=400, detail="No chunks produced")

        embed_resp = await retrieval.post(
            "/embed",
            json={"texts": [c["text"] for c in all_chunks]},
        )
        if embed_resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"embed failed: {embed_resp.text}",
            )
        embeddings = embed_resp.json()["embeddings"]

        points = []
        for chunk, vector in zip(all_chunks, embeddings, strict=True):
            points.append(
                {
                    "id": chunk["id"],
                    "vector": vector,
                    "payload": chunk["payload"],
                }
            )

        upsert_resp = await retrieval.post(
            "/upsert",
            json={"collection_id": req.collection_id, "points": points},
        )
        if upsert_resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"upsert failed: {upsert_resp.text}",
            )

        span.set_attribute("rag.chunks_created", len(all_chunks))
        logger.info(
            "ingested docs=%s chunks=%s collection=%s",
            len(req.documents),
            len(all_chunks),
            req.collection_id,
        )
        return IngestResponse(
            collection_id=req.collection_id,
            documents_ingested=len(req.documents),
            chunks_created=len(all_chunks),
        )
