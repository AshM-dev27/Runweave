"""Stream file bytes outside transactions; publish bounded immutable metadata atomically."""

import asyncio
import hashlib
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import defer

from . import artifacts
from .blob_storage import bytes_chunks
from .db import ArtifactRow, GateRow, ToolkitRunRow
from .general_db import GeneralOperationRow, GeneralRunRow

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string", "minLength": 1, "maxLength": 36},
        "offset": {"type": "integer", "minimum": 0},
        "length": {"type": "integer", "minimum": 1, "maximum": 2048},
    },
    "required": ["artifact_id"],
    "additionalProperties": False,
}


def artifact_problem(error):
    from .store import Problem

    code = str(error)
    return Problem(
        413
        if code == "artifact_size_limit"
        else 415
        if code == "unsupported_media_type"
        else 409
        if code == "artifact_integrity_error"
        else 422,
        code,
    )


class ArtifactStore:
    async def artifact_output_quota(self, db, root_id, size, general):
        from .store import Problem

        count, used = (
            await db.execute(
                select(func.count(ArtifactRow.id), func.coalesce(func.sum(ArtifactRow.size_bytes), 0))
                .join(ToolkitRunRow, ToolkitRunRow.run_id == ArtifactRow.producer_run_id)
                .where(ToolkitRunRow.root_id == root_id)
            )
        ).one()
        if count >= 8 or used + size > (self.artifact_run_bytes if general else 1048576):
            raise Problem(409, "artifact_count_limit")

    async def upload(self, content, media, filename, key, producer=None):
        return await self.upload_stream(bytes_chunks(content), media, filename, key, producer)

    async def upload_stream(self, chunks, media, filename, key, producer=None, *, owner=None, fence=None):
        from .store import Problem

        try:
            artifacts.validate_metadata(media, filename)
            with tempfile.NamedTemporaryFile() as staged:
                sha, size = hashlib.sha256(), 0
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self.artifact_max_bytes:
                        raise ValueError("artifact_size_limit")
                    sha.update(chunk)
                    await asyncio.to_thread(staged.write, chunk)
                staged.flush()
                source = Path(staged.name)
                await asyncio.to_thread(
                    artifacts.validate_file, source, media, filename, self.artifact_max_bytes
                )
                digest = sha.hexdigest()
                # Reject known conflicts/quota failures before creating any external blob.
                # Publication repeats these checks under the workspace lock.
                async with self.database.sessions() as db:
                    existing = await db.scalar(
                        select(ArtifactRow).options(defer(ArtifactRow.content)).where(ArtifactRow.key == key)
                    )
                    if existing and (
                        existing.sha256,
                        existing.media_type,
                        existing.filename,
                        existing.producer_run_id,
                    ) != (digest, media, filename, producer):
                        raise Problem(409, "Artifact idempotency conflict")
                    total = await db.scalar(select(func.coalesce(func.sum(ArtifactRow.size_bytes), 0)))
                    if not existing and total + size > self.artifact_storage_bytes:
                        raise Problem(429, "artifact_storage_limit")
                    if producer and not existing:
                        feature = await db.get(ToolkitRunRow, producer)
                        if feature is None:
                            raise Problem(404, "Run not found")
                        general = await db.get(GeneralRunRow, producer)
                        await self.artifact_output_quota(db, feature.root_id, size, general is not None)
                # A crash here may leave an unreferenced immutable blob. It cannot publish a file
                # or create a duplicate effect; retries reuse the same content address.
                blob_key = await self.blobs.put(source, digest) if self.blobs else None
                content = None if self.blobs else await asyncio.to_thread(source.read_bytes)
        except ValueError as error:
            raise artifact_problem(error) from None
        except OSError:
            raise Problem(503, "artifact_storage_unavailable") from None
        async with self.database.sessions.begin() as db:
            await db.scalar(select(GateRow).where(GateRow.id == 1).with_for_update())
            old = await db.scalar(
                select(ArtifactRow).options(defer(ArtifactRow.content)).where(ArtifactRow.key == key)
            )
            if old:
                if (old.sha256, old.media_type, old.filename, old.producer_run_id) != (
                    digest,
                    media,
                    filename,
                    producer,
                ):
                    raise Problem(409, "Artifact idempotency conflict")
                return artifacts.ref(old)
            general = await db.get(GeneralRunRow, producer) if producer else None
            if producer:
                if general:
                    run, general, root_state = await self.general_lock(db, producer)
                    root = await self.locked(db, general.root_id)
                    feature = await db.get(ToolkitRunRow, producer)
                    if owner is not None:
                        operation = await db.get(GeneralOperationRow, owner[0])
                        if (
                            operation is None
                            or operation.run_id != producer
                            or operation.data.get("owner") != owner[1]
                            or root_state.data["fence"] != fence
                        ):
                            raise Problem(409, "extension_lease_fenced")
                else:
                    root, _, run, feature = await self.tree_lock(db, producer)
            total = await db.scalar(select(func.coalesce(func.sum(ArtifactRow.size_bytes), 0)))
            if total + size > self.artifact_storage_bytes:
                raise Problem(429, "artifact_storage_limit")
            if producer:
                await self.artifact_output_quota(db, root.id, size, general is not None)
            row = ArtifactRow(
                id=str(uuid4()),
                key=key,
                sha256=digest,
                media_type=media,
                filename=filename,
                size_bytes=size,
                content=content,
                blob_key=blob_key,
                producer_run_id=producer,
            )
            db.add(row)
            await db.flush()
            reference = artifacts.ref(row)
            if producer:
                feature.state = {
                    **feature.state,
                    "artifacts": [*feature.state.get("artifacts", []), reference.model_dump(mode="json")],
                }
                await self.emit(
                    db,
                    root,
                    "artifact:" + row.id,
                    "artifact.created",
                    {"origin_run_id": producer, "artifact": reference.model_dump(mode="json")},
                )
            return reference

    async def artifact_authorized(self, db, artifact_id, run_id):
        from .store import Problem

        if run_id is None:
            return
        feature = await db.get(ToolkitRunRow, run_id)
        general = await db.get(GeneralRunRow, run_id)
        allowed = (
            general.data.get("artifact_ids", [])
            if general
            else feature.state.get("artifact_ids", [])
            if feature
            else []
        )
        allowed = [*allowed, *(a["id"] for a in feature.state.get("artifacts", []))] if feature else allowed
        if artifact_id not in allowed:
            raise Problem(404, "artifact_not_authorized")

    async def artifact_ref(self, artifact_id, run_id=None):
        from .store import Problem

        async with self.database.sessions() as db:
            await self.artifact_authorized(db, artifact_id, run_id)
            row = await db.get(ArtifactRow, artifact_id, options=[defer(ArtifactRow.content)])
            if row is None:
                raise Problem(404, "Artifact not found")
            return artifacts.ref(row)

    async def artifact_chunks(self, artifact_id, run_id=None):
        from .store import Problem

        async with self.database.sessions() as db:
            await self.artifact_authorized(db, artifact_id, run_id)
            row = await db.get(ArtifactRow, artifact_id)
            if row is None:
                raise Problem(404, "Artifact not found")
            reference, blob_key = artifacts.ref(row), row.blob_key
            try:
                inline = artifacts.verify(row)
            except ValueError:
                raise Problem(409, "artifact_integrity_error") from None
        if blob_key and self.blobs is None:
            raise Problem(503, "artifact_storage_unavailable")
        sha, size = hashlib.sha256(), 0
        try:
            async for chunk in self.blobs.chunks(blob_key) if blob_key else bytes_chunks(inline):
                size += len(chunk)
                if size > reference.size_bytes:
                    raise Problem(409, "artifact_integrity_error")
                sha.update(chunk)
                yield chunk
        except (OSError, ValueError):
            raise Problem(409, "artifact_integrity_error") from None
        if size != reference.size_bytes or sha.hexdigest() != reference.sha256:
            raise Problem(409, "artifact_integrity_error")

    @asynccontextmanager
    async def artifact_file(self, artifact_id, run_id=None):
        reference = await self.artifact_ref(artifact_id, run_id)
        with tempfile.NamedTemporaryFile() as file:
            async for chunk in self.artifact_chunks(artifact_id, run_id):
                await asyncio.to_thread(file.write, chunk)
            file.flush()
            yield reference, Path(file.name)

    async def artifact(self, artifact_id, run_id=None):
        reference = await self.artifact_ref(artifact_id, run_id)
        return reference, b"".join([chunk async for chunk in self.artifact_chunks(artifact_id, run_id)])

    async def artifact_refs_locked(self, db, run_id, *, outputs_only=False):
        feature = await db.get(ToolkitRunRow, run_id)
        general = await db.get(GeneralRunRow, run_id)
        ids = (
            []
            if outputs_only
            else (
                general.data.get("artifact_ids", [])
                if general
                else feature.state.get("artifact_ids", [])
                if feature
                else []
            )
        )
        ids = (
            list(dict.fromkeys([*ids, *(a["id"] for a in feature.state.get("artifacts", []))]))
            if feature
            else ids
        )
        rows = {
            row.id: row
            for row in await db.scalars(
                select(ArtifactRow).options(defer(ArtifactRow.content)).where(ArtifactRow.id.in_(ids))
            )
        }
        return [artifacts.ref(rows[id]) for id in ids if id in rows]

    async def artifact_list(self, run_id=None):
        async with self.database.sessions() as db:
            if run_id:
                return await self.artifact_refs_locked(db, run_id)
            return [
                artifacts.ref(row)
                for row in await db.scalars(
                    select(ArtifactRow)
                    .options(defer(ArtifactRow.content))
                    .order_by(ArtifactRow.created_at)
                    .limit(100)
                )
            ]


