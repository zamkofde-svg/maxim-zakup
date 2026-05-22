# Maxim Zakup — single-container app: ETL + FastAPI + статичный фронт

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ /app/backend/
COPY etl/ /app/etl/
COPY prototype/ /app/prototype/

# БД и кеш Drive-снапшота в /tmp (write-safe в любом контейнере)
ENV DB_PATH=/tmp/data.db
ENV DRIVE_SYNC_DIR=/tmp/drive-sync
RUN mkdir -p /tmp/drive-sync && chmod 777 /tmp/drive-sync

ENV PYTHONUNBUFFERED=1

# Один EXPOSE — Timeweb берёт его как target для проксирования
EXPOSE 8000

# CMD — слушаем на $PORT если задан (некоторые платформы пробрасывают свой),
# иначе на 8000. И на 0.0.0.0 (все интерфейсы)
CMD ["sh", "-c", "echo \"[boot] starting uvicorn on 0.0.0.0:${PORT:-8000}\" && uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
