#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="${1:-bad}"
NS="shop"
RELEASE="shop"
EXPECTED_CONTEXT="kind-shop-lab5"

ERROR_THRESHOLD="${ERROR_THRESHOLD:-5}"
P95_THRESHOLD_MS="${P95_THRESHOLD_MS:-100}"
REQUEST_COUNT="${REQUEST_COUNT:-160}"
PROM_PORT="${PROM_PORT:-19091}"
PROM_URL="http://127.0.0.1:${PROM_PORT}"

if [[ "$MODE" != "bad" && "$MODE" != "good" ]]; then
  echo "Использование: $0 bad|good"
  exit 1
fi

CTX="$(kubectl config current-context)"
if [[ "$CTX" != "$EXPECTED_CONTEXT" ]]; then
  echo "ОШИБКА: ожидается контекст $EXPECTED_CONTEXT, текущий: $CTX"
  exit 1
fi

API_FLAKY="$(
  helm get values "$RELEASE" -n "$NS" --all -o json |
    python3 -c 'import json,sys; print(str(json.load(sys.stdin).get("api",{}).get("flaky","")).lower())'
)"

if [[ "$API_FLAKY" != "false" ]]; then
  echo "ОШИБКА: сначала верни стабильный API в состояние api.flaky=false."
  exit 1
fi

if kubectl get httproutes.policy.linkerd.io shop-api-canary -n "$NS" >/dev/null 2>&1; then
  echo "ОШИБКА: shop-api-canary уже существует."
  echo "Сначала проверь состояние прошлого запуска, чтобы не смешивать эксперименты."
  exit 1
fi

BASE_REV="$(
  helm history "$RELEASE" -n "$NS" -o json |
    python3 -c 'import json,sys; print(max(int(x["revision"]) for x in json.load(sys.stdin)))'
)"

# Если ошибка произошла после начала установки canary,
# возвращаем Helm-релиз к исходной ревизии автоматически.
CANARY_STARTED=0

on_unexpected_error() {
  local rc="$1"
  local line="$2"
  local command="$3"

  trap - ERR
  echo >&2
  echo "ОШИБКА на строке $line: $command" >&2

  if [[ "$CANARY_STARTED" == "1" ]]; then
    echo "Запускаем аварийный rollback к ревизии $BASE_REV." >&2
    if ! helm rollback "$RELEASE" "$BASE_REV" -n "$NS" \
        --wait --timeout 5m; then
      echo "Helm rollback не удался; пробуем отключить canary через upgrade." >&2
      helm upgrade "$RELEASE" chart -n "$NS" --reuse-values \
        --set canary.enabled=false \
        --set-string api.flaky=false \
        --wait --timeout 5m || true
    fi
  fi

  exit "$rc"
}

trap 'on_unexpected_error "$?" "$LINENO" "$BASH_COMMAND"' ERR

if [[ "$MODE" == "bad" ]]; then
  CANARY_FAIL="true"
  WEIGHTS=(10)
else
  CANARY_FAIL="false"
  WEIGHTS=(10 30 50 75 100)
fi

REPORT="reports/canary-prometheus-gate-${MODE}-$(date -u +%Y%m%dT%H%M%SZ).csv"
mkdir -p reports
echo "timestamp_utc,mode,canary_weight,canary_pod,client_200,client_500,canary_total,canary_failures,error_percent,p95_ms,decision" > "$REPORT"

PF_PID=""
cleanup() {
  if [[ -n "$PF_PID" ]]; then
    kill "$PF_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

rollback_base() {
  local exit_code="$1"
  shift
  echo
  echo "===== $* ====="
  echo "Откатываем Helm на исходную ревизию: $BASE_REV"

  if helm rollback "$RELEASE" "$BASE_REV" -n "$NS" \
      --wait --timeout 5m; then
    kubectl rollout status deployment/shop-api -n "$NS" --timeout=5m || true
    echo "Rollback completed."
  else
    echo "ОШИБКА: автоматический rollback не завершился успешно."
    exit 10
  fi

  exit "$exit_code"
}

echo "Режим: $MODE"
echo "Исходная ревизия для rollback: $BASE_REV"
echo "Порог ошибок canary: ${ERROR_THRESHOLD}%"
echo "Порог P95 TTFB: ${P95_THRESHOLD_MS} ms"
echo "Число запросов за цикл: $REQUEST_COUNT"
echo "CSV-отчёт: $REPORT"

PROM_POD="$(
  kubectl get pods -n linkerd-viz -o name |
    grep '/prometheus-' | head -n 1
)"