class OperationArtifacts:
    """A handler receives run-scoped file access, never the store or global credentials."""

    def __init__(self, store, run_id, operation_id, owner, fence):
        self.store, self.run_id, self.operation_id, self.owner, self.fence = (
            store,
            run_id,
            operation_id,
            owner,
            fence,
        )

    async def resolve(self, artifact_id):
        return await self.store.artifact_ref(artifact_id, self.run_id)

    async def published(self, slot):
        key = "extension:" + hashlib.sha256((self.operation_id + ":" + slot).encode()).hexdigest()
        async with self.store.database.sessions() as db:
            row = await db.scalar(
                select(ArtifactRow)
                .options(defer(ArtifactRow.content))
                .where(ArtifactRow.key == key, ArtifactRow.producer_run_id == self.run_id)
            )
            return artifacts.ref(row) if row else None

    def file(self, artifact_id):
        return self.store.artifact_file(artifact_id, self.run_id)

    async def publish(self, slot, chunks, media_type, filename):
        from .store import Problem

        # Fence before consuming/storing guest bytes as well as at metadata commit.
        async with self.store.database.sessions.begin() as db:
            _, _, root = await self.store.general_lock(db, self.run_id)
            operation = await db.get(GeneralOperationRow, self.operation_id)
            if (
                operation is None
                or operation.run_id != self.run_id
                or operation.data.get("owner") != self.owner
                or root.data["fence"] != self.fence
            ):
                raise Problem(409, "extension_lease_fenced")
        key = "extension:" + hashlib.sha256((self.operation_id + ":" + slot).encode()).hexdigest()
        return await self.store.upload_stream(
            chunks,
            media_type,
            filename,
            key,
            self.run_id,
            owner=(self.operation_id, self.owner),
            fence=self.fence,
        )


class ArtifactReadHandler:
    version = 1

    async def execute(self, call):
        from jsonschema import Draft202012Validator

        from .store import Problem

        Draft202012Validator(READ_SCHEMA).validate(call.arguments)
        if call.artifacts is None:
            return {"error": "artifact_storage_unavailable"}
        try:
            ref = await call.artifacts.resolve(call.arguments["artifact_id"])
            if ref.media_type not in artifacts.TEXT_MEDIA:
                return {"error": "text_artifact_required"}
            offset, length = call.arguments.get("offset", 0), call.arguments.get("length", 2048)
            async with call.artifacts.file(ref.id) as (_, path):
                with path.open("rb") as file:
                    file.seek(offset)
                    content = file.read(length)
            return {
                "artifact": ref.model_dump(mode="json"),
                "offset": offset,
                "text": content.decode("utf-8", "replace"),
                "next_offset": min(ref.size_bytes, offset + len(content)),
                "truncated": offset + len(content) < ref.size_bytes,
                "untrusted": True,
            }
        except Problem as error:
            return {"error": error.detail}

    async def reconcile(self, call):
        return await self.execute(call)
