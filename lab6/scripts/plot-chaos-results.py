import csv
import html
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENT_FILE = ROOT / "reports/chaos-client.csv"
EVENT_FILE = ROOT / "reports/chaos-events.csv"
OUT_FILE = ROOT / "reports/chaos-result.svg"


def parse_time(value):
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def read_csv(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


requests = read_csv(CLIENT_FILE)
events = read_csv(EVENT_FILE) if EVENT_FILE.exists() else []

if not requests:
    raise SystemExit(f"Нет записей в {CLIENT_FILE}")

points = []
failed = []

for row in requests:
    try:
        timestamp = parse_time(row["timestamp"])
    except (ValueError, KeyError):
        continue

    try:
        latency = float(row["latency_ms"])
    except (ValueError, KeyError, TypeError):
        latency = None

    status = row.get("status", "").strip()
    error = row.get("error", "").strip()

    try:
        code = int(status)
    except ValueError:
        code = 0

    success = 200 <= code < 400 and error in ("", "-")

    points.append({
        "time": timestamp,
        "latency": latency,
        "status": status,
        "success": success,
    })

    if not success:
        failed.append(row)

points.sort(key=lambda p: p["time"])
events.sort(key=lambda e: parse_time(e["timestamp_utc"]))

times = [p["time"] for p in points]
start, end = min(times), max(times)
duration = max(1.0, (end - start).total_seconds())

latencies = [p["latency"] for p in points if p["latency"] is not None]
y_max = max(100, math.ceil(max(latencies, default=100) / 50) * 50)

W, H = 1450, 800
LEFT, RIGHT, TOP, BOTTOM = 105, 50, 160, 145
PW = W - LEFT - RIGHT
PH = H - TOP - BOTTOM
Y_BOTTOM = TOP + PH

def x_pos(t):
    return LEFT + (t - start).total_seconds() / duration * PW

def y_pos(value):
    return TOP + PH * (1 - value / y_max)

def esc(value):
    return html.escape(str(value))

parts = [
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
    '<rect width="100%" height="100%" fill="white"/>',
    '<style>text{font-family:Arial,sans-serif;fill:#222}'
    '.grid{stroke:#dedede;stroke-width:1}'
    '.axis{stroke:#555;stroke-width:1.5}'
    '.latency{fill:none;stroke:#2878b5;stroke-width:2}'
    '.event{stroke-width:2;stroke-dasharray:7 5}</style>',
]

ok_count = sum(1 for p in points if p["success"])
fail_count = len(points) - ok_count
api_deletes = sum(1 for e in events if e.get("component") == "api")
worker_deletes = sum(1 for e in events if e.get("component") == "worker")

parts.append(
    f'<text x="{LEFT}" y="38" font-size="25" font-weight="bold">'
    'Chaos test: доступность API при удалении Pod</text>'
)
parts.append(
    f'<text x="{LEFT}" y="68" font-size="16">'
    f'Запросов: {len(points)} | Успешных: {ok_count} | Ошибок: {fail_count} '
    f'| Удалений API: {api_deletes} | Worker: {worker_deletes}</text>'
)

# Линии сетки и шкала задержки
for i in range(6):
    value = y_max * i / 5
    y = y_pos(value)
    parts.append(
        f'<line class="grid" x1="{LEFT}" y1="{y:.1f}" '
        f'x2="{LEFT + PW}" y2="{y:.1f}"/>'
    )
    parts.append(
        f'<text x="{LEFT - 12}" y="{y + 5:.1f}" '
        f'text-anchor="end" font-size="13">{value:.0f}</text>'
    )

# Ось времени
for i in range(6):
    fraction = i / 5
    x = LEFT + PW * fraction
    t = start.timestamp() + duration * fraction
    label = datetime.fromtimestamp(t, tz=start.tzinfo).strftime("%H:%M:%S")
    parts.append(
        f'<line class="grid" x1="{x:.1f}" y1="{TOP}" '
        f'x2="{x:.1f}" y2="{Y_BOTTOM}"/>'
    )
    parts.append(
        f'<text x="{x:.1f}" y="{Y_BOTTOM + 25}" '
        f'text-anchor="middle" font-size="13">{label}</text>'
    )

parts.append(
    f'<line class="axis" x1="{LEFT}" y1="{TOP}" '
    f'x2="{LEFT}" y2="{Y_BOTTOM}"/>'
)
parts.append(
    f'<line class="axis" x1="{LEFT}" y1="{Y_BOTTOM}" '
    f'x2="{LEFT + PW}" y2="{Y_BOTTOM}"/>'
)
parts.append(
    f'<text x="28" y="{TOP + PH / 2}" font-size="15" '
    f'text-anchor="middle" transform="rotate(-90 28 {TOP + PH / 2})">'
    'Задержка, мс</text>'
)
parts.append(
    f'<text x="{LEFT + PW / 2}" y="{Y_BOTTOM + 55}" '
    f'text-anchor="middle" font-size="15">Время</text>'
)

# Вертикальные отметки удаления Pod
event_counts = Counter()
for event in events:
    try:
        t = parse_time(event["timestamp_utc"])
    except (ValueError, KeyError):
        continue

    if not (start <= t <= end):
        continue

    component = event.get("component", "unknown")
    color = "#d97706" if component == "api" else "#7c3aed"
    x = x_pos(t)
    label_y = TOP - (12 if event_counts[component] % 2 == 0 else 30)
    event_counts[component] += 1

    parts.append(
        f'<line class="event" x1="{x:.1f}" y1="{TOP}" '
        f'x2="{x:.1f}" y2="{Y_BOTTOM}" stroke="{color}"/>'
    )
    parts.append(
        f'<text x="{x + 3:.1f}" y="{label_y}" font-size="13" '
        f'fill="{color}" style="fill:{color}">{esc(component.upper())}</text>'
    )

# Линия задержки и точки запросов
line_points = [
    f'{x_pos(p["time"]):.1f},{y_pos(p["latency"]):.1f}'
    for p in points if p["latency"] is not None
]
if line_points:
    parts.append(
        f'<polyline class="latency" points="{" ".join(line_points)}"/>'
    )

for p in points:
    if p["latency"] is None:
        continue
    x, y = x_pos(p["time"]), y_pos(p["latency"])
    color = "#15803d" if p["success"] else "#dc2626"
    radius = 3 if p["success"] else 6
    parts.append(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{color}"/>'
    )

# Легенда
legend_y = H - 48
parts.extend([
    f'<line x1="{LEFT}" y1="{legend_y}" x2="{LEFT + 32}" y2="{legend_y}" stroke="#2878b5" stroke-width="3"/>',
    f'<text x="{LEFT + 40}" y="{legend_y + 5}" font-size="14">Задержка запроса</text>',
    f'<line x1="{LEFT + 250}" y1="{legend_y}" x2="{LEFT + 282}" y2="{legend_y}" stroke="#d97706" stroke-width="2" stroke-dasharray="7 5"/>',
    f'<text x="{LEFT + 290}" y="{legend_y + 5}" font-size="14">Удаление API</text>',
    f'<line x1="{LEFT + 450}" y1="{legend_y}" x2="{LEFT + 482}" y2="{legend_y}" stroke="#7c3aed" stroke-width="2" stroke-dasharray="7 5"/>',
    f'<text x="{LEFT + 490}" y="{legend_y + 5}" font-size="14">Удаление Worker</text>',
    f'<circle cx="{LEFT + 700}" cy="{legend_y}" r="5" fill="#dc2626"/>',
    f'<text x="{LEFT + 712}" y="{legend_y + 5}" font-size="14">Неуспешный запрос</text>',
    '</svg>',
])

OUT_FILE.write_text("\n".join(parts), encoding="utf-8")

print(f"График создан: {OUT_FILE}")
print(f"Успешных запросов: {ok_count}/{len(points)}")
print(f"Неуспешных запросов: {fail_count}")
if failed:
    print("Есть неуспешные запросы — проверь reports/chaos-client.csv")
