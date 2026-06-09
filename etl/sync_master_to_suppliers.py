"""
Распространение мастер-матрицы на матрицы всех поставщиков.

Режимы:
- prune=False (по умолчанию, БЕЗОПАСНО):
    * создаёт у поставщика недостающие вкладки из его whitelist (если такая вкладка есть в мастере),
    * дописывает недостающие позиции,
    * ничего не удаляет (цены поставщика не трогаем).

- prune=True (ПРИВЕДЕНИЕ К ЕДИНОМУ ВИДУ):
    * всё что выше, и плюс:
    * удаляет у поставщика вкладки, которые есть в мастере, но НЕ в whitelist этого поставщика,
    * удаляет у поставщика строки, которых нет в мастере (внутри whitelisted вкладок).
    * Чужие вкладки (которых нет в мастере вовсе) не трогает — это страховка от случайной потери данных.
    * Цены не трогаем явно (мы оперируем только колонкой A для сравнения; удаляем целые строки).

Имена вкладок берём ТОЛЬКО из Sheets API (мастер тоже читается через Sheets API),
потому что xlsx-экспорт Google вырезает '/' из имён ("Овощи/фрукты" → "Овощифрукты")
и это раньше ломало сравнение мастер↔поставщик.
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build

SA_PATH = Path.home() / ".config" / "maxim-zakup" / "sa.json"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

MASTER_FILENAME = "Матрица(для изменения позиций)"
NON_SUPPLIER_NAMES = {
    MASTER_FILENAME,
    "Карта сопоставлений", "Топ 2", "Сопоставление", "Сводная",
}
SUPPLIER_PREFIXES = ("ООО ", "АО ", "ИП ", "ПАО ", "ЗАО ")


# ========== WHITELIST ВКЛАДОК ПО ПОСТАВЩИКАМ ==========
# Какие вкладки оставлять у каждого поставщика. Имена сравниваются по нормализации _title_key
# (нижний регистр, без пробелов и '/'), чтобы "Мука/смеси" и "Мукасмеси" считались одним.
# Если поставщика нет в этой карте — берётся ALL_MASTER (т.е. все вкладки мастера разрешены).
SUPPLIER_SHEET_WHITELIST: dict[str, list[str]] = {
    'АО Группа "ЮФС"':              ["Рыба и морепродукты", "Ягода см", "Мясо"],
    "ИП Трусов Дмитрий Сергеевич":  ["Мясо"],
    "ООО Восток-запад":             ["Молочка", "Мясо", "Рыба и морепродукты", "Сыры",
                                     "Ягода см", "Бакалея", "Консервация", "Мука/смеси",
                                     "макароны", "Шоколад"],
    "ООО Метро Кэш энд Керри":      ["Молочка", "Мясо", "Рыба и морепродукты", "Сыры",
                                     "Ягода см", "Бакалея", "Консервация", "Мука/смеси",
                                     "макароны"],
    "ООО ЕвроСиб-Трейд":            ["Молочка", "Сыры"],
    "ООО Мираторг ТК":              ["Мясо"],
    "ООО Орбита и К":               ["Молочка", "Сыры", "Бакалея", "Консервация",
                                     "Мука/смеси", "макароны"],
    "ООО Тюменьмолоко":             ["Молочка", "Сыры"],
    "ООО УРАЛ ФУД":                 ["Молочка", "Сыры"],
}


def _load_credentials():
    env_content = os.environ.get("GOOGLE_SA_JSON_CONTENT")
    if env_content:
        info = json.loads(env_content)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if SA_PATH.exists():
        return service_account.Credentials.from_service_account_file(str(SA_PATH), scopes=SCOPES)
    raise RuntimeError("Нет credentials: ни GOOGLE_SA_JSON_CONTENT env, ни файла sa.json")


def _norm(s) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s).strip()).lower().replace('"', "").replace("«", "").replace("»", "")


def _title_key(t: str) -> str:
    """Ключ нормализации имён вкладок (для сравнения 'Мука/смеси' ≈ 'Мукасмеси')."""
    return (t or "").lower().replace("/", "").replace(" ", "")


def _get_services():
    creds = _load_credentials()
    return (
        build("drive", "v3", credentials=creds, cache_discovery=False),
        build("sheets", "v4", credentials=creds, cache_discovery=False),
    )


def _list_files(drive) -> list[dict]:
    """Все файлы, видимые service account."""
    resp = drive.files().list(
        pageSize=200,
        fields="files(id, name, mimeType)",
        corpora="allDrives",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        q="mimeType != 'application/vnd.google-apps.folder'",
    ).execute()
    return resp.get("files", [])


def _is_supplier_file(name: str) -> bool:
    if name in NON_SUPPLIER_NAMES:
        return False
    return any(name.startswith(p) for p in SUPPLIER_PREFIXES)


def _read_sheets(sheets, spreadsheet_id: str, sheet_titles: list[str]) -> dict[str, list[tuple[int, str, str]]]:
    """Один batchGet по списку вкладок. Возвращает {title: [(row_idx_1based, orig, norm), ...]}.
    Шапка (строка 1) и строки начинающиеся с 'Наименование' пропускаются."""
    if not sheet_titles:
        return {}
    ranges = [f"'{t}'!A1:A2000" for t in sheet_titles]
    resp = sheets.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id, ranges=ranges
    ).execute()
    result = {}
    for vr in resp.get("valueRanges", []):
        range_str = vr.get("range", "")
        title = range_str.split("!")[0].strip("'")
        values = vr.get("values", [])
        rows = []
        for idx, row in enumerate(values):
            rn = idx + 1  # 1-based
            if rn == 1:
                continue  # шапка
            if not row:
                continue
            v = row[0] if row else None
            if not v:
                continue
            s = str(v).strip()
            if not s or s.startswith("Наименование"):
                continue
            rows.append((rn, s, _norm(s)))
        result[title] = rows
    return result


def _read_master(sheets, master_id: str) -> tuple[dict[str, list[tuple[int, str, str]]], dict[str, int]]:
    """Возвращает (rows_by_title, sheet_id_by_title)."""
    meta = sheets.spreadsheets().get(spreadsheetId=master_id, fields="sheets.properties").execute()
    titles = [s["properties"]["title"] for s in meta["sheets"]]
    sheet_id_by_title = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    rows = _read_sheets(sheets, master_id, titles)
    return rows, sheet_id_by_title


def sync(dry_run: bool = False, prune: bool = False) -> dict:
    """Главная функция. См. docstring модуля."""
    drive, sheets = _get_services()
    files = _list_files(drive)
    master = next((f for f in files if f["name"] == MASTER_FILENAME), None)
    if not master:
        raise RuntimeError("Мастер-матрица не найдена в Drive")

    master_rows, _ = _read_master(sheets, master["id"])
    # master_rows: {real_title: [(rn, orig, norm), ...]}
    master_titles = list(master_rows.keys())
    master_key_to_title = {_title_key(t): t for t in master_titles}
    master_norm_by_title = {t: set(n for _, _, n in rows) for t, rows in master_rows.items()}
    master_orig_in_order = {t: [orig for _, orig, _ in rows] for t, rows in master_rows.items()}
    master_norm_in_order = {t: [n for _, _, n in rows] for t, rows in master_rows.items()}

    plan = {
        "supplier_changes": [],
        "dry_run": dry_run,
        "prune": prune,
        "total_sheets_added": 0,
        "total_sheets_deleted": 0,
        "total_rows_added": 0,
        "total_rows_deleted": 0,
    }

    supplier_files = [f for f in files if _is_supplier_file(f["name"])
                      and f["mimeType"] == "application/vnd.google-apps.spreadsheet"]

    for sup in supplier_files:
        sup_id = sup["id"]
        sup_name = sup["name"]

        # whitelist реальных мастер-имён для этого поставщика
        wl_inputs = SUPPLIER_SHEET_WHITELIST.get(sup_name, master_titles)
        wl_keys = {_title_key(t) for t in wl_inputs}
        wl_master_titles = {master_key_to_title[k] for k in wl_keys if k in master_key_to_title}

        # вкладки поставщика
        smeta = sheets.spreadsheets().get(spreadsheetId=sup_id, fields="sheets.properties").execute()
        sup_titles = [s["properties"]["title"] for s in smeta["sheets"]]
        sup_sheet_id_by_title = {s["properties"]["title"]: s["properties"]["sheetId"] for s in smeta["sheets"]}
        sup_hidden_by_title = {s["properties"]["title"]: s["properties"].get("hidden", False) for s in smeta["sheets"]}

        # сопоставляем вкладки поставщика с мастерскими по key
        sup_title_by_key = {_title_key(t): t for t in sup_titles}

        sheets_added: list[str] = []
        sheets_deleted: list[str] = []
        sheets_renamed: list[tuple[str, str]] = []
        sheets_unhidden: list[str] = []
        rows_added: dict[str, list[str]] = {}
        rows_deleted: dict[str, list[str]] = {}

        # =========== 1) УДАЛЕНИЕ ВКЛАДОК (только prune) ===========
        # Удаляем у поставщика вкладки, которые:
        #   (a) соответствуют какой-то мастер-вкладке, но НЕ в whitelist; ИЛИ
        #   (b) являются дублем разрешённой мастер-вкладки (имя отличается от мастера —
        #       например "Мукасмеси" при наличии разрешённой "Мука/смеси"). Дубль уходит.
        # Неизвестные вкладки (нет соответствия в мастере) не трогаем.
        if prune:
            del_requests = []
            # сгруппируем вкладки поставщика по _title_key, чтобы детектить задвоения
            by_key: dict[str, list[str]] = {}
            for st in sup_titles:
                k = _title_key(st)
                if k in master_key_to_title:
                    by_key.setdefault(k, []).append(st)

            rename_requests = []  # для переименования в канон
            for k, group in by_key.items():
                mt = master_key_to_title[k]
                in_wl = mt in wl_master_titles
                if not in_wl:
                    for st in group:
                        sheets_deleted.append(st)
                        del_requests.append({"deleteSheet": {"sheetId": sup_sheet_id_by_title[st]}})
                    continue
                # whitelist'нута — оставляем ровно одну, с точным именем мастера
                if mt in group:
                    canonical = mt
                    extras = [s for s in group if s != mt]
                else:
                    canonical = group[0]
                    extras = group[1:]
                for st in extras:
                    sheets_deleted.append(st)
                    del_requests.append({"deleteSheet": {"sheetId": sup_sheet_id_by_title[st]}})
                # переименовать каноническую вкладку в имя мастера, если оно отличается
                if canonical != mt:
                    rename_requests.append({"updateSheetProperties": {
                        "properties": {"sheetId": sup_sheet_id_by_title[canonical], "title": mt},
                        "fields": "title",
                    }})
                    sheets_renamed.append((canonical, mt))
            # Раскрываем все whitelisted вкладки, если они скрыты — чтобы заказчик их видел
            unhide_requests = []
            for mt in wl_master_titles:
                # ищем по любому имени (могло быть переименовано выше — но обновили sup_sheet_id_by_title только после execute)
                # на этом этапе ещё не выполнили rename, поэтому используем старое имя
                for st_name in list(sup_sheet_id_by_title.keys()):
                    if _title_key(st_name) == _title_key(mt) and sup_hidden_by_title.get(st_name):
                        unhide_requests.append({"updateSheetProperties": {
                            "properties": {"sheetId": sup_sheet_id_by_title[st_name], "hidden": False},
                            "fields": "hidden",
                        }})
                        sheets_unhidden.append(st_name)
            all_requests = del_requests + rename_requests + unhide_requests
            if all_requests and not dry_run:
                sheets.spreadsheets().batchUpdate(spreadsheetId=sup_id, body={"requests": all_requests}).execute()
            # обновим локальные структуры под переименование
            for old, new in sheets_renamed:
                if old in sup_sheet_id_by_title:
                    sup_sheet_id_by_title[new] = sup_sheet_id_by_title.pop(old)
                if old in sup_titles:
                    sup_titles[sup_titles.index(old)] = new
            # обновим локальное состояние, чтобы дальше с ним работать корректно
            for st in sheets_deleted:
                sup_titles.remove(st)
                sup_sheet_id_by_title.pop(st, None)
            sup_title_by_key = {_title_key(t): t for t in sup_titles}

        # =========== 2) СОЗДАНИЕ ОТСУТСТВУЮЩИХ WHITELIST-ВКЛАДОК ===========
        for mt in wl_master_titles:
            if _title_key(mt) in sup_title_by_key:
                continue  # уже есть
            # создаём вкладку с тем же именем, что в мастере, и заливаем шапку + позиции
            if not dry_run:
                resp_add = sheets.spreadsheets().batchUpdate(
                    spreadsheetId=sup_id,
                    body={"requests": [{"addSheet": {"properties": {"title": mt}}}]},
                ).execute()
                new_sheet_id = resp_add["replies"][0]["addSheet"]["properties"]["sheetId"]
                values_to_write = [["Наименование"]] + [[o] for o in master_orig_in_order[mt]]
                sheets.spreadsheets().values().update(
                    spreadsheetId=sup_id,
                    range=f"'{mt}'!A1",
                    valueInputOption="USER_ENTERED",
                    body={"values": values_to_write},
                ).execute()
                sup_sheet_id_by_title[mt] = new_sheet_id
                sup_titles.append(mt)
            sheets_added.append(mt)
            rows_added[mt] = list(master_orig_in_order[mt])

        sup_title_by_key = {_title_key(t): t for t in sup_titles}

        # =========== 3) ЧТЕНИЕ СУЩЕСТВУЮЩИХ WHITELIST-ВКЛАДОК ПОСТАВЩИКА ===========
        # читать нужно для тех вкладок поставщика, чей key есть в whitelist (и не были только что созданы)
        existing_to_read = [
            sup_title_by_key[_title_key(mt)]
            for mt in wl_master_titles
            if _title_key(mt) in sup_title_by_key and mt not in sheets_added
        ]
        sup_rows_by_real_title = _read_sheets(sheets, sup_id, existing_to_read) if existing_to_read else {}

        # =========== 4) ДОПИСЫВАНИЕ И УДАЛЕНИЕ ПОЗИЦИЙ ===========
        for mt in wl_master_titles:
            if mt in sheets_added:
                continue  # только что создали с полным набором
            sup_t = sup_title_by_key.get(_title_key(mt))
            if not sup_t:
                continue  # такого не должно быть, но защитимся
            sup_rows = sup_rows_by_real_title.get(sup_t, [])
            sup_norm_set = set(n for _, _, n in sup_rows)
            master_set = master_norm_by_title[mt]

            # 4a) дописываем недостающее (в порядке мастера, без дублей)
            missing = []
            already = set(sup_norm_set)
            for orig, n in zip(master_orig_in_order[mt], master_norm_in_order[mt]):
                if n in already:
                    continue
                already.add(n)
                missing.append(orig)
            if missing:
                if not dry_run:
                    sheets.spreadsheets().values().append(
                        spreadsheetId=sup_id,
                        range=f"'{sup_t}'!A:A",
                        valueInputOption="USER_ENTERED",
                        insertDataOption="INSERT_ROWS",
                        body={"values": [[m] for m in missing]},
                    ).execute()
                rows_added[mt] = missing

            # 4b) удаление лишнего (только prune): строки поставщика чей norm не в master_set
            if prune:
                extra = [(rn, orig) for (rn, orig, n) in sup_rows if n not in master_set]
                if extra:
                    if not dry_run:
                        sid = sup_sheet_id_by_title[sup_t]
                        del_req = []
                        for rn, _ in sorted(extra, key=lambda x: x[0], reverse=True):
                            del_req.append({"deleteDimension": {"range": {
                                "sheetId": sid, "dimension": "ROWS",
                                "startIndex": rn - 1, "endIndex": rn,
                            }}})
                        sheets.spreadsheets().batchUpdate(spreadsheetId=sup_id, body={"requests": del_req}).execute()
                    rows_deleted[mt] = [orig for _, orig in extra]

        if sheets_added or sheets_deleted or sheets_renamed or sheets_unhidden or rows_added or rows_deleted:
            plan["supplier_changes"].append({
                "supplier": sup_name,
                "sheets_added": sheets_added,
                "sheets_deleted": sheets_deleted,
                "sheets_renamed": sheets_renamed,
                "sheets_unhidden": sheets_unhidden,
                "rows_added": rows_added,
                "rows_added_count": sum(len(v) for v in rows_added.values()),
                "rows_deleted": rows_deleted,
                "rows_deleted_count": sum(len(v) for v in rows_deleted.values()),
            })
            plan["total_sheets_added"] += len(sheets_added)
            plan["total_sheets_deleted"] += len(sheets_deleted)
            plan["total_rows_added"] += sum(len(v) for v in rows_added.values())
            plan["total_rows_deleted"] += sum(len(v) for v in rows_deleted.values())

    return plan


def main():
    import sys
    dry = "--dry-run" in sys.argv
    prune = "--prune" in sys.argv
    print(f"== sync master → suppliers (dry_run={dry}, prune={prune}) ==")
    result = sync(dry_run=dry, prune=prune)
    print(f"\nИтого:")
    print(f"  Вкладок добавлено:  {result['total_sheets_added']}")
    print(f"  Вкладок удалено:    {result.get('total_sheets_deleted', 0)}")
    print(f"  Строк добавлено:    {result['total_rows_added']}")
    print(f"  Строк удалено:      {result.get('total_rows_deleted', 0)}")
    print(f"  Поставщиков затронуто: {len(result['supplier_changes'])}")
    for ch in result["supplier_changes"]:
        print(f"\n  → {ch['supplier']}")
        if ch.get("sheets_added"):
            print(f"     [+ вкладки]: {ch['sheets_added']}")
        if ch.get("sheets_deleted"):
            print(f"     [- вкладки]: {ch['sheets_deleted']}")
        if ch.get("sheets_renamed"):
            for old, new in ch["sheets_renamed"]:
                print(f"     [~ переименована] '{old}' → '{new}'")
        for sheet, names in ch.get("rows_added", {}).items():
            print(f"     [+ '{sheet}'] {len(names)}: {names[:3]}{'...' if len(names) > 3 else ''}")
        for sheet, names in ch.get("rows_deleted", {}).items():
            print(f"     [- '{sheet}'] {len(names)}: {names[:3]}{'...' if len(names) > 3 else ''}")


if __name__ == "__main__":
    main()
