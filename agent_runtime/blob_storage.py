"""Private immutable blob storage. Database rows own identity and authorization."""

import asyncio
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import AsyncIterator, Protocol

CHUNK_BYTES = 65536


class BlobStorage(Protocol):
    async def put(self, source: Path, sha256: str) -> str: ...
    def chunks(self, key: str) -> AsyncIterator[bytes]: ...


class FileBlobStorage:
    """A shared persistent filesystem backend; keys contain no caller-controlled paths."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def path(self, key):
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("artifact_integrity_error")
        return self.root / key[:2] / key

    async def put(self, source, sha256):
        await asyncio.to_thread(self._put, source, sha256)
        return sha256

    def _put(self, source, sha256):
        target = self.path(sha256)
        target.parent.mkdir(mode=0o700, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=target.parent)
        try:
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as output, source.open("rb") as input:
                while chunk := input.read(CHUNK_BYTES):
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != sha256:
                raise ValueError("artifact_integrity_error")
            try:
                os.link(name, target)
            except FileExistsError:
                with target.open("rb") as existing:
                    if hashlib.file_digest(existing, "sha256").hexdigest() != sha256:
                        raise ValueError("artifact_integrity_error")
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    async def chunks(self, key):
        # The storage directory is operator-owned and must not be writable by guests.
        with self.path(key).open("rb") as source:
            while chunk := await asyncio.to_thread(source.read, CHUNK_BYTES):
                yield chunk


async def bytes_chunks(content):
    for offset in range(0, len(content), CHUNK_BYTES):
        yield content[offset : offset + CHUNK_BYTES]
