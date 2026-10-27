"""Unit tests for the Weaviate-backed retrieval service.

Weaviate and the embeddings API are mocked, so these run without a cluster:

    pip install -r services/retrieval-service/requirements.txt pytest
    PYTHONPATH=shared:services/retrieval-service pytest services/retrieval-service/tests
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def wv(monkeypatch):
    """A mocked WeaviateAsyncClient with one collection handle."""
    collection = MagicMock()
    collection.data.insert_many = AsyncMock(
        return_value=SimpleNamespace(has_errors=False, errors={})
    )
    collection.query.near_vector = AsyncMock()

    client = MagicMock()
    client.collections.exists = AsyncMock(return_value=False)
    client.collections.create = AsyncMock()
    client.collections.get = MagicMock(return_value=collection)
    client.is_ready = AsyncMock(return_value=True)

    monkeypatch.setattr(main, "weaviate_client", client)
    monkeypatch.setattr(main, "chunks_counter", MagicMock())
    monkeypatch.setattr(main, "_embed", AsyncMock(return_value=[[0.1, 0.2, 0.3]]))
    main._known_collections.clear()
    return SimpleNamespace(client=client, collection=collection)


@pytest.fixture
def api():
    # No `with` block: skips the lifespan, so no real connections are opened.
    return TestClient(main.app)


def test_collection_name_mapping():
    assert main.weaviate_collection_name("docs-v1") == "Rag_docs_v1"
    assert main.weaviate_collection_name("team.a/2026") == "Rag_team_a_2026"


def test_payload_round_trip_keeps_metadata():
    payload = {"text": "Refunds within 30 days.", "chunk_index": 2,
               "source": "refund-policy.md", "category": "policy"}
    props = main.payload_to_properties(payload)
    assert props["text"] == "Refunds within 30 days."
    assert props["chunk_index"] == 2
    assert props["source"] == "refund-policy.md"
    assert main.properties_to_metadata(props) == {
        "chunk_index": 2, "source": "refund-policy.md", "category": "policy",
    }


def test_upsert_creates_collection_and_inserts(wv, api):
    point_id = str(uuid.uuid4())
    resp = api.post("/upsert", json={
        "collection_id": "docs-v1",
        "points": [{"id": point_id, "vector": [0.1, 0.2, 0.3],
                    "payload": {"text": "hello", "chunk_index": 0, "source": "a.md"}}],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"collection_id": "docs-v1", "upserted_count": 1}

    create_kwargs = wv.client.collections.create.await_args.kwargs
    assert create_kwargs["name"] == "Rag_docs_v1"
    (obj,) = wv.collection.data.insert_many.await_args.args[0]
    assert str(obj.uuid) == point_id
    assert obj.vector == {"default": [0.1, 0.2, 0.3]}
    assert obj.properties["text"] == "hello"


def test_upsert_rejects_non_uuid_ids(wv, api):
    resp = api.post("/upsert", json={
        "collection_id": "docs-v1",
        "points": [{"id": "chunk-1", "vector": [0.1], "payload": {"text": "x"}}],
    })
    assert resp.status_code == 400


def test_upsert_surfaces_weaviate_errors(wv, api):
    wv.collection.data.insert_many.return_value = SimpleNamespace(
        has_errors=True, errors={0: SimpleNamespace(message="vector length mismatch")}
    )
    resp = api.post("/upsert", json={
        "collection_id": "docs-v1",
        "points": [{"vector": [0.1], "payload": {"text": "x"}}],
    })
    assert resp.status_code == 502
    assert "vector length mismatch" in resp.json()["detail"]


def test_search_converts_distance_to_similarity(wv, api):
    wv.client.collections.exists.return_value = True
    hit_id = uuid.uuid4()
    wv.collection.query.near_vector.return_value = SimpleNamespace(objects=[
        SimpleNamespace(
            uuid=hit_id,
            properties={"text": "Refunds within 30 days.", "chunk_index": 0,
                        "source": "refund-policy.md",
                        "metadata_json": '{"source": "refund-policy.md"}'},
            metadata=SimpleNamespace(distance=0.25),
        )
    ])
    resp = api.post("/search", json={"query": "refunds?", "collection_id": "docs-v1", "top_k": 3})
    assert resp.status_code == 200, resp.text
    (chunk,) = resp.json()["chunks"]
    assert chunk["chunk_id"] == str(hit_id)
    assert chunk["score"] == pytest.approx(0.75)
    assert chunk["metadata"] == {"source": "refund-policy.md", "chunk_index": 0}

    kwargs = wv.collection.query.near_vector.await_args.kwargs
    assert kwargs["limit"] == 3
    assert kwargs["target_vector"] == "default"


def test_search_on_missing_collection_returns_empty(wv, api):
    resp = api.post("/search", json={"query": "anything", "collection_id": "nope"})
    assert resp.status_code == 200
    assert resp.json()["chunks"] == []
    wv.client.collections.create.assert_not_awaited()


def test_ready_reports_weaviate_state(wv, api):
    assert api.get("/health/ready").status_code == 200
    wv.client.is_ready.return_value = False
    assert api.get("/health/ready").status_code == 503
