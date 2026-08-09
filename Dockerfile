# Multi-stage Python Dockerfile for Polymarket Auto Trader
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim AS runner

WORKDIR /app

COPY --from=builder /install /usr/local

COPY . .

# Ensure data directory exists for SQLite database persistence
RUN mkdir -p /app/data

EXPOSE 8000

ENV PYTHONUNBUFFERED=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000

CMD ["python", "main.py"]
