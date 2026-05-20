"""
End-to-end сверка: факт закупок vs Топ-2 рекомендаций.

Пайплайн:
  1. Парсим матрицы всех поставщиков → quotes
  2. Агрегируем quotes → top2 по каждой (категория, товар)
  3. Парсим карту сопоставлений:
       - mapping_index[(system, system_name_norm)] = supplier_name (мастер)
       - supplier_alias_index[alias_norm] = canonical_supplier
  4. Парсим выгрузку факта (iiko или StoreHouse)
  5. Для каждого факта:
       a) нормализуем имя поставщика → канон
       b) ищем мастер-позицию через карту (по полю product и системе)
       c) если найдена и есть top2 — считаем дельту + статус
  6. Печатаем сводку и нераспознанные позиции

Запуск: python3 match_fact_vs_top2.py
"""
from __future__ import annotations
import re
from collections import defaultdict, Counter
from dataclasses import dataclass
from pathlib import Path

from parse_supplier_matrix import parse as parse_matrix
from parse_mapping import parse as parse_mapping
from parse_fact_iiko import parse_iiko, PurchaseFact
from parse_fact_storehouse import parse_storehouse

SAMPLES = Path(__file__).parent.parent / "sample-data"
DRIVE = SAMPLES / "drive-sync"

# Используем матрицы из Drive (актуальные). Имя «ООО ...» — как в самом названии файла.
SUPPLIERS_MATRICES = [
    ("ООО УРАЛ ФУД",                DRIVE / "ООО УРАЛ ФУД.xlsx"),
    ("ООО Метро Кэш энд Керри",     DRIVE / "ООО Метро Кэш энд Керри.xlsx"),
    ("ООО Орбита и К",              DRIVE / "ООО Орбита и К.xlsx"),
    ("ООО ЕвроСиб-Трейд",           DRIVE / "ООО ЕвроСиб-Трейд.xlsx"),
]

MAPPING_FILE = DRIVE / "Карта сопоставлений.xlsx"

# Файлы факта закупок (пока локально, потом тоже из Drive)
FACT_FILES = [
    {"path": SAMPLES / "iiko_or_sh_1.xls",  "format": "storehouse", "system": "SH"},
    {"path": SAMPLES / "iiko_or_sh_2.xlsx", "format": "iiko",       "system": "SH"},
]

# Поставщики, которых считаем «внутренними» — это не закупки, а перемещения
INTERNAL_SUPPLIERS_SUBSTRINGS = ["Цех", "Производство"]


def normalize(s: str | None) -> str:
    """Агрессивная нормализация для лукапа: lower, без пробелов и пунктуации."""
    if not s:
        return ""
    s = s.lower()
    # схлопываем все типы пробелов
    s = re.sub(r"\s+", " ", s).strip()
    # уберём кавычки и точки в типичных префиксах
    s = s.replace('"', "").replace("«", "").replace("»", "")
    return s


def build_top2_index(quotes_by_product):
    """{(category, product_normalized): (top1, top2 or None)}"""
    index = {}
    for (cat, prod), quotes in quotes_by_product.items():
        sorted_q = sorted(quotes, key=lambda q: q.unit_price)
        top1 = sorted_q[0]
        top2 = sorted_q[1] if len(sorted_q) > 1 else None
        index[(cat, normalize(prod))] = (top1, top2, prod)
    return index


def build_mapping_index(mapping_records):
    """
    {(system, system_name_norm): supplier_name}

    Если одна system_name мэппится в несколько supplier_name (бывает!),
    берём первый — это просто эвристика на старте.
    """
    index: dict[tuple[str, str], str] = {}
    conflicts = 0
    for r in mapping_records:
        key = (r.system, normalize(r.system_name))
        if key in index and index[key] != r.supplier_name:
            conflicts += 1
        index.setdefault(key, r.supplier_name)
    return index, conflicts


def build_supplier_alias_index(supplier_aliases):
    """{alias_normalized: canonical}. Также добавляем canonical → canonical для прямого поиска."""
    idx = {}
    for sa in supplier_aliases:
        idx[normalize(sa.alias)] = sa.canonical
        idx[normalize(sa.canonical)] = sa.canonical
    return idx


# ============ MAIN ============

