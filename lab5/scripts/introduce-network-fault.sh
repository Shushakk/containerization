#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

mkdir -p reports

MODES=(
  "wrong-api-db-port"
  "wrong-worker-db-label"
  "block-dns"
  "wrong-client-api-port"
)

INDEX="$(shuf -i 0-3 -n 1)"
MODE="${MODES[$INDEX]}"

cat > reports/fault-values.yaml <<YAML
networkPolicy:
  enabled: true
  faultMode: ${MODE}
YAML

printf '%s\n' "$MODE" > reports/fault-injection-internal.txt
chmod 600 reports/fault-injection-internal.txt

echo "=== Проверяем шаблоны перед внесением поломки ==="
helm lint ./chart

helm template shop ./chart \
  --namespace shop \
  -f reports/fault-values.yaml \
  > /tmp/shop-fault-rendered.yaml

echo
echo "=== Применяем случайную неисправность ==="
helm --kube-context kind-shop-lab5 upgrade shop ./chart \
  --namespace shop \
  --reuse-values \
  -f reports/fault-values.yaml \
  --force-conflicts \
  --wait \
  --timeout 10m

echo
echo "Скрытая неисправность внесена."
echo "Проверь матрицу соединений, затем события Hubble."
echo "Не открывай reports/fault-injection-internal.txt до завершения диагностики."
