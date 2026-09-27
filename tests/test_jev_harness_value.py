"""Offline controls for the cheaper alternative to model-based duplicate detection."""

import base64
import hashlib

from scripts.jev_harness_value import duplicate_write


def write(old, new, **extra):
    return {
        "path": "result.txt",
        "expected_sha256": hashlib.sha256(old).hexdigest() if old is not None else None,
        "content_base64": base64.b64encode(new).decode(),
        **extra,
    }


def test_only_exact_replacement_of_every_file_is_skippable():
    assert duplicate_write([write(b"12\n", b"12\n")])
    assert not duplicate_write([write(b"12\n", b"12")])
    assert not duplicate_write([write(b"12\n", b"12\n"), write(b"20\n", b"21\n")])
    assert not duplicate_write([write(None, b"")])


def test_unknown_malformed_or_delete_never_classified_as_duplicate():
    assert not duplicate_write([])
    assert not duplicate_write([{"path": "result.txt"}])
    assert not duplicate_write([write(b"12\n", b"12\n", delete=True)])
    assert not duplicate_write([write(b"12\n", b"12\n", content_base64="%%%")])
