#!/bin/bash
# Ежедневный бэкап SQLite БД на VPS.
# Запускается из cron на хосте (не в контейнере) — чтобы пережить контейнер.
#
# Цепляется к volume Docker по пути /var/lib/docker/volumes/maxim-zakup_app-data/_data/data.db
# Делает «горячий» sqlite3 .backup → файл с датой → ротация: оставляем 14 ежедневных + 8 еженедельных.
#
# Установка на VPS:
#   1. Скопировать на сервер: scp scripts/backup.sh root@VPS:/opt/maxim-zakup/scripts/
#   2. chmod +x /opt/maxim-zakup/scripts/backup.sh
#   3. Добавить в cron:
#        echo "0 5 * * * /opt/maxim-zakup/scripts/backup.sh >> /var/log/maxim-backup.log 2>&1" | crontab -

set -euo pipefail

BACKUP_DIR="/opt/backups/maxim-zakup"
VOLUME_DB="/var/lib/docker/volumes/maxim-zakup_app-data/_data/data.db"
DATE=$(date +%Y-%m-%d)

mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"

# 1. Горячий бэкап через sqlite3 (БД в это время доступна на запись)
if [ ! -f "$VOLUME_DB" ]; then
    echo "[$(date)] ERROR: $VOLUME_DB не найден"
    exit 1
fi

DAILY_OUT="$BACKUP_DIR/daily/data-$DATE.db"
sqlite3 "$VOLUME_DB" ".backup $DAILY_OUT"
gzip -f "$DAILY_OUT"
echo "[$(date)] daily backup → ${DAILY_OUT}.gz ($(du -h ${DAILY_OUT}.gz | cut -f1))"

# 2. По воскресеньям копируем в weekly
if [ "$(date +%u)" = "7" ]; then
    cp "${DAILY_OUT}.gz" "$BACKUP_DIR/weekly/data-$DATE.db.gz"
    echo "[$(date)] weekly snapshot saved"
fi

# 3. Ротация: храним 14 ежедневных и 8 еженедельных
find "$BACKUP_DIR/daily" -name "*.gz" -type f -mtime +14 -delete
find "$BACKUP_DIR/weekly" -name "*.gz" -type f -mtime +56 -delete

echo "[$(date)] backup OK"
