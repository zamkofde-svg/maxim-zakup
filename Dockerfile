# Maxim Zakup — single-container app: ETL + FastAPI + статичный фронт

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости — они меняются редко (хороший кеш слой)
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Код
COPY backend/ /app/backend/
COPY etl/ /app/etl/
COPY prototype/ /app/prototype/

# БД и кеш Drive-снапшота в /tmp (write-safe в любом контейнере)
ENV DB_PATH=/tmp/data.db
ENV DRIVE_SYNC_DIR=/tmp/drive-sync
RUN mkdir -p /tmp/drive-sync && chmod 777 /tmp/drive-sync

# Не буфферим Python stdout — чтобы все print() сразу видны в логах
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Самопроверка через docker healthcheck (повышает шанс что Timeweb признает контейнер здоровым)
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
