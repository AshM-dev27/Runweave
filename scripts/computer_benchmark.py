"""Explicitly paid E2B execution benchmark against an already deployed workspace.

The planner is scripted/fake; provider compute, API, persistence and cleanup are real.
Only this benchmark's runs/computers are cancelled/closed. Results contain no credentials.
"""

import argparse
import asyncio
import csv
import hashlib
import io
import json
import os
import sqlite3
import struct
import subprocess
import textwrap
import time
import zipfile
import zlib
from collections import Counter
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

from dotenv import dotenv_values

from agent_runtime.client import Client
from agent_runtime.client_errors import ClientError
from agent_runtime.schemas import AgentConfig, GeneralPolicy

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def money(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def csv_file(path, fields, rows):
    with path.open("w", newline="") as target:
        writer = csv.writer(target, lineterminator="\n")
        writer.writerow(fields)
        writer.writerows(rows)


def fixtures(directory, count):
    customers = {f"C{i:04d}": f"R{i % 5}" for i in range(1000)}
    invoices, payments, ledger = [], [], {}
    counts = dict(
        invoice_duplicates=0, invalid_amount=0, unknown_customer=0, payment_duplicates=0, orphan_payment=0
    )
    for i in range(count):
        ident = f"I{i:07d}"
        cents = 1000 + i * 37 % 100000
        customer = "UNKNOWN" if i % 499 == 0 else f"C{i % 1000:04d}"
        amount = "BAD" if i % 997 == 0 else money(cents)
        invoices.append((ident, customer, amount, "detail-" + "x" * 24))
        if i % 101 == 0:
            invoices.append((ident, customer, "9999.99", "duplicate"))
            counts["invoice_duplicates"] += 1
        if amount == "BAD":
            counts["invalid_amount"] += 1
        elif customer not in customers:
            counts["unknown_customer"] += 1
        else:
            ledger[ident] = [customers[customer], cents, 0]
        if i % 4:
            paid = cents - (i % 3) * 100
            if i % 97 == 0:
                paid = -500
            payments.append((f"P{i:07d}", ident, str(paid)))
            if ident in ledger:
                ledger[ident][2] += paid
            else:
                counts["orphan_payment"] += 1
            if i % 113 == 0:
                payments.append((f"P{i:07d}", ident, "123456"))
                counts["payment_duplicates"] += 1
    for i in range(100):
        payments.append((f"ORPHAN{i}", "MISSING", "100"))
        counts["orphan_payment"] += 1
    csv_file(directory / "customers.csv", ["customer_id", "region"], customers.items())
    csv_file(directory / "invoices.csv", ["invoice_id", "customer_id", "amount", "memo"], invoices)
    csv_file(directory / "payments.csv", ["payment_id", "invoice_id", "cents"], payments)
    delta = []
    for i, (ident, row) in enumerate(list(ledger.items())[:1500]):
        delta.append((f"DELTA{i}", ident, "250"))
        if i % 10 == 0:
            delta.append((f"DELTA{i}", ident, "250"))
    csv_file(directory / "delta.csv", ["payment_id", "invoice_id", "cents"], delta)
    return ledger, counts


def summary(ledger):
    regions = {}
    for region, invoiced, paid in ledger.values():
        row = regions.setdefault(region, {"invoiced": 0, "paid": 0, "balance": 0, "rows": 0})
        row["invoiced"] += invoiced
        row["paid"] += paid
        row["balance"] += invoiced - paid
        row["rows"] += 1
    return regions


class ReportTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"th", "td"}:
            self.cell = ""

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag in {"th", "td"} and self.cell is not None and self.row is not None:
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


