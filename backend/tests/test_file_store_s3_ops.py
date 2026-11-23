"""Unit coverage for the S3 adapter's presigned_get_url / list_keys / delete_prefix
operations and their async FileStore facade counterparts.

The sync adapter is driven through a fake boto3 client injected at the same
``_client`` seam the real adapter uses — no live S3 and no moto dependency.
The facade tests cover S3FileStore delegation (via a recording fake
S3ObjectStoreClient) and LocalFileStore's filesystem implementations.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from botocore.exceptions import BotoCoreError

from app.services.file_store import LocalFileStore, S3FileStore
from app.services.file_store_s3 import S3ObjectStoreError, _Boto3S3ObjectStoreClient

_OK_METADATA = {"ResponseMetadata": {"HTTPStatusCode": 200}}


class _FakePaginator:
    def __init__(
        self,
        pages: list[dict[str, Any]],
        record: Any,
        error: BaseException | None,
    ) -> None:
        self._pages = pages
        self._record = record
        self._error = error

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._record(("paginate", kwargs))
        if self._error is not None:
            raise self._error
        return self._pages


class _FakeBoto3S3Client:
    """Stands in for boto3's dynamically typed S3 client at the adapter seam."""

    def __init__(
        self,
        *,
        pages: list[dict[str, Any]] | None = None,
        presigned_url: str = "https://signed.example/object",
        delete_responses: list[dict[str, Any]] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.pages = pages or []
        self.presigned_url = presigned_url
        self.delete_responses = list(delete_responses or [])
        self.error = error

    def generate_presigned_url(
        self,
        operation: str,
        Params: dict[str, Any] | None = None,
        ExpiresIn: int | None = None,
    ) -> str:
        self.calls.append(("generate_presigned_url", operation, Params, ExpiresIn))
        if self.error is not None:
            raise self.error
        return self.presigned_url

    def get_paginator(self, operation: str) -> _FakePaginator:
        self.calls.append(("get_paginator", operation))
        return _FakePaginator(self.pages, self.calls.append, self.error)

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("delete_objects", Bucket, Delete))
        if self.error is not None:
            raise self.error
        if self.delete_responses:
            return self.delete_responses.pop(0)
        return dict(_OK_METADATA)


def _adapter(fake: _FakeBoto3S3Client) -> _Boto3S3ObjectStoreClient:
    client = _Boto3S3ObjectStoreClient(
        bucket="test-bucket",
        region="us-east-1",
        endpoint_url="http://127.0.0.1:1",
        access_key_id="fake-access-key",
        secret_access_key="fake-secret-key",
        force_path_style=True,
    )
    # Inject the fake at the same seam the real boto3 client occupies.
    client._client = fake  # type: ignore[assignment]
    return client


def test_presigned_get_url_forwards_bucket_key_and_ttl() -> None:
    fake = _FakeBoto3S3Client(presigned_url="https://signed.example/some-key")
    adapter = _adapter(fake)

    url = adapter.presigned_get_url("exports/user-1/export.tar.gz", 3600)

    assert url == "https://signed.example/some-key"
    assert fake.calls == [
        (
            "generate_presigned_url",
            "get_object",
            {"Bucket": "test-bucket", "Key": "exports/user-1/export.tar.gz"},
            3600,
        )
    ]


def test_list_keys_paginates_across_page_boundaries() -> None:
    pages = [
        {"Contents": [{"Key": "p/a"}, {"Key": "p/b"}], **_OK_METADATA},
        {"Contents": [{"Key": "p/c"}], **_OK_METADATA},
    ]
    fake = _FakeBoto3S3Client(pages=pages)
    adapter = _adapter(fake)

    assert adapter.list_keys("p/") == ["p/a", "p/b", "p/c"]
    assert ("get_paginator", "list_objects_v2") in fake.calls
    assert ("paginate", {"Bucket": "test-bucket", "Prefix": "p/"}) in fake.calls


