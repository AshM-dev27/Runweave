import json

import httpx
import pytest

from agent_runtime.api import create_app
from agent_runtime.client import Client, ClientError
from agent_runtime.schemas import AgentConfig


async def test_ambiguous_submission_retry_and_approval(store):
    app = httpx.ASGITransport(app=create_app(store, "test"))
    keys = []

    async def handle(request):
        if request.url.path == "/v1/runs":
            keys.append(request.headers["Idempotency-Key"])
            response = await app.handle_async_request(request)
            if len(keys) == 1:
                await response.aread()
                raise httpx.ReadError("secret response lost")
            return response
        return await app.handle_async_request(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as http:
        async with Client(http_client=http) as client:
            agent = await client.create_agent(
                AgentConfig(name="client", provider="fake", model="deterministic")
            )
            run = await client.submit(agent.id, "test")
            assert len(keys) == 2 and keys[0] == keys[1]
            await store.awaiting(
                run.id, [{"id": "call", "tool": "record_note", "arguments": {"text": "exact"}}]
            )
            assert (await client.wait(run.id)).approvals[0].arguments == {"text": "exact"}
            await client.decide(run.id, "call", True)
            await client.decide(run.id, "call", True)
            with pytest.raises(ClientError, match="409"):
                await client.decide(run.id, "call", False)
            await client.cancel(run.id)
            assert (await client.wait(run.id)).status == "cancelled"
            models = await client.models()
            assert all("credential_env" not in item and "endpoint" not in item for item in models)
        assert not http.is_closed


async def test_stream_reconnect_deduplicates(store):
    from tests.test_api import make_run

    run = await make_run(store)
    await store.cancel(run.id)
    events = await store.events(run.id)
    cursors = []

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield f"data: {events[0].model_dump_json()}\n\n".encode()
            raise httpx.ReadError("secret")

    def handle(request):
        if request.url.path.endswith("/events"):
            cursors.append(request.headers["Last-Event-ID"])
            if len(cursors) == 1:
                return httpx.Response(200, stream=Broken())
            return httpx.Response(200, text="".join(f"data: {e.model_dump_json()}\n\n" for e in events))
        return httpx.Response(
            200, json=json.loads(run.model_copy(update={"status": "cancelled"}).model_dump_json())
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as http:
        client = Client(http_client=http)
        received = [e.id async for e in client.watch(run.id)]
        assert received == [e.id for e in events]
        assert cursors == ["0", str(events[0].id)]


async def test_safe_errors():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, text="sk-secret")), base_url="http://test"
    ) as http:
        with pytest.raises(ClientError, match="Check API_KEY") as exc:
            await Client(http_client=http).models()
        assert "sk-secret" not in str(exc.value)


async def test_cli_failure_exit_and_decision_feedback(store, monkeypatch, capsys):
    from agent_runtime import cli
    from tests.test_api import make_run

    run = await make_run(store)
    await store.awaiting(run.id, [{"id": "call", "tool": "record_note", "arguments": {"text": "test"}}])
    monkeypatch.setenv("API_KEY", "test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as http:
        monkeypatch.setattr(cli, "Client", lambda *args: Client(http_client=http))
        assert await cli.run(cli.parser().parse_args(["deny", run.id, "call"])) == 0
        assert "Decision recorded" in capsys.readouterr().out
        await store.cancel(run.id)
        assert await cli.run(cli.parser().parse_args(["wait", run.id])) == 1


@pytest.mark.parametrize("submitted_key", [None, "recover-original"])
async def test_exhausted_submission_recovers_original_run(store, submitted_key):
    app = httpx.ASGITransport(app=create_app(store, "test"))
    accepted = []
    keys = []

    async def handle(request):
        response = await app.handle_async_request(request)
        if request.url.path == "/v1/runs":
            keys.append(request.headers["Idempotency-Key"])
            await response.aread()
            accepted.append(response.json()["id"])
            if len(keys) <= 3:
                raise httpx.ReadError("secret lost response")
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as http:
        client = Client(http_client=http)
        agent = await client.create_agent(
            AgentConfig(name="recovery", provider="fake", model="deterministic")
        )
        with pytest.raises(ClientError) as exc:
            await client.submit(agent.id, "test", idempotency_key=submitted_key)
        key = exc.value.idempotency_key
        assert key and keys == [key] * 3
        if submitted_key:
            assert key == submitted_key
        assert "secret" not in str(exc.value)
        recovered = await client.submit(agent.id, "test", idempotency_key=key)
        assert accepted == [recovered.id] * 4


async def test_clean_truncated_stream_replays_remaining_events(store):
    from tests.test_api import make_run

    run = await make_run(store)
    await store.awaiting(run.id, [{"id": "call", "tool": "record_note", "arguments": {"text": "test"}}])
    await store.finish(run.id, "completed", {"answer": "done"})
    events = await store.events(run.id)
    cursors = []

    def handle(request):
        if request.url.path.endswith("/events"):
            cursors.append(request.headers["Last-Event-ID"])
            batch = events[:1] if len(cursors) == 1 else events
            return httpx.Response(200, text="".join(f"data: {e.model_dump_json()}\n\n" for e in batch))
        return httpx.Response(
            200, json=json.loads(run.model_copy(update={"status": "completed"}).model_dump_json())
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as http:
        assert [e.id async for e in Client(http_client=http).watch(run.id)] == [e.id for e in events]
        assert cursors == ["0", str(events[0].id)]


@pytest.mark.parametrize("past_terminal", [0, 100])
async def test_watch_already_consumed_cursor_confirms_empty_replay(store, past_terminal):
    from tests.test_api import make_run

    run = await make_run(store)
    await store.cancel(run.id)
    cursor = (await store.events(run.id))[-1].id + past_terminal
    app = httpx.ASGITransport(app=create_app(store, "test"))
    cursors = []

    async def handle(request):
        if request.url.path.endswith("/events"):
            cursors.append(request.headers["Last-Event-ID"])
        return await app.handle_async_request(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as http:
        assert [e async for e in Client(http_client=http).watch(run.id, cursor=cursor, timeout=2)] == []
    assert cursors == [str(cursor)] * 2


@pytest.mark.parametrize("broken", [False, True])
async def test_watch_incomplete_stream_has_bounded_reconnects(store, broken):
    from tests.test_api import make_run

    run = await make_run(store)
    connections = 0

    def handle(request):
        nonlocal connections
        if request.url.path.endswith("/events"):
            connections += 1
            if broken:
                raise httpx.ReadError("secret transport details")
            return httpx.Response(200, text="")
        return httpx.Response(200, json=json.loads(run.model_dump_json()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as http:
        with pytest.raises(ClientError, match=f"Resume run {run.id} from cursor 0") as exc:
            async for _ in Client(http_client=http).watch(run.id, timeout=2):
                pytest.fail("Empty stream yielded an event")
        assert "secret" not in str(exc.value)
    assert connections == 3
