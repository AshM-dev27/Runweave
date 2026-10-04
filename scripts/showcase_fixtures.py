"""Synthetic business fixtures and deterministic showcase transformations (no model calls)."""

import csv
import hashlib
import io
import json
import math
import re
from collections import defaultdict
from pathlib import Path

CRM_FIELDS = ["Full Name", "E-mail Address", "Company", "Country"]


def csv_text(rows, fields):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def create_fixtures(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rows = [
        dict(
            zip(
                CRM_FIELDS,
                [f" Customer {i:03d} ", f" CUSTOMER{i:03d}@EXAMPLE.TEST ", f" Company {i % 17:02d} ", " my "],
            )
        )
        for i in range(160)
    ]
    rows += [{**rows[i], "E-mail Address": rows[i]["E-mail Address"].strip().lower()} for i in range(20)]
    rows += [dict(zip(CRM_FIELDS, [f"Invalid {i}", "missing-at-sign", "Test Co", "MY"])) for i in range(5)]
    rows += [dict(zip(CRM_FIELDS, ["", f"invalid{i}@example.test", "Test Co", "MY"])) for i in range(5)]
    for i in range(5):
        rows += [
            dict(zip(CRM_FIELDS, [f"Conflict {i}", f"conflict{i}@example.test", company, "MY"]))
            for company in ("Company One", "Company Two")
        ]
    (directory / "customers.csv").write_text(csv_text(rows, CRM_FIELDS))
    orders = {
        "TEST-104": {"amount_cents": 4900, "age_days": 7, "delivered": False, "refunded": False},
        "TEST-105": {"amount_cents": 7900, "age_days": 45, "delivered": False, "refunded": False},
        "TEST-106": {"amount_cents": 2900, "age_days": 8, "delivered": False, "refunded": True},
        "TEST-107": {"amount_cents": 2500, "age_days": 4, "delivered": False, "refunded": False},
    }
    (directory / "orders.json").write_text(json.dumps(orders, indent=2) + "\n")
    offers = [
        {
            "supplier": "A",
            "part": "ABC-123",
            "price_cents": 4200,
            "pack_size": 10,
            "stock_packs": 6,
            "shipping_cents": 1500,
        },
        {
            "supplier": "B",
            "part": "ABC-123",
            "price_cents": 390,
            "pack_size": 1,
            "stock_packs": 35,
            "shipping_cents": 0,
        },
        {
            "supplier": "C",
            "part": "ABC-123",
            "price_cents": 400,
            "pack_size": 1,
            "stock_packs": 80,
            "shipping_cents": None,
        },
        {
            "supplier": "C",
            "part": "ABC-123X",
            "price_cents": 200,
            "pack_size": 1,
            "stock_packs": 100,
            "shipping_cents": 0,
        },
    ]
    (directory / "offers.json").write_text(json.dumps(offers, indent=2) + "\n")
    for supplier in ("A", "B", "C"):
        selected = [o for o in offers if o["supplier"] == supplier]
        cards = "".join(
            f"<article><h2>{o['part']}</h2><p>MYR {o['price_cents'] / 100:.2f} per pack of "
            f"{o['pack_size']}; stock {o['stock_packs']} packs; shipping "
            f"{'unknown' if o['shipping_cents'] is None else 'MYR ' + format(o['shipping_cents'] / 100, '.2f')}</p></article>"
            for o in selected
        )
        (directory / f"supplier-{supplier}.html").write_text(
            f"<!doctype html><html><head><title>Dummy Supplier {supplier}</title></head>"
            f"<body><h1>Synthetic supplier {supplier}</h1>{cards}"
            f'<script type="application/json" id="offers">{json.dumps(selected)}</script></body></html>'
        )
    return {"input_rows": 200, "accepted": 160, "duplicates": 20, "invalid": 10, "conflicts": 10}


def clean_customers(content):
    groups, rejected = defaultdict(list), []
    rows = list(csv.DictReader(io.StringIO(content)))
    for index, raw in enumerate(rows, 2):
        row = {
            "name": raw["Full Name"].strip(),
            "email": raw["E-mail Address"].strip().lower(),
            "company": raw["Company"].strip(),
            "country": raw["Country"].strip().upper(),
        }
        if (
            not row["name"]
            or not row["company"]
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", row["email"])
        ):
            rejected.append({"row": index, "reason": "invalid", **row})
        else:
            groups[row["email"]].append((index, row))
    accepted = []
    for group in groups.values():
        if len({json.dumps(row, sort_keys=True) for _, row in group}) != 1:
            rejected.extend({"row": index, "reason": "conflicts", **row} for index, row in group)
        else:
            accepted.append(group[0][1])
            rejected.extend({"row": index, "reason": "duplicates", **row} for index, row in group[1:])
    counts = {
        "input_rows": len(rows),
        "accepted": len(accepted),
        **{
            reason: sum(r["reason"] == reason for r in rejected)
            for reason in ("duplicates", "invalid", "conflicts")
        },
    }
    plan_id = hashlib.sha256(json.dumps(accepted, sort_keys=True).encode()).hexdigest()
    return {"plan_id": plan_id, "counts": counts, "accepted": accepted, "exceptions": rejected}


def compare_offers(offers, quantity=50, part="ABC-123"):
    comparison = []
    for offer in offers:
        packs = math.ceil(quantity / offer["pack_size"])
        reason = (
            "wrong_variant"
            if offer["part"] != part
            else "insufficient_stock"
            if offer["stock_packs"] < packs
            else "shipping_unknown"
            if offer["shipping_cents"] is None
            else "qualified"
        )
        comparison.append(
            {
                **offer,
                "packs_needed": packs,
                "units": packs * offer["pack_size"],
                "total_cents": None
                if offer["shipping_cents"] is None
                else packs * offer["price_cents"] + offer["shipping_cents"],
                "reason": reason,
            }
        )
    qualified = [o for o in comparison if o["reason"] == "qualified"]
    best = min(qualified, key=lambda o: o["total_cents"], default=None)
    return {
        "part": part,
        "quantity": quantity,
        "currency": "MYR",
        "selected": best,
        "offers": comparison,
        "claim": "Lowest fully priced qualifying offer; unknown shipping remains unresolved.",
    }
