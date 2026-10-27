"""Retrieval service: embeddings + Weaviate vector search."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

import weaviate
from fastapi import FastAPI, HTTPException
from openai import AsyncOpenAI
from weaviate.classes.config import (
    Configure,
    DataType,
    Property,
    VectorDistances,
)
from weaviate.classes.data import DataObject
from weaviate.classes.init import AdditionalConfig, Timeout
from weaviate.classes.query import MetadataQuery
from weaviate.exceptions import WeaviateBaseError

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

WEAVIATE_HOST = os.getenv("WEAVIATE_HOST", "weaviate")
WEAVIATE_HTTP_PORT = int(os.getenv("WEAVIATE_HTTP_PORT", "8080"))
WEAVIATE_GRPC_PORT = int(os.getenv("WEAVIATE_GRPC_PORT", "50051"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip() or None

# One named vector per collection; vectors are computed by this service ("self-provided").
VECTOR_NAME = "default"
# Payload keys stored as first-class Weaviate properties. Everything else in a
# chunk's payload is kept in `metadata_json` so arbitrary document metadata never
# collides with Weaviate's schema rules or property types.
TEXT_PROP = "text"
CHUNK_INDEX_PROP = "chunk_index"
SOURCE_PROP = "source"
METADATA_PROP = "metadata_json"

weaviate_client: weaviate.WeaviateAsyncClient | None = None
openai_client: AsyncOpenAI | None = None
_known_collections: set[str] = set()


def weaviate_collection_name(collection_id: str) -> str:
    """Map an API collection_id (e.g. "docs-v1") to a valid Weaviate collection name.

    Weaviate collection names must match ^[A-Z][_0-9A-Za-z]*$, so characters
    outside [0-9A-Za-z_] become "_" and a "Rag_" prefix guarantees a leading
    capital letter: "docs-v1" -> "Rag_docs_v1".
    """
    return "Rag_" + re.sub(r"[^0-9A-Za-z_]", "_", collection_id)


def payload_to_properties(payload: dict[str, Any]) -> dict[str, Any]:
    """Split a chunk payload into Weaviate properties."""
    rest = dict(payload)
    text = str(rest.pop(TEXT_PROP, ""))
    chunk_index = rest.pop(CHUNK_INDEX_PROP, None)
    props: dict[str, Any] = {
        TEXT_PROP: text,
        SOURCE_PROP: str(rest.get(SOURCE_PROP, "")),
        METADATA_PROP: json.dumps(rest, default=str),
    }
    if chunk_index is not None:
        props[CHUNK_INDEX_PROP] = int(chunk_index)
    return props


def properties_to_metadata(props: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the chunk metadata (everything except text) from Weaviate properties."""
    try:
        metadata: dict[str, Any] = json.loads(props.get(METADATA_PROP) or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if props.get(CHUNK_INDEX_PROP) is not None:
        metadata[CHUNK_INDEX_PROP] = props[CHUNK_INDEX_PROP]
    if props.get(SOURCE_PROP) and SOURCE_PROP not in metadata:
        metadata[SOURCE_PROP] = props[SOURCE_PROP]
    return metadata


@asynccontextmanager
async def lifespan(app: FastAPI):
    global weaviate_client, openai_client, chunks_counter
    setup_telemetry(SERVICE_NAME, app)
    chunks_counter = get_meter(SERVICE_NAME).create_counter(
        "rag_retrieval_chunks_returned",
        description="Number of chunks returned by retrieval",
    )
    # skip_init_checks: don't crash the pod if Weaviate is still starting;
    # /health/ready reports readiness instead.
    weaviate_client = weaviate.use_async_with_custom(
        http_host=WEAVIATE_HOST,
        http_port=WEAVIATE_HTTP_PORT,
        http_secure=False,
        grpc_host=WEAVIATE_HOST,
        grpc_port=WEAVIATE_GRPC_PORT,
        grpc_secure=False,
        additional_config=AdditionalConfig(timeout=Timeout(init=10, query=30, insert=90)),
        skip_init_checks=True,
    )
    await weaviate_client.connect()
    openai_client = AsyncOpenAI(
        api_key=OPENAI_API_KEY or "missing",
        base_url=OPENAI_BASE_URL,
    )
    logger.info(
        "Connected to Weaviate at %s:%s (grpc %s), embeddings via %s",
        WEAVIATE_HOST,
        WEAVIATE_HTTP_PORT,
        WEAVIATE_GRPC_PORT,
        OPENAI_BASE_URL or "https://api.openai.com/v1",
    )
    yield
    await weaviate_client.close()
    weaviate_client = None
    openai_client = None


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
chunks_counter = None


def _tracer():
    return get_tracer(SERVICE_NAME)


async def _collection_exists(name: str) -> bool:
    assert weaviate_client is not None
    if name in _known_collections:
        return True
    if await weaviate_client.collections.exists(name):
        _known_collections.add(name)
        return True
    return False


async def _ensure_collection(name: str) -> None:
    """Create the collection (self-provided vectors, HNSW, cosine) if it is missing."""
    assert weaviate_client is not None
    if await _collection_exists(name):
        return
    try:
        await weaviate_client.collections.create(
            name=name,
            vector_config=Configure.Vectors.self_provided(
                name=VECTOR_NAME,
                vector_index_config=Configure.VectorIndex.hnsw(
                    distance_metric=VectorDistances.COSINE,
                ),
            ),
            properties=[
                Property(name=TEXT_PROP, data_type=DataType.TEXT),
                Property(name=CHUNK_INDEX_PROP, data_type=DataType.INT),
                Property(name=SOURCE_PROP, data_type=DataType.TEXT),
                Property(
                    name=METADATA_PROP,
                    data_type=DataType.TEXT,
                    skip_vectorization=True,
                    index_searchable=False,
                ),
            ],
        )
        logger.info("Created Weaviate collection %s", name)
    except WeaviateBaseError:
        # Another replica may have created it between our check and create.
        if not await weaviate_client.collections.exists(name):
            raise
    _known_collections.add(name)


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
        assert weaviate_client is not None
        if not await weaviate_client.is_ready():
            raise RuntimeError("is_ready() returned false")
        return HealthResponse(status="ok", service=SERVICE_NAME)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Weaviate not ready: {exc}") from exc


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
    assert weaviate_client is not None
    name = weaviate_collection_name(req.collection_id)
    with _tracer().start_as_current_span("weaviate.upsert") as span:
        set_span_attributes(
            {
                "rag.collection": req.collection_id,
                "rag.vectordb": "weaviate",
                "rag.vectordb_collection": name,
                "rag.upsert_count": len(req.points),
            }
        )
        await _ensure_collection(name)
        objects: list[DataObject] = []
        for p in req.points:
            vector = p.get("vector")
            if not vector:
                raise HTTPException(status_code=400, detail="Each point needs a vector")
            point_id = p.get("id") or str(uuid.uuid4())
            try:
                point_uuid = uuid.UUID(str(point_id))
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Point id must be a UUID, got {point_id!r}",
                ) from exc
            objects.append(
                DataObject(
                    properties=payload_to_properties(p.get("payload") or {}),
                    uuid=point_uuid,
                    vector={VECTOR_NAME: vector},
                )
            )

        collection = weaviate_client.collections.get(name)
        result = await collection.data.insert_many(objects)
        if result.has_errors:
            errors = list(result.errors.values())
            span.set_attribute("rag.upsert_errors", len(errors))
            logger.error("Weaviate insert errors=%s first=%s", len(errors), errors[0].message)
            raise HTTPException(
                status_code=502,
                detail=f"Weaviate rejected {len(errors)} of {len(objects)} objects: {errors[0].message}",
            )
        span.set_attribute("rag.upserted_count", len(objects))
        return UpsertResponse(collection_id=req.collection_id, upserted_count=len(objects))


