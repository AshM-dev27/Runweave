"""Portable acceptance evidence and an offline, non-executing verifier.

The SHA-256 seal detects changes against a previously trusted digest. It is not
a signature: anyone can create a self-consistent bundle. Command execution and
model judgments remain attestations, even when all offline checks pass.
"""

import base64
import hashlib
import json
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .general_contracts import CompletionAssessment, Criterion, GeneralPolicy, TaskGoal

MAX_CONTENT_BYTES = 4194304
MAX_BUNDLE_BYTES = 16777216
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, Field(min_length=1, max_length=160)]


class EvidenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class EvidenceGoal(TaskGoal):
    # The runtime can add one integration criterion to twelve user criteria.
    criteria: list[Criterion] = Field(min_length=1, max_length=13)


class ReviewJudgment(EvidenceContract):
    criterion_id: str = Field(min_length=1, max_length=80)
    verdict: Literal["pass", "repair", "defer"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=400)


class ReviewAttestation(EvidenceContract):
    verdict: Literal["pass", "repair", "defer"]
    reason: str | None = Field(default=None, max_length=100)
    judgments: list[ReviewJudgment] = Field(default_factory=list, max_length=13)


class EvidenceAssessment(CompletionAssessment):
    review: ReviewAttestation | None = None


class EvidenceOutput(EvidenceContract):
    answer: str = Field(max_length=16000)
    value: float | None = None


def evidence_path(value):
    parts = value.split("/")
    if (
        not value
        or len(value.encode()) > 200
        or unicodedata.normalize("NFC", value) != value
        or "\\" in value
        or len(parts) > 16
        or any(
            p in {"", ".", ".."}
            or p.casefold() in {".git", ".runtime", "__runtime__"}
            or len(p.encode()) > 100
            or any(ord(c) < 32 or ord(c) == 127 for c in p)
            for p in parts
        )
    ):
        raise ValueError("Invalid evidence path")
    return value


class EvidenceFile(EvidenceContract):
    path: str = Field(min_length=1, max_length=200)
    sha256: Digest
    size_bytes: int = Field(ge=0, le=262144)

    _path = field_validator("path")(evidence_path)


class EvidenceManifest(EvidenceContract):
    schema_version: Literal[3] = 3
    workspace_id: Identity
    parents: list[Digest] = Field(max_length=64)
    files: list[EvidenceFile] = Field(max_length=256)


class EvidenceRevision(EvidenceContract):
    revision_id: Digest
    manifest: EvidenceManifest


class EvidenceBlob(EvidenceContract):
    sha256: Digest
    content_base64: str = Field(max_length=349528)


class EvidenceWorkspace(EvidenceContract):
    workspace_id: Identity
    branch_id: Identity
    revision_id: Digest


class SourceDetails(EvidenceContract):
    path: str = Field(min_length=1, max_length=200)
    offset: int = Field(ge=0, le=262144)
    quote: str = Field(min_length=1, max_length=4096)

    _path = field_validator("path")(evidence_path)


class CommandDetails(EvidenceContract):
    argv: list[Annotated[str, Field(max_length=4096)]] = Field(min_length=1, max_length=32)
    cwd: str = Field(max_length=200)
    exit_code: int = Field(ge=0, le=255)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class EvidenceReceipt(EvidenceContract):
    id: Identity
    criterion_id: str = Field(min_length=1, max_length=80)
    check_id: str | None = Field(default=None, max_length=80)
    outcome: Literal["pass"]
    method: Literal["source", "command", "sha256", "bytes"]
    provenance: Literal["user", "runtime", "supporting"]
    operation_id: Identity
    goal_version: int = Field(ge=1)
    branch_id: Identity
    revision_id: Digest
    spec_hash: Digest
    dependency_hash: Digest
    source: SourceDetails | None = None
    command: CommandDetails | None = None


