"""
Парсер матрицы поставщика (Google Sheets / xlsx).

Вход: путь к xlsx-файлу (один поставщик)
Выход: список dict с полями: supplier, category, product, unit_type, unit_price, pkg_net, pkg_gross

Формат матрицы (по реальному шаблону «Максим»):
  - Вкладки = категории (Сыры, Молочка, ...)
  - Колонки:
    A=Наименование товара, B=Упаковка брутто, C=Стоимость (формула),
    D=Упаковка нетто, E=Цена за кг/литр (Сыры/Молочка) ИЛИ Цена упаковка
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from openpyxl import load_workbook

# Категории, где E — это цена за кг/литр; везде — за упаковку
UNIT_PRICE_CATEGORIES = {"Сыры", "Молочка"}


def _norm_str(v) -> str | None:
    """Нормализация строки: trim, схлопывание пробелов."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    # схлопываем подряд идущие пробелы
    s = re.sub(r"\s+", " ", s)
    return s


def _to_float(v) -> float | None:
    """Безопасное приведение к float, понимает '12,34' и '12.34', пропускает мусор."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


@dataclass
class PriceQuote:
    supplier: str
    category: str
    product: str        # очищенное название
    product_raw: str    # исходное название (для дебага)
    unit_type: str      # 'kg_or_l' | 'pkg'
    unit_price: float
    pkg_net: float | None
    pkg_gross: float | None
    row: int            # номер строки в файле — для дебага


def _detect_price_column(ws) -> int | None:
    """Авто-детект колонки с ценами.

    Поставщики используют разные шаблоны матрицы:
      - старые: B=упаковка, C=стоимость, D=нетто, E=цена за кг/упак
      - новые упрощённые: B='КГ'/'ШТ' (единица), C=цена
      - молочка/бакалея у некоторых: цена прямо в B

    Идея: проходим колонки B..F и считаем сколько в них числовых значений > 0
    напротив заполненной колонки A. Победителем будет колонка с максимумом.
    Если ничьей, предпочтение по приоритету E → C → B → D → F (E — самый частый
    «исторический» формат, C — типовой для новых матриц).
    """
    counts = {c: 0 for c in range(2, 7)}  # B..F
    for r in range(2, ws.max_row + 1):
        a = ws.cell(r, 1).value
        if not a:
            continue
        s = str(a).strip()
        if not s or s.startswith("Наименование"):
            continue
        for c in range(2, 7):
            v = ws.cell(r, c).value
            if v in (None, ""):
                continue
            # пробуем как число
            try:
                fv = float(str(v).replace(" ", "").replace(",", "."))
            except (TypeError, ValueError):
                continue
            if fv > 0:
                counts[c] += 1
    if max(counts.values(), default=0) == 0:
        return None
    # tie-break по приоритету
    priority = [5, 3, 2, 4, 6]  # E, C, B, D, F
    best = max(counts.values())
    for c in priority:
        if counts[c] == best:
            return c
    return None


def parse(path: str | Path, supplier_name: str) -> list[PriceQuote]:
    wb = load_workbook(path, data_only=True)  # читаем вычисленные значения формул
    quotes: list[PriceQuote] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        # Категория = имя вкладки, нормализуем регистр (некоторые поставщики могут писать с маленькой)
        category = sheet_name.strip()
        unit_type = "kg_or_l" if category in UNIT_PRICE_CATEGORIES else "pkg"

        price_col = _detect_price_column(ws)
        if price_col is None:
            continue  # пустая вкладка — нечего парсить

        for row in range(2, ws.max_row + 1):
            product_raw = ws.cell(row, 1).value
            product = _norm_str(product_raw)
            if not product:
                continue
            if product.startswith("Наименование"):
                continue

            price = _to_float(ws.cell(row, price_col).value)
            if price is None or price <= 0:
                # нет цены — поставщик не предлагает эту позицию
                continue

            # pkg_net/pkg_gross — берём только если они в "родных" колонках (не равны price_col)
            pkg_gross = _to_float(ws.cell(row, 2).value) if price_col != 2 else None
            pkg_net = _to_float(ws.cell(row, 4).value) if price_col != 4 else None

            quotes.append(PriceQuote(
                supplier=supplier_name,
                category=category,
                product=product,
                product_raw=str(product_raw),
                unit_type=unit_type,
                unit_price=price,
                pkg_net=pkg_net,
                pkg_gross=pkg_gross,
                row=row,
            ))

    return quotes


def main():
    import sys
    if len(sys.argv) < 3:
        print("Usage: parse_supplier_matrix.py <xlsx> <supplier_name>")
        sys.exit(1)
    quotes = parse(sys.argv[1], sys.argv[2])
    out = [asdict(q) for q in quotes]
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
