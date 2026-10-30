import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPORTS = Path("reports")
OUT = REPORTS / "mesh-with-linkerd-resources.json"
BASELINE = REPORTS / "baseline-no-mesh-resources.json"
BENCHMARK = REPORTS / "mesh-with-linkerd-summary.json"

PORTS = (19419, 19420, 19421)
CGROUP_COMMAND = (
    "cat /sys/fs/cgroup/cpu.stat; "
    "printf 'MEMORY_CURRENT '; cat /sys/fs/cgroup/memory.current; "
    "printf 'MEMORY_PEAK '; cat /sys/fs/cgroup/memory.peak"
)


def run(cmd):
    result = subprocess.run(
        cmd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def get_pods():
    data = json.loads(run([
        "kubectl", "get", "pods", "-n", "shop",
        "-l", "app.kubernetes.io/name=api", "-o", "json"
    ]))

    pods = []
    for item in data["items"]:
        name = item["metadata"]["name"]
        status = item.get("status", {})

        if status.get("phase") != "Running":
            raise RuntimeError(f"{name} is not Running")

        app_ready = any(
            c["name"] == "api" and c.get("ready", False)
            for c in status.get("containerStatuses", [])
        )
        proxy_ready = any(
            c["name"] == "linkerd-proxy"
            and c.get("ready", False)
            and c.get("state", {}).get("running") is not None
            for c in status.get("initContainerStatuses", [])
        )

        if not app_ready or not proxy_ready:
            raise RuntimeError(
                f"{name}: API or Linkerd proxy is not ready"
            )
        pods.append(name)

    if len(pods) != 3:
        raise RuntimeError(f"Expected 3 API pods; found {len(pods)}")

    return sorted(pods)


def api_cgroup_snapshot(pod):
    output = run([
        "kubectl", "exec", "-n", "shop", pod,
        "-c", "api", "--", "sh", "-c", CGROUP_COMMAND
    ])

    stats = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                stats[parts[0]] = int(parts[1])
            except ValueError:
                pass

    required = ("usage_usec", "MEMORY_CURRENT", "MEMORY_PEAK")
    missing = [key for key in required if key not in stats]
    if missing:
        raise RuntimeError(f"{pod}: missing cgroup values {missing}")

    return {
        "cpu_usage_usec": stats["usage_usec"],
        "memory_current_bytes": stats["MEMORY_CURRENT"],
        "memory_peak_bytes": stats["MEMORY_PEAK"],
    }


def fetch_proxy_metrics(port, process):
    deadline = time.monotonic() + 20
    last_error = None

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"kubectl port-forward exited: {process.log_path.read_text()}"
            )

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/metrics", timeout=2
            ) as response:
                body = response.read().decode("utf-8")

            values = {}
            wanted = {
                "process_cpu_seconds_total",
                "process_resident_memory_bytes",
                "process_start_time_seconds",
            }

            for line in body.splitlines():
                for metric in wanted:
                    if line.startswith(metric + " ") or line.startswith(
                        metric + "{"
                    ):
                        try:
                            values[metric] = float(line.rsplit(None, 1)[1])
                        except (ValueError, IndexError):
                            pass

            if wanted.issubset(values):
                return values

            last_error = "Required metric missing from /metrics"
        except Exception as exc:
            last_error = str(exc)

        time.sleep(0.5)

    raise RuntimeError(f"Proxy metrics on port {port} unavailable: {last_error}")


def take_snapshot(pods, ports, processes):
    result = {}
    for pod in pods:
        result[pod] = {
            "api": api_cgroup_snapshot(pod),
            "proxy": fetch_proxy_metrics(ports[pod], processes[pod]),
        }
    return result


def mib(bytes_value):
    return bytes_value / 1048576


