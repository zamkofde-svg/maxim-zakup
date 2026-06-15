"""
Прописывает заголовок «Комментарий» в ячейку F1 каждой вкладки каждой матрицы
поставщика. Заодно ставит чуть-чуть форматирования (жирный + серый фон) —
чтобы поставщик видел: вот сюда можно вписывать страну, фасовку и т.п.

Парсер парсит эту колонку как PriceQuote.supplier_comment и отображает в Топ-2
под именем поставщика.

Колонка F уже входит в редактируемую зону защиты (она проставляется скриптом
lock_supplier_matrices.py — там unprotectedRanges B2..F2000).

Запуск:
    python -m etl.add_comment_header
    python -m etl.add_comment_header --dry   # превью
"""
from __future__ import annotations
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from sync_master_to_suppliers import (  # noqa
    _get_services, _list_files, _is_supplier_file, _execute_with_retry,
)

HEADER_TEXT = "Комментарий"
COL_F_INDEX = 5  # 0-based: A=0, B=1, ..., F=5


def process_file(sheets, file_id: str, file_name: str, dry_run: bool = False) -> int:
    """Возвращает число вкладок где поставили шапку."""
    meta = _execute_with_retry(
        sheets.spreadsheets().get(spreadsheetId=file_id, fields="sheets.properties"),
        label=f"meta({file_name})",
    )
    titles = [s["properties"]["title"] for s in meta["sheets"]]
    sheet_id_by_title = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

    # Прочитать текущие значения F1 у всех вкладок одним запросом
    ranges = [f"'{t}'!F1" for t in titles]
    resp = _execute_with_retry(
        sheets.spreadsheets().values().batchGet(spreadsheetId=file_id, ranges=ranges),
        label=f"read F1({file_name})",
    )
    need_update = []  # титулы вкладок где F1 != "Комментарий"
    for vr in resp.get("valueRanges", []):
        title = vr.get("range", "").split("!")[0].strip("'")
        values = vr.get("values", [])
        cur = values[0][0] if values and values[0] else ""
        if str(cur).strip().lower() != HEADER_TEXT.lower():
            need_update.append(title)

    if not need_update:
        return 0
    if dry_run:
        return len(need_update)

    # Запишем "Комментарий" одним batchUpdate по values
    data = [{"range": f"'{t}'!F1", "values": [[HEADER_TEXT]]} for t in need_update]
    _execute_with_retry(
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=file_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ),
        label=f"write F1({file_name})",
    )

    # Лёгкое форматирование — жирный, серый фон, как у других хедеров.
    fmt_requests = []
    for t in need_update:
        sid = sheet_id_by_title[t]
        fmt_requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": COL_F_INDEX, "endColumnIndex": COL_F_INDEX + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.95},
                    }
                },
                "fields": "userEnteredFormat(textFormat,backgroundColor)",
            }
        })
    if fmt_requests:
        _execute_with_retry(
            sheets.spreadsheets().batchUpdate(spreadsheetId=file_id, body={"requests": fmt_requests}),
            label=f"format F1({file_name})",
        )
    return len(need_update)


def main():
    dry = "--dry" in sys.argv or "--dry-run" in sys.argv
    drive, sheets = _get_services()
    files = _list_files(drive)
    suppliers = [
        f for f in files
        if _is_supplier_file(f["name"])
        and f["mimeType"] == "application/vnd.google-apps.spreadsheet"
    ]
    print(f"Поставщиков: {len(suppliers)}{'  (DRY-RUN)' if dry else ''}")
    total = 0
    for sup in sorted(suppliers, key=lambda x: x["name"]):
        try:
            n = process_file(sheets, sup["id"], sup["name"], dry_run=dry)
            mark = "✓" if n else "·"
            print(f"  {mark} {sup['name']:35s} вкладок проставлено: {n}")
            total += n
        except Exception as e:
            print(f"  ✗ {sup['name']}: {type(e).__name__}: {str(e)[:120]}")
    print(f"\nИтого вкладок: {total}")


if __name__ == "__main__":
    main()