if [[ -z "$PROM_POD" ]]; then
  echo "ОШИБКА: Prometheus pod не найден в namespace linkerd-viz."
  exit 1
fi

kubectl port-forward -n linkerd-viz "$PROM_POD" \
  "${PROM_PORT}:9090" >/tmp/shop-lab6-gate-port-forward.log 2>&1 &
PF_PID=$!

READY=0
for _ in $(seq 1 15); do
  if curl -fsS --max-time 2 "${PROM_URL}/-/ready" \
      >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [[ "$READY" -ne 1 ]]; then
  echo "ОШИБКА: Prometheus недоступен через port-forward."
  cat /tmp/shop-lab6-gate-port-forward.log
  exit 1
fi

prom_query() {
  python3 - "$PROM_URL/api/v1/query" "$1" <<'PY'
import json
import math
import sys
import urllib.parse
import urllib.request

endpoint, query = sys.argv[1], sys.argv[2]
url = endpoint + "?" + urllib.parse.urlencode({"query": query})

try:
    with urllib.request.urlopen(url, timeout=10) as response:
        data = json.load(response)

    rows = data.get("data", {}).get("result", [])
    if not rows:
        print("NaN")
    else:
        value = float(rows[0].get("value", [None, "NaN"])[1])
        print(value if math.isfinite(value) else "NaN")
except Exception as exc:
    print("NaN")
    print(f"Prometheus query error: {exc}", file=sys.stderr)
PY
}

for WEIGHT in "${WEIGHTS[@]}"; do
  STABLE_WEIGHT=$((100 - WEIGHT))

  echo
  echo "=================================================="
  echo "Устанавливаем веса: v1=${STABLE_WEIGHT}%, v2=${WEIGHT}%"
  echo "Canary FAIL=${CANARY_FAIL}"
  echo "=================================================="

  CANARY_STARTED=1
  helm upgrade "$RELEASE" chart -n "$NS" --reuse-values \
    --set canary.enabled=true \
    --set canary.replicaCount=1 \
    --set-string canary.image.repository=shop.local/api \
    --set-string canary.image.tag=v2 \
    --set-string canary.image.pullPolicy=IfNotPresent \
    --set-string canary.clientImage.repository=shop.local/api \
    --set-string canary.clientImage.tag=v1 \
    --set-string canary.clientImage.pullPolicy=IfNotPresent \
    --set-string canary.fail="$CANARY_FAIL" \
    --set-string canary.flaky=false \
    --set-string canary.healthFail=false \
    --set canary.stableWeight="$STABLE_WEIGHT" \
    --set canary.canaryWeight="$WEIGHT" \
    --wait --timeout 5m

  kubectl rollout status deployment/shop-api-v2 -n "$NS" --timeout=3m

  ROUTE_CONDITIONS="$(
    kubectl get httproutes.policy.linkerd.io shop-api-canary -n "$NS" \
      -o jsonpath='{range .status.parents[*].conditions[*]}{.type}={.status}{" "}{.reason}{"\n"}{end}'
  )"
  echo "$ROUTE_CONDITIONS"

  if ! grep -q 'Accepted=True' <<< "$ROUTE_CONDITIONS" ||
     ! grep -q 'ResolvedRefs=True' <<< "$ROUTE_CONDITIONS"; then
    rollback_base 2 "GATE=NO_DATA: HTTPRoute не принял маршрут или backend-ссылки"
  fi

  CANARY_POD="$(
    kubectl get pods -n "$NS" \
      -l app.kubernetes.io/name=api-v2 \
      --field-selector=status.phase=Running \
      -o jsonpath='{.items[0].metadata.name}'
  )"

  if [[ -z "$CANARY_POD" ]]; then
    rollback_base 2 "GATE=NO_DATA: не найден работающий canary pod"
  fi

  SOURCE_POD="$(
    kubectl get pods -n "$NS" \
      -l app.kubernetes.io/name=api \
      --field-selector=status.phase=Running \
      -o jsonpath='{.items[0].metadata.name}'
  )"

  if [[ -z "$SOURCE_POD" ]]; then
    rollback_base 2 "GATE=NO_DATA: не найден исходный meshed API pod"
  fi

  LOAD_FILE="/tmp/shop-lab6-gate-load-${MODE}-${WEIGHT}.json"

  echo
  echo "Отправляем запросы из meshed pod $SOURCE_POD"
  kubectl exec -n "$NS" -i "$SOURCE_POD" -c api -- \
    python - "$REQUEST_COUNT" > "$LOAD_FILE" <<'PY'
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

