"""Explicit, bounded Jev-only benchmark collector. No automatic retries or resume."""

import argparse
import getpass
import json
import os
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts.jev_benchmark import DEFAULT_CASES, DEFAULT_MODEL, load_cases, manifest, requests

ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-requests", type=int, default=240)
    parser.add_argument("--wall-seconds", type=int, default=900)
    args = parser.parse_args()
    if not args.live:
        parser.error("Live evaluation requires --live and separate user authorization")
    dataset = load_cases(args.cases)
    prepared = list(requests(dataset, args.model))
    if not 1 <= args.repeats <= 3 or not 1 <= args.max_requests <= 240:
        parser.error("This collector permits at most three repeats and 240 attempts")
    if len(prepared) * args.repeats > args.max_requests or not 1 <= args.wall_seconds <= 900:
        parser.error("Requested evaluation exceeds its fixed call or wall-time limit")
    args.out.mkdir(parents=True, exist_ok=False)
    frozen = {
        **manifest(dataset, args.model),
        "created_at": datetime.now(UTC).isoformat(),
        "repeats": args.repeats,
        "max_requests": args.max_requests,
        "wall_seconds": args.wall_seconds,
        "request_timeout_seconds": 15,
        "automatic_retries": 0,
        "input_price_usd_per_million_as_of_2026_09_22": 0.042,
        "price_source": "https://docs.typesafe.ai/models",
        "maximum_input_tokens_per_request_from_docs": 64000,
        "maximum_estimated_cost_usd_at_published_rate": args.max_requests * 64000 * 0.042 / 1_000_000,
        "random_seed": 922,
    }
    (args.out / "manifest.json").write_text(json.dumps(frozen, indent=2))
    (args.out / "cases.json").write_text(json.dumps(dataset, indent=2, ensure_ascii=False))
    (args.out / "requests.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + chr(10) for r in prepared)
    )
    key = os.environ.get("TYPESAFE_API_KEY") or getpass.getpass("TypeSafe API key (hidden): ")
    if not key.strip():
        raise SystemExit("No API key supplied; no inference attempted")
    opener = build_opener(NoRedirect())
    started = time.monotonic()
    calls = 0
    status = "completed"
    try:
        with (args.out / "attempts.jsonl").open("x") as ledger:
            for repeat in range(1, args.repeats + 1):
                order = prepared.copy()
                random.Random(922 + repeat).shuffle(order)
                with (args.out / f"repeat-{repeat}.jsonl").open("x") as output:
                    for item in order:
                        if time.monotonic() - started >= args.wall_seconds:
                            status = "wall_limit"
                            break
                        calls += 1
                        record = {
                            "case_id": item["case_id"],
                            "request_sha256": item["request_sha256"],
                            "repeat": repeat,
                        }
                        ledger.write(json.dumps({**record, "attempt": calls}) + chr(10))
                        ledger.flush()
                        os.fsync(ledger.fileno())
                        request = Request(
                            ENDPOINT,
                            data=json.dumps(item["request"], ensure_ascii=False).encode(),
                            headers={
                                "Authorization": "Bearer " + key.strip(),
                                "Content-Type": "application/json",
                            },
                            method="POST",
                        )
                        tick = time.monotonic()
                        try:
                            with opener.open(request, timeout=15) as response:
                                payload = json.load(response)
                            record["response"] = {k: payload[k] for k in ("model", "answers", "usage")}
                        except HTTPError as exc:
                            record["error_code"] = f"http_{exc.code}"
                            if exc.code in {401, 403}:
                                status = "credential_rejected"
                        except (TimeoutError, URLError):
                            record["error_code"] = "transport_error_outcome_unknown"
                        except (ValueError, KeyError, TypeError):
                            record["error_code"] = "malformed_response"
                        record["elapsed_ms"] = round((time.monotonic() - tick) * 1000, 3)
                        output.write(json.dumps(record, ensure_ascii=False) + chr(10))
                        output.flush()
                        if calls % 10 == 0 or "error_code" in record:
                            print(
                                json.dumps(
                                    {
                                        "attempts": calls,
                                        "repeat": repeat,
                                        "last_error": record.get("error_code"),
                                    }
                                ),
                                flush=True,
                            )
                        if status == "credential_rejected":
                            break
                if status != "completed":
                    break
    except BaseException:
        status = "interrupted"
        raise
    finally:
        summary = {"status": status, "attempts": calls, "elapsed_seconds": time.monotonic() - started}
        (args.out / "terminal.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
