"""Run the public API lifecycle with the unpaid, scripted fake provider.

From the repository root, after starting local services:
    uv run --env-file .env.local python -m examples.quickstart
"""

import asyncio
import os

from agent_runtime.client import Client
from agent_runtime.schemas import AgentConfig, GeneralPolicy


async def main():
    async with Client(
        base_url=os.environ.get("RUNWEAVE_URL", "http://localhost:18000"),
        api_key=os.environ["API_KEY"],
    ) as client:
        # Create once and save agent.id when integrating your own application.
        agent = await client.create_agent(
            AgentConfig(
                name="quickstart",
                provider="fake",
                model="deterministic",
                tools=[],
                general=GeneralPolicy(),
            )
        )
        result = await client.run(
            agent_id=agent.id,
            input="Run the scripted completion demonstration.",
        )
        print(result.answer if result.outcome == "succeeded" else result.message)
        print("Run:", result.run_id)
        print("Outcome:", result.outcome)
        if result.outcome != "succeeded":
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
