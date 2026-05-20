"""
Синхронизация файлов из Google Drive в локальную папку sample-data/.

Service account имеет read access на папку «Закупка». Скачиваем все
xlsx-файлы и Google Sheets (как xlsx) — это будет «снимок» для ETL.

Запуск: python3 sync_from_drive.py
Требует: ~/.config/maxim-zakup/sa.json
"""
from __future__ import annotations
import io
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SA_PATH = Path.home() / ".config" / "maxim-zakup" / "sa.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
OUT_DIR = Path(__file__).parent.parent / "sample-data" / "drive-sync"


def safe_filename(name: str) -> str:
    """Приводит имя файла к safe-варианту для файловой системы."""
    return "".join(c if c.isalnum() or c in " -_.()" else "_" for c in name).strip()


def get_service():
    creds = service_account.Credentials.from_service_account_file(
        str(SA_PATH), scopes=SCOPES
    )
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
