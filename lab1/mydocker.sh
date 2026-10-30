#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
API="$ROOT_DIR/lab1-api"

CGROOT="/sys/fs/cgroup"
CGROUP="$CGROOT/mydocker-api"

if [[ ! -x "$API" ]]; then
    echo "Error: executable not found: $API" >&2
    exit 1
fi

if [[ $EUID -eq 0 ]]; then
    echo "Run without sudo: ./mydocker.sh" >&2
    exit 1
fi

# Проверяем доступность и включаем контроллеры
# для дочерних cgroups.
for ctrl in cpu memory pids; do
    if ! grep -qw "$ctrl" "$CGROOT/cgroup.controllers"; then
        echo "Controller unavailable: $ctrl" >&2
        exit 1
    fi

    if ! grep -qw "$ctrl" "$CGROOT/cgroup.subtree_control"; then
        echo "+$ctrl" |
            sudo tee "$CGROOT/cgroup.subtree_control" >/dev/null
    fi
done

# Убираем остаток предыдущего запуска,
# но только если в группе нет процессов.
if [[ -d "$CGROUP" ]]; then
    if [[ -s "$CGROUP/cgroup.procs" ]]; then
        echo "Cgroup is still busy: $CGROUP" >&2
        exit 1
    fi
    sudo rmdir "$CGROUP"
fi

sudo mkdir "$CGROUP"

# Устанавливаем лимиты.
echo 134217728 |
    sudo tee "$CGROUP/memory.max" >/dev/null

echo 0 |
    sudo tee "$CGROUP/memory.swap.max" >/dev/null

echo '50000 100000' |
    sudo tee "$CGROUP/cpu.max" >/dev/null

echo 64 |
    sudo tee "$CGROUP/pids.max" >/dev/null

IN_CGROUP=0

cleanup() {
    local rc=$?
    trap - EXIT

    # Перед удалением cgroup перемещаем оболочку
    # launcher обратно в родительскую группу.
    if [[ "$IN_CGROUP" == 1 ]]; then
        echo "$$" |
            sudo tee "$CGROOT/cgroup.procs" >/dev/null || true
    fi

    if [[ -d "$CGROUP" ]]; then
        sudo rmdir "$CGROUP" ||
            echo "Cgroup remains: $CGROUP" >&2
    fi

    exit "$rc"
}

trap cleanup EXIT

# Перемещаем launcher в cgroup ДО запуска API.
echo "$$" |
    sudo tee "$CGROUP/cgroup.procs" >/dev/null

IN_CGROUP=1

echo "=== Cgroup limits ==="
printf 'memory.max: '
cat "$CGROUP/memory.max"

printf 'memory.swap.max: '
cat "$CGROUP/memory.swap.max"

printf 'cpu.max: '
cat "$CGROUP/cpu.max"

printf 'pids.max: '
cat "$CGROUP/pids.max"

echo "=== Starting API in namespaces ==="

unshare \
    --user --map-root-user \
    --mount --uts --ipc --net \
    --pid --fork --mount-proc \
    sh -ceu '
        hostname api-ns
        ip link set lo up

        echo "=== Identity ==="
        id

        echo "=== Hostname ==="
        hostname

        echo "=== Network ==="
        ip -brief addr

echo "=== Capability test ==="

if setpriv \
    --bounding-set=-sys_admin \
    --inh-caps=-sys_admin \
    --ambient-caps=-sys_admin \
    hostname cap-test-after
then
    echo "ERROR: hostname change unexpectedly succeeded" >&2
    exit 1
else
    echo "Expected: hostname change denied"
fi

hostname

echo "=== Starting API as PID 1 with dropped capabilities ==="

exec setpriv \
    --bounding-set=-all \
    --inh-caps=-all \
    --ambient-caps=-all \
    --no-new-privs \
    /usr/bin/python3 "$3" "$2" "$1"
    ' sh "$API" \
    "$ROOT_DIR/seccomp-profile.json" \
    "$ROOT_DIR/seccomp_exec.py"
