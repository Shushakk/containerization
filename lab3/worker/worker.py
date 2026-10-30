import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg.rows import dict_row

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("shop-worker")

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3"))
DB_RETRY_INTERVAL = float(os.getenv("DB_RETRY_INTERVAL", "5"))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, format_string, *args):
        return


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", 8081), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health endpoint listening on :8081")


def connect_db():
    """Открывает подключение к PostgreSQL."""
    return psycopg.connect(
        host=os.getenv("DB_HOST", "shop-db-rw"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "shop"),
        user=os.getenv("DB_USER", "shop"),
        password=os.getenv("DB_PASSWORD", "shop"),
        connect_timeout=3,
        row_factory=dict_row,
    )


def process_one_order():
    """Находит необработанный заказ и помечает его обработанным."""
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                item TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                processed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        order = conn.execute("""
            SELECT id, item, quantity
            FROM orders
            WHERE processed = FALSE
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        """).fetchone()

        if order is None:
            return False

        conn.execute(
            "UPDATE orders SET processed = TRUE WHERE id = %s",
            (order["id"],),
        )

        logger.info(
            "Processed order id=%s item=%s quantity=%s",
            order["id"],
            order["item"],
            order["quantity"],
        )
        return True


def main():
    start_health_server()
    logger.info("Shop worker started")

    while True:
        try:
            processed = process_one_order()
            if not processed:
                time.sleep(POLL_INTERVAL)
        except psycopg.Error:
            logger.exception("Database unavailable; retrying")
            time.sleep(DB_RETRY_INTERVAL)


if __name__ == "__main__":
    main()
