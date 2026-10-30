#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

cat > reports/fault-values.yaml <<'YAML'
networkPolicy:
  enabled: true
  faultMode: none
YAML

echo "=== Возвращаем штатные сетевые правила ==="

helm --kube-context kind-shop-lab5 upgrade shop ./chart \
  --namespace shop \
  --reuse-values \
  -f reports/fault-values.yaml \
  --force-conflicts \
  --wait \
  --timeout 10m

echo
echo "Штатные правила восстановлены."
