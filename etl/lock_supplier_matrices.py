"""
Защищает матрицы поставщиков: поставщик может вводить ТОЛЬКО цены (B..F).
Всё остальное (колонка A с наименованиями, шапка, добавление/удаление строк
и вкладок) — заблокировано через нативный механизм Google Sheets
«Защищённые диапазоны».

Алгоритм:
  Для каждой матрицы поставщика:
    Для каждой вкладки:
      1) Удаляем все ранее установленные protectedRanges (чтобы не дублировать).
      2) Добавляем ОДИН protectedRange: вся вкладка защищена, КРОМЕ B2..F2000
         (это «зона цен»). 2000 — большой запас, чтобы новые добавляемые
         мастер-позиции попадали в редактируемую зону автоматически.

Service Account при этом остаётся редактором — кнопка «Привести к мастеру»
по-прежнему может добавлять/удалять строки.

Запуск:
    python -m etl.lock_supplier_matrices             # реальная защита
    python -m etl.lock_supplier_matrices --dry       # превью (что сделается)
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

# Большой запас на новые строки. У нас max ~200 позиций в категории — 2000 хватит надолго.
PRICE_ZONE_END_ROW = 2000  # endRowIndex (exclusive). Покрывает строки 2..2000.
PRICE_ZONE_START_COL = 1   # колонка B (0-based, inclusive)
PRICE_ZONE_END_COL = 6     # колонка G (0-based, exclusive) → защищаем не-цены вне B..F


def _build_protect_request(sheet_id: int):
    """Defence-in-depth: защищаем ВСЁ кроме «зоны цен» B2..F<N>."""
    return {
        "addProtectedRange": {
            "protectedRange": {
                "range": {"sheetId": sheet_id},  # вся вкладка
                "description": "Только МАКСИМ. Поставщик вводит только цены (B..F).",
                "warningOnly": False,
                "unprotectedRanges": [
                    {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,  # со строки 2
                        "endRowIndex": PRICE_ZONE_END_ROW,
                        "startColumnIndex": PRICE_ZONE_START_COL,
                        "endColumnIndex": PRICE_ZONE_END_COL,
                    }
                ],
            }
        }
    }


def lock_one(sheets, file_id: str, file_name: str, dry_run: bool = False) -> dict:
    """Защищает все вкладки одного файла."""
    # Текущие protectedRanges и список вкладок
    meta = _execute_with_retry(
        sheets.spreadsheets().get(
            spreadsheetId=file_id,
            fields="sheets(properties.sheetId,properties.title,protectedRanges)",
        ),
        label=f"meta({file_name})",
    )
    requests = []
    info = {"sheets": [], "removed_old": 0, "added_new": 0}
    for s in meta["sheets"]:
        props = s["properties"]
        sheet_id = props["sheetId"]
        title = props["title"]
        # Сносим все старые protectedRanges на этой вкладке (если есть) — чтобы не плодить
        for pr in s.get("protectedRanges", []) or []:
            requests.append({"deleteProtectedRange": {"protectedRangeId": pr["protectedRangeId"]}})
            info["removed_old"] += 1
        # Ставим свежую защиту
        requests.append(_build_protect_request(sheet_id))
        info["added_new"] += 1
        info["sheets"].append(title)

    if dry_run or not requests:
        return info
    _execute_with_retry(
        sheets.spreadsheets().batchUpdate(spreadsheetId=file_id, body={"requests": requests}),
        label=f"protect({file_name})",
    )
    return info


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
    total_added = 0
    total_removed = 0
    errors = 0
    for sup in sorted(suppliers, key=lambda x: x["name"]):
        try:
            r = lock_one(sheets, sup["id"], sup["name"], dry_run=dry)
            print(f"  ✓ {sup['name']:35s} вкладок:{len(r['sheets']):2d}  старых защит-:{r['removed_old']:2d}  новых защит+:{r['added_new']:2d}")
            total_added += r["added_new"]
            total_removed += r["removed_old"]
        except Exception as e:
            print(f"  ✗ {sup['name']}: {type(e).__name__}: {str(e)[:120]}")
            errors += 1
    print(f"\nИтого: добавлено защит {total_added}, удалено старых {total_removed}, ошибок {errors}")


if __name__ == "__main__":
    main()
