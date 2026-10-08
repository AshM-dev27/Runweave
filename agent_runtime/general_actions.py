"""V3 capability implementations; all effects run in activities under durable intents."""

import base64
import copy
import json
import time
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select

from .db import NoteRow, RunRow, ToolkitRunRow
from .general_contracts import Assignment, Criterion, StepDecision, TaskGoal, TaskState
from .general_db import (
    GeneralAttemptRow,
    GeneralEffectRow,
    GeneralOperationRow,
    GeneralRunRow,
    ProjectBlobRow,
)
from .project_store import covered, decode, digest, fail, path, three_way


class GeneralActions:
    async def general_operation(self, run_id, step, decision):
        """Prepare/execute local actions atomically; commands leave a durable intent."""
        decision = StepDecision.model_validate(decision).model_dump()
        op_id = f"{run_id}:action:{step}"
        async with self.database.sessions.begin() as db:
            row, gr, root = await self.general_lock(db, run_id, active=False)
            old = await db.get(GeneralOperationRow, op_id)
            if old:
                if old.fingerprint != digest(decision):
                    fail("operation_conflict", 409)
                if old.data.get("result") is not None:
                    return old.data["result"]
                # A running hosted tool must reach its deadline and clean up even when
                # the root's active budget has just expired. No new dispatch is allowed.
                if old.data.get("external") and old.data.get("deferred"):
                    return {"external": True, "operation_id": op_id}
                await self.general_lock(db, run_id)
                if old.data.get("external"):
                    return {"external": True, "operation_id": op_id}
                if old.data.get("command"):
                    return {"command": True, "operation_id": op_id}
                operation = old
            else:
                await self.general_lock(db, run_id)
                if step != gr.data["step"]:
                    fail("stale_decision", 409)
                operation = GeneralOperationRow(
                    id=op_id,
                    run_id=run_id,
                    fingerprint=digest(decision),
                    data={"status": "pending", "decision": decision, "sequence": step * 2 + 1},
                )
                db.add(operation)
                await db.flush()
            model_op = await db.get(GeneralOperationRow, f"{run_id}:model:{step}")
            if model_op and model_op.data.get("semantic_result") and model_op.data.get("result") == decision:
                b = model_op.data["binding"]
                if gr.data.get("workspace"):
                    branch, _ = await self.granted_branch(db, run_id)
                    if branch.head != b["state"]["head"]:
                        fail("stale_decision", 409)
                if (
                    gr.data["task_state"]["version"],
                    gr.data["goal"]["version"],
                    gr.data["task_state"]["head"],
                ) != (b["state"]["version"], b["goal_version"], b["state"]["head"]):
                    fail("stale_decision", 409)
            action = decision["action"]
            if decision["goal_additions"] and not operation.data.get("refined"):
                state = copy.deepcopy(gr.data)
                goal = state["goal"]
                ids = {c["id"] for c in goal["criteria"]}
                additions = decision["goal_additions"]
                if (
                    len(goal["criteria"]) + len(additions) > 12
                    or any(c["id"] in ids or c["id"].startswith("runtime.") for c in additions)
                    or len({c["id"] for c in additions}) != len(additions)
                ):
                    fail("criterion_obligations_immutable", 409)
                goal["criteria"] += [{**c, "origin": "model"} for c in additions]
                goal["version"] += 1
                if len(json.dumps(goal).encode()) > 8192:
                    fail("context_limit", 409)
                state["task_state"]["goal_version"] = goal["version"]
                state["task_state"]["unresolved"] += [c["id"] for c in additions]
                gr.data = state
            operation.data = {**operation.data, "refined": True}
            gr.data = {**gr.data, "current_operation": op_id}
            if decision["plan"] is not None:
                gr.data = {**gr.data, "task_state": {**gr.data["task_state"], "plan": decision["plan"]}}
            if len(json.dumps(gr.data["task_state"]).encode()) > 8192:
                fail("context_limit", 409)
            kind = action["kind"]
            progress = False
            if kind == "complete":
                result = await self.general_complete(db, row, gr, action)
            elif kind == "blocked":
                result = {"blocked": action["reason"]}
                await self.general_stop_locked(db, row, gr, "task_blocked", action["reason"])
            elif kind == "discover":
                query = action["query"].casefold()
                loaded = [
                    a
                    for a, e in sorted(gr.data["tools"].items())
                    if query in (a + e["description"]).casefold()
                ][:4]
                gr.data = {**gr.data, "loaded": loaded}
                from .general_catalog import descriptor

                result = {"capabilities": [descriptor(gr.data["tools"][a]).model_dump() for a in loaded]}
            elif kind == "assign":
                result = await self.general_assign(db, row, gr, root, op_id, action["assignments"])
                progress = True
            elif kind == "join":
                if any(c not in gr.data["children"] for c in action["child_ids"]):
                    fail("child_not_authorized", 404)
                result = {"join": action["child_ids"]}
            elif kind == "merge":
                result = await self.general_merge(db, row, gr, action)
                progress = not result.get("conflicts")
            else:
                alias, args = action["capability"], action["arguments"]
                entry = gr.data["tools"].get(alias)
                if entry is None:
                    fail("capability_not_authorized", 403)
                if entry.get("handler_version") != 3 or digest(
                    {k: v for k, v in entry.items() if k != "registration_id"}
                ) != entry.get("registration_id"):
                    fail("capability_snapshot_unavailable", 409)
                if entry.get("extension"):
                    self.extensions.handler(entry["extension"])
                    self.extensions.validate_arguments(entry, args)
                    # Unknown external effects block new mutations until reconciled.
                    unknown = await db.scalars(
                        select(GeneralOperationRow).where(
                            GeneralOperationRow.run_id.in_([root.run_id, *root.data["children"]])
                        )
                    )
                    if entry["effect"]["kind"] != "read" and any(
                        o.data.get("status") == "outcome_unknown" for o in unknown
                    ):
                        fail("unresolved_external_effect", 409)
                if not operation.data.get("reserved"):
                    await self.general_charge_locked(
                        db, gr, root, "tool_attempts", integration=alias == "workspace_verify"
                    )
                    operation.data = {**operation.data, "reserved": True}
                    if alias not in {"workspace_command", "workspace_verify"}:
                        db.add(
                            GeneralAttemptRow(
                                id=op_id + ":tool:1",
                                operation_id=op_id,
                                ordinal=1,
                                data={"kind": "tool", "reserved": True},
                            )
                        )
                operation.data = {
                    **operation.data,
                    "registration_id": entry["registration_id"],
                    "argument_digest": digest(args),
                    "effect_policy": entry["effect"],
                }
                effect = entry["effect"]
                target = args.get("key", "*") if effect["domain"] == "records" else "*"
                scope = effect["domain"] + ":" + str(target)
                if effect["approval"] == "required":
                    if scope in root.data["denied"]:
                        result = {"error": "effect_denied"}
                    elif row.decisions.get(op_id) is None:
                        if any(
                            [
                                (await db.get(RunRow, c)).status not in {"completed", "failed", "cancelled"}
                                for c in root.data["children"]
                                if c != run_id
                            ]
                        ):
                            fail("join_before_approval", 409)
                        root_row = await db.get(RunRow, root.run_id)
                        from .approval_preview import build_preview

                        approval = {"id": op_id, "tool": alias, "arguments": args, "origin_run_id": run_id}
                        preview = await build_preview(db, gr.data, entry, args)
                        if preview is not None:
                            approval["preview"] = preview
                        root.data = {**root.data, "approval_started": time.time()}
                        root_row.status, root_row.approvals = "awaiting_approval", [approval]
                        row.status = "awaiting_approval"
                        operation.data = {
                            **operation.data,
                            "scope": scope,
                            "registration_id": entry["registration_id"],
                        }
                        await self.emit(db, root_row, op_id + ":approval", "approval.required", approval)
                        return {"approval": op_id, "root_id": root.run_id}
                    elif row.decisions[op_id] is False:
                        result = {"error": "effect_denied"}
                    else:
                        if entry.get("extension"):
                            operation.data = {**operation.data, "external": True}
                            gr.data = {**gr.data, "cleanup_state": "pending"}
                            return {"external": True, "operation_id": op_id}
                        result = await self.general_effect(db, row, op_id, alias, args)
                else:
                    if entry.get("extension"):
                        operation.data = {**operation.data, "external": True}
                        gr.data = {**gr.data, "cleanup_state": "pending"}
                        return {"external": True, "operation_id": op_id}
                    result = await self.general_capability(db, row, gr, root, operation, alias, args)
                    if result.get("command"):
                        return {**result, "operation_id": op_id}
                    progress = bool(
                        result.get("changed") or result.get("verification_ids") or result.get("source")
                    )
            if kind == "invoke":
                attempt = await db.scalar(
                    select(GeneralAttemptRow).where(
                        GeneralAttemptRow.operation_id == op_id, GeneralAttemptRow.ordinal == 1
                    )
                )
                if attempt is None and operation.data.get("reserved"):
                    attempt = GeneralAttemptRow(
                        id=op_id + ":tool:1",
                        operation_id=op_id,
                        ordinal=1,
                        data={"kind": "tool", "reserved": True},
                    )
                    db.add(attempt)
                if attempt is not None:
                    attempt.data = {
                        **attempt.data,
                        "settled": True,
                        "outcome": "failed" if result.get("error") else "complete",
                    }
            operation.data = {**operation.data, "status": "complete", "result": result}
            await self.general_checkpoint(db, row, gr, op_id, result, progress)
            return result

    async def general_charge_locked(self, db, gr, root, kind, integration=False):
        from .resources import check, policy, reserve_for

        if policy(root.data):
            reserve = (
                reserve_for(root.data, kind)
                if gr.parent_id or (kind != "model_attempts" and not integration)
                else 0
            )
            check(root, gr, kind, reserve=reserve)
            rs = copy.deepcopy(root.data)
            gs = rs if root.run_id == gr.run_id else copy.deepcopy(gr.data)
            rs["budget"][kind] += 1
            gs.setdefault("local_usage", {})[kind] = gs.get("local_usage", {}).get(kind, 0) + 1
            root.data = rs
            if gr.parent_id:
                gr.data = gs
            return
        rs = copy.deepcopy(root.data)
        gs = rs if root.run_id == gr.run_id else copy.deepcopy(gr.data)
        limits = rs["policy"]["limits"]
        local = gs.setdefault("local_usage", {})
        local_limit = gs.get("local_limits", limits).get(kind, limits[kind])
        project_work = bool(rs["policy"].get("delegation")) or any(
            a.startswith("workspace_") for a in rs["tools"]
        )
        reserve = (
            (2 if rs["policy"].get("review") else 1)
            if kind == "model_attempts"
            else 2
            if project_work and kind in {"tool_attempts", "command_attempts"}
            else 0
        )
        ceiling = limits[kind] - (
            reserve if gr.parent_id or (kind != "model_attempts" and not integration) else 0
        )
        if rs["budget"][kind] >= ceiling or local.get(kind, 0) >= local_limit:
            fail("budget_exhausted", 409)
        rs["budget"][kind] += 1
        local[kind] = local.get(kind, 0) + 1
        root.data = rs
        if gr.parent_id:
            gr.data = gs

    async def general_capability(self, db, row, gr, root, operation, alias, args):
        if alias == "skill_read":
            if set(args) != {"alias"} or args["alias"] not in gr.data.get("skills", {}):
                fail("skill_not_authorized", 403)
            skill = gr.data["skills"][args["alias"]]
            gr.data = {
                **gr.data,
                "loaded_skills": sorted(set(gr.data.get("loaded_skills", [])) | {args["alias"]}),
            }
            return {"skill": skill, "permissions": "unchanged"}
        if alias == "session_history":
            from .context import excerpt, session_messages, terms

            if gr.parent_id:
                fail("session_history_not_granted", 403)
            if set(args) - {"message_id", "query", "offset", "length"}:
                fail("invalid_arguments")
            offset, length = args.get("offset", 0), args.get("length", 2000)
            if type(offset) is not int or type(length) is not int or offset < 0 or not 1 <= length <= 4096:
                fail("invalid_arguments")
            items = await session_messages(db, row, str(args.get("query", ""))[:512], args.get("message_id"))
            if args.get("message_id"):
                item = next((m for m in items if m["id"] == args["message_id"]), None)
                if item is None:
                    fail("history_message_not_found", 404)
                return {
                    "message": {
                        **item,
                        "content": item["content"][offset : offset + length],
                        "offset": offset,
                        "truncated": offset + length < len(item["content"]),
                    },
                    "untrusted": True,
                }
            query = terms(str(args.get("query", ""))[:512])
            ranked = sorted(
                enumerate(items),
                key=lambda pair: (len(terms(pair[1]["content"]) & query), pair[0]),
                reverse=True,
            )
            return {
                "messages": [excerpt(item, query, min(length, 1000)) for _, item in ranked[:4]],
                "untrusted": True,
            }
        if alias == "add":
            if set(args) != {"a", "b"} or not all(isinstance(x, (int, float)) for x in args.values()):
                fail("invalid_arguments")
            return {"value": args["a"] + args["b"]}
        if not alias.startswith("workspace_"):
            fail("capability_unavailable", 409)
        await self.general_ensure_workspace(db, gr)
        branch, grant, files = await self.branch_files(db, row.id, args.get("revision"))
        if "branch_id" in args and args["branch_id"] != branch.id:
            fail("branch_not_authorized", 404)
        if alias == "workspace_read":
            name = path(args.get("path"))
            if name not in files:
                fail("File not found", 404)
            offset, length = args.get("offset", 0), args.get("length", 16384)
            if (
                not isinstance(offset, int)
                or offset < 0
                or not isinstance(length, int)
                or not 1 <= length <= 16384
            ):
                fail("invalid_read_bounds")
            data = files[name][offset : offset + length]
            result = {
                "revision_id": args.get("revision", branch.head),
                "path": name,
                "sha256": digest(files[name]),
                "size_bytes": len(files[name]),
                "content_base64": base64.b64encode(data).decode(),
                "text": data.decode("utf-8", errors="replace")[:512],
                "text_truncated": len(data.decode("utf-8", errors="replace")) > 512,
                "truncated": offset + length < len(files[name]),
            }
            if args.get("criterion_id"):
                criterion = next(
                    (c for c in gr.data["goal"]["criteria"] if c["id"] == args["criterion_id"]), None
                )
                quote = args.get("quote", "")
                if (
                    criterion is None
                    or criterion["evidence_policy"] != "source"
                    or not quote
                    or len(quote.encode()) > 4096
                    or quote.encode() not in data
                ):
                    fail("invalid_source_evidence")
                vid = operation.id + ":source"
                await self.general_record(
                    db,
                    gr,
                    "verification",
                    vid,
                    {
                        "criterion_id": criterion["id"],
                        "check_id": None,
                        "outcome": "pass",
                        "method": "source",
                        "provenance": "runtime",
                        "operation_id": operation.id,
                        "goal_version": gr.data["goal"]["version"],
                        "branch_id": branch.id,
                        "revision_id": result["revision_id"],
                        "spec_hash": digest({"quote": quote}),
                        "dependency_hash": digest(files[name]),
                        "details": {
                            "path": name,
                            "offset": offset + data.index(quote.encode()),
                            "quote": quote,
                        },
                    },
                )
                result["verification_ids"] = [vid]
            return result
        if alias == "workspace_search":
            query = args.get("query", "")
            prefix, cursor = args.get("prefix", ""), args.get("cursor", 0)
            path(prefix, prefix=True)
            if (
                not isinstance(query, str)
                or not 1 <= len(query) <= 200
                or not isinstance(cursor, int)
                or cursor < 0
            ):
                fail("invalid_search")
            hits, matched = [], 0
            for p, content in sorted(files.items()):
                if not covered(p, [prefix]):
                    continue
                for i, line in enumerate(content.decode("utf-8", errors="replace").splitlines()):
                    if query not in line:
                        continue
                    if matched >= cursor:
                        hits.append({"path": p, "line": i + 1, "excerpt": line[:240]})
                    matched += 1
                    if len(hits) > 40:
                        break
                if len(hits) > 40:
                    break
            return {
                "hits": hits[:40],
                "next_cursor": cursor + 40 if len(hits) > 40 else None,
            }
        if alias in {"workspace_write", "workspace_patch"}:
            expected = args.get("expected_revision")
            if expected != branch.head:
                fail("revision_conflict", 409)
            writes = args.get("writes" if alias == "workspace_write" else "patches", [])
            if not 1 <= len(writes) <= 8 or len(json.dumps(writes).encode()) > (
                2800000 if alias == "workspace_write" else 65536
            ):
                fail("invalid_write_bounds")
            candidate = dict(files)
            seen = set()
            for write in writes:
                name = path(write.get("path"))
                if (
                    name in seen
                    or "expected_sha256" not in write
                    or write["expected_sha256"] != (digest(files[name]) if name in files else None)
                ):
                    fail("file_conflict", 409)
                seen.add(name)
                if alias == "workspace_patch":
                    if name not in files:
                        fail("file_conflict", 409)
                    try:
                        text = files[name].decode("utf-8")
                        for hunk in write["hunks"]:
                            if not hunk["old"] or text.count(hunk["old"]) != 1:
                                fail("patch_conflict", 409)
                            text = text.replace(hunk["old"], hunk["new"], 1)
                        candidate[name] = text.encode()
                    except (KeyError, UnicodeError):
                        fail("invalid_patch")
                elif write.get("delete", False):
                    candidate.pop(name, None)
                else:
                    candidate[name] = decode(write.get("content_base64", ""))
            revision = await self.branch_commit(db, row.id, candidate, expected)
            if revision != expected:
                await self.general_require_integration(db, gr)
            result = {"revision_id": revision, "changed": revision != expected}
            if revision == expected and gr.data["policy"].get("context_policy") == "memory-v1":
                result["feedback"] = (
                    "These files already have the requested bytes. Continue remaining work; do not repeat the same write. Complete runs pending checks."
                )
            return result
        if alias == "workspace_outputs":
            op = await db.get(GeneralOperationRow, args.get("operation_id", ""))
            if op is None or op.run_id != row.id:
                fail("Operation not found", 404)
            if args.get("name") is not None:
                item = op.data.get("outputs", {}).get(args["name"])
                offset, length = args.get("offset", 0), args.get("length", 16384)
                if item is None:
                    fail("Output not found", 404)
                if (
                    type(offset) is not int
                    or offset < 0
                    or type(length) is not int
                    or not 1 <= length <= 16384
                ):
                    fail("invalid_read_bounds")
                blob = await db.get(ProjectBlobRow, item["sha256"])
                if (
                    blob is None
                    or digest(blob.content) != item["sha256"]
                    or len(blob.content) != item["size_bytes"]
                ):
                    fail("output_integrity", 409)
                chunk = blob.content[offset : offset + length]
                return {
                    "name": args["name"],
                    **item,
                    "content_base64": base64.b64encode(chunk).decode(),
                    "text": chunk.decode("utf-8", errors="replace")[:512],
                    "truncated": offset + length < len(blob.content),
                }
            return {
                "outputs": [
                    {"name": n, "sha256": v["sha256"], "size_bytes": v["size_bytes"]}
                    for n, v in op.data.get("outputs", {}).items()
                ]
            }
        if alias in {"workspace_command", "workspace_verify"}:
            if args.get("expected_revision") != branch.head:
                fail("revision_conflict", 409)
            spec, criterion, provenance = None, None, "supporting"
            if alias == "workspace_verify":
                for c in gr.data["goal"]["criteria"]:
                    for check in c["checks"]:
                        if check["id"] == args.get("check_id"):
                            spec, criterion, provenance = (
                                check,
                                c["id"],
                                "supporting" if c["origin"] == "model" else c["origin"],
                            )
                if spec is None:
                    from .general_contracts import CheckSpec

                    spec = CheckSpec.model_validate(args.get("supporting_check")).model_dump()
                    criterion = args.get("criterion_id", "supporting")
                if (
                    provenance in {"user", "runtime"}
                    and gr.data["policy"].get("context_policy") == "memory-v1"
                ):
                    from .general_receipts import current_checks

                    receipt = (await current_checks(self, db, gr)).get((criterion, spec["id"]))
                    if receipt and receipt["outcome"] == "pass":
                        return {
                            "verification_ids": [receipt["id"]],
                            "outcome": "pass",
                            "reused": True,
                            "feedback": "This check already passed for the current revision; proceed to remaining work or complete.",
                        }
                if spec["kind"] != "command":
                    name = path(spec["path"])
                    content = files.get(name)
                    expected = spec["expected"]
                    passed = content is not None and (
                        digest(content) == expected
                        if spec["kind"] == "sha256"
                        else content == decode(expected)
                    )
                    vid = await self.general_verification(
                        db,
                        gr,
                        operation,
                        branch,
                        spec,
                        criterion,
                        provenance,
                        "pass" if passed else "fail",
                        {},
                    )
                    return {"verification_ids": [vid], "outcome": "pass" if passed else "fail"}
            if (
                spec
                and provenance == "user"
                and any(digest(files.get(p, b"")) != h for p, h in gr.data.get("input_checks", {}).items())
            ):
                fail("input_check_modified", 409)
            argv = spec["argv"] if spec else args.get("argv")
            cwd = spec["cwd"] if spec else args.get("cwd", "")
            path(cwd, prefix=True)
            if (
                not isinstance(argv, list)
                or not 1 <= len(argv) <= 32
                or not all(isinstance(a, str) for a in argv)
                or argv[0] not in {"python", "python3", "pytest"}
                or sum(len(a.encode()) for a in argv) > 4096
            ):
                fail("invalid_command")
            await self.general_charge_locked(
                db, gr, root, "command_attempts", integration=alias == "workspace_verify"
            )
            reservation = 5242880
            root_state = copy.deepcopy(root.data)
            reserved = root_state["budget"].get("reserved_project_bytes", 0)
            if (
                root_state["budget"]["storage_bytes"] + reserved + reservation
                > root_state["policy"]["limits"]["storage_bytes"]
            ):
                fail("project_storage_limit", 409)
            root_state["budget"]["reserved_project_bytes"] = reserved + reservation
            root.data = root_state
            ordinal = operation.data.get("command_attempt", 0) + 1
            db.add(
                GeneralAttemptRow(
                    id=operation.id + ":command:" + str(ordinal),
                    operation_id=operation.id,
                    ordinal=ordinal,
                    data={"kind": "command", "reserved": True},
                )
            )
            command = {
                "argv": argv,
                "image_digest": root.data["operator"]["image_digest"],
                "cwd": cwd,
                "deadline": time.time() + 110,
                "wall_seconds": min(60, max(1, args.get("wall_seconds", 60))),
            }
            operation.data = {
                **operation.data,
                "command": command,
                "command_attempt": ordinal,
                "storage_reservation": reservation,
                "input_revision": branch.head,
                "commit": bool(args.get("commit", False)) and spec is None,
                "spec": spec,
                "criterion": criterion,
                "provenance": provenance,
                "fence": root.data["fence"],
            }
            gr.data = {**gr.data, "cleanup_state": "pending"}
            return {"command": True}
        fail("capability_unavailable", 409)

    async def general_require_integration(self, db, gr):
        state = copy.deepcopy(gr.data)
        goal = state["goal"]
        if not any(c["id"] == "runtime.integrated" for c in goal["criteria"]):
            # Syntax is a narrow runtime floor; caller behavior checks remain independent.
            criterion = Criterion(
                id="runtime.integrated",
                statement="Fresh Python syntax check on integrated project head (syntax only)",
                evidence_policy="check",
                origin="runtime",
                checks=[
                    {
                        "id": "runtime.syntax",
                        "kind": "command",
                        "argv": [
                            "python",
                            "-I",
                            "-c",
                            "import ast,pathlib; [ast.parse(p.read_text(),filename=str(p)) for p in pathlib.Path('.').rglob('*.py')]",
                        ],
                    }
                ],
            )
            goal["criteria"].append(criterion.model_dump())
            goal["version"] += 1
            state["task_state"]["goal_version"] = goal["version"]
            state["task_state"]["unresolved"].append(criterion.id)
        gr.data = state

    async def general_verification(
        self, db, gr, operation, branch, spec, criterion, provenance, outcome, details
    ):
        vid = operation.id + ":verification"
        revision = await self.project_revision(db, branch.workspace_id, branch.head)
        relevant = [
            f
            for f in revision.data["files"]
            if not f["path"].rsplit("/", 1)[-1].startswith("test_") and not f["path"].endswith("_test.py")
        ]
        details = {**details, "progress_dependency_hash": digest(relevant)}
        data = {
            "criterion_id": criterion,
            "check_id": spec["id"],
            "outcome": outcome,
            "method": spec["kind"],
            "provenance": provenance,
            "operation_id": operation.id,
            "goal_version": gr.data["goal"]["version"],
            "branch_id": branch.id,
            "revision_id": branch.head,
            "spec_hash": digest(spec),
            "dependency_hash": digest(revision.data["files"]),
            "details": details,
        }
        await self.general_record(db, gr, "verification", vid, data)
        return vid

    async def general_command_commit(self, run_id, op_id, bundle, attempt_identity=None):
        async with self.database.sessions.begin() as db:
            row, gr, root = await self.general_lock(db, run_id, active=False)
            op = await db.get(GeneralOperationRow, op_id)
            if op is None or op.run_id != run_id:
                fail("Operation not found", 404)
            if op.data.get("result") is not None:
                return op.data["result"]
            if attempt_identity is not None and attempt_identity != op_id + ":command:" + str(
                op.data["command_attempt"]
            ):
                fail("attempt_superseded", 409)
            if bundle.get("error") == "sandbox_pending":
                fail("sandbox_pending", 409)
            if (
                row.status in {"completed", "failed", "cancelled"}
                or root.data["fence"] != op.data["fence"]
                or self.general_time_remaining(root.data) <= 0
            ):
                result = {"error": "late_result_fenced"}
            elif bundle.get("error"):
                result = {"error": bundle["error"]}
            else:
                if bundle.get("image_digest") != op.data["command"]["image_digest"]:
                    fail("sandbox_integrity", 409)
                if (
                    sum(x["size_bytes"] for x in bundle["logs"].values()) > 65536
                    or sum(x["size_bytes"] for x in bundle["outputs"].values()) > 1048576
                    or len(bundle["outputs"]) > 16
                ):
                    fail("sandbox_output_limit", 409)
                files, outputs = {}, {}
                for name, item in bundle["files"].items():
                    path(name)
                    b = decode(item["content_base64"])
                    if digest(b) != item["sha256"] or len(b) != item["size_bytes"]:
                        fail("sandbox_integrity", 409)
                    files[name] = b
                for name, item in {
                    **bundle["logs"],
                    **{"files/" + n: item for n, item in bundle["outputs"].items()},
                }.items():
                    path(name)
                    b = decode(item["content_base64"])
                    if digest(b) != item["sha256"] or len(b) != item["size_bytes"]:
                        fail("sandbox_integrity", 409)
                    if await db.get(ProjectBlobRow, item["sha256"]) is None:
                        total = await db.scalar(select(func.coalesce(func.sum(ProjectBlobRow.length), 0)))
                        if total + len(b) > 268435456:
                            fail("project_storage_limit", 409)
                        db.add(ProjectBlobRow(id=item["sha256"], content=b, length=len(b)))
                        await db.flush()
                    outputs[name] = {k: item[k] for k in ("sha256", "size_bytes")}
                from .project_store import validate_files

                validate_files(files, root.data["policy"]["limits"])
                rs = copy.deepcopy(root.data)
                hashes = rs.get("project_hashes", {})
                hashes.update({item["sha256"]: item["size_bytes"] for item in outputs.values()})
                if sum(hashes.values()) > rs["policy"]["limits"]["storage_bytes"]:
                    fail("project_storage_limit", 409)
                rs["project_hashes"] = hashes
                rs["budget"]["storage_bytes"] = sum(hashes.values())
                root.data = rs
                branch, grant, before = await self.branch_files(db, run_id)
                if branch.head != op.data["input_revision"]:
                    result = {"error": "revision_conflict", "candidate_digest": digest(bundle["files"])}
                    if op.data["commit"]:
                        _, _, original = await self.branch_files(db, run_id, op.data["input_revision"])
                        if any(
                            original.get(p) != files.get(p) and not covered(p, grant.data["write_prefixes"])
                            for p in set(original) | set(files)
                        ):
                            fail("workspace_write_denied", 403)
                        state = copy.deepcopy(root.data)
                        hashes = dict(state.get("project_hashes", {}))
                        hashes.update({digest(b): len(b) for b in files.values()})
                        limits = state["policy"]["limits"]
                        if (
                            sum(hashes.values()) > limits["storage_bytes"]
                            or state["budget"]["revisions"] >= limits["revisions"]
                        ):
                            fail("project_storage_limit", 409)
                        candidate = await self.project_commit(
                            db, branch.workspace_id, files, [op.data["input_revision"]], limits
                        )
                        grant.data = {**grant.data, "revisions": [*grant.data["revisions"], candidate]}
                        state["project_hashes"] = hashes
                        state["budget"]["storage_bytes"] = sum(hashes.values())
                        state["budget"]["revisions"] += 1
                        root.data = state
                        result["candidate_revision"] = candidate
                else:
                    revision = branch.head
                    if op.data["commit"]:
                        revision = await self.branch_commit(db, run_id, files, branch.head)
                        if revision != op.data["input_revision"]:
                            await self.general_require_integration(db, gr)
                    result = {
                        "revision_id": revision,
                        "exit_code": bundle["exit_code"],
                        "argv": op.data["command"]["argv"],
                        "cwd": op.data["command"]["cwd"],
                        "logs": {
                            name: {
                                "text": decode(item["content_base64"]).decode("utf-8", errors="replace")[
                                    :1000
                                ],
                                "truncated": item["size_bytes"] > 1000,
                                "operation_id": op.id,
                                "name": name,
                            }
                            for name, item in bundle["logs"].items()
                        },
                        "untrusted": True,
                        "image_digest": bundle["image_digest"],
                        "truncated": bundle["truncated"],
                        "output_names": sorted(outputs),
                        "changed": revision != op.data["input_revision"],
                        "workspace_effect": {
                            "commit_requested": op.data["commit"],
                            "persisted": bool(op.data["commit"]),
                            "changed_paths": sorted(
                                p for p in set(before) | set(files) if before.get(p) != files.get(p)
                            ),
                        },
                    }
                    if op.data["spec"]:
                        outcome = (
                            "inconclusive"
                            if files != before
                            else "pass"
                            if bundle["exit_code"] == op.data["spec"]["expected_exit"]
                            else "fail"
                        )
                        vid = await self.general_verification(
                            db,
                            gr,
                            op,
                            branch,
                            op.data["spec"],
                            op.data["criterion"],
                            op.data["provenance"],
                            outcome,
                            {
                                "image_digest": bundle["image_digest"],
                                "argv": op.data["command"]["argv"],
                                "cwd": op.data["command"]["cwd"],
                                "exit_code": bundle["exit_code"],
                            },
                        )
                        result.update(verification_ids=[vid], outcome=outcome)
                op.data = {**op.data, "outputs": outputs}
            root_state = copy.deepcopy(root.data)
            root_state["budget"]["reserved_project_bytes"] = max(
                0,
                root_state["budget"].get("reserved_project_bytes", 0) - op.data.get("storage_reservation", 0),
            )
            root.data = root_state
            attempt = await db.get(GeneralAttemptRow, op_id + ":command:" + str(op.data["command_attempt"]))
            if attempt is not None:
                attempt.data = {
                    **attempt.data,
                    "settled": True,
                    "outcome": "failed" if result.get("error") else "complete",
                }
            op.data = {**op.data, "result": result, "status": "complete"}
            await self.general_checkpoint(
                db,
                row,
                gr,
                op.id,
                result,
                bool(
                    result.get("changed")
                    or result.get("outcome") == "pass"
                    or (gr.data.get("completion_loop_version", 1) >= 2 and result.get("verification_ids"))
                ),
            )
            return result

    async def general_effect(self, db, row, op_id, alias, args):
        if alias == "record_note":
            text = args.get("text")
            if set(args) != {"text"} or not isinstance(text, str) or not 1 <= len(text) <= 4000:
                fail("invalid_arguments")
            db.add(NoteRow(operation_id=op_id, run_id=row.id, text=text))
            result = {"recorded": True}
        elif alias == "record_set":
            if (
                set(args) != {"key", "value", "expected_version"}
                or not isinstance(args["key"], str)
                or not 1 <= len(args["key"]) <= 100
                or not isinstance(args["value"], str)
                or len(args["value"]) > 4000
                or type(args["expected_version"]) is not int
                or args["expected_version"] < 0
            ):
                fail("invalid_arguments")
            record = await db.get(GeneralEffectRow, args["key"])
            if (record.version if record else 0) != args["expected_version"]:
                return {"error": "record_conflict"}
            if record:
                record.value, record.version = args["value"], record.version + 1
            else:
                record = GeneralEffectRow(key=args["key"], value=args["value"], version=1)
                db.add(record)
            result = {"key": record.key, "version": record.version}
        else:
            fail("effect_unavailable", 409)
        await self.emit(
            db, row, op_id + ":effect", "tool.effect_committed", {"operation_id": op_id, "tool": alias}
        )
        return {**result, "effect": "committed", "operation_id": op_id}

    async def general_stop_locked(self, db, row, gr, code, detail=""):
        if row.status not in {"completed", "failed", "cancelled"}:
            row.status, row.error = "failed", code
            state = copy.deepcopy(gr.data)
            state["completion_assessment"] = {
                "proposal_id": "stop:" + row.id,
                "state_version": state["task_state"]["version"],
                "goal_version": state["goal"]["version"],
                "revision_id": state["task_state"]["head"],
                "criteria": [
                    {
                        "criterion_id": c["id"],
                        "disposition": "inconclusive",
                        "assessment": "Not established before stopping",
                        "evidence_ids": [],
                    }
                    for c in state["goal"]["criteria"]
                ],
                "remaining_gaps": [c["id"] for c in state["goal"]["criteria"] if c["required"]],
                "limitations": ["Task stopped before all required criteria were established."],
                "stop_reason": code,
                "accepted": False,
            }
            if gr.parent_id is None:
                state["fence"] += 1
            state["stop_reason"] = code
            state["task_state"]["blockers"] = [detail or code]
            gr.data = state
            await self.emit(db, row, "terminal", "run.failed", {"error": code})

    async def general_stop(self, run_id, code):
        async with self.database.sessions.begin() as db:
            row, gr, _ = await self.general_lock(db, run_id, active=False)
            await self.general_stop_locked(db, row, gr, code)

    async def general_assign(self, db, row, gr, root, op_id, assignments):
        policy = gr.data["policy"].get("delegation")
        if gr.parent_id or not policy or len(gr.data["children"]) + len(assignments) > policy["max_children"]:
            fail("delegation_limit", 409)
        await self.general_ensure_workspace(db, gr)
        branch, grant, _ = await self.branch_files(db, row.id)
        active_children = [
            cid
            for cid in gr.data["children"]
            if (await db.get(RunRow, cid)).status not in {"completed", "failed", "cancelled"}
        ]
        if active_children and len(active_children) + len(assignments) > policy["max_simultaneous"]:
            fail("join_required", 409)
        # Validate dependencies for the entire assignment before creating any child.
        effective_assignments = []
        for raw in assignments:
            assignment = Assignment.model_validate(raw)
            authorized = set(policy["tools"]) & set(gr.data["tools"])
            if not set(assignment.tools).issubset(authorized):
                fail("child_grant_denied", 403)
            needs_verification = bool(
                {"workspace_write", "workspace_command"} & set(assignment.tools)
            ) or any(c.checks for c in assignment.criteria)
            if (
                gr.data.get("completion_loop_version", 1) >= 2
                and needs_verification
                and "workspace_verify" not in assignment.tools
            ):
                if "workspace_verify" not in authorized:
                    fail(
                        "child_verification_required: authorize workspace_verify in parent and delegation policy",
                        403,
                    )
                assignment = assignment.model_copy(update={"tools": [*assignment.tools, "workspace_verify"]})
            effective_assignments.append(assignment)
        ids, sequential = [], False
        for i, raw in enumerate(assignments):
            assignment = effective_assignments[i]
            if any(
                c.id.startswith("runtime.") or any(check.id.startswith("runtime.") for check in c.checks)
                for c in assignment.criteria
            ):
                fail("reserved_criterion_identity", 422)
            if (
                assignment.base_revision not in grant.data["revisions"]
                or not set(assignment.tools).issubset(policy["tools"])
                or not set(assignment.artifact_ids).issubset(gr.data["artifact_ids"])
            ):
                fail("child_grant_denied", 403)
            for p in assignment.read_prefixes:
                path(p, prefix=True)
                if not covered(p, grant.data["read_prefixes"]):
                    fail("child_grant_denied", 403)
            for p in assignment.write_prefixes:
                path(p, prefix=True)
                if not covered(p, assignment.read_prefixes) or not covered(p, grant.data["write_prefixes"]):
                    fail("child_grant_denied", 403)
            from .resources import shared

            if not shared(root.data) and any(
                v > policy["limits"][k] for k, v in assignment.limits.model_dump().items()
            ):
                fail("child_budget_denied", 403)
            sequential |= any(
                gr.data["tools"][a]["effect"]["approval"] == "required" for a in assignment.tools
            )
            if sequential and active_children:
                fail("join_required", 409)
            cid = str(uuid5(NAMESPACE_URL, op_id + ":" + str(i)))
            goal = TaskGoal(
                outcome=assignment.objective,
                constraints=gr.data["goal"]["constraints"],
                assumptions=gr.data["goal"].get("assumptions", []),
                criteria=[c.model_copy(update={"origin": "model"}) for c in assignment.criteria],
            )
            child = RunRow(
                id=cid,
                session_id=row.session_id,
                agent_id=row.agent_id,
                key="general-child:" + cid,
                fingerprint=digest(raw),
                config={
                    **row.config,
                    "tools": assignment.tools,
                    "general": {**row.config["general"], "delegation": None},
                },
                input=assignment.objective,
                registration_id=row.registration_id,
                approval_wait_seconds=row.approval_wait_seconds,
            )
            db.add(child)
            await db.flush()
            data = {
                "policy": {
                    **gr.data["policy"],
                    "delegation": None,
                    "review": gr.data["policy"].get("review")
                    if (gr.data["policy"].get("review") or {}).get("scope") == "all"
                    else None,
                },
                "operator": gr.data["operator"],
                "context_policy_version": gr.data.get("context_policy_version", 1),
                "executor_version": gr.data.get("executor_version", 1),
                "review_registration_id": gr.data.get("review_registration_id"),
                "skills": gr.data.get("skills", {}),
                "tools": {a: gr.data["tools"][a] for a in assignment.tools},
                "goal": goal.model_dump(),
                "task_state": TaskState(unresolved=[c.id for c in goal.criteria]).model_dump(),
                "budget": {},
                "local_limits": assignment.limits.model_dump(),
                "artifact_ids": assignment.artifact_ids,
                "children": {},
                "denied": [],
                "fence": 0,
                "cleanup_state": "complete",
                "step": 0,
                "stagnation": 0,
                "seen": [],
                "last_result": None,
                "role": assignment.role,
                "assignment": raw,
                "effective_capabilities": assignment.tools,
            }
            if root.data.get("adaptive"):
                data["adaptive"] = copy.deepcopy(root.data["adaptive"])
            cg = GeneralRunRow(run_id=cid, root_id=root.run_id, parent_id=row.id, data=data)
            db.add(cg)
            db.add(
                ToolkitRunRow(
                    run_id=cid,
                    root_id=root.run_id,
                    parent_id=row.id,
                    state={"execution_version": 3, "cleanup_state": "complete"},
                )
            )
            await db.flush()
            await self.general_attach(
                db,
                cg,
                branch.workspace_id,
                assignment.base_revision,
                assignment.read_prefixes,
                assignment.write_prefixes,
            )
            gr.data = {
                **gr.data,
                "children": {
                    **gr.data["children"],
                    cid: {
                        "base_revision": assignment.base_revision,
                        "initial_revision": cg.data["workspace"]["revision_id"],
                        "write_prefixes": assignment.write_prefixes,
                    },
                },
            }
            gr.data = {**gr.data, "cleanup_state": "pending"}
            ids.append(cid)
            await self.emit(
                db, row, "child:" + cid, "child.created", {"child_run_id": cid, "role": assignment.role}
            )
        if len(ids) > policy["max_simultaneous"]:
            sequential = True
        return {"children": ids, "sequential": sequential}

    async def general_merge(self, db, row, gr, action):
        child = gr.data["children"].get(action["child_id"])
        if child is None or action["base_revision"] != child["base_revision"]:
            fail("child_not_authorized", 404)
        cr = await db.get(RunRow, action["child_id"])
        if cr.status not in {"completed", "failed", "cancelled"}:
            fail("child_still_running", 409)
        cb, _, source = await self.branch_files(db, cr.id, action["source_revision"])
        if cb.head != action["source_revision"]:
            fail("revision_conflict", 409)
        branch, _, parent = await self.branch_files(db, row.id)
        if branch.head != action["expected_revision"]:
            fail("revision_conflict", 409)
        base = await self.project_files(db, branch.workspace_id, child["initial_revision"])
        merged, conflicts = three_way(base, parent, source, child["write_prefixes"])
        if conflicts:
            return {"conflicts": conflicts, "revision_id": branch.head}
        revision = await self.branch_commit(db, row.id, merged, branch.head, [branch.head, cb.head])
        if revision != action["expected_revision"]:
            await self.general_require_integration(db, gr)
        return {"revision_id": revision, "changed": revision != action["expected_revision"], "conflicts": []}
