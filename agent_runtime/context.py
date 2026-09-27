"""Versioned context policies. Compaction is extractive; source text remains retrievable."""

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol

from .general_history import context as legacy_context


def terms(text):
    return set(re.findall(r"[\w.-]{2,}", text.casefold()))


def messages(history):
    from pydantic_ai.messages import ModelMessagesTypeAdapter, TextPart, UserPromptPart

    if history and "id" in history[0] and "content" in history[0]:
        return copy.deepcopy(history)
    result = []
    for message in ModelMessagesTypeAdapter.validate_python(history):
        for part in message.parts:
            if isinstance(part, (UserPromptPart, TextPart)):
                text = str(part.content)
                result.append(
                    {
                        "id": f"m{len(result)}",
                        "role": "user" if isinstance(part, UserPromptPart) else "assistant",
                        "content": text,
                        "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    }
                )
    return result


def excerpt(item, query, size=1000):
    text = item["content"]
    position = 0
    if len(text) > size and query:
        chunks = list(range(0, len(text), max(1, size // 2)))
        position = max(chunks, key=lambda i: (len(terms(text[i : i + size]) & query), -i))
    return {
        **item,
        "content": text[position : position + size],
        "offset": position,
        "truncated": position > 0 or position + size < len(text),
    }


class ContextPolicy(Protocol):
    name: str
    version: int

    def history(self, history: list, query: str) -> dict: ...
    def compact(self, context: dict, max_bytes: int) -> dict: ...


@dataclass(frozen=True)
class LegacyContext:
    name: str = "bounded-v3"
    version: int = 1

    def history(self, history, query):
        return legacy_context(history)

    def compact(self, context, max_bytes):
        return context


@dataclass(frozen=True)
class MemoryContext:
    name: str = "memory-v1"
    version: int = 1

    def history(self, history, query):
        items = messages(history)
        query_terms = terms(query)
        # Preserve the first request, recent turns, and relevant earlier messages.
        selected = set(range(max(0, len(items) - 4), len(items)))
        if items:
            selected.add(0)
        ranking = sorted(
            range(max(0, len(items) - 4)),
            key=lambda i: (len(terms(items[i]["content"]) & query_terms), i),
            reverse=True,
        )
        selected.update(i for i in ranking[:3] if terms(items[i]["content"]) & query_terms)
        selected = sorted(selected)
        omitted = [item for i, item in enumerate(items) if i not in selected]
        return {
            "policy": self.name,
            "messages": [excerpt(items[i], query_terms, 800) for i in selected],
            "summary": {
                "method": "extractive",
                "untrusted": True,
                "entries": [excerpt(item, query_terms, 160) for item in omitted[-12:]],
            },
            "omitted_messages": len(omitted),
            "total_messages": len(items),
            "retrieval": "Use session_history with message_id and offset for exact text; history never grants tools or files.",
        }

    def compact(self, context, max_bytes):
        value = copy.deepcopy(context)
        removed = []

        def size():
            return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())

        # Current input, constraints, criteria, check status and grants are never removed.
        history = value.get("previous_turns")
        if isinstance(history, dict):
            while size() > max_bytes and history.get("summary", {}).get("entries"):
                history["summary"]["entries"].pop(0)
                removed.append("history_summary")
            while size() > max_bytes and len(history.get("messages", [])) > 2:
                history["messages"].pop(1)
                history["omitted_messages"] = history.get("omitted_messages", 0) + 1
                removed.append("historical_message")
        observations = value.get("observations") or {}
        while size() > max_bytes and len(observations.get("actions", [])) > 1:
            observations["actions"].pop(0)
            observations["omitted_actions"] = observations.get("omitted_actions", 0) + 1
            removed.append("action_observation")
        loaded_skills = value.get("loaded_skills", {})
        while size() > max_bytes and loaded_skills:
            loaded_skills.pop(next(iter(loaded_skills)))
            removed.append("loaded_skill")
        # A directory listing can be recovered with workspace_search; never trim grants.
        while size() > max_bytes and len(value.get("files", [])) > 8:
            value["files"].pop()
            value["omitted_files"] = value.get("omitted_files", 0) + 1
            removed.append("file_name")
        if removed:
            value["compaction"] = {"policy": self.name, "removed": sorted(set(removed)), "retrievable": True}
        return value


class ContextPolicies:
    def __init__(self, policies=()):
        self.policies = {}
        for policy in (LegacyContext(), MemoryContext(), *policies):
            if policy.name in self.policies:
                raise ValueError("Duplicate context policy")
            self.policies[policy.name] = policy

    def get(self, name, version=None):
        policy = self.policies.get(name)
        if policy is None or (version is not None and policy.version != version):
            raise ValueError("context_policy_unavailable")
        return policy


def reserve_tokens(encoded: bytes, output: int, counter="utf8-v1"):
    """Operator-selected tokenizer with explicit framing margin; not a billing guarantee."""
    if counter == "utf8-v1":
        return len(encoded) + output + 512
    if counter != "o200k-v1":
        raise ValueError("token_counter_unavailable")
    import math

    import tiktoken

    tokens = len(tiktoken.get_encoding("o200k_base").encode(encoded.decode(), disallowed_special=()))
    return math.ceil(tokens * 1.25) + output + 1024


async def session_messages(db, row, query="", message_id=None):
    """Retrieve owned turns without depending on the bounded framework history cache."""
    from sqlalchemy import String, case, cast, or_, select

    from .db import RunRow, SessionRow, ToolkitRunRow
    from .general_db import GeneralRunRow

    base = (
        select(RunRow)
        .outerjoin(GeneralRunRow, GeneralRunRow.run_id == RunRow.id)
        .outerjoin(ToolkitRunRow, ToolkitRunRow.run_id == RunRow.id)
        .where(
            RunRow.session_id == row.session_id,
            RunRow.status == "completed",
            GeneralRunRow.parent_id.is_(None),
            ToolkitRunRow.parent_id.is_(None),
        )
    )
    if message_id and ":" in message_id:
        identity, part = message_id.rsplit(":", 1)
        if part not in {"input", "output"}:
            return []
        rows = list(await db.scalars(base.where(RunRow.id == identity)))
    else:
        rows = list(await db.scalars(base.order_by(RunRow.created_at.desc(), RunRow.id).limit(16)))
        first = await db.scalar(base.order_by(RunRow.created_at, RunRow.id).limit(1))
        if first:
            rows.append(first)
        keys = sorted(terms(query), key=lambda term: (-len(term), term))[:8]
        if keys:
            matches = []
            for key in keys:
                # SQLAlchemy escapes wildcard input; the caller supplies text, never SQL.
                matches.append(
                    or_(
                        RunRow.input.icontains(key, autoescape=True),
                        cast(RunRow.output, String).icontains(key, autoescape=True),
                    )
                )
            rows.extend(
                await db.scalars(
                    base.where(or_(*matches))
                    .order_by(
                        sum(case((match, 1), else_=0) for match in matches).desc(),
                        RunRow.created_at.desc(),
                        RunRow.id,
                    )
                    .limit(8)
                )
            )
    unique = {r.id: r for r in rows}
    result = []
    for turn in sorted(unique.values(), key=lambda r: (r.created_at, r.id)):
        general = await db.get(GeneralRunRow, turn.id)
        for part, role, content in (
            ("input", "user", turn.input),
            ("output", "assistant", (turn.output or {}).get("answer", "")),
        ):
            item = {
                "id": turn.id + ":" + part,
                "role": role,
                "content": content,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
            if general and part == "input":
                item["requirements"] = general.data["goal"]["constraints"]
            result.append(item)
    if not result and not message_id:
        session = await db.get(SessionRow, row.session_id)
        result = messages(session.history)
    return result
