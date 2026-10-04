"""The simple client preserves durable identities, explicit decisions and safe errors."""

import json

import httpx
import pytest

from agent_runtime.api import create_app
from agent_runtime.client import Client, ClientError
from agent_runtime.client_results import RunProgress
from agent_runtime.schemas import AgentConfig


def transport_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    )


async def setup(store):
    return await store.agent(AgentConfig(name="Simple client", provider="fake", model="deterministic"))


async def test_run_upload_and_lost_submission_reuse_same_identity(store, tmp_path):
    agent = await setup(store)
    file = tmp_path / "sales.csv"
    file.write_text("product,total\nA,12\n")
    app = httpx.ASGITransport(app=create_app(store, "test"))
    submissions, uploads, progress = [], [], []

    async def handle(request):
        response = await app.handle_async_request(request)
        if response.is_error:
            return response
        if request.method == "POST" and request.url.path == "/v1/artifacts":
            await response.aread()
            uploads.append((request.headers["Idempotency-Key"], response.json()["id"]))
            if len(uploads) == 1:
                raise httpx.ReadError("secret upload response")
        if request.method == "POST" and request.url.path == "/v1/runs":
            await response.aread()
            run_id = response.json()["id"]
            submissions.append((request.headers["Idempotency-Key"], run_id))
            await store.finish(run_id, "completed", {"answer": "Total: 12", "value": 12})
            if len(submissions) == 1:
                raise httpx.ReadError("secret submission response")
        return response

    async def update(event):
        progress.append(event)

    async with transport_client(handle) as http:
        client = Client(http_client=http)
        first = await client.run(
            agent.id, "Summarize", files=[file], idempotency_key="file-task", on_progress=update
        )
        second = await client.run(agent.id, "Summarize", files=[file], idempotency_key="file-task")
        assert first.run_id == second.run_id and first.answer == "Total: 12" and first.value == 12
        assert len({x[0] for x in submissions}) == len({x[1] for x in submissions}) == 1
        assert len({x[0] for x in uploads}) == len({x[1] for x in uploads}) == 1
        assert [x.stage for x in progress] == ["uploading", "submitted", "completed"]
        assert progress[1].run_id == first.run_id and progress[1].idempotency_key == "file-task"
        assert "details" not in first.model_dump() and first.details.id == first.run_id
        file.write_text("product,total\nA,99\n")
        with pytest.raises(ClientError) as exc:
            await client.run(agent.id, "Summarize", files=[file], idempotency_key="file-task")
        assert exc.value.status_code == 409 and exc.value.idempotency_key == "file-task"
        assert len(submissions) == 3


async def test_run_timeout_can_resume_without_resubmission(store):
    agent = await setup(store)
    app = httpx.ASGITransport(app=create_app(store, "test"))
    submitted = []

    async def handle(request):
        response = await app.handle_async_request(request)
        if request.method == "POST" and request.url.path == "/v1/runs":
            await response.aread()
            submitted.append(response.json()["id"])
        return response

    async with transport_client(handle) as http:
        client = Client(http_client=http)
        with pytest.raises(ClientError) as exc:
            await client.run(agent.id, "test", timeout=0.02)
        assert exc.value.code == "wait_timeout"
        assert exc.value.run_id == submitted[0] and exc.value.idempotency_key
        assert (await client.get(submitted[0])).status == "queued"
        await store.finish(submitted[0], "completed", {"answer": "done"})
        result = await client.result(exc.value.run_id)
        assert result.answer == "done" and len(submitted) == 1


async def test_run_returns_exact_approval_without_deciding(store):
    agent = await setup(store)
    app = httpx.ASGITransport(app=create_app(store, "test"))
    writes = []

    async def handle(request):
        if request.method != "GET":
            writes.append(request.url.path)
        response = await app.handle_async_request(request)
        if request.method == "POST" and request.url.path == "/v1/runs":
            await response.aread()
            await store.awaiting(
                response.json()["id"],
                [{"id": "approval-1", "tool": "record_note", "arguments": {"text": "exact text"}}],
            )
        return response

    async with transport_client(handle) as http:
        result = await Client(http_client=http).run(agent.id, "note test")
    assert result.status == "awaiting_approval"
    assert result.approvals[0].arguments == {"text": "exact text"}
    assert "approve or deny" in result.next_action
    assert writes == ["/v1/runs"]


