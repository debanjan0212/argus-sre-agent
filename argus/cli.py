"""argus investigate | eval | serve | mcp"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import yaml


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="argus", description="AI SRE agent over MCP")
    sub = p.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("investigate", help="investigate one alert")
    src = inv.add_mutually_exclusive_group(required=True)
    src.add_argument("--scenario", help="replay a recorded incident (offline, no cluster needed)")
    src.add_argument("--alert", help="alert JSON file (Alertmanager alert object), live backends")
    inv.add_argument("--gitops", help="GitOps repo dir: commit the fix on a branch / open a PR")
    inv.add_argument("--json", action="store_true", help="print the full result as JSON")
    ev = sub.add_parser("eval", help="run the eval suite")
    ev.add_argument("--dir", default="evals/scenarios")
    ev.add_argument("--min-score", type=float, default=1.0)
    ev.add_argument("--out")
    sub.add_parser("serve", help="run the Alertmanager webhook receiver")
    sub.add_parser("mcp", help="run the bundled observability MCP server (stdio)")
    args = p.parse_args(argv)

    if args.cmd == "eval":
        from argus.evals import run

        return run(args.dir, args.min_score, args.out)
    if args.cmd == "serve":
        from argus.webhook import main as serve

        serve()
        return 0
    if args.cmd == "mcp":
        from argus.mcp_server import main as mcp_main

        mcp_main()
        return 0

    from argus.agent import investigate, to_markdown
    from argus.guardrails import Guardrails
    from argus.llm import load_llm
    from argus.remediation import propose_change

    if args.scenario:
        os.environ.update({"ARGUS_BACKEND": "fixture", "ARGUS_FIXTURE": str(Path(args.scenario).resolve())})
        alert = yaml.safe_load(Path(args.scenario).read_text())["alert"]
    else:
        alert = json.loads(Path(args.alert).read_text())
    result = asyncio.run(investigate(alert, load_llm(), Guardrails(audit_path=os.getenv("ARGUS_AUDIT_LOG"))))
    if args.gitops:
        result["change"] = propose_change(result, args.gitops)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(to_markdown(result))
        if change := result.get("change"):
            print(f"\nGitOps: {change['status']} {change.get('branch', '')} {change.get('url', change.get('note', ''))}")
            if change.get("diff"):
                print(change["diff"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
