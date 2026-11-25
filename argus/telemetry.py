"""OpenTelemetry for Argus itself, in the GenAI conventions, so the agent that investigates alerts can be watched
like any other agent (by Themis, Tempo, Jaeger or anything that reads OTLP).

Off until OTEL_EXPORTER_OTLP_ENDPOINT is set. The standard OTEL_* variables do the rest:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://themis:8600
    OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<ingest key>
    OTEL_SERVICE_NAME=argus               (the default)

One trace per investigation:

    invoke_agent argus                session.id = the incident id
      chat <model>                    each model turn, with token counts; the runbook is not a model
      execute_tool <tool>             each MCP tool call; a call the guardrails refused is marked denied

Prompts, tool arguments and tool output are never recorded: they can hold what an attacker wrote into a log line.
"""

from __future__ import annotations

import atexit
import os

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

TRACER = trace.get_tracer("argus")


def setup() -> bool:
    """Send spans over OTLP/HTTP when OTEL_EXPORTER_OTLP_ENDPOINT is set. Safe to call more than once."""
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") and not os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        return False
    if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        return True  # someone (a test, an auto-instrumentation agent) set one up already: use theirs
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "argus")}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)  # `argus investigate` ends in seconds: send what is queued before it exits
    return True


def investigation(incident_id: str, alertname: str):
    attributes = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": "argus",
        "session.id": incident_id,
        "themis.journey.name": "investigate " + alertname,
    }
    return TRACER.start_as_current_span("invoke_agent argus", attributes=attributes)


def model_call(provider: str, model: str):
    attributes = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": provider, "gen_ai.request.model": model}
    return TRACER.start_as_current_span("chat " + model, attributes=attributes)


def tool_call(name: str):
    attributes = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "gen_ai.tool.type": "function"}
    return TRACER.start_as_current_span("execute_tool " + name, attributes=attributes)


def record_usage(span, usage: dict) -> None:
    """OpenAI-style usage: prompt_tokens and completion_tokens."""
    if "prompt_tokens" in usage:
        span.set_attribute("gen_ai.usage.input_tokens", int(usage["prompt_tokens"]))
    if "completion_tokens" in usage:
        span.set_attribute("gen_ai.usage.output_tokens", int(usage["completion_tokens"]))


def failed(span, err: BaseException | str, error_type: str = "") -> None:
    span.set_status(Status(StatusCode.ERROR, str(err)[:200]))
    span.set_attribute("error.type", error_type or type(err).__name__)
