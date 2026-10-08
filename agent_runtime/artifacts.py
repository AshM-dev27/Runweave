"""Bounded immutable artifacts; repository archives retain their text-only contract."""

import codecs
import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath

from .tool_contracts import ArtifactRef

MAX_ARTIFACT = 16 * 1024 * 1024
MAX_STORAGE = 512 * 1024 * 1024
TEXT_MEDIA = {"text/plain", "text/markdown", "text/csv", "application/json", "text/x-diff"}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MEDIA = TEXT_MEDIA | {
    "application/zip",
    "application/pdf",
    XLSX,
    "image/png",
    "image/jpeg",
    "image/webp",
    "application/octet-stream",
}
FILE_MEDIA = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".zip": "application/zip",
    ".diff": "text/x-diff",
    ".patch": "text/x-diff",
    ".pdf": "application/pdf",
    ".xlsx": XLSX,
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bin": "application/octet-stream",
}


def safe_path(name):
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
        and "\x00" not in name
        and len(name) <= 200
    )


def archive(content):
    result = {}
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            if len(z.infolist()) > 100:
                raise ValueError
            total = 0
            for item in z.infolist():
                if item.is_dir():
                    continue
                mode = item.external_attr >> 16
                total += item.file_size
                if (
                    not safe_path(item.filename)
                    or item.filename in result
                    or (stat.S_IFMT(mode) not in {0, stat.S_IFREG})
                    or item.flag_bits & 1
                    or item.file_size > 65536
                    or total > 1048576
                    or item.file_size > max(1, item.compress_size) * 100
                ):
                    raise ValueError
                data = z.read(item)
                result[item.filename] = data.decode("utf-8")
    except Exception:
        raise ValueError("invalid_repository_archive") from None
    if not result:
        raise ValueError("empty_repository_archive")
    return result


def validate_metadata(media, filename):
    if media not in MEDIA:
        raise ValueError("unsupported_media_type")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", filename):
        raise ValueError("invalid_filename")


def validate_file(source, media, filename, max_bytes=MAX_ARTIFACT):
    """Validate a staged file without expanding binary documents into host memory."""
    validate_metadata(media, filename)
    if source.stat().st_size > max_bytes:
        raise ValueError("artifact_size_limit")
    with source.open("rb") as file:
        prefix = file.read(16)
        if media in TEXT_MEDIA:
            file.seek(0)
            decoder = codecs.getincrementaldecoder("utf-8")()
            try:
                while chunk := file.read(65536):
                    if "\x00" in decoder.decode(chunk):
                        raise ValueError
                decoder.decode(b"", final=True)
                if media == "application/json":
                    file.seek(0)
                    json.load(file)
            except (ValueError, UnicodeError, RecursionError):
                raise ValueError("invalid_artifact_content") from None
        elif media == "application/zip":
            file.seek(0)
            archive(file.read())
        elif media == XLSX:
            try:
                with zipfile.ZipFile(source) as z:
                    entries = z.infolist()
                    names = {e.filename for e in entries}
                    if (
                        not {"[Content_Types].xml", "xl/workbook.xml"}.issubset(names)
                        or len(entries) > 10000
                        or len(names) != len(entries)
                        or sum(e.file_size for e in entries) > 256 * 1024 * 1024
                        or any(not safe_path(e.filename) or e.flag_bits & 1 for e in entries)
                    ):
                        raise ValueError
            except (ValueError, zipfile.BadZipFile):
                raise ValueError("invalid_artifact_content") from None
        elif (
            media == "application/pdf"
            and not prefix.startswith(b"%PDF-")
            or media == "image/png"
            and not prefix.startswith(b"\x89PNG\r\n\x1a\n")
            or media == "image/jpeg"
            and not prefix.startswith(b"\xff\xd8\xff")
            or media == "image/webp"
            and not (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP")
        ):
            raise ValueError("invalid_artifact_content")


def validate(content, media, filename):
    if len(content) > MAX_ARTIFACT:
        raise ValueError("artifact_size_limit")
    validate_metadata(media, filename)
    if media == "application/zip":
        archive(content)
    elif media in TEXT_MEDIA:
        try:
            text = content.decode("utf-8")
            if "\x00" in text:
                raise ValueError
            if media == "application/json":
                json.loads(text)
        except Exception:
            raise ValueError("invalid_artifact_content") from None
    else:
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile() as file:
            file.write(content)
            file.flush()
            validate_file(Path(file.name), media, filename)
    return hashlib.sha256(content).hexdigest()


def ref(row):
    return ArtifactRef(**{k: getattr(row, k) for k in ArtifactRef.model_fields})


def verify(row):
    if row.blob_key is not None:
        if row.content is not None or row.blob_key != row.sha256:
            raise ValueError("artifact_integrity_error")
        return None
    if row.content is None:
        raise ValueError("artifact_integrity_error")
    if len(row.content) != row.size_bytes or hashlib.sha256(row.content).hexdigest() != row.sha256:
        raise ValueError("artifact_integrity_error")
    return row.content