@pytest.mark.parametrize(
    "reason,limit_type", [("capacity", "budget"), ("limit", "budget"), ("limit", "ceiling")]
)
async def test_run_capacity_waits_but_budget_and_ceiling_return_attention(store, reason, limit_type):
    agent = await setup(store)
    from agent_runtime.schemas import RunCreate

    run = await store.submit(RunCreate(agent_id=agent.id, input="test"), "fixture")
    reads, writes = [], []
    resource = {
        "version": 1,
        "pause": {"block": {"reason": reason, "limit_type": limit_type}},
        "limits": {"model_attempts": 3},
    }

    def handle(request):
        if request.method != "GET":
            writes.append(request.url.path)
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json=resource)
        reads.append(request.url.path)
        state = run.model_dump(mode="json")
        state["status"] = "completed" if reason == "capacity" and len(reads) > 1 else "paused_budget"
        return httpx.Response(200, json=state)

    async with transport_client(handle) as http:
        result = await Client(http_client=http).result(run.id)
    assert not writes
    if reason == "capacity":
        assert result.status == "completed" and len(reads) == 2
    else:
        assert result.status == "paused_budget" and result.resources == resource
        assert ("ceiling" in result.message) == (limit_type == "ceiling")


async def test_callback_failure_preserves_run_id_and_does_not_cancel(store):
    agent = await setup(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as http:
        client = Client(http_client=http)

        def broken(progress):
            assert isinstance(progress, RunProgress)
            raise ValueError("sk-secret callback details")

        with pytest.raises(ClientError) as exc:
            await client.run(agent.id, "test", on_progress=broken)
        assert exc.value.code == "progress_callback_failed" and exc.value.run_id and exc.value.idempotency_key
        assert "secret" not in str(exc.value)
        assert (await client.get(exc.value.run_id)).status == "queued"


@pytest.mark.parametrize(
    "failure", ["oversize", "invalid_json", "missing", "symlink", "unsupported", "too_many", "directory"]
)
async def test_files_preflight_before_any_network(failure, tmp_path):
    valid = tmp_path / "valid.txt"
    valid.write_text("valid")
    bad = tmp_path / "bad.txt"
    paths = [valid, bad]
    if failure == "oversize":
        bad.write_bytes(b"x" * 262145)
    elif failure == "invalid_json":
        bad = tmp_path / "bad.json"
        bad.write_text("not JSON")
        paths[1] = bad
    elif failure == "symlink":
        bad.symlink_to(valid)
    elif failure == "unsupported":
        paths[1] = tmp_path / "file.pdf"
    elif failure == "too_many":
        paths = [valid] * 9
    elif failure == "directory":
        bad.mkdir()

    def forbidden(_):
        pytest.fail("Invalid local input caused network I/O")

    async with transport_client(forbidden) as http:
        with pytest.raises(ClientError) as exc:
            await Client(http_client=http).run("agent", "task", files=paths)
        assert exc.value.idempotency_key


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
async def test_invalid_wait_before_submission(timeout):
    async with transport_client(lambda _: pytest.fail("invalid wait submitted a task")) as http:
        with pytest.raises(ClientError, match="positive"):
            await Client(http_client=http).run("agent", "task", timeout=timeout)


@pytest.mark.parametrize(
    "payload",
    [
        {"detail": "sk-secret arbitrary error"},
        {
            "detail": [
                {
                    "loc": ["body", "model"],
                    "input": "sk-secret",
                    "msg": "sk-secret",
                    "ctx": {"error": "sk-secret"},
                }
            ]
        },
        {"detail": [{"loc": ["body", "sk-secret-key"], "msg": "sk-secret"}]},
    ],
)
async def test_actionable_errors_never_echo_untrusted_values(payload):
    async with transport_client(lambda _: httpx.Response(422, json=payload)) as http:
        with pytest.raises(ClientError) as exc:
            await Client(http_client=http).models()
    assert "secret" not in str(exc.value) and "secret" not in str(exc.value.fields)
    if (
        payload["detail"]
        and isinstance(payload["detail"], list)
        and payload["detail"][0]["loc"][-1] == "model"
    ):
        assert exc.value.fields == ("model",) and "model" in str(exc.value)


async def test_specific_model_error_has_actionable_safe_message(store):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as http:
        with pytest.raises(ClientError, match=r"client.models\(\)") as exc:
            await Client(http_client=http).create_agent(
                AgentConfig(name="bad", provider="openai", model="not-installed")
            )
    assert exc.value.code == "unsupported_model" and exc.value.status_code == 422


async def test_documentation_auth_and_groups_match_runtime(store):
    app = create_app(store, "test")
    schema = app.openapi()
    assert schema["components"]["securitySchemes"]["RunweaveAPIKey"]["scheme"] == "bearer"
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            assert operation["tags"] and operation["description"]
            assert operation["security"] == [{"RunweaveAPIKey": []}]
            assert not any(p["name"].lower() == "authorization" for p in operation.get("parameters", []))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        for header in (None, "wrong", "Bearer wrong", "Bearer", "Basic test"):
            assert (
                await http.get("/v1/models", headers={"Authorization": header} if header else {})
            ).status_code == 401
        assert (await http.get("/v1/models", headers={"Authorization": "Bearer test"})).status_code == 200


async def test_exhausted_upload_error_recovers_with_parent_request_key(store, tmp_path):
    agent = await setup(store)
    file = tmp_path / "input.txt"
    file.write_text("content")
    app = httpx.ASGITransport(app=create_app(store, "test"))
    attempts = []

    async def handle(request):
        response = await app.handle_async_request(request)
        if request.method == "POST" and request.url.path == "/v1/artifacts":
            await response.aread()
            attempts.append(response.json()["id"])
            if len(attempts) <= 3:
                raise httpx.ReadError("private transport data")
        if request.method == "POST" and request.url.path == "/v1/runs":
            await response.aread()
            await store.finish(response.json()["id"], "completed", {"answer": "done"})
        return response

    async with transport_client(handle) as http:
        client = Client(http_client=http)
        with pytest.raises(ClientError) as exc:
            await client.run(agent.id, "test", files=[file])
        key = exc.value.idempotency_key
        assert key and not key.startswith("run-file-") and exc.value.run_id is None
        assert "private" not in str(exc.value)
        result = await client.run(agent.id, "test", files=[file], idempotency_key=key)
        assert result.status == "completed" and len(set(attempts)) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_terminal_outcome_waits_for_cleanup_and_keeps_advanced_details(store, status):
    agent = await setup(store)
    from agent_runtime.schemas import RunCreate

    run = await store.submit(RunCreate(agent_id=agent.id, input="test"), "terminal")
    reads, progress = [], []

    def handle(request):
        reads.append(request.url.path)
        result = run.model_dump(mode="json")
        result.update(
            status=status,
            cleanup_state="pending" if len(reads) == 1 else "complete",
            error="sk-private-unknown-error",
        )
        return httpx.Response(200, json=result)

    async with transport_client(handle) as http:
        result = await Client(http_client=http).result(run.id, on_progress=progress.append)
    assert result.status == status and result.answer is None and len(reads) == 2
    assert [event.stage for event in progress] == ["cleaning_up", status]
    assert "private" not in result.model_dump_json()
    assert result.details.error == "sk-private-unknown-error"


async def test_cli_simple_run_and_resume_return_compact_results(store, monkeypatch, capsys):
    from agent_runtime import cli

    agent = await setup(store)
    app = httpx.ASGITransport(app=create_app(store, "test"))

    async def handle(request):
        response = await app.handle_async_request(request)
        if request.method == "POST" and request.url.path == "/v1/runs":
            await response.aread()
            await store.finish(response.json()["id"], "completed", {"answer": "friendly answer"})
        return response

    monkeypatch.setenv("API_KEY", "test")
    async with transport_client(handle) as http:
        monkeypatch.setattr(cli, "Client", lambda *args: Client(http_client=http))
        assert await cli.run(cli.parser().parse_args(["run", agent.id, "test", "--json"])) == 0
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["answer"] == "friendly answer" and "details" not in result
        assert "Retry key:" in captured.err
        assert await cli.run(cli.parser().parse_args(["result", result["run_id"]])) == 0
        assert "friendly answer" in capsys.readouterr().out


async def test_streaming_auth_failure_stays_safe_without_reading_error_body():
    class SecretStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("An error body from the streaming endpoint was read")
            yield b"sk-secret"

    async with transport_client(lambda _: httpx.Response(401, stream=SecretStream())) as http:
        with pytest.raises(ClientError, match="Check API_KEY") as exc:
            async for _ in Client(http_client=http).watch("run-id"):
                pytest.fail("Unauthorized stream yielded an event")
        assert exc.value.status_code == 401


@pytest.mark.parametrize("approved", [True, False])
async def test_decided_approval_is_not_represented_during_worker_handoff(store, approved):
    from agent_runtime.schemas import RunCreate

    agent = await setup(store)
    run = await store.submit(RunCreate(agent_id=agent.id, input="note test"), "handoff")
    await store.awaiting(
        run.id,
        [
            {"id": "first", "tool": "record_note", "arguments": {"text": "one"}},
            {"id": "second", "tool": "record_note", "arguments": {"text": "two"}},
        ],
    )
    await store.decide(run.id, "first", approved)
    app = httpx.ASGITransport(app=create_app(store, "test"))
    async with httpx.AsyncClient(
        transport=app, base_url="http://test", headers={"Authorization": "Bearer test"}
    ) as http:
        client = Client(http_client=http)
        pending = await client.result(run.id)
        assert [a.id for a in pending.approvals] == ["second"]
        decided = await client.decide(run.id, "second", approved)
        assert decided.approvals == []
        seen = []
        with pytest.raises(ClientError) as exc:
            await client.result(run.id, timeout=0.2, on_progress=seen.append)
        assert exc.value.code == "wait_timeout" and exc.value.run_id == run.id
        assert [p.stage for p in seen] == ["resuming"]
        assert (await client.get(run.id)).status == "awaiting_approval"
        await store.resumed(run.id)
        await store.finish(run.id, "completed", {"answer": "settled"})
        assert (await client.result(run.id)).answer == "settled"
        events = await store.events(run.id)
        assert sum(e.type == "approval.decided" for e in events) == 2
