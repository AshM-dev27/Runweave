"""One Runweave task submission; real E2B actions, with an unpaid scripted planner.

Worker requires E2B_API_KEY and the installed e2b_python capability.
Run: uv run --env-file .env.api.local python -m examples.e2b_invoice
"""

import argparse
import asyncio
import csv
import hashlib
import io
import json
import os
from pathlib import Path
from uuid import uuid4

from agent_runtime.client import Client
from agent_runtime.general_contracts import GeneralLimits, GeneralPolicy
from agent_runtime.schemas import AgentConfig

CSV_INPUT = "invoice_id,amount\nINV-001,100.00\nINV-002,50.00\nINV-001,100.00\nREF-001,-20.00\n"
EXPECTED_ROWS = [["invoice_id", "amount"], ["INV-001", "100.00"], ["INV-002", "50.00"], ["REF-001", "-20.00"]]
EXPECTED_SUMMARY = {"input_rows": 4, "unique_rows": 3, "duplicates_removed": 1, "net_total": "130.00"}
PROGRAM = """import csv, json
from decimal import Decimal
from pathlib import Path
with Path("invoices.csv").open(newline="") as source:
    rows = list(csv.DictReader(source))
unique = {}
for row in rows:
    unique.setdefault(row["invoice_id"], row)
with Path("cleaned.csv").open("w", newline="") as output:
    writer = csv.DictWriter(output, fieldnames=["invoice_id", "amount"])
    writer.writeheader()
    writer.writerows(unique.values())
summary = {"input_rows": len(rows), "unique_rows": len(unique),
           "duplicates_removed": len(rows) - len(unique),
           "net_total": str(sum((Decimal(r["amount"]) for r in unique.values()), Decimal(0)))}
Path("summary.json").write_text(json.dumps(summary, sort_keys=True))
print("Invoice report generated")
"""


def job():
    return {"code": PROGRAM, "files": {"invoices.csv": CSV_INPUT}, "outputs": ["cleaned.csv", "summary.json"]}


def verify_output(output):
    if not output.get("cleanup_complete") or output.get("error"):
        raise ValueError("E2B execution or cleanup did not complete")
    if output.get("receipt", {}).get("exit_code") != 0:
        raise ValueError("E2B Python did not exit successfully")
    files = output["outputs"]
    for item in files.values():
        content = item["text"].encode()
        if item["sha256"] != hashlib.sha256(content).hexdigest() or item["size_bytes"] != len(content):
            raise ValueError("E2B output digest mismatch")
    rows = list(csv.reader(io.StringIO(files["cleaned.csv"]["text"])))
    summary = json.loads(files["summary.json"]["text"])
    if rows != EXPECTED_ROWS or summary != EXPECTED_SUMMARY:
        raise ValueError("Invoice result differs from independently specified expectations")
    return summary


async def run_demo(client, output_directory):
    capabilities = {entry["alias"] for entry in await client.installed_capabilities()}
    if "e2b_python" not in capabilities:
        raise ValueError("Install the e2b_python capability on the API and worker first")
    agent = await client.create_agent(
        AgentConfig(
            name="e2b-invoice-demo",
            provider="fake",
            model="deterministic",
            tools=["workspace_read", "e2b_python"],
            adaptive=False,
            general=GeneralPolicy(
                limits=GeneralLimits(
                    model_attempts=4, tool_attempts=8, total_tokens=16000, active_seconds=150
                )
            ),
        )
    )
    workspace = await client.workspace_create({"invoices.csv": CSV_INPUT.encode()})
    script = [
        {"action": {"kind": "invoke", "capability": "workspace_read", "arguments": {"path": "invoices.csv"}}},
        {"action": {"kind": "invoke", "capability": "e2b_python", "arguments": job()}},
    ]
    key = "e2b-demo-" + uuid4().hex
    result = await client.run(
        agent_id=agent.id,
        input="general:" + json.dumps(script),
        workspace=workspace,
        idempotency_key=key,
        timeout=180,
        on_progress=lambda progress: print(progress.stage + ": " + progress.message),
    )
    if result.outcome != "succeeded":
        raise ValueError("Runweave task did not succeed: " + result.outcome)
    operations = (await client.operations(result.run_id))["items"]
    outputs = [
        operation["result"]["output"]
        for operation in operations
        if (operation.get("result") or {}).get("output", {}).get("provider") == "e2b"
    ]
    if len(outputs) != 1:
        raise ValueError("Expected exactly one durable E2B job receipt")
    output = outputs[0]
    summary = verify_output(output)
    output_directory.mkdir(parents=True, exist_ok=True)
    for name, item in output["outputs"].items():
        (output_directory / name).write_bytes(item["text"].encode())
    print("Run:", result.run_id)
    print("E2B actions:", " -> ".join(output["actions"]))
    print("Verified net total:", summary["net_total"])
    print("Sandbox cleanup:", output["cleanup_complete"])
    print("Files:", str(output_directory.resolve()))
    return result, output


async def main(output_directory):
    async with Client(
        base_url=os.environ.get("RUNWEAVE_URL", "http://localhost:18000"), api_key=os.environ["API_KEY"]
    ) as client:
        await run_demo(client, output_directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/e2b-demo"))
    args = parser.parse_args()
    asyncio.run(main(args.output))
