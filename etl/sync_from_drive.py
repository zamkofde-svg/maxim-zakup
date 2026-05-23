"""
Синхронизация файлов из Google Drive в локальную папку sample-data/.

Service account имеет read access на папку «Закупка». Скачиваем все
xlsx-файлы и Google Sheets (как xlsx) — это будет «снимок» для ETL.

Запуск: python3 sync_from_drive.py
Требует: ~/.config/maxim-zakup/sa.json
"""
from __future__ import annotations
import io
import json
import os
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SA_PATH = Path.home() / ".config" / "maxim-zakup" / "sa.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
# В облаке — DRIVE_SYNC_DIR через env (/tmp/drive-sync). Локально — sample-data/drive-sync
OUT_DIR = Path(os.environ.get("DRIVE_SYNC_DIR", str(Path(__file__).parent.parent / "sample-data" / "drive-sync")))


def _load_credentials():
    """Грузим creds: сначала из env var GOOGLE_SA_JSON_CONTENT (для облака),
    потом из файла ~/.config/maxim-zakup/sa.json (для локалки)."""
    env_content = os.environ.get("GOOGLE_SA_JSON_CONTENT")
    if env_content:
        info = json.loads(env_content)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if SA_PATH.exists():
        return service_account.Credentials.from_service_account_file(str(SA_PATH), scopes=SCOPES)
    raise RuntimeError("Нет credentials: ни GOOGLE_SA_JSON_CONTENT env, ни файла sa.json")


def safe_filename(name: str) -> str:
    """Приводит имя файла к safe-варианту для файловой системы."""
    return "".join(c if c.isalnum() or c in " -_.()" else "_" for c in name).strip()


def get_service():
    creds = _load_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def download_file(service, file_id: str, name: str, mime: str, out_path: Path) -> int:
    """Скачивает файл. Возвращает размер в байтах."""
    if mime == "application/vnd.google-apps.spreadsheet":
        # Google Sheets → экспорт в xlsx
        request = service.files().export_media(
            fileId=file_id,
            mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        if not out_path.suffix == ".xlsx":
            out_path = out_path.with_suffix(".xlsx")
    else:
        # Обычный файл (xlsx и т.д.)
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)

    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    data = fh.getvalue()
    out_path.write_bytes(data)
    return len(data)


FACTS_SUBFOLDER_NAME = "Факты iiko-SH"
FACTS_OUT_DIR = OUT_DIR / "facts"


def sync():
    """Скачивает все файлы из Drive (матрицы + мастер + карту) → OUT_DIR.
    Отдельно скачивает выгрузки факта из подпапки «Факты iiko-SH» → OUT_DIR/facts.
    Возвращает счётчик."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FACTS_OUT_DIR.mkdir(parents=True, exist_ok=True)
    svc = get_service()

    # 1. Все файлы и папки
    resp = svc.files().list(
        pageSize=200,
        fields="files(id, name, mimeType, modifiedTime, parents)",
        corpora="allDrives",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute()
    all_items = resp.get("files", [])

    # Находим подпапку «Факты iiko-SH»
    facts_folder = next(
        (f for f in all_items
         if f["mimeType"] == "application/vnd.google-apps.folder" and f["name"] == FACTS_SUBFOLDER_NAME),
        None
    )
    facts_folder_id = facts_folder["id"] if facts_folder else None

    total_bytes = 0
    files_main = 0
    files_facts = 0

    for f in all_items:
        if f["mimeType"] == "application/vnd.google-apps.folder":
            continue

        parents = f.get("parents", [])
        is_in_facts = facts_folder_id and (facts_folder_id in parents)

        out_name = safe_filename(f["name"])
        # Факты могут быть .xls (StoreHouse XML) и .xlsx (iiko) — сохраняем оригинальное расширение
        # Если файл — Google Sheet, экспортируется как .xlsx
        if f["mimeType"] == "application/vnd.google-apps.spreadsheet":
            if not out_name.endswith(".xlsx"):
                out_name += ".xlsx"
        # для остальных оставляем как есть (.xls / .xlsx)
        elif not (out_name.endswith(".xlsx") or out_name.endswith(".xls")):
            out_name += ".xlsx"

        if is_in_facts:
            out_path = FACTS_OUT_DIR / out_name
            files_facts += 1
        else:
            out_path = OUT_DIR / out_name
            files_main += 1

        total_bytes += download_file(svc, f["id"], f["name"], f["mimeType"], out_path)

    return {
        "files_main": files_main,
        "files_facts": files_facts,
        "bytes": total_bytes,
        "facts_folder_found": bool(facts_folder),
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    svc = get_service()

    # Перечисляем всё что видим
    resp = svc.files().list(
        pageSize=200,
        fields="files(id, name, mimeType, modifiedTime)",
        corpora="allDrives",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        q="mimeType != 'application/vnd.google-apps.folder'",
    ).execute()
    files = resp.get("files", [])

    print(f"Найдено файлов: {len(files)}")
    print(f"Папка выгрузки: {OUT_DIR}\n")

    total_bytes = 0
    for f in files:
        out_name = safe_filename(f["name"])
        if not out_name.endswith(".xlsx"):
            out_name += ".xlsx"
        out_path = OUT_DIR / out_name

        size = download_file(svc, f["id"], f["name"], f["mimeType"], out_path)
        total_bytes += size
        print(f"  ✓ {out_name:<45} {size:>8} bytes  modified={f.get('modifiedTime', '?')[:10]}")

    print(f"\nИтого: {len(files)} файлов, {total_bytes:,} байт")


if __name__ == "__main__":
    main()
