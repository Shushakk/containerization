import json
import subprocess
import sys
import time
from pathlib import Path

REPORTS = Path("reports")
OUT = REPORTS / "baseline-no-mesh-resources.json"


def run(cmd):
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)


def get_pods():
    output = run([
        "kubectl", "-n", "shop", "get", "pods",
        "-l", "app.kubernetes.io/name=api",
        "-o", "jsonpath={.items[*].metadata.name}",
    ])
    pods = output.split()
    if len(pods) != 3:
        raise RuntimeError(f"Expected 3 API pods, found {len(pods)}: {pods}")
    return pods


def snapshot(pods):
    result = {}

    command = (
        "cat /sys/fs/cgroup/cpu.stat; "
        "printf 'MEMORY_CURRENT '; cat /sys/fs/cgroup/memory.current; "
        "printf 'MEMORY_PEAK '; cat /sys/fs/cgroup/memory.peak"
    )

    for pod in pods:
        output = run([
            "kubectl", "-n", "shop", "exec", pod, "--",
            "sh", "-c", command,
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

        result[pod] = {
            "cpu_usage_usec": stats["usage_usec"],
            "memory_current_bytes": stats["MEMORY_CURRENT"],
            "memory_peak_bytes": stats["MEMORY_PEAK"],
        }

    return result


def main():
    REPORTS.mkdir(exist_ok=True)
    pods = get_pods()

    print("API pods:", ", ".join(pods))
    print("Collecting cgroup values before load...")
    before = snapshot(pods)

    print("\nRunning the same 120-request benchmark...")
    started = time.monotonic()
    subprocess.run([sys.executable, "scripts/bench-order.py"], check=True)
    elapsed = time.monotonic() - started

    print("\nCollecting cgroup values after load...")
    after = snapshot(pods)

    per_pod = {}
    total_cpu_seconds = 0.0

    for pod in pods:
        cpu_delta = (
            after[pod]["cpu_usage_usec"]
            - before[pod]["cpu_usage_usec"]
        )
        if cpu_delta < 0:
            raise RuntimeError(f"CPU counter decreased for {pod}")

        cpu_seconds = cpu_delta / 1_000_000
        total_cpu_seconds += cpu_seconds

        per_pod[pod] = {
            "cpu_seconds_during_test": round(cpu_seconds, 6),
            "memory_before_mib": round(
                before[pod]["memory_current_bytes"] / 1048576, 3
            ),
            "memory_after_mib": round(
                after[pod]["memory_current_bytes"] / 1048576, 3
            ),
            "memory_peak_since_pod_start_mib": round(
                after[pod]["memory_peak_bytes"] / 1048576, 3
            ),
        }

    summary = {
        "phase": "baseline_without_mesh",
        "requests": 120,
        "concurrency": 4,
        "measurement_window_seconds": round(elapsed, 3),
        "api_cpu_seconds_total": round(total_cpu_seconds, 6),
        "average_api_cpu_cores": round(total_cpu_seconds / elapsed, 6),
        "average_api_cpu_millicores": round(
            total_cpu_seconds / elapsed * 1000, 3
        ),
        "memory_current_total_after_mib": round(
            sum(x["memory_after_mib"] for x in per_pod.values()), 3
        ),
        "pods": per_pod,
    }

    OUT.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("\n===== BASELINE RESOURCE RESULTS =====")
    print(f"Measurement window: {summary['measurement_window_seconds']} s")
    print(f"Total API CPU time: {summary['api_cpu_seconds_total']} CPU-seconds")
    print(f"Average API CPU: {summary['average_api_cpu_millicores']} millicores")
    print(f"API memory after load: {summary['memory_current_total_after_mib']} MiB")

    for pod, values in per_pod.items():
        print(f"\n{pod}")
        for key, value in values.items():
            print(f"  {key}: {value}")

    print(f"\nSaved: {OUT}")


if __name__ == "__main__":
    main()
