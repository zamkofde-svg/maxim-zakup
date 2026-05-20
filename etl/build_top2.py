"""
Сборка Топ-2 рекомендаций из матриц всех поставщиков.

Демо: парсим 2 файла (УРАЛ ФУД, ЕвроСиб-Трейд) → агрегируем → выводим Топ-2 по каждой позиции.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from parse_supplier_matrix import parse, PriceQuote

SAMPLES = Path(__file__).parent.parent / "sample-data"

SUPPLIERS = [
    ("УРАЛ ФУД",       SAMPLES / "ural_food.xlsx"),
    ("ЕвроСиб-Трейд",  SAMPLES / "eurosib_trade.xlsx"),
]


def main():
    all_quotes: list[PriceQuote] = []
    for name, path in SUPPLIERS:
        quotes = parse(path, name)
        print(f"  {name}: {len(quotes)} заполненных позиций")
        all_quotes.extend(quotes)

    # Группируем по (категория, товар)
    by_product: dict[tuple[str, str], list[PriceQuote]] = defaultdict(list)
    for q in all_quotes:
        by_product[(q.category, q.product)].append(q)

    print(f"\nВсего уникальных (категория, товар) с хотя бы одной ценой: {len(by_product)}")

    # Делим на позиции где есть конкуренция (≥2 поставщика) и где нет
    competitive = {k: v for k, v in by_product.items() if len(v) >= 2}
    monopoly = {k: v for k, v in by_product.items() if len(v) == 1}
    print(f"  с конкуренцией (≥2 поставщика): {len(competitive)}")
    print(f"  только один поставщик:           {len(monopoly)}")

    if competitive:
        print("\n=== ТОП-2 ПО КОНКУРЕНТНЫМ ПОЗИЦИЯМ ===")
        for (cat, prod), quotes in sorted(competitive.items()):
            # сортируем по цене (asc)
            sorted_q = sorted(quotes, key=lambda q: q.unit_price)
            top1 = sorted_q[0]
            top2 = sorted_q[1]
            unit_label = "₽/кг·л" if top1.unit_type == "kg_or_l" else "₽/упак"
            delta = top2.unit_price - top1.unit_price
            delta_pct = (delta / top1.unit_price) * 100 if top1.unit_price else 0
            print(f"\n  [{cat}] {prod}")
            print(f"    🥇 {top1.unit_price:>8.2f} {unit_label}  ← {top1.supplier}")
            print(f"    🥈 {top2.unit_price:>8.2f} {unit_label}  ← {top2.supplier}  (+{delta:.2f}, +{delta_pct:.1f}%)")


if __name__ == "__main__":
    main()
