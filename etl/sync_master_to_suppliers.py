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
import random
import re
import time
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


def _execute_with_retry(request, *, max_attempts: int = 7, label: str = ""):
    """Выполняет request.execute() с ретраем на 429/500/503 с экспоненциальным backoff.
    Защита от 'Write requests per minute per user' = 60 у Google Sheets."""
    last_err = None
    for attempt in range(max_attempts):
        try:
            return request.execute()
        except HttpError as e:
            status = getattr(e.resp, "status", 0)
            if status in (429, 500, 502, 503, 504):
                # ждём 2^attempt секунд + джиттер (до 1 сек)
                delay = (2 ** attempt) + random.random()
                last_err = e
                print(f"[retry] {label or 'request'}: HTTP {status}, ждём {delay:.1f}с (попытка {attempt + 1}/{max_attempts})", flush=True)
                time.sleep(delay)
                continue
            raise
    raise last_err if last_err else RuntimeError("retry exhausted without error")

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
SUPPLIER_PREFIXES = ("ООО", "АО", "ИП", "ПАО", "ЗАО")


# ========== WHITELIST ВКЛАДОК ПО ПОСТАВЩИКАМ ==========
# По умолчанию ВСЕ поставщики получают ВСЕ вкладки мастер-матрицы. Заказчик решил
# держать единый шаблон. Если в будущем понадобится ограничить какого-то поставщика —
# допишите его сюда: {имя: [список разрешённых вкладок мастера]}.
# Имена вкладок сравниваются по нормализации _title_key (без пробелов, '/' и регистра),
# чтобы "Мука/смеси" и "Мукасмеси" считались одной вкладкой.
SUPPLIER_SHEET_WHITELIST: dict[str, list[str]] = {}


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
    resp = _execute_with_retry(drive.files().list(
        pageSize=200,
        fields="files(id, name, mimeType)",
        corpora="allDrives",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        q="mimeType != 'application/vnd.google-apps.folder'",
    ), label="drive.files.list")
    return resp.get("files", [])


def _is_supplier_file(name: str) -> bool:
    """Файл — это матрица поставщика, если имя начинается с организационно-правовой формы
    («ООО», «АО», «ИП», «ПАО», «ЗАО») и после неё идёт разделитель: пробел, кавычка или точка.
    Это ловит и стандартное «ООО Метро…», и нестандартное «ООО"АЙСБЕРГ 8"» (без пробела)."""
    if name in NON_SUPPLIER_NAMES:
        return False
    s = (name or "").strip()
    for p in SUPPLIER_PREFIXES:
        if len(s) <= len(p):
            continue
        if not s.startswith(p):
            continue
        next_ch = s[len(p)]
        if next_ch in (" ", '"', '«', "'", ".", " "):
            return True
    return False


def _read_sheets(sheets, spreadsheet_id: str, sheet_titles: list[str]) -> dict[str, list[tuple[int, str, str]]]:
    """Один batchGet по списку вкладок. Возвращает {title: [(row_idx_1based, orig, norm), ...]}.
    Шапка (строка 1) и строки начинающиеся с 'Наименование' пропускаются."""
    if not sheet_titles:
        return {}
    ranges = [f"'{t}'!A1:A2000" for t in sheet_titles]
    resp = _execute_with_retry(sheets.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id, ranges=ranges
    ), label="sheets.values.batchGet")
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
    meta = _execute_with_retry(sheets.spreadsheets().get(spreadsheetId=master_id, fields="sheets.properties"), label="sheets.get(master)")
    titles = [s["properties"]["title"] for s in meta["sheets"]]
    sheet_id_by_title = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    rows = _read_sheets(sheets, master_id, titles)
    return rows, sheet_id_by_title


