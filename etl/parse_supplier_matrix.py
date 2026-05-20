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


def parse(path: str | Path, supplier_name: str) -> list[PriceQuote]:
    wb = load_workbook(path, data_only=True)  # читаем вычисленные значения формул
    quotes: list[PriceQuote] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        # Категория = имя вкладки, нормализуем регистр (некоторые поставщики могут писать с маленькой)
        category = sheet_name.strip()
        unit_type = "kg_or_l" if category in UNIT_PRICE_CATEGORIES else "pkg"

        for row in range(2, ws.max_row + 1):
            product_raw = ws.cell(row, 1).value
            product = _norm_str(product_raw)
            if not product:
                continue

            price = _to_float(ws.cell(row, 5).value)
            if price is None:
                # нет цены — поставщик не предлагает эту позицию
                continue

            pkg_gross = _to_float(ws.cell(row, 2).value)
            pkg_net = _to_float(ws.cell(row, 4).value)

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