count = int(sys.argv[1])
base = "http://shop-api.shop.svc.cluster.local:8000"

def send(i):
    payload = json.dumps({
        "item": f"lab6-gate-{i}",
        "quantity": 1
    }).encode()
    request = urllib.request.Request(
        base + "/order",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
            return str(response.status)
    except urllib.error.HTTPError as exc:
        exc.read()
        return str(exc.code)
    except Exception:
        return "ERROR"

with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(send, range(1, count + 1)))

counts = Counter(results)
print(json.dumps({
    "requests": count,
    "statuses": dict(counts),
    "client_200": counts.get("200", 0),
    "client_500": counts.get("500", 0),
    "other_errors": sum(v for k, v in counts.items() if k not in ("200", "500"))
}))
PY

  cat "$LOAD_FILE"

  echo
  echo "Ожидаем сбора свежих метрик..."
  sleep 15

  TOTAL="$(prom_query "sum(increase(response_total{namespace=\"shop\",deployment=\"shop-api-v2\",pod=\"${CANARY_POD}\",direction=\"inbound\"}[3m]))")"
  FAILURES="$(prom_query "sum(increase(response_total{namespace=\"shop\",deployment=\"shop-api-v2\",pod=\"${CANARY_POD}\",direction=\"inbound\",classification=\"failure\"}[3m]))")"
  P95="$(prom_query "histogram_quantile(0.95,sum by (le) (rate(response_latency_ms_bucket{namespace=\"shop\",deployment=\"shop-api-v2\",pod=\"${CANARY_POD}\",direction=\"inbound\"}[3m])))")"

  EVAL="$(
    python3 - "$TOTAL" "$FAILURES" "$P95" \
      "$ERROR_THRESHOLD" "$P95_THRESHOLD_MS" <<'PY'
import math
import sys

total, failures, p95, err_limit, p95_limit = map(float, sys.argv[1:])
if not all(math.isfinite(x) for x in (total, failures, p95)) or total <= 0:
    print("NaN\tNaN\tNO_DATA\tmissing_canary_metrics")
else:
    error_pct = failures * 100 / total
    if error_pct > err_limit or p95 > p95_limit:
        decision = "FAIL"
        reason = "threshold_exceeded"
    else:
        decision = "PASS"
        reason = "within_thresholds"
    print(f"{error_pct:.3f}\t{p95:.3f}\t{decision}\t{reason}")
PY
  )"

  IFS=$'\t' read -r ERROR_PCT P95_MS DECISION REASON <<< "$EVAL"

  LOAD_200="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("client_200",0))' "$LOAD_FILE")"
  LOAD_500="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("client_500",0))' "$LOAD_FILE")"

  echo
  echo "===== GATE RESULT ====="
  echo "Canary pod: $CANARY_POD"
  echo "Ответы canary в Prometheus: $TOTAL"
  echo "Ошибки canary в Prometheus: $FAILURES"
  echo "Доля ошибок canary: ${ERROR_PCT}%"
  echo "P95 TTFB: ${P95_MS} ms"
  echo "Решение: $DECISION ($REASON)"
  echo "Клиент: HTTP 200=$LOAD_200; HTTP 500=$LOAD_500"

  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$(date -u +%FT%TZ)" "$MODE" "$WEIGHT" "$CANARY_POD" \
    "$LOAD_200" "$LOAD_500" "$TOTAL" "$FAILURES" \
    "$ERROR_PCT" "$P95_MS" "$DECISION" >> "$REPORT"

  if [[ "$DECISION" == "NO_DATA" ]]; then
    rollback_base 2 "GATE=NO_DATA: метрик canary недостаточно; продвижение запрещено"
  fi

  if [[ "$DECISION" == "FAIL" ]]; then
    if [[ "$MODE" == "bad" ]]; then
      rollback_base 0 "GATE=REJECT: плохая версия обнаружена; автоматический rollback"
    else
      rollback_base 2 "GATE=REJECT: ошибка или задержка превысила порог"
    fi
  fi

  if [[ "$MODE" == "bad" ]]; then
    rollback_base 3 "GATE=UNEXPECTED_PASS: плохая версия не обнаружена; выполняем защитный rollback"
  fi

  echo "GATE=PASS: canary остаётся в пределах порогов."
done

echo
echo "RESULT=GOOD_RELEASE_AT_100_PERCENT"
kubectl get httproutes.policy.linkerd.io shop-api-canary -n shop -o yaml
echo
echo "CSV: $REPORT"
