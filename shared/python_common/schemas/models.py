"""Shared Pydantic schemas for RAG microservices."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str


class DocumentInput(BaseModel):
    text: str = Field(..., min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    collection_id: str = Field(default="docs-v1", min_length=1)
    documents: list[DocumentInput] = Field(..., min_length=1)
    chunk_size: int = Field(default=512, ge=64, le=4096)
    chunk_overlap: int = Field(default=64, ge=0, le=1024)


class IngestResponse(BaseModel):
    collection_id: str
    documents_ingested: int
    chunks_created: int
    request_id: str | None = None


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    collection_id: str = Field(default="docs-v1", min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)
    model: str = Field(default="openai/gpt-4o-mini")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    provider: str = Field(default="openai")


class Citation(BaseModel):
    chunk_id: str
    text: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    model: str
    request_id: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    collection_id: str = Field(default="docs-v1")
    top_k: int = Field(default=5, ge=1, le=50)


class RetrievedChunk(BaseModel):
    chunk_id: str
    text: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    collection_id: str
    query: str


class EmbedRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1)


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    model: str
    dimensions: int


class UpsertRequest(BaseModel):
    collection_id: str
    points: list[dict[str, Any]] = Field(..., min_length=1)


class UpsertResponse(BaseModel):
    collection_id: str
    upserted_count: int


class ChatMessage(BaseModel):
    role: str
    content: str


class CompletionRequest(BaseModel):
    model: str = Field(default="openai/gpt-4o-mini")
    messages: list[ChatMessage] = Field(..., min_length=1)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    provider: str = Field(default="openai")


class CompletionResponse(BaseModel):
    content: str
    model: str
    provider: str
    usage: dict[str, int] = Field(default_factory=dict)
