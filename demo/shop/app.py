"""Tiny demo microservice. One image plays frontend, checkout or payments (SERVICE_NAME), calls the
next hop (DOWNSTREAM_URL), and can be broken on purpose with FAULT_* env vars.

Emits OTel-semconv HTTP metrics (Prometheus), OTLP traces (Tempo) and JSON logs (stdout -> Loki).
"""

import json
import os
import random
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Histogram, generate_latest

SERVICE = os.getenv("SERVICE_NAME", "frontend")
DOWNSTREAM = os.getenv("DOWNSTREAM_URL", "")
RELEASE_BUG = os.getenv("RELEASE_BUG") == "1"  # baked into the "1.4.0" image to simulate a bad release

provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(SERVICE)

LATENCY = Histogram(
    "http_server_request_duration_seconds",
    "HTTP server request duration",
    ["service", "http_route", "http_response_status_code"],
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
leak = []


def fault(name: str) -> float:
    return float(os.getenv(name, "0"))


def log(level: str, message: str, **extra) -> None:
    span = trace.get_current_span().get_span_context()
    print(
        json.dumps({"level": level, "service": SERVICE, "message": message, "trace_id": format(span.trace_id, "032x"), **extra}),
        file=sys.stdout,
        flush=True,
    )


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/metrics":
            return self._reply(200, generate_latest(), CONTENT_TYPE_LATEST)
        if self.path == "/healthz":
            return self._reply(200, b'{"ok":true}')
        start, code = time.perf_counter(), 200
        with tracer.start_as_current_span(
            f"GET {self.path}", context=propagate.extract(dict(self.headers)), kind=trace.SpanKind.SERVER
        ):
            try:
                code = self.handle_request()
            except Exception as exc:  # noqa: BLE001
                code = 500
                log("error", f"{type(exc).__name__}: {exc} in GET {self.path}")
            self._reply(code, json.dumps({"service": SERVICE, "status": code}).encode())
        LATENCY.labels(SERVICE, self.path, str(code)).observe(time.perf_counter() - start)

    def handle_request(self) -> int:
        if kb := int(fault("FAULT_LEAK_KB")):
            leak.append(bytearray(kb * 1024))
        if ms := fault("FAULT_CPU_MS"):
            end = time.perf_counter() + ms / 1000
            while time.perf_counter() < end:
                pass
        if ms := fault("FAULT_LATENCY_MS"):
            time.sleep(ms / 1000)
        if RELEASE_BUG and random.random() < 0.3:
            raise KeyError("currency")
        if random.random() < fault("FAULT_ERROR_RATE"):
            log("error", "injected failure")
            return 500
        if DOWNSTREAM:
            headers = {}
            with tracer.start_as_current_span(f"call {DOWNSTREAM}", kind=trace.SpanKind.CLIENT):
                propagate.inject(headers)
                try:
                    urllib.request.urlopen(urllib.request.Request(DOWNSTREAM, headers=headers), timeout=10).close()
                except Exception as exc:  # noqa: BLE001
                    log("error", f"downstream call failed: {exc}", downstream=DOWNSTREAM)
                    return 502
        return 200

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    log("info", f"{SERVICE} listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
