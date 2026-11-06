"""Data backends behind the MCP tools.

HttpBackend talks to open APIs (PromQL, LogQL, TraceQL, kubectl), so it works the same on
EKS, GKE, AKS, OpenShift or kind. It sends no auth headers, so a managed backend
(Amazon Managed Prometheus, Grafana Cloud, ...) needs an auth proxy in front of it.
FixtureBackend replays a recorded incident, which powers the offline demo and the eval suite.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime

import yaml

_ERROR_RE = re.compile(r"(?i)\b(error|exception|fatal|panic|timeout|refused|killed)\b")
_NORMALIZERS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE), "<uuid>"),
    (re.compile(r"\b(0x)?[0-9a-f]{12,}\b", re.IGNORECASE), "<hex>"),
    (re.compile(r"\d+(\.\d+)?"), "<n>"),
]


def _message(line: str) -> str:
    if line.startswith("{"):
        try:
            obj = json.loads(line)
            return f"{obj.get('level', '')} {obj.get('message') or obj.get('msg') or line}".strip()
        except ValueError:
            pass
    return line


def summarize_logs(lines: list[str], level: str, limit: int) -> dict:
    """Group log lines into patterns so the agent sees signal, not 10k raw lines."""
    msgs = [_message(line) for line in lines]
    if level == "error":
        msgs = [m for m in msgs if _ERROR_RE.search(m)]
    patterns = Counter()
    for m in msgs:
        for rx, repl in _NORMALIZERS:
            m = rx.sub(repl, m)
        patterns[m] += 1
    return {
        "total_lines": len(msgs),
        "top_patterns": [{"pattern": p, "count": c} for p, c in patterns.most_common(5)],
        "samples": msgs[: min(limit, 5)],
    }


class FixtureBackend:
    """Serves a recorded incident from a scenario YAML file."""

    def __init__(self, path: str):
        with open(path) as f:
            self.data = yaml.safe_load(f)["data"]

    def _get(self, key: str, name: str, default):
        return self.data.get(key, {}).get(name, default)

    def firing_alerts(self) -> list:
        return self.data.get("alerts", [])

    def golden_signals(self, service, namespace, minutes):
        return self._get("golden_signals", service, {"error": f"no metrics for service {service!r}"})

    def service_dependencies(self, service, minutes):
        return {"service": service, "calls": self._get("dependencies", service, [])}

    def search_logs(self, service, namespace, minutes, level, limit):
        return summarize_logs(self._get("logs", service, []), level, limit)

    def slow_traces(self, service, minutes, min_duration_ms, limit):
        return self._get("traces", service, [])[:limit]

    def workload_status(self, namespace, name):
        return self._get("workloads", name, {"error": f"deployment {name!r} not found in {namespace!r}"})

    def k8s_events(self, namespace, name):
        return self._get("events", name, [])

    def rollout_history(self, namespace, name):
        return self._get("rollouts", name, [])

    def promql(self, query):
        return {"error": "raw PromQL is not available in fixture mode; use the typed tools"}


class HttpBackend:
    """Live backend. Metric names follow OpenTelemetry HTTP semantic conventions."""

    def __init__(self):
        self.prom = os.getenv("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
        self.loki = os.getenv("LOKI_URL", "http://localhost:3100").rstrip("/")
        self.tempo = os.getenv("TEMPO_URL", "http://localhost:3200").rstrip("/")
        self.label = os.getenv("ARGUS_SERVICE_LABEL", "service")
        self.metric = os.getenv("ARGUS_HTTP_METRIC", "http_server_request_duration_seconds")

    # --- helpers -----------------------------------------------------------------------------
    @staticmethod
    def _get_json(url: str, params: dict) -> dict:
        req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)

    def _prom(self, query: str) -> list:
        return self._get_json(f"{self.prom}/api/v1/query", {"query": query})["data"]["result"]

    def _scalar(self, query: str):
        result = self._prom(query)
        if not result:
            return None
        value = float(result[0]["value"][1])
        return None if math.isnan(value) or math.isinf(value) else round(value, 4)

    @staticmethod
    def _kubectl(*args: str) -> dict:
        out = subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip())
        return json.loads(out.stdout)

    @staticmethod
    def _age_min(ts: str | None):
        if not ts:
            return None
        then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return round((datetime.now(UTC) - then).total_seconds() / 60)

    # --- tools -------------------------------------------------------------------------------
    def firing_alerts(self) -> list:
        alerts = self._get_json(f"{self.prom}/api/v1/alerts", {})["data"]["alerts"]
        return [{"labels": a["labels"], "state": a["state"], "activeAt": a.get("activeAt")} for a in alerts]

    def golden_signals(self, service, namespace, minutes):
        sel = f'{self.label}="{service}"'
        count, bucket, w = f"{self.metric}_count", f"{self.metric}_bucket", f"[{min(minutes, 5)}m]"
        rate = f"sum(rate({count}{{{sel}}}{w}))"
        errors = f'sum(rate({count}{{{sel},http_response_status_code=~"5.."}}{w}))'
        p95 = f"histogram_quantile(0.95, sum by (le) (rate({bucket}{{{sel}}}{w}))) * 1000"
        pods = f'namespace="{namespace}",pod=~"{service}-.*"'
        cpu = (
            f'sum(rate(container_cpu_usage_seconds_total{{{pods},container!=""}}{w})) / '
            f'sum(kube_pod_container_resource_limits{{{pods},resource="cpu"}}) * 100'
        )
        mem = (
            f'sum(container_memory_working_set_bytes{{{pods},container!=""}}) / '
            f'sum(kube_pod_container_resource_limits{{{pods},resource="memory"}}) * 100'
        )

        def baseline(q: str) -> str:  # same query, one incident-window earlier
            return q.replace(f"{w})", f"{w} offset {minutes}m)")

        err = f"({errors} / {rate}) or vector(0)"
        return {
            "service": service,
            "request_rate_rps": self._scalar(rate),
            "error_rate": self._scalar(err),
            "error_rate_baseline": self._scalar(baseline(err)),
            "p95_latency_ms": self._scalar(p95),
            "p95_latency_baseline_ms": self._scalar(baseline(p95)),
            "cpu_pct_of_limit": self._scalar(cpu),
            "memory_pct_of_limit": self._scalar(mem),
        }

    def service_dependencies(self, service, minutes):
        # Needs Tempo's metrics-generator (service graphs) writing to Prometheus.
        q = f'sum by (server) (rate(traces_service_graph_request_total{{client="{service}"}}[{minutes}m]))'
        return {"service": service, "calls": [r["metric"]["server"] for r in self._prom(q)]}

    def search_logs(self, service, namespace, minutes, level, limit):
        end = time.time_ns()
        data = self._get_json(
            f"{self.loki}/loki/api/v1/query_range",
            {
                "query": f'{{service_name="{service}"}}',
                "start": end - minutes * 60 * 10**9,
                "end": end,
                "limit": 1000,
                "direction": "backward",
            },
        )
        lines = [v[1] for stream in data["data"]["result"] for v in stream["values"]]
        return summarize_logs(lines, level, limit)

    def slow_traces(self, service, minutes, min_duration_ms, limit):
        end = int(time.time())
        found = self._get_json(
            f"{self.tempo}/api/search",
            {
                "q": f'{{ resource.service.name = "{service}" && duration > {min_duration_ms}ms }}',
                "limit": limit,
                "start": end - minutes * 60,
                "end": end,
            },
        ).get("traces", [])
        return [self._trace_summary(t["traceID"], t.get("durationMs")) for t in found]

    def _trace_summary(self, trace_id: str, duration_ms):
        trace = self._get_json(f"{self.tempo}/api/traces/{trace_id}", {})
        spans = []
        for batch in trace.get("batches") or trace.get("resourceSpans") or []:
            attrs = {a["key"]: a["value"].get("stringValue") for a in batch["resource"].get("attributes", [])}
            for scope in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
                for s in scope["spans"]:
                    ms = (int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"])) / 1e6
                    spans.append({"service": attrs.get("service.name"), "name": s["name"], "duration_ms": round(ms, 1)})
        root = spans[0]["service"] if spans else None
        downstream = [s for s in spans if s["service"] != root] or spans
        slowest = max(downstream, key=lambda s: s["duration_ms"], default=None)
        return {"trace_id": trace_id, "duration_ms": duration_ms, "slowest_span": slowest}

    def workload_status(self, namespace, name):
        dep = self._kubectl("get", "deployment", name, "-n", namespace)
        container = dep["spec"]["template"]["spec"]["containers"][0]
        selector = ",".join(f"{k}={v}" for k, v in dep["spec"]["selector"]["matchLabels"].items())
        restarts, reasons = 0, Counter()
        for pod in self._kubectl("get", "pods", "-n", namespace, "-l", selector)["items"]:
            for cs in pod["status"].get("containerStatuses", []):
                restarts += cs.get("restartCount", 0)
                if term := cs.get("lastState", {}).get("terminated"):
                    reasons[term.get("reason")] += 1
                if wait := cs.get("state", {}).get("waiting"):
                    reasons[wait.get("reason")] += 1
        return {
            "name": name,
            "replicas": dep["spec"].get("replicas", 1),
            "ready": dep["status"].get("readyReplicas", 0),
            "restarts": restarts,
            "last_termination_reason": reasons.most_common(1)[0][0] if reasons else None,
            "image": container["image"],
            "limits": container.get("resources", {}).get("limits", {}),
        }

    def k8s_events(self, namespace, name):
        items = self._kubectl("get", "events", "-n", namespace)["items"]
        mine = [e for e in items if e["involvedObject"]["name"].startswith(name)]
        mine.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
        return [
            {
                "type": e["type"],
                "reason": e["reason"],
                "message": e.get("message", ""),
                "count": e.get("count", 1),
                "age_min": self._age_min(e.get("lastTimestamp") or e.get("eventTime")),
            }
            for e in mine[-20:]
        ]

    def rollout_history(self, namespace, name):
        dep = self._kubectl("get", "deployment", name, "-n", namespace)
        selector = ",".join(f"{k}={v}" for k, v in dep["spec"]["selector"]["matchLabels"].items())
        history = []
        for rs in self._kubectl("get", "replicasets", "-n", namespace, "-l", selector)["items"]:
            history.append(
                {
                    "revision": int(rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", 0)),
                    "image": rs["spec"]["template"]["spec"]["containers"][0]["image"],
                    "age_min": self._age_min(rs["metadata"]["creationTimestamp"]),
                }
            )
        return sorted(history, key=lambda h: h["revision"])

    def promql(self, query):
        return self._prom(query)[:20]


def load_backend():
    if os.getenv("ARGUS_BACKEND", "http") == "fixture":
        return FixtureBackend(os.environ["ARGUS_FIXTURE"])
    return HttpBackend()
