# Artifact storage and transfer

Artifacts are immutable files identified by application IDs and SHA-256 digests. Runs explicitly attach input IDs; handlers can access those inputs and files produced by the same run. Model context, events, receipts and Temporal history carry metadata/references, never binary file bodies. Small text excerpts can be read through the granted `artifact_read` capability.

## Storage

Without `ARTIFACT_STORAGE_PATH`, new artifacts remain inline in PostgreSQL. With it, new bytes are stored in a private content-addressed directory; PostgreSQL stores the file's identity, digest, media type, size, producer and blob key. The local Compose stack configures a persistent `artifact-data` volume shared by the API and worker. Existing inline files remain readable. `BlobStorage` is a private backend interface; the included implementation is shared filesystem storage. An S3-compatible implementation is a future adapter, not included in this release.

Set the same persistent path/backend on the API and every worker. All must see identical bytes, including after replacement. The production Compose profile mounts `ARTIFACT_STORAGE_HOST_PATH` at `/var/lib/runweave/artifacts` in both services. Provision it with owner UID/GID 10001 and restrictive permissions before starting services. Multiple hosts require a shared filesystem with atomic link and directory-fsync support; independent local directories are unsuitable.

Default operator limits:

| Setting | Default | Meaning |
| --- | --- | --- |
| `ARTIFACT_MAX_BYTES` | 16 MiB | Maximum bytes per upload/publication |
| `ARTIFACT_STORAGE_BYTES` | 512 MiB | Workspace total of published artifact sizes, including deduplicated bytes per reference |
| `ARTIFACT_RUN_BYTES` | 64 MiB | Produced files across a general root and its children |

There are at most eight produced artifacts per root tree. Existing toolkit runs retain their 1 MiB produced-file total; their isolated analysis tools and project workspaces retain their own smaller limits. Filesystem capacity and temporary staging space must also accommodate concurrent transfers. Exhausted/missing storage is an explicit error.

Publication validates staged bytes, writes a durable blob, then commits metadata and `artifact.created` in one transaction. An operation/output-path identity makes retries return the original file. A crash before metadata commits can leave an unreferenced content blob; it is never exposed as a completed output. Back up the full blob directory, including such blobs. Automatic orphan deletion is deliberately absent: never delete a blob based only on a concurrent database scan or while publication/recovery is active. Monitor physical disk usage separately from published-byte accounting.

## Formats

UTF-8 text, Markdown, CSV, JSON, diffs, PDF, XLSX, PNG, JPEG, WebP and opaque binary are supported. Text/JSON encoding and syntax are checked. Binary signatures/XLSX container structure are checked; this does not validate document meaning, formulas, rendering or business correctness. Documents are processed inside authorized isolated computers. Parsers and packages must be installed in an operator-approved E2B template; guest internet remains disabled.

`application/zip` retains the bounded text-only repository-archive contract: safe paths, no encryption/special files, at most 100 entries, 64 KiB per entry and 1 MiB expanded total. XLSX has a separate container validator and is not interpreted as a repository archive.

## Client and API

```python
result = await client.run(
    agent_id,
    "Process the attached invoice CSV and return a cleaned CSV and reconciliation report.",
    files=["invoices.csv"],
    idempotency_key="invoice-batch-2026-10-07",
)
for file in result.files:
    await client.download_file(file.id, output_directory / file.filename)
```

The agent must have `e2b_files` granted and a configured live model to interpret natural language. Default fake models execute scripted demonstrations only. `client.run()` snapshots and validates all local files before any upload, then streams them; retries use identical bytes and keys. `client.upload_file(path)` uploads a file separately. `client.download_file(id, path)` verifies streamed bytes before atomically replacing the destination; failed downloads preserve the existing destination. `client.download(id)` remains available for callers who need bytes in memory.

`POST /v1/artifacts` accepts raw streamed bytes with `Content-Type`, `X-Filename` and `Idempotency-Key`. Downloads include digest ETag, length, attachment disposition and `nosniff`. Missing/corrupt blobs return an error before response headers are sent. `/v1/runs/{id}/artifacts` lists authorized inputs and produced files; `RunResult.files` contains produced files only.

## Backup and restore

Keep admission and workers stopped while taking coordinated PostgreSQL/Temporal/blob snapshots. Preserve every blob referenced by the database; deduplication means one blob may back several artifacts. Restore original metadata and blobs together, then verify downloads and resume reconciliation before reopening admission. PostgreSQL-only backup is insufficient for external blobs. Keep an existing blob backend/path available while restoring older runs; changing the path does not migrate files. No existing inline artifacts are rewritten automatically.
