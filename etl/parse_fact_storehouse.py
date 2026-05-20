"""
Парсер выгрузки StoreHouse 4: «Движение группы товаров» в формате SpreadsheetML 2003 (XML с расширением .xls).

Формат (FastReport export):
  - <Workbook><Worksheet><Table><Row><Cell><Data>
  - Cell может иметь ss:Index — пропуск колонок
  - Структура строк:
      r1-r11: заголовки отчёта (период, фильтры)
      r9-r10: двухуровневая шапка (Тип | Номер | Дата | Поставщик | Получатель | Колич. | Цена | Закупочные суммы (3 колонки) | ...)
      далее блоки по товарам:
          строка-заголовок товара (только колонка 3 = '*Название')
          строки-документы: тип='п/н' | номер | дата | поставщик | получатель | кол. | цена | суммы
          строка 'Итого:' — пропускаем
"""
from __future__ import annotations
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path

from parse_fact_iiko import PurchaseFact  # переиспользуем dataclass

NS = "{urn:schemas-microsoft-com:office:spreadsheet}"


def _cells_by_col(row_el) -> dict[int, str | None]:
    """Возвращает {col_index: value} с учётом ss:Index (StoreHouse часто пропускает пустые)."""
    result: dict[int, str | None] = {}
    col_pointer = 1
    for cell in row_el.findall(NS + "Cell"):
        idx = cell.get(NS + "Index")
        if idx is not None:
            col_pointer = int(idx)
        data = cell.find(NS + "Data")
        v = data.text if data is not None else None
        result[col_pointer] = v
        col_pointer += 1
    return result


def _to_float(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", ".").replace(" ", "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_date(v) -> str | None:
    """StoreHouse даёт дату как '03.12.2025'. Возвращает YYYY-MM-DD."""
    if not v:
        return None
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", v.strip())
    if not m:
        return None
    d, mn, y = m.groups()
    return f"{y}-{mn}-{d}"


def parse_storehouse(path: str | Path) -> list[PurchaseFact]:
    path = Path(path)
    tree = ET.parse(path)
    root = tree.getroot()

    facts: list[PurchaseFact] = []
    for ws in root.findall(NS + "Worksheet"):
        table = ws.find(NS + "Table")
        if table is None:
            continue
        rows = table.findall(NS + "Row")

        current_product: str | None = None
        in_data = False  # стало True после строки шапки

        for ri, row in enumerate(rows, start=1):
            cells = _cells_by_col(row)
            c3 = cells.get(3)

            # Признак шапки: видим 'Тип' в c3 и 'Поставщик' в c6
            if not in_data:
                if c3 == "Тип" and cells.get(6) == "Поставщик":
                    in_data = True
                continue

            # Пустая или служебная строка
            if not c3:
                continue

            # Строка-итог
            if c3.startswith("Итого"):
                continue

            # Строка-заголовок товара: только c3 с текстом, нет c4/c5
            if c3.startswith("*") and cells.get(4) is None and cells.get(5) is None:
                current_product = c3.strip()
                continue

            # Строка-документ: c3='п/н' (тип), есть дата (c5), есть поставщик (c6)
            if c3 in ("п/н", "р/н", "вн") and cells.get(5) and cells.get(6):
                if current_product is None:
                    continue
                date = _parse_date(cells.get(5))
                supplier = (cells.get(6) or "").strip()
                receiver = (cells.get(8) or "").strip() or None
                qty = _to_float(cells.get(9))
                unit_price = _to_float(cells.get(10))
                # суммы — c11..c13: сумма б/н, НДС, сумма в/н
                total = _to_float(cells.get(13))

                if date and supplier and qty is not None and unit_price is not None:
                    facts.append(PurchaseFact(
                        source="storehouse",
                        source_file=path.name,
                        group=None,  # в SH-выгрузке группа в фильтре отчёта (нужно бы извлекать)
                        product=current_product,
                        date=date,
                        supplier=supplier,
                        restaurant=receiver,
                        quantity=qty,
                        unit_price=unit_price,
                        total=total or (qty * unit_price),
                        row=ri,
                    ))

    return facts


def main():
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "../sample-data/iiko_or_sh_1.xls"
    facts = parse_storehouse(path)

    print(f"=== StoreHouse: {path} ===")
    print(f"Записей закупок: {len(facts)}")

    print("\nПервые 5 строк:")
    for f in facts[:5]:
        print(f"  {f.date} | {f.product[:35]:<37} | {f.supplier[:30]:<32} | "
              f"{(f.restaurant or '')[:20]:<22} | кол.{f.quantity:>7} | цена {f.unit_price:>8.2f}")

    from collections import Counter
    suppliers = Counter(f.supplier for f in facts)
    restaurants = Counter(f.restaurant for f in facts if f.restaurant)
    products = Counter(f.product for f in facts)
    print(f"\nУникальных товаров: {len(products)}")
    print(f"Уникальных поставщиков: {len(suppliers)}")
    print(f"Уникальных получателей: {len(restaurants)}")

    print(f"\nТоп-5 поставщиков:")
    for s, n in suppliers.most_common(5):
        print(f"  {n:>4} × {s}")

    print(f"\nТоп-5 получателей:")
    for r, n in restaurants.most_common(5):
        print(f"  {n:>4} × {r}")


if __name__ == "__main__":
    main()