INGEST = """
import csv, json, sqlite3, time, resource
started=time.monotonic()
from decimal import Decimal, InvalidOperation
from pathlib import Path
db=sqlite3.connect('working.sqlite')
db.executescript('CREATE TABLE ledger(id TEXT PRIMARY KEY, region TEXT, invoiced INTEGER, paid INTEGER); CREATE TABLE payments(id TEXT PRIMARY KEY); CREATE TABLE exceptions(kind TEXT,id TEXT);')
customers={r['customer_id']:r['region'] for r in csv.DictReader(open('customers.csv'))}
seen=set(); counts={k:0 for k in ['invoice_duplicates','invalid_amount','unknown_customer','payment_duplicates','orphan_payment']}
def bad(kind, ident):
 counts[kind]+=1; db.execute('INSERT INTO exceptions VALUES (?,?)',(kind,ident))
for r in csv.DictReader(open('invoices.csv')):
 ident=r['invoice_id']
 if ident in seen: bad('invoice_duplicates',ident); continue
 seen.add(ident)
 try:
  value=Decimal(r['amount'])*100
  if not value.is_finite() or value!=value.to_integral_value(): raise InvalidOperation()
  cents=int(value)
 except (InvalidOperation, ValueError): bad('invalid_amount',ident); continue
 if r['customer_id'] not in customers: bad('unknown_customer',ident); continue
 db.execute('INSERT INTO ledger VALUES (?,?,?,0)',(ident,customers[r['customer_id']],cents))
for r in csv.DictReader(open('payments.csv')):
 if not db.execute('INSERT OR IGNORE INTO payments VALUES (?)',(r['payment_id'],)).rowcount: bad('payment_duplicates',r['payment_id']); continue
 if not db.execute('UPDATE ledger SET paid=paid+? WHERE id=?',(int(r['cents']),r['invoice_id'])).rowcount: bad('orphan_payment',r['payment_id'])
db.commit()
checkpoint=sqlite3.connect('checkpoint.sqlite'); db.backup(checkpoint); checkpoint.close()
Path('ingest.json').write_text(json.dumps(counts,sort_keys=True))
print(json.dumps({'compute_seconds':round(time.monotonic()-started,3),'cpu_seconds':round(time.process_time(),3),'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
"""

REPORT = """
import csv, json, sqlite3, time, resource
started=time.monotonic()
from pathlib import Path
db=sqlite3.connect('working.sqlite')
with open('reconciliation.csv','w',newline='') as f:
 w=csv.writer(f,lineterminator='\\n'); w.writerow(['invoice_id','region','invoiced','paid','balance'])
 for ident,region,invoiced,paid in db.execute('SELECT * FROM ledger ORDER BY id'): w.writerow([ident,region,invoiced,paid,invoiced-paid])
with open('exceptions.csv','w',newline='') as f:
 w=csv.writer(f,lineterminator='\\n'); w.writerow(['kind','id']); w.writerows(db.execute('SELECT * FROM exceptions ORDER BY rowid'))
regions={r:{'invoiced':i,'paid':p,'balance':i-p,'rows':n} for r,i,p,n in db.execute('SELECT region,sum(invoiced),sum(paid),count(*) FROM ledger GROUP BY region')}
Path('summary.json').write_text(json.dumps(regions,sort_keys=True))
print(json.dumps({'compute_seconds':round(time.monotonic()-started,3),'cpu_seconds':round(time.process_time(),3),'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
"""

HTML = """
import html,json
from pathlib import Path
data=json.loads(Path('summary.json').read_text())
rows=''.join('<tr><th>'+html.escape(region)+'</th>'+''.join('<td>'+str(item[k])+'</td>' for k in ['rows','invoiced','paid','balance'])+'</tr>' for region,item in sorted(data.items()))
Path('report.html').write_text('<!doctype html><html><meta charset="utf-8"><title>Reconciliation</title><h1>Invoice reconciliation</h1><p>Amounts in integer cents.</p><table><tr><th>Region</th><th>Invoices</th><th>Invoiced</th><th>Paid</th><th>Balance</th></tr>'+rows+'</table></html>')
"""

DELTA = """
import csv,json,sqlite3
from pathlib import Path
db=sqlite3.connect('working.sqlite'); applied=0
for r in csv.DictReader(open('delta.csv')):
 if db.execute('INSERT OR IGNORE INTO payments VALUES (?)',(r['payment_id'],)).rowcount:
  assert db.execute('UPDATE ledger SET paid=paid+? WHERE id=?',(int(r['cents']),r['invoice_id'])).rowcount==1
  applied+=1
db.commit()
Path(RESULT).write_text(json.dumps({'applied':applied}))
"""


def workbook(rows):
    body = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    for i, (category, value) in enumerate(rows, 1):
        body += f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>{category}</t></is></c><c r="B{i}"><v>{value}</v></c></row>'
    body += "</sheetData></worksheet>"
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sales" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": body,
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return stream.getvalue()


