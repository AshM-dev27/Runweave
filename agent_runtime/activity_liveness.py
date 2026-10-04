"""Worker liveness and renewable external-operation leases; no workflow-side I/O."""

import asyncio
import time
from contextlib import asynccontextmanager

from temporalio import activity

HEARTBEAT_SECONDS = 12
RENEW_SECONDS = 2
LEASE_SECONDS = 8


def enabled():
    return activity.in_activity() and activity.info().heartbeat_timeout is not None


@asynccontextmanager
async def heartbeat():
    async def pulse():
        while True:
            activity.heartbeat()
            await asyncio.sleep(RENEW_SECONDS)

    task = asyncio.create_task(pulse()) if enabled() else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def leased_call(store, run_id, operation_id, owner, call):
    if not enabled():
        return await call()
    from .general_db import GeneralOperationRow
    from .project_store import fail

    async def renew():
        while True:
            await asyncio.sleep(RENEW_SECONDS)
            async with store.database.sessions.begin() as db:
                operation = await db.get(GeneralOperationRow, operation_id, with_for_update=True)
                if operation is None or operation.run_id != run_id or operation.data.get("owner") != owner:
                    fail("extension_lease_fenced", 409)
                if operation.data.get("lease", 0) <= time.time():
                    fail("extension_lease_expired", 409)
                operation.data = {**operation.data, "lease": time.time() + LEASE_SECONDS}

    work, renewal = asyncio.create_task(call()), asyncio.create_task(renew())
    try:
        done, _ = await asyncio.wait([work, renewal], return_when=asyncio.FIRST_COMPLETED)
        if renewal in done:
            await renewal
            raise RuntimeError("Lease renewal stopped")
        return await work
    finally:
        for task in (work, renewal):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, renewal, return_exceptions=True)
