FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin batch

WORKDIR /app
COPY batch.py /app/batch.py

USER 10001:10001

CMD ["python", "/app/batch.py"]
