FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=8000
ENV OTEL_SERVICE_NAME=api
ENV OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4317

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]