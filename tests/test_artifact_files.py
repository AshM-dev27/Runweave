"""File contracts, storage integrity and bounded transfers across process lifetimes."""

import hashlib
import io
import zipfile

import httpx
import pytest
from sqlalchemy import select

from agent_runtime.api import create_app
from agent_runtime.artifacts import XLSX
from agent_runtime.blob_storage import FileBlobStorage
from agent_runtime.client import Client, ClientError
from agent_runtime.db import ArtifactRow
from agent_runtime.store import Problem, Store


def workbook():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return output.getvalue()


@pytest.mark.parametrize(
    "media,filename,content",
    [
        ("application/pdf", "report.pdf", b"%PDF-1.7\n" + b"x" * 300000),
        (XLSX, "report.xlsx", workbook()),
        ("image/png", "image.png", b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2000),
        ("application/octet-stream", "result.bin", bytes(range(256)) * 2000),
        ("text/csv", "rows.csv", b"id,value\n" + b"1,2\n" * 100000),
    ],
)
async def test_binary_and_larger_files_stream_through_public_api_and_restart(
    store, tmp_path, media, filename, content
):
    store.blobs = FileBlobStorage(tmp_path / "blobs")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as http:
        client = Client(http_client=http)
        source = tmp_path / filename
        source.write_bytes(content)
        ref = await client.upload_file(source, idempotency_key="binary")
        assert (await client.upload_file(source, idempotency_key="binary")).id == ref.id
        assert ref.sha256 == hashlib.sha256(content).hexdigest()
        async with store.database.sessions() as db:
            row = await db.get(ArtifactRow, ref.id)
            assert row.content is None and row.blob_key == ref.sha256
        destination = tmp_path / "download"
        assert await client.download_file(ref.id, destination) == destination
        assert destination.read_bytes() == content
    replacement = Store(store.database, blobs=FileBlobStorage(tmp_path / "blobs"))
    assert (await replacement.artifact(ref.id))[1] == content
    assert not list((tmp_path / "blobs").rglob(".pending-*"))


async def test_corrupt_missing_and_unconfigured_blob_are_explicit_errors(store, tmp_path):
    store.blobs = FileBlobStorage(tmp_path / "blobs")
    ref = await store.upload(b"original", "text/plain", "file.txt", "file")
    blob = store.blobs.path(ref.sha256)
    blob.write_bytes(b"tampered")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as http:
        response = await http.get(f"/v1/artifacts/{ref.id}/content")
        assert response.status_code == 409 and response.json()["detail"] == "artifact_integrity_error"
        destination = tmp_path / "existing.txt"
        destination.write_bytes(b"keep")
        with pytest.raises(ClientError):
            await Client(http_client=http).download_file(ref.id, destination)
        assert destination.read_bytes() == b"keep" and not list(tmp_path.glob(".runweave-*"))
        blob.unlink()
        assert (await http.get(f"/v1/artifacts/{ref.id}/content")).status_code == 409
        store.blobs = None
        assert (await http.get(f"/v1/artifacts/{ref.id}/content")).status_code == 503


async def test_stream_size_and_storage_quotas_publish_no_metadata(store, tmp_path):
    store.blobs = FileBlobStorage(tmp_path / "blobs")
    store.artifact_max_bytes = 32

    async def chunks():
        yield b"x" * 20
        yield b"x" * 20

    with pytest.raises(Problem) as exc:
        await store.upload_stream(chunks(), "text/plain", "file.txt", "large")
    assert exc.value.status == 413
    store.artifact_storage_bytes = 3
    with pytest.raises(Problem) as exc:
        await store.upload(b"four", "text/plain", "file.txt", "quota")
    assert exc.value.detail == "artifact_storage_limit"
    assert await store.artifact_list() == []
    assert not list((tmp_path / "blobs").rglob("*"))


async def test_same_blob_different_artifacts_and_key_conflict(store, tmp_path):
    store.blobs = FileBlobStorage(tmp_path / "blobs")
    one = await store.upload(b"same", "text/plain", "one.txt", "one")
    two = await store.upload(b"same", "text/plain", "two.txt", "two")
    assert one.id != two.id and one.sha256 == two.sha256
    with pytest.raises(Problem) as exc:
        await store.upload(b"changed", "text/plain", "one.txt", "one")
    assert exc.value.status == 409
    async with store.database.sessions() as db:
        assert len(list(await db.scalars(select(ArtifactRow)))) == 2


@pytest.mark.parametrize(
    "media,filename,content",
    [
        ("application/pdf", "file.pdf", b"text"),
        ("image/png", "file.png", b"text"),
        (XLSX, "file.xlsx", b"not a zip"),
        ("application/json", "file.json", b"bad"),
    ],
)
async def test_invalid_declared_formats_rejected(store, media, filename, content):
    with pytest.raises(Problem, match="invalid_artifact_content"):
        await store.upload(content, media, filename, "invalid")


async def test_client_retries_streamed_upload_with_identical_snapshot(store, tmp_path):
    source = tmp_path / "source.bin"
    expected = bytes(range(256)) * 2000
    source.write_bytes(expected)
    app = httpx.ASGITransport(app=create_app(store, "test"))
    attempts = []

    async def handler(request):
        attempts.append(await request.aread())
        response = await app.handle_async_request(request)
        await response.aread()
        if len(attempts) == 1:
            source.write_bytes(b"changed after upload")
            raise httpx.ReadError("lost response")
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as http:
        http.headers["Authorization"] = "Bearer test"
        ref = await Client(http_client=http).upload_file(source, idempotency_key="lost")
    assert attempts == [expected, expected]
    assert (await store.artifact(ref.id))[1] == expected


async def test_blob_upload_rejects_bad_keys_and_corrupt_existing_bytes(tmp_path):
    storage = FileBlobStorage(tmp_path / "blobs")
    source = tmp_path / "source"
    source.write_bytes(b"correct")
    digest = hashlib.sha256(b"correct").hexdigest()
    await storage.put(source, digest)
    storage.path(digest).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="artifact_integrity_error"):
        await storage.put(source, digest)
    with pytest.raises(ValueError):
        storage.path("../outside")
