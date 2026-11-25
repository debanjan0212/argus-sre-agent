"""What Argus tells an OpenTelemetry backend (Themis, Tempo, Jaeger) about its own investigations."""

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from argus.agent import incident_id, investigate
from argus.guardrails import Guardrails
from argus.llm import OpenAICompatLLM, Reply, RunbookLLM, ToolCall

ROOT = Path(__file__).resolve().parent.parent
SCENARIO = ROOT / "evals/scenarios/02-oom-kill.yaml"
SPANS = InMemorySpanExporter()
if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(SPANS))
    trace.set_tracer_provider(provider)


@pytest.fixture(autouse=True)
def scenario(monkeypatch):
    SPANS.clear()
    monkeypatch.setenv("ARGUS_BACKEND", "fixture")
    monkeypatch.setenv("ARGUS_FIXTURE", str(SCENARIO))
    return yaml.safe_load(SCENARIO.read_text())["alert"]


def by_kind(kind):
    return [s for s in SPANS.get_finished_spans() if s.attributes.get("gen_ai.operation.name") == kind]


def test_one_investigation_is_one_trace_with_every_tool_call(scenario):
    result = asyncio.run(investigate(scenario, RunbookLLM(), Guardrails()))
    (root,) = by_kind("invoke_agent")
    assert root.parent is None and root.attributes["gen_ai.agent.name"] == "argus"
    assert root.attributes["session.id"] == incident_id(scenario) == result["incident_id"]
    tools = by_kind("execute_tool")
    assert len(tools) == result["tool_calls"] > 0
    assert all(t.parent.span_id == root.context.span_id and t.status.is_ok for t in tools)
    assert by_kind("chat") == []  # the runbook is rules, not a model


def test_a_refused_tool_call_is_marked_denied(scenario):
    class Rogue:
        name, turn = "rogue", 0

        async def complete(self, messages, tools):
            self.turn += 1
            if self.turn == 1:
                return Reply(None, [ToolCall("1", "delete_namespace", {"namespace": "shop"})])
            return Reply(json.dumps({"category": "unknown", "component": "", "remediation": {"action": "none", "params": {}}}))

    asyncio.run(investigate(scenario, Rogue(), Guardrails()))
    (denied,) = [t for t in by_kind("execute_tool") if t.attributes["gen_ai.tool.name"] == "delete_namespace"]
    assert denied.attributes["themis.gate.decision"] == "deny" and not denied.status.is_ok


def test_model_turns_carry_tokens_and_an_outage_is_an_error(scenario):
    class Model:
        name, model, turn = "openai-compatible", "qwen3", 0

        async def complete(self, messages, tools):
            self.turn += 1
            if self.turn == 1:
                return Reply(
                    None,
                    [ToolCall("1", "golden_signals", {"service": "checkout"})],
                    {"prompt_tokens": 900, "completion_tokens": 40},
                )
            raise ConnectionError("model API down")

    result = asyncio.run(investigate(scenario, Model(), Guardrails()))
    ok, down = by_kind("chat")
    assert ok.name == "chat qwen3" and ok.attributes["gen_ai.usage.input_tokens"] == 900 and ok.status.is_ok
    assert not down.status.is_ok and down.attributes["error.type"] == "ConnectionError"
    assert result["mode"].startswith("runbook-fallback")  # and the investigation still finished


def test_the_openai_compatible_client_reads_usage():
    llm = OpenAICompatLLM("http://model.invalid/v1", "m")
    answer = {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    llm._post = lambda body: answer
    assert asyncio.run(llm.complete([], [])).usage == {"prompt_tokens": 7, "completion_tokens": 3}
