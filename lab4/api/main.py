import logging
import os
import random
import time

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from prometheus_client import (
    Counter,
    Histogram,
    CONTENT_TYPE_LATEST,
    generate_latest,
)
from pydantic import BaseModel, ConfigDict, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shop-api")

app = FastAPI(title="Shop API")

APP_VERSION = os.getenv("APP_VERSION", "v1").lower()
if APP_VERSION not in {"v1", "v2"}:
    raise ValueError("APP_VERSION must be v1 or v2")

FAIL = os.getenv("FAIL", "false").lower() in {"1", "true", "yes"}
FLAKY = os.getenv("FLAKY", "false").lower() in {"1", "true", "yes"}
HEALTH_FAIL = os.getenv("HEALTH_FAIL", "false").lower() in {
    "1", "true", "yes"
}

HTTP_REQUESTS = Counter(
    "shop_http_requests_total",
    "Total HTTP requests",
    ["version", "method", "path", "status"],
)

HTTP_REQUEST_DURATION = Histogram(
    "shop_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["version", "method", "path"],
)


@app.middleware("http")
async def observe_http(request: Request, call_next):
    path = request.url.path

    if path == "/metrics":
        return await call_next(request)

    started = time.perf_counter()
    status_code = "500"

    try:
        response = await call_next(request)
        status_code = str(response.status_code)
        return response
    finally:
        HTTP_REQUESTS.labels(
            APP_VERSION, request.method, path, status_code
        ).inc()
        HTTP_REQUEST_DURATION.labels(
            APP_VERSION, request.method, path
        ).observe(time.perf_counter() - started)


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.get("/version", response_class=PlainTextResponse)
def version():
    return PlainTextResponse(APP_VERSION)


def connect_db():
    return psycopg.connect(
        host=os.getenv("DB_HOST", "shop-db-rw"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "shop"),
        user=os.getenv("DB_USER", "shop"),
        password=os.getenv("DB_PASSWORD", "shop"),
        connect_timeout=3,
        row_factory=dict_row,
    )


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id BIGSERIAL PRIMARY KEY,
            item TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            processed BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


class OrderCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    item: str = Field(min_length=1, max_length=200)
    quantity: int = Field(gt=0, le=10000)


@app.get("/health", response_class=PlainTextResponse)
def health():
    if HEALTH_FAIL:
        return PlainTextResponse("unhealthy", status_code=503)
    return PlainTextResponse("ok")


@app.post("/order")
def create_order(order: OrderCreate):
    # v2 с FAIL=true имитирует полностью неудачный релиз.
    # Ошибка возникает до записи в БД.
    if APP_VERSION == "v2" and FAIL:
        logger.warning("Simulated v2 failure: FAIL=true")
        raise HTTPException(
            status_code=500,
            detail="Simulated v2 failure",
        )

    # v1 с FLAKY=true случайно отклоняет часть запросов.
    # Такие ошибки тоже возникают до обращения к БД.
    if APP_VERSION == "v1" and FLAKY and random.random() < 0.30:
        logger.warning("Simulated transient v1 failure: FLAKY=true")
        raise HTTPException(
            status_code=500,
            detail="Simulated transient failure",
        )

    try:
        with connect_db() as conn:
            ensure_schema(conn)
            result = conn.execute(
                """
                INSERT INTO orders (item, quantity)
                VALUES (%s, %s)
                RETURNING id, item, quantity, processed, created_at
                """,
                (order.item, order.quantity),
            ).fetchone()
            return result
    except psycopg.Error as exc:
        logger.exception("Cannot create order")
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        ) from exc


@app.get("/orders")
def get_orders():
    try:
        with connect_db() as conn:
            ensure_schema(conn)
            result = conn.execute(
                """
                SELECT id, item, quantity, processed, created_at
                FROM orders
                ORDER BY id DESC
                LIMIT 100
                """
            ).fetchall()
            return result
    except psycopg.Error as exc:
        logger.exception("Cannot read orders")
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        ) from exc