class EvidenceConfiguration(EvidenceContract):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    model_registration_id: Digest
    review_registration_id: Digest | None = None
    tool_registration_ids: dict[Identity, Digest] = Field(max_length=32)
    skill_registration_ids: dict[Identity, Digest] = Field(max_length=8)
    policy: GeneralPolicy
    policy_sha256: Digest
    context_policy_version: int = Field(ge=1)
    executor_version: int = Field(ge=1)
    execution_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class EvidencePayload(EvidenceContract):
    run_id: Identity
    root_run_id: Identity
    parent_run_id: Identity | None = None
    status: Literal["completed"]
    completion_operation_id: Identity
    terminal_state_version: int = Field(ge=2)
    goal: EvidenceGoal
    goal_sha256: Digest
    output: EvidenceOutput
    output_sha256: Digest
    assessment: EvidenceAssessment
    assessment_sha256: Digest
    configuration: EvidenceConfiguration
    workspace: EvidenceWorkspace | None = None
    revisions: list[EvidenceRevision] = Field(default_factory=list, max_length=64)
    receipts: list[EvidenceReceipt] = Field(default_factory=list, max_length=156)
    blobs: list[EvidenceBlob] = Field(default_factory=list, max_length=256)


class AcceptanceBundle(EvidenceContract):
    schema_version: Literal[1] = 1
    hash_algorithm: Literal["sha256"] = "sha256"
    payload: EvidencePayload
    sha256: Digest


