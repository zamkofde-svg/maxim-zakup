"""
Разведчик Google Drive: показывает всё что видит наш service account.
Помогает понять, какие папки расшарены и какие файлы доступны.

Запуск: python3 drive_explorer.py
Требует: ~/.config/maxim-zakup/sa.json
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build

SA_PATH = Path.home() / ".config" / "maxim-zakup" / "sa.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def get_service():
    env_content = os.environ.get("GOOGLE_SA_JSON_CONTENT")
    if env_content:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(env_content), scopes=SCOPES
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            str(SA_PATH), scopes=SCOPES
        )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_all(service, page_size=200):
    """Перечисляет всё к чему есть доступ — папки и файлы."""
    items = []
    page_token = None
    while True:
        resp = service.files().list(
            pageSize=page_size,
            pageToken=page_token,
            fields="nextPageToken, files(id, name, mimeType, parents, owners(emailAddress), modifiedTime, size)",
            # включаем shared with me + drives
            corpora="allDrives",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def main():
    if not SA_PATH.exists():
        raise SystemExit(f"Нет ключа: {SA_PATH}")

    print(f"Service account: проверяем доступ через {SA_PATH}\n")
    svc = get_service()

    # Кто я (вернёт email service account)
    about = svc.about().get(fields="user(emailAddress)").execute()
    print(f"Я подключился как: {about['user']['emailAddress']}\n")

    items = list_all(svc)
    print(f"Всего видимых элементов: {len(items)}\n")

    folders = [i for i in items if i["mimeType"] == "application/vnd.google-apps.folder"]
    files = [i for i in items if i["mimeType"] != "application/vnd.google-apps.folder"]

    print(f"=== ПАПКИ ({len(folders)}) ===")
    for f in folders:
        owner = (f.get("owners") or [{}])[0].get("emailAddress", "?")
        print(f"  📁 {f['name']}")
        print(f"      id: {f['id']}")
        print(f"      владелец: {owner}")

    print(f"\n=== ФАЙЛЫ ({len(files)}) ===")
    # Группируем по типу для читаемости
    sheets = [f for f in files if f["mimeType"] == "application/vnd.google-apps.spreadsheet"]
    xlsx = [f for f in files if "spreadsheetml" in f["mimeType"]]
    other = [f for f in files if f not in sheets and f not in xlsx]

    print(f"\nGoogle Sheets ({len(sheets)}):")
    for f in sheets:
        size = f.get("size", "—")
        print(f"  📊 {f['name']}  id={f['id'][:30]}  modified={f.get('modifiedTime', '?')[:10]}")

    if xlsx:
        print(f"\nExcel xlsx ({len(xlsx)}):")
        for f in xlsx:
            print(f"  📊 {f['name']}  size={f.get('size', '?')}")

    if other:
        print(f"\nДругое ({len(other)}):")
        for f in other[:20]:
            print(f"  📄 {f['name']}  type={f['mimeType']}")


if __name__ == "__main__":
    main()
