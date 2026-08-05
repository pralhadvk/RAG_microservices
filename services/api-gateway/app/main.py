"""API gateway: public entry point with validation, baggage, and routing."""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from python_common.clients import TracedHttpClient
from python_common.schemas import (
    HealthResponse,
    IngestRequest,
    IngestResponse,
    QueryRequest,
    QueryResponse,
)
from python_common.telemetry import (
    configure_logging,
    detach_baggage,
    get_tracer,
    set_request_baggage,
    set_span_attributes,
    setup_telemetry,
)

SERVICE_NAME = "api-gateway"
configure_logging(SERVICE_NAME)
logger = logging.getLogger(SERVICE_NAME)

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://rag-orchestrator:8000")
INGESTION_URL = os.getenv("INGESTION_URL", "http://ingestion-service:8000")

orchestrator: TracedHttpClient | None = None
ingestion: TracedHttpClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator, ingestion
    setup_telemetry(SERVICE_NAME, app)
    orchestrator = TracedHttpClient(ORCHESTRATOR_URL, timeout=120.0)
    ingestion = TracedHttpClient(INGESTION_URL, timeout=120.0)
    yield
    if orchestrator:
        await orchestrator.close()
    if ingestion:
        await ingestion.close()
    orchestrator = None
    ingestion = None


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)


def _tracer():
    return get_tracer(SERVICE_NAME)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    user_id = request.headers.get("x-user-id")
    session_id = request.headers.get("x-session-id")
    token = set_request_baggage(request_id, user_id=user_id, session_id=session_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        detach_baggage(token)


@app.get("/health", response_model=HealthResponse)
@app.get("/health/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME)


@app.get("/health/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    assert orchestrator is not None and ingestion is not None
    o = await orchestrator.get("/health/live")
    i = await ingestion.get("/health/live")
    if o.status_code >= 400 or i.status_code >= 400:
        raise HTTPException(status_code=503, detail="downstream not ready")
    return HealthResponse(status="ok", service=SERVICE_NAME)


@app.post("/api/v1/query", response_model=QueryResponse)
async def query(
    req: QueryRequest,
    x_request_id: str | None = Header(default=None),
) -> QueryResponse:
    assert orchestrator is not None
    request_id = x_request_id or str(uuid.uuid4())
    with _tracer().start_as_current_span("gateway.query") as span:
        set_span_attributes(
            {
                "rag.query": req.query,
                "rag.collection": req.collection_id,
                "rag.top_k": req.top_k,
                "rag.model": req.model,
                "rag.temperature": req.temperature,
                "rag.max_tokens": req.max_tokens,
                "rag.provider": req.provider,
                "rag.request_id": request_id,
            }
        )
        resp = await orchestrator.post("/query", json=req.model_dump())
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        data = resp.json()
        data["request_id"] = request_id
        logger.info("proxy query request_id=%s", request_id)
        return QueryResponse(**data)


@app.post("/api/v1/ingest", response_model=IngestResponse)
async def ingest(
    req: IngestRequest,
    x_request_id: str | None = Header(default=None),
) -> IngestResponse:
    assert ingestion is not None
    request_id = x_request_id or str(uuid.uuid4())
    with _tracer().start_as_current_span("gateway.ingest") as span:
        set_span_attributes(
            {
                "rag.collection": req.collection_id,
                "rag.document_count": len(req.documents),
                "rag.chunk_size": req.chunk_size,
                "rag.chunk_overlap": req.chunk_overlap,
                "rag.request_id": request_id,
            }
        )
        resp = await ingestion.post("/ingest", json=req.model_dump())
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        data = resp.json()
        data["request_id"] = request_id
        logger.info("proxy ingest request_id=%s docs=%s", request_id, len(req.documents))
        return IngestResponse(**data)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    logger.exception("unhandled error path=%s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": str(exc)})