class EvidenceVerification(EvidenceContract):
    schema_version: Literal[1] = 1
    valid: bool
    integrity_verified: bool
    origin_authenticated: Literal[False] = False
    commands_executed: Literal[False] = False
    deterministic_checks: list[str] = Field(default_factory=list)
    attestations: list[str] = Field(default_factory=list)
    judgments: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def evidence_digest(value):
    """Canonical JSON hashing matches the runtime's immutable revision format."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def load_evidence_bundle(data: bytes | str) -> AcceptanceBundle:
    """Parse a bounded untrusted JSON bundle; reject ambiguous object keys."""
    if not isinstance(data, (bytes, str)):
        raise ValueError("Evidence must be JSON text or bytes")
    try:
        encoded = data.encode() if isinstance(data, str) else data
        if len(encoded) > MAX_BUNDLE_BYTES:
            raise ValueError("Evidence bundle exceeds the serialized size limit")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("Duplicate evidence JSON key")
                result[key] = value
            return result

        def constant(_):
            raise ValueError("Nonfinite evidence JSON number")

        raw = json.loads(encoded, object_pairs_hook=pairs, parse_constant=constant)
        from .result_contracts import bounded_json

        bounded_json(raw, max_depth=32, max_nodes=200000)
        # JSON can encode lone surrogates; all exported text must have real UTF-8 bytes.
        json.dumps(raw, ensure_ascii=False, allow_nan=False).encode()
        return AcceptanceBundle.model_validate(raw, strict=True)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("Invalid evidence JSON encoding or nesting") from exc


def _operation(identity, run_id):
    prefix = run_id + ":action:"
    return identity.startswith(prefix) and identity[len(prefix) :].isdigit()


def verify_evidence_bundle(bundle: AcceptanceBundle | dict) -> EvidenceVerification:
    """Check bundled bytes and references without DB, network, models, or execution.

    ``valid`` means internal consistency and the stated deterministic assertions,
    not authenticated origin or independently proven task correctness.
    """
    limitations = [
        "Unsigned hashes do not authenticate the exporter or prevent deliberate resealing.",
        "Command outcomes and pinned registration identities are runtime attestations; commands are not rerun.",
        "Model assessments and semantic review are judgments, not independently verified facts.",
        "A source quote match proves occurrence in captured bytes, not support for the answer.",
        "Freshness is relative to the bundled accepted revision, not current external state.",
    ]
    try:
        raw = bundle.model_dump(mode="json") if isinstance(bundle, AcceptanceBundle) else bundle
        encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > MAX_BUNDLE_BYTES:
            raise ValueError("size")
        bundle = AcceptanceBundle.model_validate(raw, strict=True)
    except (ValidationError, ValueError, TypeError, RecursionError, UnicodeError):
        return EvidenceVerification(
            valid=False, integrity_verified=False, errors=["invalid_bundle_schema"], limitations=limitations
        )
    p = bundle.payload
    errors, checks, attestations, judgments = [], [], [], []
    integrity = bundle.sha256 == evidence_digest(p)
    if not integrity:
        errors.append("bundle_hash_mismatch")
    for name in ("goal", "output", "assessment"):
        if evidence_digest(getattr(p, name)) != getattr(p, name + "_sha256"):
            errors.append(name + "_hash_mismatch")
    if evidence_digest(p.configuration.policy) != p.configuration.policy_sha256:
        errors.append("policy_hash_mismatch")
    if (p.parent_run_id is None) != (p.root_run_id == p.run_id) or p.parent_run_id == p.run_id:
        errors.append("run_tree_mismatch")
    if not _operation(p.completion_operation_id, p.run_id):
        errors.append("completion_operation_mismatch")
    assessment = p.assessment
    if (
        not assessment.accepted
        or assessment.remaining_gaps
        or assessment.goal_version != p.goal.version
        or assessment.state_version + 1 != p.terminal_state_version
        or assessment.revision_id != (p.workspace.revision_id if p.workspace else None)
    ):
        errors.append("acceptance_binding_mismatch")
    from .result_contracts import validate_result_contract

    contract_errors = validate_result_contract(p.goal.result_contract, p.output.answer)
    errors.extend(contract_errors)
    if p.goal.result_contract is not None and not contract_errors:
        checks.append("answer:" + p.goal.result_contract.kind)
    revisions = {r.revision_id: r.manifest for r in p.revisions}
    if len(revisions) != len(p.revisions):
        errors.append("duplicate_revision")
    if p.workspace:
        if p.workspace.revision_id not in revisions:
            errors.append("missing_accepted_revision")
    elif p.revisions or p.receipts or p.blobs:
        errors.append("evidence_without_workspace")
    entries = {}
    for revision in p.revisions:
        manifest = revision.manifest
        if evidence_digest(manifest) != revision.revision_id:
            errors.append("revision_hash_mismatch:" + revision.revision_id)
        if not p.workspace or manifest.workspace_id != p.workspace.workspace_id:
            errors.append("revision_workspace_mismatch:" + revision.revision_id)
        names = [f.path for f in manifest.files]
        folded = {name.casefold() for name in names}
        if (
            len(folded) != len(names)
            or names != sorted(names)
            or any(
                "/".join(name.split("/")[:i]) in folded
                for name in folded
                for i in range(1, len(name.split("/")))
            )
        ):
            errors.append("invalid_manifest_paths:" + revision.revision_id)
        if sum(f.size_bytes for f in manifest.files) > MAX_CONTENT_BYTES:
            errors.append("revision_size_limit:" + revision.revision_id)
        entries[revision.revision_id] = {f.path: f for f in manifest.files}
    blobs, total = {}, 0
    for blob in p.blobs:
        try:
            content = base64.b64decode(blob.content_base64, validate=True)
        except ValueError:
            errors.append("invalid_blob_encoding:" + blob.sha256)
            continue
        if blob.sha256 in blobs:
            errors.append("duplicate_blob:" + blob.sha256)
        if len(content) > 262144 or evidence_digest(content) != blob.sha256:
            errors.append("blob_hash_or_size_mismatch:" + blob.sha256)
        total += len(content)
        blobs[blob.sha256] = content
    if total > MAX_CONTENT_BYTES:
        errors.append("content_size_limit")
    for files in entries.values():
        for item in files.values():
            if item.sha256 in blobs and len(blobs[item.sha256]) != item.size_bytes:
                errors.append("manifest_blob_size_mismatch:" + item.path)
    goals = {c.id: c for c in p.goal.criteria}
    dispositions = {d.criterion_id: d for d in assessment.criteria}
    receipts = {r.id: r for r in p.receipts}
    if len(dispositions) != len(assessment.criteria) or set(dispositions) - goals.keys():
        errors.append("invalid_assessment_criteria")
    if len(receipts) != len(p.receipts):
        errors.append("duplicate_receipt")
    for disposition in assessment.criteria:
        if len(set(disposition.evidence_ids)) != len(disposition.evidence_ids) or any(
            vid in receipts and receipts[vid].criterion_id != disposition.criterion_id
            for vid in disposition.evidence_ids
        ):
            errors.append("invalid_criterion_evidence:" + disposition.criterion_id)
    cited = {vid for d in assessment.criteria for vid in d.evidence_ids}
    if cited != receipts.keys():
        errors.append("receipt_reference_mismatch")
    used_blobs, used_revisions = set(), {p.workspace.revision_id} if p.workspace else set()
    good = set()
    for receipt in p.receipts:
        before = len(errors)
        criterion = goals.get(receipt.criterion_id)
        disposition = dispositions.get(receipt.criterion_id)
        manifest = revisions.get(receipt.revision_id)
        used_revisions.add(receipt.revision_id)
        suffix = ":source" if receipt.method == "source" else ":verification"
        if (
            criterion is None
            or disposition is None
            or receipt.id not in disposition.evidence_ids
            or not _operation(receipt.operation_id, p.run_id)
            or receipt.id != receipt.operation_id + suffix
            or receipt.goal_version != p.goal.version
            or not p.workspace
            or receipt.branch_id != p.workspace.branch_id
            or manifest is None
        ):
            errors.append("receipt_binding_mismatch:" + receipt.id)
            continue
        if receipt.method == "source":
            source = receipt.source
            if (
                criterion.evidence_policy != "source"
                or receipt.check_id is not None
                or receipt.provenance != "runtime"
                or source is None
                or receipt.command is not None
            ):
                errors.append("source_receipt_mismatch:" + receipt.id)
                continue
            entry = entries[receipt.revision_id].get(source.path)
            content = blobs.get(entry.sha256) if entry else None
            if entry:
                used_blobs.add(entry.sha256)
            quote = source.quote.encode()
            if (
                entry is None
                or content is None
                or len(quote) > 4096
                or receipt.dependency_hash != entry.sha256
                or receipt.spec_hash != evidence_digest({"quote": source.quote})
                or content[source.offset : source.offset + len(quote)] != quote
            ):
                errors.append("source_bytes_mismatch:" + receipt.id)
            else:
                checks.append("source_quote:" + receipt.id)
                judgments.append("source_support:" + receipt.criterion_id)
                if receipt.revision_id != p.workspace.revision_id:
                    limitations.append("Historical source revision: " + receipt.id)
        else:
            spec = next((s for s in criterion.checks if s.id == receipt.check_id), None)
            if (
                criterion.evidence_policy != "check"
                or spec is None
                or receipt.provenance not in {"user", "runtime"}
                or receipt.provenance != criterion.origin
                or receipt.revision_id != p.workspace.revision_id
                or receipt.dependency_hash != evidence_digest([f.model_dump() for f in manifest.files])
                or receipt.spec_hash != evidence_digest(spec)
                or receipt.method != spec.kind
                or receipt.source is not None
            ):
                errors.append("check_binding_mismatch:" + receipt.id)
                continue
            if spec.kind == "command":
                command = receipt.command
                if (
                    command is None
                    or command.argv != spec.argv
                    or command.cwd != spec.cwd
                    or command.exit_code != spec.expected_exit
                    or command.image_digest != p.configuration.execution_image_digest
                ):
                    errors.append("command_attestation_mismatch:" + receipt.id)
                else:
                    attestations.append("command:" + receipt.id)
            else:
                entry = entries[receipt.revision_id].get(spec.path)
                content = blobs.get(entry.sha256) if entry else None
                if entry:
                    used_blobs.add(entry.sha256)
                try:
                    matches = content is not None and (
                        evidence_digest(content) == spec.expected
                        if spec.kind == "sha256"
                        else content == base64.b64decode(spec.expected, validate=True)
                    )
                except ValueError:
                    matches = False
                if not matches or receipt.command is not None:
                    errors.append("file_assertion_failed:" + receipt.id)
                else:
                    checks.append(spec.kind + ":" + receipt.id)
        if len(errors) == before:
            good.add(receipt.id)
    if set(blobs) != used_blobs:
        errors.append("unexpected_or_missing_blobs")
    if set(revisions) != used_revisions:
        errors.append("unexpected_or_missing_revisions")
    for criterion in p.goal.criteria:
        disposition = dispositions.get(criterion.id)
        if not criterion.required:
            continue
        if disposition is None or disposition.disposition != "satisfied":
            errors.append("criterion_unsatisfied:" + criterion.id)
            continue
        valid = [receipts[v] for v in disposition.evidence_ids if v in good]
        if criterion.evidence_policy == "assessment":
            if not disposition.assessment:
                errors.append("missing_model_assessment:" + criterion.id)
            judgments.append("assessment:" + criterion.id)
        elif criterion.evidence_policy == "check":
            if not valid or not {s.id for s in criterion.checks}.issubset({r.check_id for r in valid}):
                errors.append("missing_check_evidence:" + criterion.id)
        elif not any(r.method == "source" for r in valid):
            errors.append("missing_source_evidence:" + criterion.id)
    policy = p.configuration.policy.review
    if policy:
        review = assessment.review
        expected = {c.id for c in p.goal.criteria if c.required} or {"runtime.review.outcome"}
        if (
            p.configuration.review_registration_id is None
            or review is None
            or review.verdict != "pass"
            or {j.criterion_id for j in review.judgments} != expected
            or len(review.judgments) != len(expected)
            or any(j.verdict != "pass" or j.confidence < policy.threshold for j in review.judgments)
        ):
            errors.append("semantic_review_attestation_mismatch")
        else:
            judgments.append("semantic_review")
    elif assessment.review is not None:
        errors.append("unexpected_semantic_review")
    attestations.append("runtime_acceptance_and_registration_identities")
    return EvidenceVerification(
        valid=not errors,
        integrity_verified=integrity and not any("hash" in error for error in errors),
        deterministic_checks=sorted(set(checks)),
        attestations=sorted(set(attestations)),
        judgments=sorted(set(judgments)),
        errors=sorted(set(errors)),
        limitations=limitations,
    )


async def build_evidence_bundle(store, run_id: str) -> AcceptanceBundle:
    """Export a completed v3 run through an explicit public-field allowlist."""
    from .general_db import GeneralOperationRow, GeneralRecordRow
    from .project_store import fail

    async with store.database.sessions.begin() as db:
        row, gr, _ = await store.general_lock(db, run_id, active=False)
        state = gr.data["task_state"]
        assessment = gr.data.get("completion_assessment")
        if (
            row.status != "completed"
            or not assessment
            or not assessment.get("accepted")
            or not row.output
            or (await store.public(db, row)).outcome != "succeeded"
        ):
            fail("evidence_requires_accepted_completion", 409)
        operation_id = (state.get("checkpoint_id") or "").removesuffix(":checkpoint")
        operation = await db.get(GeneralOperationRow, operation_id)
        action = operation.data.get("decision", {}).get("action", {}) if operation else {}
        if (
            operation is None
            or operation.run_id != run_id
            or operation.data.get("status") != "complete"
            or action.get("kind") != "complete"
            or operation.data.get("result") != assessment
            or action.get("answer") != row.output["answer"]
            or action.get("value") != row.output.get("value")
        ):
            fail("evidence_completion_binding_missing", 409)
        goal = EvidenceGoal.model_validate(gr.data["goal"], strict=True)
        assessed = EvidenceAssessment.model_validate(assessment, strict=True)
        output = EvidenceOutput.model_validate(row.output, strict=True)
        policy = GeneralPolicy.model_validate(gr.data["policy"], strict=True)
        configuration = EvidenceConfiguration(
            provider=row.config["provider"],
            model=row.config["model"],
            model_registration_id=row.registration_id,
            review_registration_id=gr.data.get("review_registration_id"),
            tool_registration_ids={a: t["registration_id"] for a, t in gr.data["tools"].items()},
            skill_registration_ids={a: s["registration_id"] for a, s in gr.data.get("skills", {}).items()},
            policy=policy,
            policy_sha256=evidence_digest(policy),
            context_policy_version=gr.data.get("context_policy_version", 1),
            executor_version=gr.data.get("executor_version", 1),
            execution_image_digest=gr.data["operator"]["image_digest"],
        )
        workspace, revisions, receipts, blobs = None, {}, [], {}
        if gr.data.get("workspace"):
            branch, grant = await store.granted_branch(db, run_id)
            if branch.head != assessed.revision_id or branch.head != state["head"]:
                fail("evidence_revision_conflict", 409)
            workspace = EvidenceWorkspace(
                workspace_id=branch.workspace_id, branch_id=branch.id, revision_id=branch.head
            )

            async def revision(identity):
                if identity not in grant.data["revisions"]:
                    fail("evidence_revision_not_authorized", 409)
                if identity not in revisions:
                    saved = await store.project_revision(db, branch.workspace_id, identity)
                    revisions[identity] = EvidenceRevision(revision_id=identity, manifest=saved.data)
                return revisions[identity]

            await revision(branch.head)
        ids = sorted({vid for d in assessed.criteria for vid in d.evidence_ids})
        for identity in ids:
            record = await db.get(GeneralRecordRow, identity)
            if record is None or record.run_id != run_id or record.kind != "verification" or not workspace:
                fail("evidence_receipt_missing", 409)
            value, details = record.data, record.data.get("details", {})
            fields = {
                k: value[k]
                for k in (
                    "criterion_id",
                    "check_id",
                    "outcome",
                    "method",
                    "provenance",
                    "operation_id",
                    "goal_version",
                    "branch_id",
                    "revision_id",
                    "spec_hash",
                    "dependency_hash",
                )
            }
            source = command = None
            if value["method"] == "source":
                source = {k: details[k] for k in ("path", "offset", "quote")}
                name = source["path"]
            elif value["method"] == "command":
                command = {k: details[k] for k in ("argv", "cwd", "exit_code", "image_digest")}
                name = None
            else:
                spec = next(
                    (
                        s
                        for c in goal.criteria
                        if c.id == value["criterion_id"]
                        for s in c.checks
                        if s.id == value["check_id"]
                    ),
                    None,
                )
                if spec is None:
                    fail("evidence_spec_missing", 409)
                name = spec.path
            await revision(value["revision_id"])
            op = await db.get(GeneralOperationRow, value["operation_id"])
            if (
                op is None
                or op.run_id != run_id
                or op.data.get("status") != "complete"
                or identity not in (op.data.get("result") or {}).get("verification_ids", [])
            ):
                fail("evidence_operation_missing", 409)
            if name is not None:
                _, _, files = await store.branch_files(db, run_id, value["revision_id"])
                if name not in files:
                    fail("evidence_file_missing", 409)
                content = files[name]
                blobs[evidence_digest(content)] = content
                if sum(map(len, blobs.values())) > MAX_CONTENT_BYTES:
                    fail("evidence_content_limit", 413)
            receipts.append(EvidenceReceipt(id=identity, **fields, source=source, command=command))
        payload = EvidencePayload(
            run_id=run_id,
            root_run_id=gr.root_id,
            parent_run_id=gr.parent_id,
            status="completed",
            completion_operation_id=operation_id,
            terminal_state_version=state["version"],
            goal=goal,
            goal_sha256=evidence_digest(goal),
            output=output,
            output_sha256=evidence_digest(output),
            assessment=assessed,
            assessment_sha256=evidence_digest(assessed),
            configuration=configuration,
            workspace=workspace,
            revisions=[revisions[r] for r in sorted(revisions)],
            receipts=receipts,
            blobs=[
                EvidenceBlob(sha256=h, content_base64=base64.b64encode(b).decode())
                for h, b in sorted(blobs.items())
            ],
        )
        bundle = AcceptanceBundle(payload=payload, sha256=evidence_digest(payload))
        if not verify_evidence_bundle(bundle).valid:
            fail("evidence_inconsistent", 409)
        return bundle
