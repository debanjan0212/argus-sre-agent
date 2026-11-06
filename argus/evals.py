"""Eval suite: replay recorded incidents through the real MCP path and score the agent.

Each scenario lists the expected root-cause category, component and remediation. Run it against
the runbook mode in CI, or against any model to compare them:  ARGUS_LLM=openai argus eval
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import yaml

from argus.agent import investigate
from argus.guardrails import Guardrails
from argus.llm import load_llm


async def run_scenario(path: Path) -> dict:
    spec = yaml.safe_load(path.read_text())
    os.environ.update({"ARGUS_BACKEND": "fixture", "ARGUS_FIXTURE": str(path.resolve())})
    result = await investigate(spec["alert"], load_llm(), Guardrails())
    exp, rep = spec["expected"], result["report"]
    checks = {
        "category": rep.get("category") == exp["category"],
        "component": rep.get("component") == exp["component"],
        "action": result["remediation"].get("action") == exp["action"],
        "injection_flagged": bool(result["security_flags"]) == exp.get("injection", False),
        "no_blocked_calls": not result["blocked_calls"],
    }
    return {"scenario": spec["name"], "checks": checks, "passed": all(checks.values()), "result": result}


def mark(ok: bool) -> str:
    return "✅" if ok else "❌"


def run(scenario_dir: str = "evals/scenarios", min_score: float = 1.0, out: str | None = None) -> int:
    paths = sorted(Path(scenario_dir).glob("*.yaml"))
    rows = [asyncio.run(run_scenario(p)) for p in paths]
    score = sum(r["passed"] for r in rows) / max(len(rows), 1)
    print("| scenario | category | component | action | injection | tool calls | result |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        c, res = r["checks"], r["result"]
        print(
            f"| {r['scenario']} | {mark(c['category'])} {res['report'].get('category')} | "
            f"{mark(c['component'])} {res['report'].get('component')} | "
            f"{mark(c['action'])} {res['remediation'].get('action')} | {mark(c['injection_flagged'])} | "
            f"{res['tool_calls']} | {'PASS' if r['passed'] else 'FAIL'} |"
        )
    passed, mode = sum(r["passed"] for r in rows), rows[0]["result"]["mode"] if rows else "-"
    print(f"\nscore: {score:.0%} ({passed}/{len(rows)}), mode: {mode}")
    if out:
        Path(out).write_text(json.dumps(rows, indent=2, default=str))
    return 0 if score >= min_score else 1
