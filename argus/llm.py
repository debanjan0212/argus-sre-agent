"""Model providers. Messages use the OpenAI chat format, which nearly every provider and gateway
speaks: OpenAI, Azure OpenAI, Anthropic's OpenAI-compatible endpoint, Ollama, vLLM, LiteLLM,
OpenRouter, Bedrock/Vertex through a gateway. RunbookLLM needs no model at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.request
from collections import Counter
from dataclasses import dataclass, field

from argus.guardrails import to_mi


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Reply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class OpenAICompatLLM:
    name = "openai-compatible"

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model, self.api_key = model, api_key

    def _post(self, body: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)

    async def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        body = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            body["tools"] = tools
        msg = (await asyncio.to_thread(self._post, body))["choices"][0]["message"]
        calls = [
            ToolCall(c["id"], c["function"]["name"], json.loads(c["function"]["arguments"] or "{}"))
            for c in msg.get("tool_calls") or []
        ]
        return Reply(msg.get("content"), calls)


class RunbookLLM:
    """LLM-free mode: a deterministic SRE runbook that speaks the same tool-calling protocol.

    It runs the investigation an on-call engineer would: golden signals, follow the degradation
    down the service graph, then pull workload state, events, releases and logs for the suspect.
    Used for air-gapped clusters, for CI evals, and as the fallback when the model API is down.
    """

    name = "runbook"

    def __init__(self):
        self.ev: dict[tuple[str, str], object] = {}
        self.issued: dict[str, tuple[str, str]] = {}
        self.stage = 0
        self.alert: dict = {}
        self.depth: dict[str, int] = {}
        self.suspect = ""
        self.trace_note = ""

    # protocol --------------------------------------------------------------------------------
    async def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        if not self.alert:
            self.alert = json.loads(messages[1]["content"].split("ALERT:", 1)[1])
        for m in messages:
            if m["role"] == "tool" and m["tool_call_id"] in self.issued:
                self.ev[self.issued.pop(m["tool_call_id"])] = _json(m["content"])
        while self.stage < 4:
            calls = self._plan()
            self.stage += 1
            if calls:
                return Reply(None, calls)
        return Reply(json.dumps(self._diagnose()))

    def _call(self, tool: str, subject: str, **args) -> ToolCall:
        cid = f"rb-{len(self.ev) + len(self.issued)}-{tool}-{subject}"
        self.issued[cid] = (tool, subject)
        return ToolCall(cid, tool, args)

    # investigation plan ---------------------------------------------------------------------
    @property
    def service(self) -> str:
        labels = self.alert.get("labels", {})
        return labels.get("service") or labels.get("deployment") or labels.get("app", "")

    @property
    def ns(self) -> str:
        return self.alert.get("labels", {}).get("namespace", "default")

    def _deps(self, svc: str) -> list[str]:
        return [d for d in self._get("service_dependencies", svc, "calls") if d not in self.depth]

    def _plan(self) -> list[ToolCall]:
        svc, ns = self.service, self.ns
        if self.stage == 0:
            self.depth[svc] = 0
            return [
                self._call("golden_signals", svc, service=svc, namespace=ns),
                self._call("service_dependencies", svc, service=svc),
                self._call("slow_traces", svc, service=svc),
            ]
        if self.stage in (1, 2):  # walk two hops down the service graph
            frontier = [s for s, d in self.depth.items() if d == self.stage - 1]
            calls = []
            for parent in frontier:
                for dep in self._deps(parent):
                    self.depth[dep] = self.stage
                    calls += [
                        self._call("golden_signals", dep, service=dep, namespace=ns),
                        self._call("service_dependencies", dep, service=dep),
                    ]
            return calls
        self.suspect = s = self._pick_suspect()
        return [
            self._call("workload_status", s, name=s, namespace=ns),
            self._call("k8s_events", s, name=s, namespace=ns),
            self._call("rollout_history", s, name=s, namespace=ns),
            self._call("search_logs", s, service=s, namespace=ns),
        ]

    def _get(self, tool: str, svc: str, key: str | None = None):
        """Evidence for (tool, service); {} or [] when missing or when the tool returned an error."""
        val = self.ev.get((tool, svc))
        val = val if isinstance(val, dict) else {}
        return val.get(key) or [] if key else val

    def _gs(self, svc: str) -> dict:
        return self._get("golden_signals", svc)

    def _degraded(self, svc: str) -> bool:
        g = self._gs(svc)
        err, base = g.get("error_rate") or 0, g.get("error_rate_baseline") or 0
        lat, lbase = g.get("p95_latency_ms") or 0, g.get("p95_latency_baseline_ms") or 0
        return err >= max(0.05, 5 * base) or (lbase > 0 and lat >= 2 * lbase) or (g.get("cpu_pct_of_limit") or 0) >= 85

    def _pick_suspect(self) -> str:
        degraded = [s for s in self.depth if self._degraded(s)]
        traces = self._get("slow_traces", self.service, "traces")
        slow = Counter((t.get("slowest_span") or {}).get("service") for t in traces if isinstance(t, dict))
        if slow and (top := slow.most_common(1)[0][0]) in degraded:
            self.trace_note = f"{slow[top]} of {len(traces)} slow traces spend most of their time in {top}"
            return top
        return max(degraded, key=lambda s: self.depth[s]) if degraded else self.service

    # diagnosis rules ------------------------------------------------------------------------
    def _diagnose(self) -> dict:
        s = self.suspect
        g, wl = self._gs(s), self._get("workload_status", s)
        events = self._get("k8s_events", s, "events")
        rollouts = self._get("rollout_history", s, "revisions")
        patterns = [p["pattern"] for p in self._get("search_logs", s, "top_patterns")]
        err, base = g.get("error_rate") or 0, g.get("error_rate_baseline") or 0
        lat, lbase = g.get("p95_latency_ms") or 0, g.get("p95_latency_baseline_ms") or 0
        cpu, mem = g.get("cpu_pct_of_limit") or 0, g.get("memory_pct_of_limit") or 0

        evidence = []
        if g:
            evidence.append(
                f"{s}: error rate {err:.1%} (baseline {base:.1%}), p95 {lat:.0f}ms "
                f"(baseline {lbase:.0f}ms), CPU {cpu:.0f}% / memory {mem:.0f}% of limit"
            )
        if s != self.service:
            evidence.append(f"alert fired on {self.service}, but the degradation traces down to {s}")
        if self.trace_note:
            evidence.append(self.trace_note)
        if patterns:
            evidence.append(f"top error log pattern on {s}: {patterns[0]!r}")

        def report(category, cause, confidence, action, params=None, rationale=""):
            return {
                "summary": f"{category.replace('_', ' ')} on {s}: {cause}",
                "category": category,
                "component": s,
                "namespace": self.ns,
                "root_cause": cause,
                "evidence": evidence,
                "confidence": confidence,
                "remediation": {"action": action, "params": params or {}, "rationale": rationale},
            }

        if not any(self._degraded(x) for x in self.depth):
            return report(
                "self_resolved",
                "signals are back within baseline; the alert condition has cleared",
                0.8,
                "none",
                rationale="No action needed. Keep the alert open for one more window.",
            )

        oom_events = [e for e in events if isinstance(e, dict) and "OOM" in str(e.get("reason", ""))]
        if wl.get("last_termination_reason") == "OOMKilled" or oom_events:
            current = wl.get("limits", {}).get("memory", "")
            evidence.append(f"{s}: {wl.get('restarts', 0)} restarts, last termination OOMKilled, memory limit {current}")
            target = f"{to_mi(current) * 2}Mi" if to_mi(current) else ""
            return report(
                "resource_exhaustion",
                f"containers are OOMKilled at the {current} memory limit",
                0.85,
                "increase_memory_limit",
                {"memory": target},
                "Raise the limit to stop the crash loop now; check for a leak before the next release.",
            )

        fresh = len(rollouts) >= 2 and rollouts[-1].get("age_min") is not None and rollouts[-1]["age_min"] <= 60
        if fresh and (err >= max(0.05, 5 * base) or (lbase and lat >= 2 * lbase)):
            new, old = rollouts[-1], rollouts[-2]
            evidence.append(
                f"{s}: revision {new.get('revision')} ({new.get('image')}) rolled out "
                f"{new.get('age_min')} min ago, right before the degradation"
            )
            return report(
                "bad_deploy",
                f"release {new.get('image')} introduced the regression",
                0.9 if patterns else 0.75,
                "rollback_image",
                {"image": old.get("image")},
                f"Roll back to {old.get('image')}, the last revision with healthy signals.",
            )

        if any(re.search(r"(?i)pool|connection|database|deadlock", p) for p in patterns) and cpu < 70:
            return report(
                "dependency_failure",
                "requests are waiting on an exhausted database connection pool",
                0.75,
                "escalate",
                rationale="No safe automatic fix: scaling out would add more "
                "connections to a saturated database. Page the service owner and check DB load.",
            )

        if cpu >= 85 and lbase and lat >= 2 * lbase:
            replicas = int(wl.get("replicas", 1))
            return report(
                "capacity_saturation",
                f"CPU at {cpu:.0f}% of limit is driving latency up",
                0.8,
                "scale_replicas",
                {"replicas": replicas * 2},
                f"Scale {s} from {replicas} to {replicas * 2} replicas, then review HPA targets.",
            )

        return report(
            "unknown",
            "signals are degraded but no runbook rule matched",
            0.4,
            "escalate",
            rationale="Hand over to a human with the evidence collected so far.",
        )


def _json(text: str):
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    try:
        return json.loads(text[start:]) if start >= 0 else text
    except ValueError:
        return text


def load_llm():
    if os.getenv("ARGUS_LLM", "runbook") == "runbook":
        return RunbookLLM()
    return OpenAICompatLLM(
        os.getenv("ARGUS_LLM_BASE_URL", "https://api.openai.com/v1"),
        os.environ["ARGUS_MODEL"],
        os.getenv("ARGUS_LLM_API_KEY", ""),
    )