def main():
    REPORTS.mkdir(exist_ok=True)

    if not BASELINE.exists():
        raise RuntimeError(f"Baseline not found: {BASELINE}")

    pods = get_pods()
    ports = dict(zip(pods, PORTS))
    processes = {}
    log_handles = {}

    print("API pods:")
    for pod in pods:
        print(f"  {pod} -> localhost:{ports[pod]} -> proxy:4191")

    try:
        print("\nStarting port-forward sessions...")
        for pod, port in ports.items():
            log_path = Path(f"/tmp/lab6-port-forward-{port}.log")
            handle = log_path.open("w")
            log_handles[pod] = handle
            process = subprocess.Popen(
                [
                    "kubectl", "port-forward", "--address", "127.0.0.1",
                    "-n", "shop", f"pod/{pod}", f"{port}:4191"
                ],
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            process.log_path = log_path
            processes[pod] = process

        print("Waiting for proxy metrics and taking pre-load snapshot...")
        before = take_snapshot(pods, ports, processes)

        print("\nRunning the same 120-request benchmark...")
        started = time.monotonic()
        subprocess.run(
            [sys.executable, "scripts/bench-order-mesh.py"],
            check=True,
        )
        elapsed = time.monotonic() - started

        benchmark = json.loads(BENCHMARK.read_text(encoding="utf-8"))
        if benchmark.get("successful_requests") != 120:
            raise RuntimeError(
                "Benchmark did not produce 120 successful requests. "
                "Inspect reports/mesh-with-linkerd-summary.json"
            )

        print("\nTaking post-load snapshot...")
        after = take_snapshot(pods, ports, processes)

        per_pod = {}
        api_cpu_total = 0.0
        proxy_cpu_total = 0.0
        api_mem_before_total = 0.0
        api_mem_after_total = 0.0
        proxy_rss_before_total = 0.0
        proxy_rss_after_total = 0.0

        for pod in pods:
            a0, a1 = before[pod]["api"], after[pod]["api"]
            p0, p1 = before[pod]["proxy"], after[pod]["proxy"]

            if p0["process_start_time_seconds"] != p1[
                "process_start_time_seconds"
            ]:
                raise RuntimeError(
                    f"{pod}: Linkerd proxy restarted during the test"
                )

            api_cpu = (a1["cpu_usage_usec"] - a0["cpu_usage_usec"]) / 1e6
            proxy_cpu = (
                p1["process_cpu_seconds_total"]
                - p0["process_cpu_seconds_total"]
            )

            if api_cpu < 0 or proxy_cpu < 0:
                raise RuntimeError(f"{pod}: a CPU counter decreased")

            api_mem_before = mib(a0["memory_current_bytes"])
            api_mem_after = mib(a1["memory_current_bytes"])
            proxy_rss_before = mib(p0["process_resident_memory_bytes"])
            proxy_rss_after = mib(p1["process_resident_memory_bytes"])

            api_cpu_total += api_cpu
            proxy_cpu_total += proxy_cpu
            api_mem_before_total += api_mem_before
            api_mem_after_total += api_mem_after
            proxy_rss_before_total += proxy_rss_before
            proxy_rss_after_total += proxy_rss_after

            per_pod[pod] = {
                "api_cpu_seconds_during_test": round(api_cpu, 6),
                "api_memory_before_mib": round(api_mem_before, 3),
                "api_memory_after_mib": round(api_mem_after, 3),
                "api_memory_peak_since_start_mib": round(
                    mib(a1["memory_peak_bytes"]), 3
                ),
                "proxy_cpu_seconds_during_test": round(proxy_cpu, 6),
                "proxy_rss_before_mib": round(proxy_rss_before, 3),
                "proxy_rss_after_mib": round(proxy_rss_after, 3),
            }

        total_cpu = api_cpu_total + proxy_cpu_total
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

        summary = {
            "phase": "post_mesh_with_linkerd",
            "requests": benchmark["requests"],
            "successful_requests": benchmark["successful_requests"],
            "concurrency": benchmark["concurrency"],
            "measurement_window_seconds": round(elapsed, 3),
            "api_cpu_seconds_total": round(api_cpu_total, 6),
            "proxy_cpu_seconds_total": round(proxy_cpu_total, 6),
            "api_plus_proxy_cpu_seconds_total": round(total_cpu, 6),
            "api_average_cpu_millicores": round(
                api_cpu_total / elapsed * 1000, 3
            ),
            "proxy_average_cpu_millicores": round(
                proxy_cpu_total / elapsed * 1000, 3
            ),
            "api_plus_proxy_average_cpu_millicores": round(
                total_cpu / elapsed * 1000, 3
            ),
            "api_memory_before_total_mib": round(
                api_mem_before_total, 3
            ),
            "api_memory_after_total_mib": round(
                api_mem_after_total, 3
            ),
            "proxy_rss_before_total_mib": round(
                proxy_rss_before_total, 3
            ),
            "proxy_rss_after_total_mib": round(
                proxy_rss_after_total, 3
            ),
            "pods": per_pod,
            "baseline_api_cpu_seconds_total": baseline[
                "api_cpu_seconds_total"
            ],
            "baseline_api_memory_after_total_mib": baseline[
                "memory_current_total_after_mib"
            ],
            "baseline_api_cpu_millicores": baseline[
                "average_api_cpu_millicores"
            ],
            "note": (
                "API memory uses cgroup accounting. Proxy memory uses "
                "process RSS; these memory measures are reported separately "
                "and should not be treated as identical accounting."
            ),
        }

        OUT.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        print("\n===== MESH RESOURCE RESULTS =====")
        print(f"Window: {summary['measurement_window_seconds']} s")
        print(f"API CPU time: {summary['api_cpu_seconds_total']} CPU-seconds")
        print(f"Proxy CPU time: {summary['proxy_cpu_seconds_total']} CPU-seconds")
        print(f"Combined CPU time: {summary['api_plus_proxy_cpu_seconds_total']} CPU-seconds")
        print(f"API avg CPU: {summary['api_average_cpu_millicores']} mCPU")
        print(f"Proxy avg CPU: {summary['proxy_average_cpu_millicores']} mCPU")
        print(f"API + proxy avg CPU: {summary['api_plus_proxy_average_cpu_millicores']} mCPU")
        print(f"API memory after load: {summary['api_memory_after_total_mib']} MiB")
        print(f"Proxy RSS before load: {summary['proxy_rss_before_total_mib']} MiB")
        print(f"Proxy RSS after load: {summary['proxy_rss_after_total_mib']} MiB")

        print("\nPer-pod details:")
        for pod, values in per_pod.items():
            print(f"\n{pod}")
            for key, value in values.items():
                print(f"  {key}: {value}")

        base_cpu = baseline["api_cpu_seconds_total"]
        cpu_delta = (total_cpu / base_cpu - 1) * 100 if base_cpu else 0
        base_mem = baseline["memory_current_total_after_mib"]
        api_mem_delta = (
            (api_mem_after_total / base_mem - 1) * 100 if base_mem else 0
        )

        print("\n===== BASELINE COMPARISON =====")
        print(f"Baseline API CPU time: {base_cpu:.6f} CPU-seconds")
        print(f"API + proxy CPU vs baseline API CPU: {cpu_delta:+.1f}%")
        print(f"Baseline API memory: {base_mem:.3f} MiB")
        print(f"API memory vs baseline API memory: {api_mem_delta:+.1f}%")
        print("\nNote: memory comparison excludes proxy RSS; it is reported separately.")
        print(f"\nSaved: {OUT}")

    finally:
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for handle in log_handles.values():
            handle.close()


if __name__ == "__main__":
    main()
