"""Argus observability MCP server.

Task-shaped, read-only tools over Prometheus, Loki, Tempo and Kubernetes. Any MCP client can use
it (Claude, Cursor, your own agent), and Argus can use any other MCP server next to it.

Run:  python -m argus.mcp_server            (stdio)
      python -m argus.mcp_server --http     (streamable HTTP on :8765/mcp)
"""

from __future__ import annotations

import functools
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from argus.backends import load_backend

server = MCPServer("argus-observability")
backend = load_backend()
NAMESPACE = os.getenv("ARGUS_NAMESPACE", "shop")
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


# Every tool returns an object: MCP splits a returned list into one content block per item.
def safe(fn):
    """Return backend failures to the agent as data instead of killing the session."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - tool errors are information for the agent
            return {"error": f"{type(exc).__name__}: {exc}"}

    return wrapper


@server.tool(annotations=READ_ONLY)
@safe
def list_firing_alerts() -> dict:
    """List alerts that are currently firing, with their labels."""
    return {"alerts": backend.firing_alerts()}


@server.tool(annotations=READ_ONLY)
@safe
def golden_signals(service: str, namespace: str = NAMESPACE, minutes: int = 30) -> dict:
    """Request rate, error rate, p95 latency and CPU/memory saturation for a service, now vs the
    previous window (baseline). Start most investigations here."""
    return backend.golden_signals(service, namespace, minutes)


@server.tool(annotations=READ_ONLY)
@safe
def service_dependencies(service: str, minutes: int = 30) -> dict:
    """Downstream services this service calls, from the trace service graph."""
    return backend.service_dependencies(service, minutes)


@server.tool(annotations=READ_ONLY)
@safe
def search_logs(service: str, namespace: str = NAMESPACE, minutes: int = 30, level: str = "error", limit: int = 20) -> dict:
    """Search a service's logs. Returns grouped patterns with counts plus a few samples.
    level='error' keeps only error-like lines; level='all' keeps everything."""
    return backend.search_logs(service, namespace, minutes, level, limit)


@server.tool(annotations=READ_ONLY)
@safe
def slow_traces(service: str, minutes: int = 30, min_duration_ms: int = 500, limit: int = 5) -> dict:
    """Slow traces that pass through a service, each with its slowest downstream span."""
    return {"traces": backend.slow_traces(service, minutes, min_duration_ms, limit)}


@server.tool(annotations=READ_ONLY)
@safe
def workload_status(name: str, namespace: str = NAMESPACE) -> dict:
    """Kubernetes Deployment status: replicas, ready pods, restarts, last termination reason,
    image and resource limits."""
    return backend.workload_status(namespace, name)


@server.tool(annotations=READ_ONLY)
@safe
def k8s_events(name: str, namespace: str = NAMESPACE) -> dict:
    """Recent Kubernetes events for a workload and its pods (OOMKilling, BackOff, Unhealthy...)."""
    return {"events": backend.k8s_events(namespace, name)}


@server.tool(annotations=READ_ONLY)
@safe
def rollout_history(name: str, namespace: str = NAMESPACE) -> dict:
    """Deployment revisions with image and age in minutes. Use it to spot a recent bad release."""
    return {"revisions": backend.rollout_history(namespace, name)}


@server.tool(annotations=READ_ONLY)
@safe
def promql(query: str) -> dict:
    """Run a raw PromQL instant query. Prefer the typed tools; use this for anything they miss."""
    return {"result": backend.promql(query)}


def main():
    if "--http" in sys.argv:
        server.run(transport="streamable-http", host="0.0.0.0", port=int(os.getenv("ARGUS_MCP_PORT", "8765")))
    else:
        server.run()


if __name__ == "__main__":
    main()
