"""Prometheus metrics, request tracing middleware and optional OpenTelemetry wiring."""
from __future__ import annotations

import logging
import os
import time
import uuid

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from backend.core.logging import request_id_var

log = logging.getLogger("maniac.http.access")

HTTP_REQUESTS = Counter("maniac_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "maniac_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1, 2.5, 5),
)
JOBS_TOTAL = Counter("maniac_jobs_total", "Background jobs processed", ["name", "status"])
JOB_DURATION = Histogram("maniac_job_duration_seconds", "Background job duration", ["name"])
QUEUE_DEPTH = Gauge("maniac_job_queue_depth", "Jobs waiting in the persistent queue")
ACTIVE_DOWNLOADS = Gauge("maniac_active_downloads", "Downloads currently in progress")
IMPORTS_TOTAL = Counter("maniac_imports_total", "Import attempts", ["media_type", "result"])
IMPORT_FAILURES_TOTAL = Counter("maniac_import_failures_total", "Import failures by diagnosed reason", ["reason"])
INDEXER_REQUESTS = Counter("maniac_indexer_requests_total", "Indexer requests", ["indexer", "result"])
LIBRARY_ISSUES_OPEN = Gauge("maniac_library_issues_open", "Open library issues", ["type", "severity"])
WS_CLIENTS = Gauge("maniac_websocket_clients", "Connected WebSocket clients")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - start) * 1000:.1f}"
            return response
        finally:
            elapsed = time.perf_counter() - start
            route = request.scope.get("route")
            route_name = getattr(route, "path", None) or ("static" if not request.url.path.startswith("/api") else "unmatched")
            HTTP_REQUESTS.labels(request.method, route_name, str(status)).inc()
            HTTP_LATENCY.labels(request.method, route_name).observe(elapsed)
            if request.url.path.startswith("/api"):
                log.info(
                    "%s %s %s %.1fms",
                    request.method,
                    request.url.path,
                    status,
                    elapsed * 1000,
                    extra={"status": status, "duration_ms": round(elapsed * 1000, 1)},
                )
            request_id_var.reset(token)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def setup_opentelemetry(app) -> bool:
    """Enable OTel tracing when the SDK is installed and an OTLP endpoint is configured."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logging.getLogger("maniac").warning("OTEL endpoint configured but opentelemetry packages are not installed")
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": "maniac-media"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    return True