BINARY = """
import importlib.util,json,struct,zipfile,zlib
from xml.etree import ElementTree as ET
from pathlib import Path
with zipfile.ZipFile('sales.xlsx') as z: root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}; totals={}; count=0
for row in root.findall('m:sheetData/m:row',ns):
 cells=row.findall('m:c',ns); category=cells[0].find('m:is/m:t',ns).text; value=int(cells[1].find('m:v',ns).text)
 totals[category]=totals.get(category,0)+value; count+=1
Path('sales-summary.json').write_text(json.dumps({'rows':count,'totals':totals,'libraries':{p:importlib.util.find_spec(p) is not None for p in ['pandas','openpyxl','PIL','pypdf','matplotlib','duckdb']}}))
width,height=400,200; raw=bytearray()
for y in range(height):
 raw.append(0)
 for x in range(width):
  group=min(4,x//80); bar=int(totals['G'+str(group)]/max(totals.values())*180)
  raw.extend((40,100+group*20,180) if y>=height-bar and x%80<60 else (250,250,250))
def chunk(kind,data): return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
Path('sales.png').write_bytes(b'\\x89PNG\\r\\n\\x1a\\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b''))
with zipfile.ZipFile('sales.xlsx') as src, zipfile.ZipFile('sales-copy.xlsx','w',zipfile.ZIP_DEFLATED) as dst:
 for info in src.infolist(): dst.writestr(info.filename,src.read(info.filename))
"""


def invoke(code, outputs, *, computer="analysis", artifacts=None, capability="computer_python"):
    args = {
        "code": textwrap.dedent(code).strip(),
        "artifacts": artifacts or {},
        "outputs": {name: {"filename": name, "media_type": media} for name, media in outputs.items()},
    }
    if capability == "computer_python":
        args["computer"] = computer
    assert len(args["code"].encode()) <= 8192
    return {"action": {"kind": "invoke", "capability": capability, "arguments": args}}


