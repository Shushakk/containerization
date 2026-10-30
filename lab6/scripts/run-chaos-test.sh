#!/usr/bin/env bash
set -uo pipefail

CTX="kind-shop-lab5"
NS="shop"
DURATION="${1:-180}"
INTERVAL="${2:-5}"

API_SELECTOR='app.kubernetes.io/instance=shop,app.kubernetes.io/name=api,app.kubernetes.io/part-of=shop'
WORKER_SELECTOR='app.kubernetes.io/instance=shop,app.kubernetes.io/name=worker,app.kubernetes.io/part-of=shop'

CLIENT="shop-chaos-client"
CLIENT_CSV="reports/chaos-client.csv"
EVENTS_CSV="reports/chaos-events.csv"
POD_SNAPSHOTS="reports/chaos-pods-snapshots.log"
STREAM_ERRORS="reports/chaos-client-stream-errors.log"

mkdir -p reports

if ! kubectl --context "$CTX" get pod "$CLIENT" -n "$NS" >/dev/null; then
    echo "ERROR: клиент $CLIENT не найден"
    exit 1
fi

ready_count() {
    kubectl --context "$CTX" get pods -n "$NS" \
        -l "$1" -o json |
    python3 -c '
import json, sys
pods = json.load(sys.stdin)["items"]
def ready(p):
    return (
        not p["metadata"].get("deletionTimestamp")
        and p["status"].get("phase") == "Running"
        and any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in p["status"].get("conditions", [])
        )
    )
print(sum(ready(p) for p in pods))
'
}

desired_count() {
    kubectl --context "$CTX" get deployment "$1" -n "$NS" \
        -o jsonpath='{.spec.replicas}'
}

terminating_count() {
    kubectl --context "$CTX" get pods -n "$NS" -o json |
    python3 -c '
import json, sys
pods = json.load(sys.stdin)["items"]
names = {"api", "worker"}
print(sum(
    1 for p in pods
    if p["metadata"].get("labels", {}).get("app.kubernetes.io/name") in names
    and p["metadata"].get("deletionTimestamp")
))
'
}

snapshot() {
    {
        echo
        echo "===== $(date -Is) ====="
        kubectl --context "$CTX" get pods -n "$NS" -o wide
    } >> "$POD_SNAPSHOTS"
}

wait_for_ready() {
    local timeout="$1"
    local deadline=$((SECONDS + timeout))

    while (( SECONDS < deadline )); do
        local ar wr ad wd term

        ar=$(ready_count "$API_SELECTOR" 2>/dev/null) || ar=0
        wr=$(ready_count "$WORKER_SELECTOR" 2>/dev/null) || wr=0
        ad=$(desired_count shop-api 2>/dev/null) || ad=0
        wd=$(desired_count shop-worker 2>/dev/null) || wd=0
        term=$(terminating_count 2>/dev/null) || term=1

        if [[ "$ar" =~ ^[0-9]+$ && "$wr" =~ ^[0-9]+$ &&
              "$ad" =~ ^[0-9]+$ && "$wd" =~ ^[0-9]+$ &&
              "$term" =~ ^[0-9]+$ ]] &&
           (( ad > 0 && wd > 0 && ar >= ad && wr >= wd && term == 0 )); then
            echo "READY: api=$ar/$ad worker=$wr/$wd; terminating=$term"
            return 0
        fi

        echo "WAIT: api=${ar}/${ad} worker=${wr}/${wd}; terminating=${term}"
        sleep 2
    done

    echo "ERROR: replicas не восстановились за ${timeout}s"
    return 1
}

record_event() {
    printf '%s,%s,%s,%s,%s,%s,%s\n' \
        "$(date -u +%FT%TZ)" "$1" "$2" "$3" "$4" "$5" "$6" \
        >> "$EVENTS_CSV"
}

