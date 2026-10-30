#!/usr/bin/env bash
set -euo pipefail

cd "$HOME/shop-lab3"

GOOD_REVISION=15
TOTAL=200
CONCURRENCY=4
ERROR_THRESHOLD=5.0
NAMESPACE=shop

echo "===== 1. Проверяем кластер и ревизию для отката ====="

CONTEXT=$(kubectl config current-context)
echo "Context: $CONTEXT"

if [[ "$CONTEXT" != "kind-shop-lab5" ]]; then
  echo "ERROR: неправильный Kubernetes context"
  exit 1
fi

helm history shop -n "$NAMESPACE"

if ! helm history shop -n "$NAMESPACE" -o json |
  python3 -c '
import json, sys
items = json.load(sys.stdin)
sys.exit(0 if any(int(x["revision"]) == 15 for x in items) else 1)
'; then
  echo "ERROR: ревизия 15 не найдена. Откат отменён."
  exit 1
fi

GOOD_MANIFEST=$(helm get manifest shop -n "$NAMESPACE" \
  --revision "$GOOD_REVISION")

if ! grep -q 'linkerd.io/inject: enabled' <<<"$GOOD_MANIFEST"; then
  echo "ERROR: ревизия 15 не содержит включённый Linkerd"
  exit 1
fi

if grep -q 'name: shop-api-v2' <<<"$GOOD_MANIFEST"; then
  echo "ERROR: ревизия 15 неожиданно содержит canary v2"
  exit 1
fi

kubectl get httproute.policy.linkerd.io shop-api-canary \
  -n "$NAMESPACE"

echo
echo "===== 2. Включаем искусственный отказ v2 ====="

helm upgrade shop chart -n "$NAMESPACE" \
  --reuse-values \
  --set-string canary.fail=true \
  --wait --timeout 5m

kubectl rollout status deployment/shop-api-v2 \
  -n "$NAMESPACE" --timeout=5m

echo
echo "===== 3. Проверяем, что v2 действительно возвращает HTTP 500 ====="

PROBE=$(
  kubectl exec -i -n "$NAMESPACE" deploy/shop-canary-client \
    -c client -- python - <<'PY'
import json
import urllib.error
import urllib.request

url = "http://shop-api-v2.shop.svc.cluster.local:8000/order"
payload = json.dumps({
    "item": "lab6-canary-failure-probe",
    "quantity": 1,
}).encode()

req = urllib.request.Request(
    url, data=payload,
    headers={"Content-Type": "application/json", "Connection": "close"},
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=10) as response:
        status = response.status
        response.read()
except urllib.error.HTTPError as exc:
    status = exc.code
    exc.read()

print(f"FAIL_PROBE_STATUS={status}")
PY
)

echo "$PROBE"

if ! grep -q '^FAIL_PROBE_STATUS=500$' <<<"$PROBE"; then
  echo "ERROR: v2 не подтвердил ожидаемый отказ."
  echo "Возвращаем canary.fail=false."
  helm upgrade shop chart -n "$NAMESPACE" \
    --reuse-values --set-string canary.fail=false \
    --wait --timeout 5m
  exit 1
fi

echo
echo "===== 4. Выполняем $TOTAL запросов через mesh ====="

RESULT=$(
  kubectl exec -i -n "$NAMESPACE" deploy/shop-canary-client \
    -c client -- python - "$TOTAL" "$CONCURRENCY" <<'PY'
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

total = int(sys.argv[1])
concurrency = int(sys.argv[2])
url = "http://shop-api.shop.svc.cluster.local:8000/order"

def send(index):
    payload = json.dumps({
        "item": f"lab6-canary-gate-{index}",
        "quantity": 1,
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Connection": "close",
            "X-Lab-Request": str(index),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
            return str(response.status)
    except urllib.error.HTTPError as exc:
        exc.read()
        return str(exc.code)
    except Exception as exc:
        return "ERROR_" + type(exc).__name__

with ThreadPoolExecutor(max_workers=concurrency) as pool:
    statuses = list(pool.map(send, range(1, total + 1)))

counts = Counter(statuses)
success = counts["200"]
errors = total - success

result = {
    "total": total,
    "concurrency": concurrency,
    "success": success,
    "errors": errors,
    "error_rate_percent": round(errors * 100 / total, 2),
    "status_counts": dict(counts),
}

print("CANARY_GATE_RESULT=" + json.dumps(result))
PY
)

echo "$RESULT"

GATE_JSON=$(sed -n 's/^CANARY_GATE_RESULT=//p' <<<"$RESULT" | tail -n 1)

if [[ -z "$GATE_JSON" ]]; then
  echo "ERROR: не удалось прочитать результаты теста."
  echo "Состояние canary.fail остаётся true — проверь вывод и не продолжай rollout."
  exit 1
fi

ERROR_RATE=$(python3 -c \
  'import json,sys; print(json.loads(sys.argv[1])["error_rate_percent"])' \
  "$GATE_JSON")

ERRORS=$(python3 -c \
  'import json,sys; print(json.loads(sys.argv[1])["errors"])' \
  "$GATE_JSON")

echo
echo "===== 5. Решение автоматического gate ====="
echo "Errors: $ERRORS / $TOTAL"
echo "Error rate: $ERROR_RATE%"
echo "Threshold: $ERROR_THRESHOLD%"

if python3 -c \
  'import sys; sys.exit(0 if float(sys.argv[1]) > float(sys.argv[2]) else 1)' \
  "$ERROR_RATE" "$ERROR_THRESHOLD"; then

  echo
  echo "GATE FAILED: error rate exceeds threshold."
  echo "AUTOPILOT: откатываем Helm к ревизии $GOOD_REVISION."

  helm rollback shop "$GOOD_REVISION" -n "$NAMESPACE" \
    --wait --timeout 5m

  echo
  echo "===== 6. Проверяем стабильную версию после отката ====="

  kubectl rollout status deployment/shop-api \
    -n "$NAMESPACE" --timeout=5m

  kubectl get pods -n "$NAMESPACE" \
    -l app.kubernetes.io/name=api -o wide

  echo
  echo "Health:"
  curl -fsS --connect-timeout 3 --max-time 10 \
    http://127.0.0.1:30080/health
  echo

  echo "Version:"
  curl -fsS --connect-timeout 3 --max-time 10 \
    http://127.0.0.1:30080/version
  echo

  echo
  echo "===== 7. Проверяем, что canary удалён ====="
  kubectl get deployment,service -n "$NAMESPACE"
  kubectl get httproute.policy.linkerd.io shop-api-canary \
    -n "$NAMESPACE" 2>&1 || true

  echo
  echo "===== 8. Проверяем Linkerd после отката ====="
  linkerd check
  linkerd viz stat deploy -n "$NAMESPACE"

  echo
  echo "AUTOPILOT RESULT: ROLLED BACK TO REVISION $GOOD_REVISION"

else
  echo
  echo "GATE PASSED: error rate did not exceed threshold."
  echo "Возвращаем v2 в нормальный режим, чтобы не оставлять отказ включённым."

  helm upgrade shop chart -n "$NAMESPACE" \
    --reuse-values \
    --set-string canary.fail=false \
    --wait --timeout 5m

  echo "Canary оставлен в работе; автоматический откат не потребовался."
fi