def test_list_object_sizes_carries_each_size_across_pages() -> None:
    """Plan 06-19: a companion export bounds its media by these sizes BEFORE reading any."""
    pages = [
        {
            "Contents": [{"Key": "a.png", "Size": 10}, {"Key": "b.mp4", "Size": 2048}],
            **_OK_METADATA,
        },
        {"Contents": [{"Key": "c.gif", "Size": 0}], **_OK_METADATA},
    ]
    fake = _FakeBoto3S3Client(pages=pages)
    adapter = _adapter(fake)

    assert adapter.list_object_sizes("") == [("a.png", 10), ("b.mp4", 2048), ("c.gif", 0)]
    assert ("paginate", {"Bucket": "test-bucket", "Prefix": ""}) in fake.calls


def test_list_keys_empty_prefix_returns_empty_list() -> None:
    fake = _FakeBoto3S3Client(pages=[dict(_OK_METADATA)])
    adapter = _adapter(fake)

    assert adapter.list_keys("missing/") == []


def test_delete_prefix_batches_pages_and_returns_total() -> None:
    pages = [
        {"Contents": [{"Key": "p/a"}, {"Key": "p/b"}], **_OK_METADATA},
        dict(_OK_METADATA),
        {"Contents": [{"Key": "p/c"}], **_OK_METADATA},
    ]
    fake = _FakeBoto3S3Client(pages=pages)
    adapter = _adapter(fake)

    assert adapter.delete_prefix("p/") == 3

    delete_calls = [call for call in fake.calls if call[0] == "delete_objects"]
    assert delete_calls == [
        ("delete_objects", "test-bucket", {"Objects": [{"Key": "p/a"}, {"Key": "p/b"}]}),
        ("delete_objects", "test-bucket", {"Objects": [{"Key": "p/c"}]}),
    ]


def test_delete_prefix_raises_on_per_key_errors() -> None:
    # boto3 does NOT raise when DeleteObjects returns per-key Errors inside a
    # 200 response — the adapter must check and raise itself (Pitfall 8).
    pages = [{"Contents": [{"Key": "p/a"}, {"Key": "p/b"}], **_OK_METADATA}]
    delete_responses = [
        {
            **_OK_METADATA,
            "Deleted": [{"Key": "p/a"}],
            "Errors": [{"Key": "p/b", "Code": "InternalError", "Message": "backend blew up"}],
        }
    ]
    fake = _FakeBoto3S3Client(pages=pages, delete_responses=delete_responses)
    adapter = _adapter(fake)

    with pytest.raises(S3ObjectStoreError, match="S3 prefix delete reported failed keys") as err:
        adapter.delete_prefix("p/")
    assert "InternalError" not in str(err.value)
    assert "backend blew up" not in str(err.value)
    assert "p/b" not in str(err.value)


def test_boto_failures_surface_as_sanitized_errors() -> None:
    failing = _FakeBoto3S3Client(error=BotoCoreError())
    adapter = _adapter(failing)

    with pytest.raises(S3ObjectStoreError, match="S3 presigned URL generation failed"):
        adapter.presigned_get_url("k", 60)
    with pytest.raises(S3ObjectStoreError, match="S3 object list failed"):
        adapter.list_keys("p/")
    with pytest.raises(S3ObjectStoreError, match="S3 prefix delete failed"):
        adapter.delete_prefix("p/")


