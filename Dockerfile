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

# HEALTHCHECK НЕ ставим — Timeweb проверяет своим механизмом,
# а наш docker HEALTHCHECK может конфликтовать с ним и убивать контейнер.

CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