echo "timestamp_utc,component,pod,action,api_ready,worker_ready,detail" > "$EVENTS_CSV"
echo 'timestamp,status,latency_ms,error' > "$CLIENT_CSV"
: > "$POD_SNAPSHOTS"
: > "$STREAM_ERRORS"

echo "=== Проверяем исходную готовность ==="
wait_for_ready 120 || exit 1

echo
echo "=== Начинаем сбор клиентских запросов ==="
kubectl --context "$CTX" logs -n "$NS" "$CLIENT" \
    --follow --tail=0 >> "$CLIENT_CSV" 2>> "$STREAM_ERRORS" &
LOGGER_PID=$!

cleanup() {
    kill "$LOGGER_PID" 2>/dev/null || true
    wait "$LOGGER_PID" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

sleep 2
if ! kill -0 "$LOGGER_PID" 2>/dev/null; then
    echo "ERROR: поток логов клиента завершился. См. $STREAM_ERRORS"
    exit 1
fi

END=$((SECONDS + DURATION))
ITERATION=0

echo
echo "=== CHAOS RUN: duration=${DURATION}s, interval=${INTERVAL}s ==="

while (( SECONDS < END )); do
    if ! wait_for_ready 120; then
        record_event "none" "none" "WAIT_TIMEOUT" "0" "0" "recovery_timeout"
        break
    fi

    snapshot

    COMPONENT=$(shuf -e api worker -n 1)

    if [[ "$COMPONENT" == "api" ]]; then
        SELECTOR="$API_SELECTOR"
        GRACE=25
    else
        SELECTOR="$WORKER_SELECTOR"
        GRACE=10
    fi

    PODS=$(kubectl --context "$CTX" get pods -n "$NS" \
        -l "$SELECTOR" -o json |
        python3 -c '
import json, sys
for p in json.load(sys.stdin)["items"]:
    m = p["metadata"]
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in p["status"].get("conditions", [])
    )
    if (
        not m.get("deletionTimestamp")
        and p["status"].get("phase") == "Running"
        and ready
    ):
        print(m["name"])
')

    POD=$(printf '%s\n' "$PODS" | sed '/^$/d' | shuf -n 1)

    if [[ -z "${POD:-}" ]]; then
        echo "SKIP: нет подходящего готового Pod-а"
        sleep "$INTERVAL"
        continue
    fi

    AR=$(ready_count "$API_SELECTOR" 2>/dev/null) || AR=0
    WR=$(ready_count "$WORKER_SELECTOR" 2>/dev/null) || WR=0

    ITERATION=$((ITERATION + 1))
    echo
    echo "[$(date -Is)] DELETE #${ITERATION}: ${COMPONENT}/${POD}; api=${AR} worker=${WR}; grace=${GRACE}s"

    if kubectl --context "$CTX" delete pod "$POD" -n "$NS" \
        --grace-period="$GRACE" --wait=false; then
        record_event "$COMPONENT" "$POD" "DELETE_REQUESTED" "$AR" "$WR" "grace_${GRACE}s"
    else
        record_event "$COMPONENT" "$POD" "DELETE_FAILED" "$AR" "$WR" "kubectl_error"
    fi

    snapshot
    sleep "$INTERVAL"
done

echo
echo "=== Проверяем восстановление после последнего удаления ==="
wait_for_ready 120 || echo "WARNING: проверь Deployment и события кластера"

snapshot

echo
echo "=== Сводка запросов клиента ==="
awk -F, '
NR > 1 {
    total++
    if ($2 >= 200 && $2 < 400 && $4 == "-") success++
    else failed++
}
END {
    printf "Всего запросов: %d\nУспешных: %d\nНеуспешных: %d\n", total, success, failed
}' "$CLIENT_CSV"

echo
echo "=== События хаоса ==="
cat "$EVENTS_CSV"

echo
echo "Результаты:"
echo "  $CLIENT_CSV"
echo "  $EVENTS_CSV"
echo "  $POD_SNAPSHOTS"
echo "  $STREAM_ERRORS"