class _RecordingS3ObjectStoreClient:
    """Recording fake satisfying the S3ObjectStoreClient protocol."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.threads: list[int] = []

    def _record(self, *call: Any) -> None:
        self.calls.append(call)
        self.threads.append(threading.get_ident())

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self._record("put", key, data, content_type)

    def get(self, key: str) -> bytes:
        self._record("get", key)
        return b""

    def delete(self, key: str) -> None:
        self._record("delete", key)

    def exists(self, key: str) -> bool:
        self._record("exists", key)
        return True

    def presigned_get_url(self, key: str, expires_in_seconds: int) -> str:
        self._record("presigned_get_url", key, expires_in_seconds)
        return "https://signed.example/facade"

    def list_keys(self, prefix: str) -> list[str]:
        self._record("list_keys", prefix)
        return ["p/a"]

    def delete_prefix(self, prefix: str) -> int:
        self._record("delete_prefix", prefix)
        return 2

    def close(self) -> None:
        self._record("close")


def _s3_facade(fake: _RecordingS3ObjectStoreClient) -> S3FileStore:
    store = S3FileStore(
        bucket="test-bucket",
        region="us-east-1",
        endpoint_url="http://127.0.0.1:1",
        access_key_id="fake-access-key",
        secret_access_key="fake-secret-key",
        force_path_style=True,
    )
    store.client = fake
    return store


@pytest.mark.asyncio
async def test_s3_facade_delegates_new_operations_off_the_event_loop() -> None:
    fake = _RecordingS3ObjectStoreClient()
    store = _s3_facade(fake)

    assert await store.presigned_get_url("k", 900) == "https://signed.example/facade"
    assert await store.list_keys("p/") == ["p/a"]
    assert await store.delete_prefix("p/") == 2

    assert fake.calls == [
        ("presigned_get_url", "k", 900),
        ("list_keys", "p/"),
        ("delete_prefix", "p/"),
    ]
    # asyncio.to_thread delegation: the sync client must never run on the loop thread.
    loop_thread = threading.get_ident()
    assert all(ident != loop_thread for ident in fake.threads)


@pytest.mark.asyncio
async def test_local_list_keys_returns_relative_keys(tmp_path: Any) -> None:
    store = LocalFileStore(str(tmp_path))
    await store.put("exports/u1/a.txt", b"a")
    await store.put("exports/u1/nested/b.txt", b"b")
    await store.put("exports/u2/c.txt", b"c")

    keys = await store.list_keys("exports/u1")

    assert sorted(keys) == ["exports/u1/a.txt", "exports/u1/nested/b.txt"]


@pytest.mark.asyncio
async def test_local_list_keys_missing_prefix_returns_empty(tmp_path: Any) -> None:
    store = LocalFileStore(str(tmp_path))

    assert await store.list_keys("nothing/here") == []


@pytest.mark.asyncio
async def test_local_delete_prefix_removes_files_and_counts(tmp_path: Any) -> None:
    store = LocalFileStore(str(tmp_path))
    await store.put("exports/u1/a.txt", b"a")
    await store.put("exports/u1/nested/b.txt", b"b")
    await store.put("exports/u2/keep.txt", b"c")

    assert await store.delete_prefix("exports/u1") == 2
    assert await store.exists("exports/u1/a.txt") is False
    assert await store.exists("exports/u1/nested/b.txt") is False
    assert await store.exists("exports/u2/keep.txt") is True


@pytest.mark.asyncio
async def test_local_delete_prefix_missing_returns_zero(tmp_path: Any) -> None:
    store = LocalFileStore(str(tmp_path))

    assert await store.delete_prefix("nothing") == 0


@pytest.mark.asyncio
async def test_local_presigned_get_url_returns_file_uri(tmp_path: Any) -> None:
    store = LocalFileStore(str(tmp_path))
    await store.put("exports/u1/a.txt", b"a")

    url = await store.presigned_get_url("exports/u1/a.txt", 3600)

    assert url.startswith("file://")
    assert url == store._path("exports/u1/a.txt").as_uri()


@pytest.mark.asyncio
async def test_local_prefix_operations_reject_traversal(tmp_path: Any) -> None:
    store = LocalFileStore(str(tmp_path / "base"))

    with pytest.raises(ValueError, match="escapes base path"):
        await store.list_keys("../outside")
    with pytest.raises(ValueError, match="escapes base path"):
        await store.delete_prefix("../outside")
    with pytest.raises(ValueError, match="escapes base path"):
        await store.presigned_get_url("../outside", 60)