@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    assert weaviate_client is not None
    name = weaviate_collection_name(req.collection_id)
    with _tracer().start_as_current_span("retrieval.search") as span:
        set_span_attributes(
            {
                "rag.query": req.query,
                "rag.collection": req.collection_id,
                "rag.vectordb": "weaviate",
                "rag.vectordb_collection": name,
                "rag.top_k": req.top_k,
            }
        )
        chunks: list[RetrievedChunk] = []
        if await _collection_exists(name):
            query_vec = (await _embed([req.query]))[0]
            collection = weaviate_client.collections.get(name)
            response = await collection.query.near_vector(
                near_vector=query_vec,
                target_vector=VECTOR_NAME,
                limit=req.top_k,
                return_metadata=MetadataQuery(distance=True),
            )
            for obj in response.objects:
                props: dict[str, Any] = dict(obj.properties or {})
                distance = obj.metadata.distance if obj.metadata else None
                chunks.append(
                    RetrievedChunk(
                        chunk_id=str(obj.uuid),
                        text=str(props.get(TEXT_PROP, "")),
                        # Cosine distance is 1 - cosine similarity; report similarity
                        # so higher always means more relevant.
                        score=1.0 - float(distance) if distance is not None else 0.0,
                        metadata=properties_to_metadata(props),
                    )
                )
        else:
            logger.warning("search on missing collection=%s; returning no chunks", name)

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
