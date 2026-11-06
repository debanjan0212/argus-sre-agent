"""Alertmanager webhook receiver. Alerts in, RCA reports (and GitOps PRs) out.

POST /alertmanager   Alertmanager webhook payload
GET  /reports        recent investigations (JSON)
GET  /metrics        Prometheus metrics about Argus itself
GET  /healthz
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
import urllib.request
from collections import Counter, OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from argus.agent import investigate, to_markdown
from argus.guardrails import Guardrails
from argus.llm import load_llm
from argus.remediation import propose_change

DEDUPE_SECONDS = int(os.getenv("ARGUS_DEDUPE_SECONDS", "1800"))
reports: OrderedDict[str, dict] = OrderedDict()
seen: dict[str, float] = {}
metrics = Counter()
duration_sum = 0.0
lock = threading.Lock()


def handle_alert(alert: dict) -> None:
    global duration_sum
    guard = Guardrails(audit_path=os.getenv("ARGUS_AUDIT_LOG"))
    try:
        result = asyncio.run(investigate(alert, load_llm(), guard))
        if repo := os.getenv("ARGUS_GITOPS_DIR"):
            with lock:  # one git operation at a time
                result["change"] = propose_change(result, repo)
        if hook := os.getenv("ARGUS_NOTIFY_WEBHOOK"):  # Slack / Google Chat incoming webhooks
            req = urllib.request.Request(
                hook, data=json.dumps({"text": to_markdown(result)}).encode(), headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=10).close()
        status = "ok"
    except Exception as exc:  # noqa: BLE001 - a failed investigation must not kill the receiver
        result, status = {"incident_id": alert.get("fingerprint", "?"), "error": str(exc)}, "error"
    with lock:
        metrics[f'argus_investigations_total{{status="{status}"}}'] += 1
        if status == "ok":
            metrics[f'argus_remediations_total{{action="{result["remediation"]["action"]}"}}'] += 1
            metrics["argus_tool_calls_total"] += result["tool_calls"]
            duration_sum += result["duration_s"]
        reports[result["incident_id"]] = result
        while len(reports) > 100:
            reports.popitem(last=False)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, '{"ok": true}')
        if self.path == "/reports":
            with lock:
                return self._send(200, json.dumps(list(reports.values())[::-1], default=str))
        if self.path == "/metrics":
            with lock:
                lines = [f"{k} {v}" for k, v in sorted(metrics.items())]
                lines.append(f"argus_investigation_duration_seconds_sum {duration_sum}")
            return self._send(200, "\n".join(lines) + "\n", "text/plain; version=0.0.4")
        self._send(404, '{"error": "not found"}')

    def do_POST(self):
        if self.path != "/alertmanager":
            return self._send(404, '{"error": "not found"}')
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except ValueError:
            return self._send(400, '{"error": "invalid JSON"}')
        now, accepted = time.time(), 0
        for alert in payload.get("alerts", []):
            key = alert.get("fingerprint") or json.dumps(alert.get("labels", {}), sort_keys=True)
            with lock:
                if alert.get("status") != "firing" or now - seen.get(key, 0) < DEDUPE_SECONDS:
                    continue  # Alertmanager re-sends; investigate each incident once per window
                seen[key] = now
            threading.Thread(target=handle_alert, args=(alert,), daemon=True).start()
            accepted += 1
        self._send(202, json.dumps({"accepted": accepted}))

    def log_message(self, fmt, *args):  # quieter access log
        pass


def main():
    repo, token, gitops = os.getenv("ARGUS_GITHUB_REPO"), os.getenv("GITHUB_TOKEN"), os.getenv("ARGUS_GITOPS_DIR")
    if repo and token and gitops and not os.path.isdir(gitops):  # in-cluster: clone the GitOps repo once
        subprocess.run(["git", "clone", f"https://x-access-token:{token}@github.com/{repo}.git", gitops], check=True)
    port = int(os.getenv("ARGUS_PORT", "8080"))
    print(f"argus webhook listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
