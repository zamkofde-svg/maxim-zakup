# Maxim Zakup — single-container app: ETL + FastAPI + статичный фронт
# Подходит для Timeweb Cloud Apps, Beget VPS, Render, Yandex Cloud — любого Docker-runtime

FROM python:3.11-slim

# Системные зависимости (минимум)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости — они меняются редко (хороший кеш слой)
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Потом код
COPY backend/ /app/backend/
COPY etl/ /app/etl/
COPY prototype/ /app/prototype/

# БД и кеш Drive-снапшота в /tmp (write-safe в любом контейнере)
ENV DB_PATH=/tmp/data.db
ENV DRIVE_SYNC_DIR=/tmp/drive-sync
RUN mkdir -p /tmp/drive-sync && chmod 777 /tmp/drive-sync

# Порт жёстко 8000 — Timeweb автодетектит по EXPOSE и пробрасывает healthcheck сюда.
EXPOSE 8000

CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
