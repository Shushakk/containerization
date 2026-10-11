import asyncio
import json
import os
import random
import time
from typing import Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import Counter, Histogram, make_asgi_app


PORT = int(os.getenv("PORT", "8000"))
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "api")
OTEL_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://jaeger:4317",
)

# OpenTelemetry

resource = Resource.create(
    {
        "service.name": SERVICE_NAME,
    }
)

tracer_provider = TracerProvider(resource=resource)

otlp_exporter = OTLPSpanExporter(
    endpoint=OTEL_ENDPOINT,
    insecure=True,
)

tracer_provider.add_span_processor(
    BatchSpanProcessor(otlp_exporter)
)

trace.set_tracer_provider(tracer_provider)

tracer = trace.get_tracer(__name__)


# Prometheus metrics

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total number of HTTP requests",
    ["method", "path", "status"],
)

ERROR_COUNT = Counter(
    "http_errors_total",
    "Total number of HTTP 5xx responses",
    ["method", "path", "status"],
)

REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
)


# FastAPI

app = FastAPI(title="Monitoring Lab API")


def get_trace_id() -> str:
    """Return trace_id of the currently active OpenTelemetry span."""
    span = trace.get_current_span()
    context = span.get_span_context()

    if context.is_valid:
        return format(context.trace_id, "032x")

    return "0" * 32


def write_log(
    level: str,
    message: str,
    **extra: object,
) -> None:
    """Write one structured JSON log line to stdout."""

    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "service": SERVICE_NAME,
        "trace_id": get_trace_id(),
        "message": message,
        **extra,
    }

    print(
        json.dumps(record, ensure_ascii=False),
        flush=True,
    )


@app.middleware("http")
async def monitoring_middleware(
    request: Request,
    call_next: Callable,
):
    start = time.perf_counter()

    try:
        response = await call_next(request)

        status = response.status_code
        path = request.url.path
        method = request.method

        duration = time.perf_counter() - start

        REQUEST_COUNT.labels(
            method=method,
            path=path,
            status=str(status),
        ).inc()

        REQUEST_DURATION.labels(
            method=method,
            path=path,
        ).observe(duration)

        if status >= 500:
            ERROR_COUNT.labels(
                method=method,
                path=path,
                status=str(status),
            ).inc()

        write_log(
            "ERROR" if status >= 500 else "INFO",
            "HTTP request completed",
            method=method,
            path=path,
            status=status,
            duration_ms=round(duration * 1000, 2),
        )

        return response

    except Exception as exc:
        duration = time.perf_counter() - start

        REQUEST_COUNT.labels(
            method=request.method,
            path=request.url.path,
            status="500",
        ).inc()

        REQUEST_DURATION.labels(
            method=request.method,
            path=request.url.path,
        ).observe(duration)

        ERROR_COUNT.labels(
            method=request.method,
            path=request.url.path,
            status="500",
        ).inc()

        write_log(
            "ERROR",
            "Unhandled exception",
            method=request.method,
            path=request.url.path,
            status=500,
            duration_ms=round(duration * 1000, 2),
            error=str(exc),
        )

        raise


@app.get("/health")
async def health():
    return PlainTextResponse("ok")


@app.get("/fail")
async def fail():
    # The root HTTP span was created automatically
    # by OpenTelemetry FastAPI instrumentation.
    span = trace.get_current_span()

    span.set_status(
        Status(
            StatusCode.ERROR,
            "intentional failure",
        )
    )

    write_log(
        "ERROR",
        "Intentional 5xx error",
    )

    return JSONResponse(
        status_code=500,
        content={
            "error": "intentional failure",
        },
    )


@app.get("/slow")
async def slow():
    span = trace.get_current_span()

    # Nested span: this is what we want to see
    # as a separate block in the Jaeger waterfall.
    with tracer.start_as_current_span("slow-op"):
        delay = random.uniform(1.0, 3.0)

        await asyncio.sleep(delay)

        write_log(
            "INFO",
            "Slow operation completed",
            delay_seconds=round(delay, 2),
        )

    return {
        "status": "ok",
        "delay": round(delay, 2),
    }


@app.get("/load")
async def load():
    """
    Generate a burst of requests to the same service.
    These requests will increase RPS and create more traces/logs.
    """

    url = f"http://127.0.0.1:{PORT}/health"

    async with httpx.AsyncClient(timeout=10.0) as client:
        tasks = [
            client.get(url)
            for _ in range(20)
        ]

        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

    successful = sum(
        isinstance(result, httpx.Response)
        and result.status_code == 200
        for result in results
    )

    write_log(
        "INFO",
        "Load burst generated",
        total_requests=len(results),
        successful_requests=successful,
    )

    return {
        "generated": len(results),
        "successful": successful,
    }


# Prometheus /metrics

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

# OpenTelemetry FastAPI instrumentation

FastAPIInstrumentor.instrument_app(app)