def main():
    # 1. Загружаем матрицы
    all_quotes = []
    for name, path in SUPPLIERS_MATRICES:
        all_quotes.extend(parse_matrix(path, name))
    print(f"Матрицы: {len(all_quotes)} заполненных цен от {len(SUPPLIERS_MATRICES)} поставщиков")

    # 2. Группируем и строим Топ-2
    by_prod = defaultdict(list)
    for q in all_quotes:
        by_prod[(q.category, q.product)].append(q)
    top2_idx = build_top2_index(by_prod)
    n_with_top2 = sum(1 for _, (_, t2, _) in top2_idx.items() if t2 is not None)
    print(f"  уникальных позиций: {len(top2_idx)}  с Топ-2 (≥2 поставщика): {n_with_top2}")

    # 3. Карта сопоставлений
    mapping_records, supplier_aliases = parse_mapping(MAPPING_FILE)
    map_idx, conflicts = build_mapping_index(mapping_records)
    sup_alias_idx = build_supplier_alias_index(supplier_aliases)
    print(f"Карта: {len(mapping_records)} записей маппинга (конфликтов: {conflicts}), "
          f"{len(supplier_aliases)} синонимов поставщиков")

    # 4. Загружаем все файлы факта
    all_facts: list[tuple[PurchaseFact, str]] = []  # (fact, default_system)
    for spec in FACT_FILES:
        if spec["format"] == "iiko":
            facts = parse_iiko(spec["path"])
        else:
            facts = parse_storehouse(spec["path"])
        for f in facts:
            all_facts.append((f, spec["system"]))
        print(f"  факт: {spec['path'].name} ({spec['format']}, system={spec['system']}) — {len(facts)} закупок")
    print(f"Всего фактов: {len(all_facts)}\n")

    # 5. Сопоставление
    buckets = Counter()
    overpayment_total = 0.0
    matched_with_top2 = []  # для печати топа переплат
    unmapped_products = Counter()
    unmapped_suppliers = Counter()
    no_top2_examples = []
    internal_count = 0

    for fact, system in all_facts:
        # Фильтр: внутренние перемещения (Цех/Производство и т.п.)
        if any(s in fact.supplier for s in INTERNAL_SUPPLIERS_SUBSTRINGS):
            internal_count += 1
            buckets["internal"] += 1
            continue

        # Шаг A: канонизировать поставщика
        norm_sup = normalize(fact.supplier)
        canonical_supplier = sup_alias_idx.get(norm_sup) or fact.supplier
        if norm_sup not in sup_alias_idx:
            unmapped_suppliers[fact.supplier] += 1

        # Шаг B: найти мастер-позицию через карту
        prod_norm = normalize(fact.product)
        master_supplier_name = map_idx.get((system, prod_norm))

        if not master_supplier_name:
            buckets["unmapped_product"] += 1
            unmapped_products[fact.product] += 1
            continue

        # Шаг C: найти Топ-2 — ищем по нормализованному имени, перебирая все категории
        # (мы не знаем категорию факта, поэтому ищем по всем)
        normalized_master = normalize(master_supplier_name)
        found_top2 = None
        for (cat, p_norm), (top1, top2, raw) in top2_idx.items():
            if p_norm == normalized_master:
                found_top2 = (cat, top1, top2)
                break

        if not found_top2:
            buckets["no_quotes"] += 1
            continue

        cat, top1, top2 = found_top2

        if top2 is None:
            buckets["no_top2"] += 1
            if len(no_top2_examples) < 5:
                no_top2_examples.append((fact, top1))
            continue

        # Шаг D: считаем дельту относительно Топ-2 (как и в исходной системе)
        delta_per_unit = fact.unit_price - top2.unit_price
        delta_pct = (delta_per_unit / top2.unit_price * 100) if top2.unit_price else 0
        overpay = max(0, delta_per_unit) * fact.quantity

        if delta_per_unit <= 0:
            buckets["green_top2"] += 1
        elif delta_pct < 15:
            buckets["yellow"] += 1
            overpayment_total += overpay
        else:
            buckets["red"] += 1
            overpayment_total += overpay

        matched_with_top2.append({
            "fact": fact,
            "top1": top1,
            "top2": top2,
            "delta_per_unit": delta_per_unit,
            "delta_pct": delta_pct,
            "overpay": overpay,
        })

    # 6. Отчёт
    print("=" * 70)
    print("РЕЗУЛЬТАТ СВЕРКИ")
    print("=" * 70)
    print(f"\nВсего фактов закупок: {len(all_facts)}")
    print(f"  из них внутренние перемещения (Цех/Производство): {internal_count}  — исключены из сверки")
    print(f"  внешние закупки: {len(all_facts) - internal_count}")
    print(f"\nРаспределение по внешним закупкам:")
    print(f"  🟢 По Топ-2 или дешевле:        {buckets['green_top2']:>5}")
    print(f"  🟡 Дороже на 0–15%:              {buckets['yellow']:>5}")
    print(f"  🔴 Дороже на 15%+:               {buckets['red']:>5}")
    print(f"  ⚪ Сопоставлено, но без Топ-2:   {buckets['no_top2']:>5}  (есть в карте, но < 2 поставщиков предложили)")
    print(f"  ⚪ Сопоставлено, нет в матрицах: {buckets['no_quotes']:>5}  (карта связывает с позицией, у которой никто из 2 поставщиков не дал цены)")
    print(f"  ❌ Не сопоставлено с картой:     {buckets['unmapped_product']:>5}")

    print(f"\n💰 Суммарная переплата vs Топ-2: {overpayment_total:,.2f} ₽")

    if matched_with_top2:
        sorted_matches = sorted(matched_with_top2, key=lambda x: -x["overpay"])
        print(f"\nТоп-5 переплат:")
        for m in sorted_matches[:5]:
            f = m["fact"]
            print(f"  +{m['overpay']:>9,.2f} ₽  ({m['delta_pct']:+5.1f}%)  "
                  f"{f.date} {f.product[:35]:<37} {f.supplier[:25]:<27} "
                  f"кол.{f.quantity:>6} цена {f.unit_price:>7.2f} vs Топ-2 {m['top2'].unit_price:>7.2f}")

    if no_top2_examples:
        print(f"\nПримеры «есть Топ-1, но нет Топ-2» (5):")
        for f, t1 in no_top2_examples:
            print(f"  {f.product[:50]:<52}  →  Топ-1 {t1.unit_price:>7.2f} ({t1.supplier})")

    print(f"\nТоп-10 нераспознанных продуктов (нужно добавить в карту):")
    for p, n in unmapped_products.most_common(10):
        print(f"  {n:>3}× {p[:80]}")

    print(f"\nТоп-10 нераспознанных поставщиков:")
    for s, n in unmapped_suppliers.most_common(10):
        print(f"  {n:>3}× {s}")


if __name__ == "__main__":
    main()
