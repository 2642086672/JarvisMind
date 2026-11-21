"""Tests for Qdrant VectorStore persistence and operations in JME-4."""
import pytest

from jarvismind.core.errors import VectorIndexError
from jarvismind.services.vector_store import VectorStore, to_qdrant_point_id


def test_to_qdrant_point_id():
    # Hex string
    hex_id = "f47ac10b58cc4372a5670e02b2c3d479"
    pid1 = to_qdrant_point_id(hex_id)
    assert pid1 == "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    # UUID string
    uuid_str = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    pid2 = to_qdrant_point_id(uuid_str)
    assert pid2 == uuid_str

    # Arbitrary non-uuid string
    pid3 = to_qdrant_point_id("custom-key-123")
    assert len(pid3) == 36


def test_qdrant_in_memory_crud():
    store = VectorStore(path=":memory:", collection_name="test_memories", dimension=4)
    try:
        assert store.is_ready is True
        status = store.get_status()
        assert status.is_ready is True
        assert status.total_points == 0

        # Upsert
        mid1 = "m0000000000000000000000000000001"
        vec1 = [0.1, 0.2, 0.3, 0.4]
        payload1 = {"memory_type": "SEMANTIC", "status": "ACTIVE"}
        assert store.upsert_memory(mid1, vec1, payload1) is True

        status2 = store.get_status()
        assert status2.total_points == 1

        # Search
        results = store.search(vec1, limit=5)
        assert len(results) == 1
        assert results[0].memory_id == mid1
        assert results[0].payload["memory_type"] == "SEMANTIC"
        assert results[0].score > 0.99

        # Delete
        assert store.delete_memory(mid1) is True
        assert len(store.search(vec1, limit=5)) == 0
    finally:
        store.close()


def test_qdrant_disk_persistence_and_reopen(tmp_path):
    storage_dir = tmp_path / "qdrant_disk"
    store1 = VectorStore(
        path=str(storage_dir), collection_name="persistent_coll", dimension=4
    )
    mid1 = "m0000000000000000000000000000001"
    mid2 = "m0000000000000000000000000000002"
    vec1 = [1.0, 0.0, 0.0, 0.0]
    vec2 = [0.0, 1.0, 0.0, 0.0]

    try:
        store1.upsert_batch(
            [
                (mid1, vec1, {"memory_type": "FACT", "status": "ACTIVE"}),
                (mid2, vec2, {"memory_type": "PROFILE", "status": "ACTIVE"}),
            ]
        )
        assert store1.get_status().total_points == 2
    finally:
        store1.close()

    # Reopen brand new VectorStore instance pointing to the exact same disk path
    store2 = VectorStore(
        path=str(storage_dir), collection_name="persistent_coll", dimension=4
    )
    try:
        assert store2.is_ready is True
        status = store2.get_status()
        assert status.total_points == 2

        # Verify search works after reopening
        res = store2.search([1.0, 0.0, 0.0, 0.0], limit=1)
        assert len(res) == 1
        assert res[0].memory_id == mid1
        assert res[0].payload["memory_type"] == "FACT"

        all_ids = store2.get_all_point_ids()
        assert all_ids == {mid1, mid2}
    finally:
        store2.close()


def test_qdrant_filtering_by_type_and_status():
    store = VectorStore(path=":memory:", collection_name="filtered_coll", dimension=4)
    try:
        store.upsert_batch(
            [
                ("m1", [1.0, 0.0, 0.0, 0.0], {"memory_type": "FACT", "status": "ACTIVE"}),
                ("m2", [0.9, 0.1, 0.0, 0.0], {"memory_type": "FACT", "status": "ARCHIVED"}),
                ("m3", [0.8, 0.2, 0.0, 0.0], {"memory_type": "PROFILE", "status": "ACTIVE"}),
            ]
        )

        # Filter by status ACTIVE only
        res_active = store.search([1.0, 0.0, 0.0, 0.0], limit=10, status="ACTIVE")
        active_ids = {r.memory_id for r in res_active}
        assert active_ids == {"m1", "m3"}

        # Filter by type FACT and status ACTIVE
        res_fact = store.search(
            [1.0, 0.0, 0.0, 0.0], limit=10, memory_type="FACT", status="ACTIVE"
        )
        assert len(res_fact) == 1
        assert res_fact[0].memory_id == "m1"
    finally:
        store.close()


def test_qdrant_recreate_collection():
    store = VectorStore(path=":memory:", collection_name="recreate_coll", dimension=4)
    try:
        store.upsert_memory("m1", [1.0, 0.0, 0.0, 0.0], {"status": "ACTIVE"})
        assert store.get_status().total_points == 1

        # Recreate with dimension 8
        store.recreate_collection(dimension=8)
        status = store.get_status()
        assert status.total_points == 0
        assert status.dimension == 8
    finally:
        store.close()
