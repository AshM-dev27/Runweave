"""Validate and snapshot explicit local files before any upload; stream stable bytes on retries."""

import asyncio
import os
import stat
import tempfile
from pathlib import Path

from .artifacts import FILE_MEDIA, MAX_ARTIFACT, validate_file
from .blob_storage import CHUNK_BYTES
from .client_errors import PUBLIC_ERRORS, ClientError


class PreparedFiles(list):
    def __init__(self, files, max_bytes=MAX_ARTIFACT):
        super().__init__()
        self.directory = tempfile.TemporaryDirectory(prefix="runweave-files-")
        try:
            if files is None:
                return
            if not isinstance(files, (list, tuple)) or len(files) > 8:
                raise ClientError("files must be a list of at most eight local file paths.")
            for index, item in enumerate(files):
                path = Path(item)
                media = FILE_MEDIA.get(path.suffix.lower())
                if media is None:
                    raise ClientError(
                        "Unsupported file type. Use text, CSV, JSON, PDF, XLSX, images, binary files or a repository ZIP."
                    )
                target = Path(self.directory.name) / str(index)
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as source, target.open("wb") as output:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ClientError("Upload a regular file.")
                    size = 0
                    while chunk := source.read(CHUNK_BYTES):
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError("artifact_size_limit")
                        output.write(chunk)
                validate_file(target, media, path.name, max_bytes)
                self.append((path.name, media, target))
        except (OSError, TypeError):
            self.close()
            raise ClientError(
                "Cannot read an attached file. Check that each path is a readable regular file."
            ) from None
        except ValueError as exc:
            self.close()
            code, message = PUBLIC_ERRORS.get(
                str(exc), ("invalid_file", "Check the attached file's content and format.")
            )
            raise ClientError(message, code=code) from None
        except BaseException:
            self.close()
            raise

    def close(self):
        self.directory.cleanup()


async def file_chunks(path):
    with Path(path).open("rb") as source:
        while chunk := await asyncio.to_thread(source.read, CHUNK_BYTES):
            yield chunk
