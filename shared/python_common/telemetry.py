"""OpenTelemetry bootstrap shared across all RAG microservices."""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import baggage, metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator

logger = logging.getLogger(__name__)

_initialized = False


def setup_telemetry(service_name: str, app: Any | None = None) -> None:
    """Initialize tracing and metrics exporters. Safe to call once per process."""
    global _initialized
    if _initialized:
        if app is not None:
            FastAPIInstrumentor.instrument_app(app)
        return

    endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://otel-collector.observability.svc.cluster.local:4317",
    )
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": os.getenv("POD_NAMESPACE", "rag-system"),
            "deployment.environment": os.getenv("ENVIRONMENT", "kind"),
        }
    )

    set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )

    tracer_provider = TracerProvider(resource=resource)
    try:
        span_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to configure OTLP span exporter: %s", exc)
        trace.set_tracer_provider(tracer_provider)

    try:
        metric_exporter = OTLPMetricExporter(endpoint=endpoint, insecure=True)
        reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=15000)
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to configure OTLP metric exporter: %s", exc)

    if not _initialized:
        HTTPXClientInstrumentor().instrument()

    if app is not None:
        FastAPIInstrumentor.instrument_app(app)

    _initialized = True
    logger.info("Telemetry initialized for %s -> %s", service_name, endpoint)


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def get_meter(name: str) -> metrics.Meter:
    return metrics.get_meter(name)


def set_span_attributes(attrs: dict[str, Any]) -> None:
    """Attach RAG request parameters to the current span."""
    span = trace.get_current_span()
    if not span or not span.is_recording():
        return
    for key, value in attrs.items():
        if value is None:
            continue
        if isinstance(value, (str, bool, int, float)):
            span.set_attribute(key, value)
        else:
            span.set_attribute(key, str(value))


def set_request_baggage(
    request_id: str,
    user_id: str | None = None,
    session_id: str | None = None,
):
    """Attach cross-service baggage; returns a context token to detach later."""
    from opentelemetry import context

    ctx = baggage.set_baggage("request_id", request_id)
    if user_id:
        ctx = baggage.set_baggage("user_id", user_id, context=ctx)
    if session_id:
        ctx = baggage.set_baggage("session_id", session_id, context=ctx)
    return context.attach(ctx)


def detach_baggage(token) -> None:
    from opentelemetry import context

    context.detach(token)


def get_request_id_from_baggage() -> str | None:
    return baggage.get_baggage("request_id")


def configure_logging(service_name: str) -> None:
    """JSON-ish structured logging with trace correlation fields."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    class TraceFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            span = trace.get_current_span()
            ctx = span.get_span_context() if span else None
            if ctx and ctx.is_valid:
                record.trace_id = format(ctx.trace_id, "032x")
                record.span_id = format(ctx.span_id, "016x")
            else:
                record.trace_id = "-"
                record.span_id = "-"
            record.service = service_name
            record.request_id = get_request_id_from_baggage() or "-"
            return super().format(record)

    handler = logging.StreamHandler()
    handler.setFormatter(
        TraceFormatter(
            '{"ts":"%(asctime)s","level":"%(levelname)s","service":"%(service)s",'
            '"trace_id":"%(trace_id)s","span_id":"%(span_id)s",'
            '"request_id":"%(request_id)s","msg":"%(message)s"}'
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
