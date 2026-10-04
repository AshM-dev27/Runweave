import asyncio
import time

import pytest
from test_general_semantic import http_client
from test_harness_extensions import Lookup, action, create, registration

from agent_runtime import activity_liveness as liveness
from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_db import GeneralOperationRow
from agent_runtime.general_runtime import general_action


async def operation(store, client):
    store.extensions = ExtensionRegistry({"tools": [registration()]}, handlers={"installed.lookup": Lookup()})
    run = await create(client)
    await general_action(action(run.id))
    identity = run.id + ":action:0"
    async with store.database.sessions.begin() as db:
        row = await db.get(GeneralOperationRow, identity)
        row.data = {**row.data, "owner": "owner", "lease": time.time() + 0.3}
    return run.id, identity


async def test_lease_renews_during_io_and_stops_after_completion(store, monkeypatch):
    async with http_client(store) as client:
        run_id, identity = await operation(store, client)
        monkeypatch.setattr(liveness, "enabled", lambda: True)
        monkeypatch.setattr(liveness, "RENEW_SECONDS", 0.03)
        monkeypatch.setattr(liveness, "LEASE_SECONDS", 0.3)

        async def work():
            await asyncio.sleep(0.45)
            return {"done": True}

        assert await liveness.leased_call(store, run_id, identity, "owner", work) == {"done": True}
        async with store.database.sessions() as db:
            lease = (await db.get(GeneralOperationRow, identity)).data["lease"]
        assert lease > time.time()
        await asyncio.sleep(0.07)
        async with store.database.sessions() as db:
            assert (await db.get(GeneralOperationRow, identity)).data["lease"] == lease


async def test_fenced_worker_cancels_its_io_without_overwriting_new_owner(store, monkeypatch):
    async with http_client(store) as client:
        run_id, identity = await operation(store, client)
        monkeypatch.setattr(liveness, "enabled", lambda: True)
        monkeypatch.setattr(liveness, "RENEW_SECONDS", 0.03)
        cancelled = asyncio.Event()

        async def work():
            async with store.database.sessions.begin() as db:
                row = await db.get(GeneralOperationRow, identity)
                row.data = {**row.data, "owner": "replacement"}
            try:
                await asyncio.sleep(5)
            finally:
                cancelled.set()

        with pytest.raises(Exception, match="extension_lease_fenced"):
            await liveness.leased_call(store, run_id, identity, "owner", work)
        assert cancelled.is_set()
        async with store.database.sessions() as db:
            assert (await db.get(GeneralOperationRow, identity)).data["owner"] == "replacement"
