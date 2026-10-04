"""Deterministic inputs, independent expected bytes, and private SDK preflight actions."""

import base64

from agent_runtime.general_contracts import DelegationPolicy, GeneralPolicy
from agent_runtime.schemas import AgentConfig
from scripts.lightweight_budget import MANIFEST


def b64(value):
    return base64.b64encode(value).decode()


def fixture(name):
    tools = ["workspace_read", "workspace_write", "workspace_command", "workspace_verify"]
    files, expected = {}, {}
    if name == "bug":
        files = {
            "main.py": b"def double(x):\n    return x + 2\n",
            "test_main.py": b"from main import double\ndef test_double():\n    assert double(4) == 8\n    assert double(-3) == -6\n    assert double(0) == 0\n",
        }
        prompt = "Fix double; preserve supplied tests; complete after verification."
        checks = [{"id": "pytest", "kind": "command", "argv": ["pytest", "-q"]}]
        preserve = ["test_main.py"]
    elif name == "csv":
        files = {"input.csv": b"name,amount\n Alice , 7 \nBob,-2\nCara,\nDan,oops\nEve,9\n"}
        prompt = "Trim fields; accept signed integers; omit blank/invalid amounts; preserve valid row order. Write cleaned.csv and summary.csv; preserve input."
        expected = {
            "cleaned.csv": b"name,amount\nAlice,7\nBob,-2\nEve,9\n",
            "summary.csv": b"valid_rows,rejected_rows,total\n3,2,14\n",
        }
        preserve = list(files)
    elif name == "recovery":
        files = {
            "report.py": b"import csv\nimport sys\nfrom pathlib import Path\nwith open(sys.argv[1]) as source:\n    total = sum(int(row['amount']) for row in csv.DictReader(source))\nPath('result.csv').write_text(f'total\\n{total}\\n')\n",
            "inputs.csv": b"amount\n7\n-2\n9\n",
        }
        prompt = "First run python report.py input.csv. Diagnose its failure, correct the input path using available files, rerun, and verify result.csv; preserve supplied files."
        expected = {"result.csv": b"total\n14\n"}
        preserve = list(files)
    elif name == "direct":
        prompt = "Write sum.txt containing 7+5 followed by newline; complete."
        expected = {"sum.txt": b"12\n"}
        preserve = []
        tools = ["workspace_write", "workspace_verify"]
    else:
        assert name == "parallel"
        prompt = "Assign two specialists together: one writes left.txt with 12 plus newline, the other right.txt with 20 plus newline. Join both, merge both, verify integrated files, complete."
        expected = {"left.txt": b"12\n", "right.txt": b"20\n"}
        preserve = []
        tools = ["workspace_write", "workspace_verify"]
    if name != "bug":
        checks = [
            {"id": f"bytes{i}", "kind": "bytes", "path": p, "expected": b64(v)}
            for i, (p, v) in enumerate(expected.items())
        ]
    task = {
        "outcome": prompt,
        "criteria": [
            {
                "id": "output",
                "statement": "Satisfy the requested output and supplied checks",
                "evidence_policy": "check",
                "checks": checks,
            }
        ],
    }
    return dict(files=files, prompt=prompt, expected=expected, preserve=preserve, task=task, tools=tools)


def config(name):
    f = fixture(name)
    policy = GeneralPolicy(resources=None, limits={"model_attempts": MANIFEST["scenario_limits"][name]})
    if name in {"direct", "parallel"}:
        policy.delegation = DelegationPolicy(tools=f["tools"], limits={"model_attempts": 3})
        policy = GeneralPolicy.model_validate(policy.model_dump())
    return AgentConfig(
        name=name,
        provider="openai",
        model="gpt-5.6-luna",
        tools=f["tools"],
        max_tokens=MANIFEST["scenario_caps"][name],
        instructions="Work autonomously using authorized actions. Preserve supplied files and checks. Complete with scoped evidence.",
        general=policy,
    )


def write(files):
    return {"kind": "write", "files": [{"path": p, "content_base64": b64(v)} for p, v in files.items()]}


def actions(name, child=None):
    done = {
        "kind": "complete",
        "answer": "Verified requested output.",
        "assessments": [
            {"criterion": "c0", "disposition": "satisfied", "assessment": "Output produced and checked."}
        ],
    }
    if child:
        return [write({child: b"12\n" if child == "left.txt" else b"20\n"}), done]
    if name == "bug":
        return [
            {"kind": "read", "path": "main.py"},
            write({"main.py": b"def double(x):\n    return x * 2\n"}),
            done,
        ]
    if name == "csv":
        return [{"kind": "read", "path": "input.csv"}, write(fixture(name)["expected"]), done]
    if name == "recovery":
        return [
            {"kind": "command", "argv": ["python", "report.py", "input.csv"], "commit": True},
            {"kind": "read", "path": "inputs.csv"},
            {"kind": "command", "argv": ["python", "report.py", "inputs.csv"], "commit": True},
            done,
        ]
    if name == "direct":
        return [write(fixture(name)["expected"]), done]
    return [
        {
            "kind": "assign",
            "assignments": [
                {
                    "role": side,
                    "objective": f"Write {side}.txt with {value} plus newline; complete.",
                    "acceptance": [f"{side}.txt contains exactly {value} plus newline"],
                    "capabilities": ["workspace_write", "workspace_verify"],
                    "inputs": [],
                    "outputs": [f"{side}.txt"],
                }
                for side, value in [("left", 12), ("right", 20)]
            ],
        },
        {"kind": "join", "children": ["d0", "d1"]},
        {"kind": "merge", "child": "d0"},
        {"kind": "merge", "child": "d1"},
        done,
    ]
