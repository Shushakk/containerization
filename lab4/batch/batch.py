import os

TARGET_MB = int(os.getenv("BATCH_MEMORY_MB", "64"))
CHUNK_MB = 4

# Выделяем заданный объём памяти и записываем в каждую страницу,
# чтобы память действительно использовалась.
blocks = []
allocated_mb = 0

while allocated_mb < TARGET_MB:
    size_mb = min(CHUNK_MB, TARGET_MB - allocated_mb)
    block = bytearray(size_mb * 1024 * 1024)

    for offset in range(0, len(block), 4096):
        block[offset] = 1

    blocks.append(block)
    allocated_mb += size_mb

print(
    f"Batch started: allocated={allocated_mb} MiB; CPU loop active",
    flush=True,
)

# Бесконечный вычислительный цикл: полезную работу не выполняет.
value = 1
while True:
    value = (value * 1664525 + 1013904223) & 0xFFFFFFFF