class Benchmark:
    def __init__(self, client, output):
        self.client, self.output = client, output
        self.runs, self.sessions, self.records = [], set(), []
        self.agent = None

    def save(self):
        (self.output / "results.json").write_text(
            json.dumps(
                {"planner": "fake/deterministic", "compute": "real E2B", "records": self.records}, indent=2
            )
        )

    async def submit(self, name, actions, *, session_id=None, artifacts=None, key=None):
        prompt = "general:" + json.dumps(actions)
        start = time.monotonic()
        run = await self.client.submit(
            self.agent.id,
            prompt,
            session_id=session_id,
            artifact_ids=artifacts or [],
            idempotency_key=key or f"computer-bench-{uuid4().hex}",
        )
        self.runs.append(run.id)
        self.sessions.add(run.session_id)
        print(json.dumps({"case": name, "stage": "submitted", "run_id": run.id}), flush=True)
        return run, start, prompt

    async def result(self, name, run, start):
        result = await self.client.result(run.id, timeout=180)
        operations = (await self.client.operations(run.id))["items"]
        errors = []
        commands = []
        for op in operations:
            value = op.get("result") or {}
            if isinstance(value, dict):
                error = value.get("error") or (value.get("output") or {}).get("error")
                if error:
                    errors.append(error)
                receipt = (value.get("output") or {}).get("receipt")
                if receipt:
                    command = {"exit_code": receipt["exit_code"]}
                    try:
                        metrics = json.loads(receipt.get("stdout", ""))
                    except ValueError:
                        metrics = {}
                    if isinstance(metrics, dict):
                        command.update(
                            {
                                k: metrics[k]
                                for k in ["compute_seconds", "cpu_seconds", "max_rss_kib"]
                                if k in metrics
                            }
                        )
                    commands.append(command)
        record = {
            "case": name,
            "run_id": run.id,
            "session_id": run.session_id,
            "seconds": round(time.monotonic() - start, 3),
            "status": result.status,
            "outcome": result.outcome,
            "outcome_reason": result.outcome_reason,
            "errors": errors,
            "commands": commands,
            "files": [f.model_dump(mode="json") for f in result.files],
        }
        self.records.append(record)
        self.save()
        print(
            json.dumps({k: v for k, v in record.items() if k != "files"} | {"file_count": len(result.files)}),
            flush=True,
        )
        return result, record

    async def run(self, name, actions, **kwargs):
        run, start, _ = await self.submit(name, actions, **kwargs)
        return await self.result(name, run, start)

    async def download(self, result, name):
        ref = next(f for f in result.files if f.filename == name)
        path = self.output / (result.run_id + "-" + name)
        await self.client.download_file(ref.id, path)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == ref.sha256
        return path

    async def close_session(self, session_id):
        identities = [c.id for c in await self.client.computers(session_id)]
        for identity in identities:
            await self.client.close_computer(identity)
        async with asyncio.timeout(60):
            while any(c.status != "closed" for c in await self.client.computers(session_id)):
                await asyncio.sleep(0.5)

    async def reconciliation(self, count, restart_worker):
        ledger, expected_counts = fixtures(self.output, count)
        delta_count = min(1500, len(ledger))
        refs = {
            name: await self.client.upload_file(self.output / name)
            for name in ["customers.csv", "invoices.csv", "payments.csv", "delta.csv"]
        }
        result, record = await self.run(
            "multi_file_reconciliation",
            [
                invoke(
                    INGEST,
                    {"checkpoint.sqlite": "application/octet-stream", "ingest.json": "application/json"},
                    artifacts={
                        name: refs[name].id for name in ["customers.csv", "invoices.csv", "payments.csv"]
                    },
                ),
                invoke(
                    REPORT,
                    {
                        "reconciliation.csv": "text/csv",
                        "exceptions.csv": "text/csv",
                        "summary.json": "application/json",
                    },
                ),
                invoke(HTML, {"report.html": "text/plain"}),
            ],
            artifacts=[refs[n].id for n in ["customers.csv", "invoices.csv", "payments.csv"]],
        )
        assert result.outcome == "succeeded", record
        assert json.loads((await self.download(result, "ingest.json")).read_text()) == expected_counts
        assert json.loads((await self.download(result, "summary.json")).read_text()) == summary(ledger)
        with (await self.download(result, "reconciliation.csv")).open() as source:
            rows = list(csv.DictReader(source))
        actual = {r["invoice_id"]: [r["region"], int(r["invoiced"]), int(r["paid"])] for r in rows}
        assert len(rows) == len(ledger) and actual == ledger
        assert all(int(r["balance"]) == int(r["invoiced"]) - int(r["paid"]) for r in rows)
        with (await self.download(result, "exceptions.csv")).open() as source:
            exceptions = list(csv.DictReader(source))
        assert Counter(r["kind"] for r in exceptions) == expected_counts
        table = ReportTable()
        table.feed((await self.download(result, "report.html")).read_text())
        assert table.rows[0] == ["Region", "Invoices", "Invoiced", "Paid", "Balance"]
        assert {
            r[0]: dict(zip(["rows", "invoiced", "paid", "balance"], map(int, r[1:]))) for r in table.rows[1:]
        } == summary(ledger)
        checkpoint = await self.download(result, "checkpoint.sqlite")
        with sqlite3.connect(checkpoint) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT count(*) FROM ledger").fetchone()[0] == len(ledger)
        record.update(
            verified=True,
            source_invoice_rows=count,
            valid_invoices=len(ledger),
            input_bytes=sum(refs[n].size_bytes for n in ["customers.csv", "invoices.csv", "payments.csv"]),
            exceptions=expected_counts,
            published_bytes=sum(f.size_bytes for f in result.files),
        )
        identity = (await self.client.computers(result.session_id))[0].id
        if restart_worker:
            subprocess.run(
                [
                    "sudo",
                    "-n",
                    "env",
                    "API_PORT=18000",
                    "docker",
                    "compose",
                    "--profile",
                    "app",
                    "up",
                    "-d",
                    "--no-deps",
                    "--no-build",
                    "--force-recreate",
                    "worker",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        key = f"computer-bench-delta-{uuid4().hex}"
        actions = [
            invoke(
                DELTA.replace("RESULT", repr("delta.json")),
                {"delta.json": "application/json"},
                artifacts={"delta.csv": refs["delta.csv"].id},
            ),
            invoke(DELTA.replace("RESULT", repr("replay.json")), {"replay.json": "application/json"}),
            invoke(
                REPORT.replace("'summary.json'", "'updated-summary.json'"),
                {"updated-summary.json": "application/json"},
            ),
        ]
        run, start, prompt = await self.submit(
            "incremental_update_and_replay",
            actions,
            session_id=result.session_id,
            artifacts=[refs["delta.csv"].id],
            key=key,
        )
        updated, delta_record = await self.result("incremental_update_and_replay", run, start)
        assert updated.outcome == "succeeded", delta_record
        assert json.loads((await self.download(updated, "delta.json")).read_text()) == {
            "applied": delta_count
        }
        assert json.loads((await self.download(updated, "replay.json")).read_text()) == {"applied": 0}
        for row in list(ledger.values())[:1500]:
            row[2] += 250
        assert json.loads((await self.download(updated, "updated-summary.json")).read_text()) == summary(
            ledger
        )
        repeated = await self.client.submit(
            self.agent.id,
            prompt,
            session_id=result.session_id,
            artifact_ids=[refs["delta.csv"].id],
            idempotency_key=key,
        )
        assert repeated.id == run.id
        computer = (await self.client.computers(result.session_id))[0]
        assert computer.id == identity and computer.operation_count == 6
        delta_record.update(
            verified=True,
            unique_delta_payments=delta_count,
            replayed_payments_applied=0,
            submission_reused_same_run=True,
            worker_replaced=restart_worker,
            computer_id=identity,
            operation_count=computer.operation_count,
        )
        await self.close_session(result.session_id)
        self.save()

    async def binary(self):
        rows = [(f"G{i % 5}", 100 + i % 1000) for i in range(6000)]
        expected = {f"G{i}": sum(v for g, v in rows if g == f"G{i}") for i in range(5)}
        source = self.output / "sales.xlsx"
        source.write_bytes(workbook(rows))
        ref = await self.client.upload_file(source)
        result, record = await self.run(
            "xlsx_to_summary_and_png",
            [
                invoke(
                    BINARY,
                    {
                        "sales-summary.json": "application/json",
                        "sales.png": "image/png",
                        "sales-copy.xlsx": XLSX,
                    },
                    artifacts={"sales.xlsx": ref.id},
                )
            ],
            artifacts=[ref.id],
        )
        assert result.outcome == "succeeded", record
        actual = json.loads((await self.download(result, "sales-summary.json")).read_text())
        assert actual["rows"] == len(rows) and actual["totals"] == expected
        png = (await self.download(result, "sales.png")).read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", png[16:24]) == (400, 200)
        pos = 8
        compressed = b""
        while pos < len(png):
            size = struct.unpack(">I", png[pos : pos + 4])[0]
            kind = png[pos + 4 : pos + 8]
            data = png[pos + 8 : pos + 8 + size]
            assert (
                zlib.crc32(kind + data) & 0xFFFFFFFF
                == struct.unpack(">I", png[pos + 8 + size : pos + 12 + size])[0]
            )
            if kind == b"IDAT":
                compressed += data
            pos += size + 12
        assert len(zlib.decompress(compressed)) == 200 * (1 + 400 * 3)
        copied = await self.download(result, "sales-copy.xlsx")
        with zipfile.ZipFile(source) as left, zipfile.ZipFile(copied) as right:
            assert {n: left.read(n) for n in left.namelist()} == {n: right.read(n) for n in right.namelist()}
            assert len(ElementTree.fromstring(right.read("xl/worksheets/sheet1.xml"))[0]) == 6000
        record.update(
            verified=True, rows=6000, guest_libraries=actual["libraries"], image_dimensions=[400, 200]
        )
        await self.close_session(result.session_id)
        self.save()

    async def failure_recovery(self):
        good = "from pathlib import Path\nimport json\nPath('checkpoint.json').write_text(json.dumps({'values':list(range(1000))}))"
        broken = "from pathlib import Path\nPath('partial.json').write_text('{}')\nraise RuntimeError('intentional benchmark failure after checkpoint')"
        result, record = await self.run(
            "failure_after_checkpoint",
            [
                invoke(good, {"checkpoint.json": "application/json"}, computer="recovery"),
                invoke(broken, {"partial.json": "application/json"}, computer="recovery"),
            ],
        )
        assert result.outcome != "succeeded" and result.outcome_reason == "tool_error", record
        assert [f.filename for f in result.files] == ["checkpoint.json"]
        assert json.loads((await self.download(result, "checkpoint.json")).read_text()) == {
            "values": list(range(1000))
        }
        await self.close_session(result.session_id)
        ref = result.files[0]
        recovered, recovery_record = await self.run(
            "recover_from_published_checkpoint",
            [
                invoke(
                    "import json\nfrom pathlib import Path\nv=json.loads(Path('checkpoint.json').read_text())['values']; v.extend(range(1000,1100)); Path('recovered.json').write_text(json.dumps({'count':len(v),'sum':sum(v)}))",
                    {"recovered.json": "application/json"},
                    computer="restarted",
                    artifacts={"checkpoint.json": ref.id},
                )
            ],
            session_id=result.session_id,
            artifacts=[ref.id],
        )
        assert recovered.outcome == "succeeded", recovery_record
        assert json.loads((await self.download(recovered, "recovered.json")).read_text()) == {
            "count": 1100,
            "sum": 604450,
        }
        record.update(verified=True, checkpoint_survived=True, partial_output_not_published=True)
        recovery_record.update(verified=True, recovered_items=1100)
        await self.close_session(result.session_id)
        self.save()

    async def boundaries(self):
        start = time.monotonic()
        oversized = self.output / "oversized.bin"
        try:
            with oversized.open("wb") as f:
                f.truncate(16 * 1024 * 1024 + 1)
            try:
                await self.client.upload_file(oversized)
            except ClientError as exc:
                assert exc.code == "artifact_size_limit"
                self.records.append(
                    {
                        "case": "oversized_upload",
                        "verified": True,
                        "error": exc.code,
                        "seconds": round(time.monotonic() - start, 3),
                        "bytes": 16 * 1024 * 1024 + 1,
                    }
                )
            else:
                raise AssertionError("Oversized upload was admitted")
        finally:
            oversized.unlink(missing_ok=True)
        stale, record = await self.run(
            "stale_output_rejected",
            [
                invoke(
                    "from pathlib import Path\nPath('same.json').write_text('{\"version\":1}')",
                    {"same.json": "application/json"},
                    computer="stale",
                ),
                invoke(
                    "print('Intentionally omitted the declared output')",
                    {"same.json": "application/json"},
                    computer="stale",
                ),
            ],
        )
        assert stale.outcome != "succeeded" and len(stale.files) == 1, record
        assert json.loads((await self.download(stale, "same.json")).read_text()) == {"version": 1}
        record.update(verified=True, stale_output_not_republished=True)
        await self.close_session(stale.session_id)
        timed, record = await self.run(
            "command_time_limit",
            [
                invoke(
                    "import time\ntime.sleep(25)", {"never.json": "application/json"}, capability="e2b_files"
                )
            ],
        )
        assert timed.outcome != "succeeded" and not timed.files, record
        record.update(verified=True, requested_sleep_seconds=25, configured_command_seconds=20)
        names = [f"output{i}.json" for i in range(9)]
        code = "from pathlib import Path\n" + "\n".join(
            f"Path({name!r}).write_text('{{}}')" for name in names
        )
        limited, record = await self.run(
            "root_output_count_limit",
            [
                invoke(code, dict.fromkeys(names[:7], "application/json"), computer="quota"),
                invoke(code, dict.fromkeys(names[7:], "application/json"), computer="quota"),
            ],
        )
        assert limited.outcome == "needs_attention" and len(limited.files) == 8, record
        record.update(verified=True, requested_outputs=9, published_outputs=8, partial_publication=True)
        await self.close_session(limited.session_id)
        limited, record = await self.run(
            "conversation_computer_limit",
            [
                invoke(
                    "from pathlib import Path\nPath('ok.json').write_text('{}')",
                    {"ok.json": "application/json"},
                    computer=name,
                )
                for name in ["first", "second", "third"]
            ],
        )
        assert limited.outcome == "needs_attention" and len(limited.files) == 2, record
        computers = await self.client.computers(limited.session_id)
        assert len(computers) == 2 and "computer_session_limit" in record["errors"]
        record.update(verified=True, requested_computers=3, acquired_computers=2)
        await self.close_session(limited.session_id)
        invalid, record = await self.run(
            "invalid_output_metadata_preflight",
            [invoke("print('must never execute')", {"report.html": "text/html"}, computer="invalid")],
        )
        assert invalid.outcome == "needs_attention" and not invalid.files, record
        assert not await self.client.computers(invalid.session_id)
        record.update(verified=True, provider_admission=False)
        self.save()

    async def capacity(self):
        action = invoke(
            "import time,json\nfrom pathlib import Path\ntime.sleep(3)\nPath('ready.json').write_text(json.dumps({'ready':True}))",
            {"ready.json": "application/json"},
            computer="pool",
        )
        submitted = await asyncio.gather(*(self.submit(f"parallel_{i}", [action]) for i in range(4)))
        completed = await asyncio.gather(
            *(self.result(f"parallel_{i}", r, t) for i, (r, t, _) in enumerate(submitted))
        )
        assert all(result.outcome == "succeeded" for result, _ in completed)
        status = await self.client.extension_status()
        assert status["items"][0]["active"] == 4 and status["items"][0]["pending_cleanup"] == 0, status
        fifth, start, _ = await self.submit("fifth_waits_for_warm_slot", [action])
        await asyncio.sleep(5)
        waiting = await self.client.get(fifth.id)
        assert waiting.status not in {"completed", "failed", "cancelled"}, waiting
        assert not await self.client.computers(fifth.session_id)
        assert (await self.client.extension_status())["items"][0]["active"] == 4
        await self.close_session(completed[0][0].session_id)
        result, record = await self.result("fifth_waits_for_warm_slot", fifth, start)
        assert result.outcome == "succeeded", record
        assert json.loads((await self.download(result, "ready.json")).read_text()) == {"ready": True}
        record.update(verified=True, held_slots_before_release=4, fifth_acquired_only_after_release=True)
        for result, parallel_record in completed:
            assert json.loads((await self.download(result, "ready.json")).read_text()) == {"ready": True}
            parallel_record.update(verified=True)
            await self.close_session(result.session_id)
        await self.close_session(fifth.session_id)
        self.save()

    async def cleanup(self):
        for identity in self.runs:
            run = await self.client.get(identity)
            if run.status not in {"completed", "failed", "cancelled"}:
                await self.client.cancel(identity)
                await self.client.wait(identity, timeout=60, stop_at_budget=False, stop_at_approval=False)
        for session in self.sessions:
            await self.close_session(session)


async def main(args):
    os.umask(0o077)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    key = dotenv_values(args.api_env, interpolate=False).get("API_KEY")
    if not key:
        raise ValueError("Private API environment file must contain API_KEY")
    async with Client(base_url=args.url, api_key=key) as client:
        baseline = await client.extension_status()
        if any(i["active"] for i in baseline["items"]):
            raise ValueError("Begin with no held E2B slots; close existing computers first")
        benchmark = Benchmark(client, output)
        benchmark.agent = await client.create_agent(
            AgentConfig(
                name="real-computer-benchmark",
                provider="fake",
                model="deterministic",
                tools=["computer_python", "e2b_files"],
                general=GeneralPolicy(),
            )
        )
        try:
            for name, case in [
                ("reconciliation", lambda: benchmark.reconciliation(args.rows, args.restart_worker)),
                ("binary", benchmark.binary),
                ("failure_recovery", benchmark.failure_recovery),
                ("boundaries", benchmark.boundaries),
                ("capacity", benchmark.capacity),
            ]:
                try:
                    await case()
                except Exception as exc:
                    benchmark.records.append(
                        {"case": name, "verified": False, "exception_type": type(exc).__name__}
                    )
                    benchmark.save()
                    print(
                        json.dumps(
                            {
                                "case": name,
                                "stage": "verification_failed",
                                "exception_type": type(exc).__name__,
                            }
                        ),
                        flush=True,
                    )
                    await benchmark.cleanup()
        finally:
            await benchmark.cleanup()
            final = await client.extension_status()
            benchmark.records.append(
                {
                    "case": "final_cleanup",
                    "verified": all(
                        i["active"] == 0 and i["pending_cleanup"] == 0 and i["outcome_unknown"] == 0
                        for i in final["items"]
                    ),
                    "status": final,
                }
            )
            benchmark.save()
        print("Benchmark evidence:", output, flush=True)
        if any(r.get("verified") is False for r in benchmark.records):
            raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Required acknowledgement: uses billable E2B compute"
    )
    parser.add_argument("--url", default="http://127.0.0.1:18000")
    parser.add_argument("--api-env", type=Path, default=Path(".env.api.local"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("var/benchmarks") / ("computers-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")),
    )
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument(
        "--restart-worker",
        action="store_true",
        help="Replace the local Compose worker between reconciliation tasks",
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; preflight does not execute provider calls")
    if not 1500 <= args.rows <= 150000:
        parser.error("--rows must be between 1500 and 150000")
    asyncio.run(main(args))