def _known_supplier_norms() -> set[str]:
    """Возвращает нормализованные имена поставщиков, которые уже есть в БД.
    Используется чтобы пометить новых поставщиков в результате sync()."""
    try:
        import sys as _sys
        backend_path = str(Path(__file__).resolve().parent.parent)
        if backend_path not in _sys.path:
            _sys.path.insert(0, backend_path)
        from backend.db import SessionLocal  # type: ignore
        from backend.models import Supplier  # type: ignore
        from backend.importer import normalize as _norm_name  # type: ignore
        from sqlalchemy import select
        db = SessionLocal()
        try:
            return {s.name_normalized for s in db.execute(select(Supplier)).scalars().all()}
        finally:
            db.close()
    except Exception:
        return set()


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
        "new_suppliers": [],     # имена поставщиков, появившихся в Drive впервые
        "dry_run": dry_run,
        "prune": prune,
        "total_sheets_added": 0,
        "total_sheets_deleted": 0,
        "total_rows_added": 0,
        "total_rows_deleted": 0,
    }

    # Множество поставщиков, которые уже были известны системе. Всё что не в нём —
    # новый поставщик (заказчику покажем в финальном алерте).
    known_norms = _known_supplier_norms()

    supplier_files = [f for f in files if _is_supplier_file(f["name"])
                      and f["mimeType"] == "application/vnd.google-apps.spreadsheet"]

    # Сразу заполняем список новых поставщиков
    try:
        from backend.importer import normalize as _norm_name  # type: ignore
    except Exception:
        # fallback на простую нормализацию если бэкенда нет
        def _norm_name(s):
            return (s or "").lower().strip().replace('"', "").replace("«", "").replace("»", "")
    for f in supplier_files:
        if _norm_name(f["name"]) not in known_norms:
            plan["new_suppliers"].append(f["name"])

    for sup in supplier_files:
        sup_id = sup["id"]
        sup_name = sup["name"]

        # whitelist реальных мастер-имён для этого поставщика
        wl_inputs = SUPPLIER_SHEET_WHITELIST.get(sup_name, master_titles)
        wl_keys = {_title_key(t) for t in wl_inputs}
        wl_master_titles = {master_key_to_title[k] for k in wl_keys if k in master_key_to_title}

        # вкладки поставщика
        smeta = _execute_with_retry(sheets.spreadsheets().get(spreadsheetId=sup_id, fields="sheets.properties"), label=f"sheets.get({sup_name})")
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
                _execute_with_retry(
                    sheets.spreadsheets().batchUpdate(spreadsheetId=sup_id, body={"requests": all_requests}),
                    label=f"batchUpdate(del/rename/unhide@{sup_name})",
                )
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
                resp_add = _execute_with_retry(
                    sheets.spreadsheets().batchUpdate(
                        spreadsheetId=sup_id,
                        body={"requests": [{"addSheet": {"properties": {"title": mt}}}]},
                    ),
                    label=f"addSheet({mt}@{sup_name})",
                )
                new_sheet_id = resp_add["replies"][0]["addSheet"]["properties"]["sheetId"]
                values_to_write = [["Наименование"]] + [[o] for o in master_orig_in_order[mt]]
                _execute_with_retry(
                    sheets.spreadsheets().values().update(
                        spreadsheetId=sup_id,
                        range=f"'{mt}'!A1",
                        valueInputOption="USER_ENTERED",
                        body={"values": values_to_write},
                    ),
                    label=f"values.update({mt}@{sup_name})",
                )
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
                    _execute_with_retry(
                        sheets.spreadsheets().values().append(
                            spreadsheetId=sup_id,
                            range=f"'{sup_t}'!A:A",
                            valueInputOption="USER_ENTERED",
                            insertDataOption="INSERT_ROWS",
                            body={"values": [[m] for m in missing]},
                        ),
                        label=f"values.append({mt}@{sup_name})",
                    )
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
                        _execute_with_retry(
                            sheets.spreadsheets().batchUpdate(spreadsheetId=sup_id, body={"requests": del_req}),
                            label=f"batchUpdate(deleteRows@{sup_name})",
                        )
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
