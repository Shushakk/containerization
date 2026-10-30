import csv
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:30080")
COUNT = int(os.getenv("REQUEST_COUNT", "120"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "4"))

REPORTS = Path("reports")
CSV_PATH = REPORTS / "baseline-no-mesh.csv"
JSON_PATH = REPORTS / "baseline-no-mesh-summary.json"


def get(path):
    with urllib.request.urlopen(BASE_URL + path, timeout=5) as response:
        return response.status, response.read().decode("utf-8")


def percentile(values, p):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def submit_order(index):
    payload = json.dumps({
        "item": f"lab6-baseline-{index}",
        "quantity": 1,
    }).encode("utf-8")

    request = urllib.request.Request(
        BASE_URL + "/order",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.perf_counter()
    status = "ERROR"
    error = ""

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = str(response.status)
            response.read()
    except urllib.error.HTTPError as exc:
        status = str(exc.code)
        error = exc.read().decode("utf-8", errors="replace")[:300]
    except Exception as exc:
        error = str(exc)

    elapsed_ms = (time.perf_counter() - started) * 1000

    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "request": index,
        "status": status,
        "latency_ms": round(elapsed_ms, 3),
        "error": error,
    }


def main():
    REPORTS.mkdir(exist_ok=True)

    status, version = get("/version")
    if status != 200 or version.strip() != "v1":
        raise SystemExit(
            f"Expected API v1; received HTTP {status}, version={version!r}"
        )

    status, health = get("/health")
    if status != 200:
        raise SystemExit(f"Health check failed: HTTP {status}")

    print(f"API version: {version.strip()}")
    print(f"Health: HTTP {status}")
    print(f"Requests: {COUNT}; concurrency: {CONCURRENCY}")
    print("Running POST /order load test...")

    rows = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = [
            executor.submit(submit_order, i)
            for i in range(1, COUNT + 1)
        ]
        for future in as_completed(futures):
            rows.append(future.result())

    rows.sort(key=lambda row: row["request"])

    with CSV_PATH.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "timestamp_utc", "request", "status", "latency_ms", "error"
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    latencies = [row["latency_ms"] for row in rows]
    successes = sum(row["status"] == "200" for row in rows)
    errors = len(rows) - successes
    status_counts = dict(Counter(row["status"] for row in rows))

    summary = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "phase": "baseline_without_mesh",
        "base_url": BASE_URL,
        "version": version.strip(),
        "requests": COUNT,
        "concurrency": CONCURRENCY,
        "successful_requests": successes,
        "errors": errors,
        "error_rate_percent": round(errors / len(rows) * 100, 3),
        "status_counts": status_counts,
        "latency_ms": {
            "min": round(min(latencies), 3),
            "mean": round(statistics.mean(latencies), 3),
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(percentile(latencies, 0.95), 3),
            "max": round(max(latencies), 3),
        },
    }

    JSON_PATH.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("\n===== BASELINE RESULTS =====")
    print(f"HTTP status counts: {status_counts}")
    print(f"Successful: {successes}/{COUNT}")
    print(f"Errors: {errors}/{COUNT} ({summary['error_rate_percent']}%)")
    print("Latency (ms):")
    for key, value in summary["latency_ms"].items():
        print(f"  {key}: {value}")
    print(f"\nCSV: {CSV_PATH}")
    print(f"Summary: {JSON_PATH}")


if __name__ == "__main__":
    main()
