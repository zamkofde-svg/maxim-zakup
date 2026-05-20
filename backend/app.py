"""
FastAPI приложение — backend для прототипа.

Запуск: uvicorn backend.app:app --reload --port 8000
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import select, func, desc
from sqlalchemy.orm import Session, selectinload

from backend.db import get_db, init_db, SessionLocal
from backend.models import (
    Category, AccountingSystem, Restaurant,
    Supplier, SupplierAlias,
    ProductMaster, AccountingAlias,
    PriceQuote, PriceHistory,
    PurchaseFact, Deviation,
    ImportRun, UnmappedItem,
)

# ---- App init ----

app = FastAPI(title="Maxim Zakup API", version="0.1.0")

# CORS — для прототипа на github.io в будущем
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()


# ============ HEALTH / STATS ============

@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    return {
        "status": "ok",
        "ts": datetime.utcnow().isoformat(),
        "counts": {
            "products": db.scalar(select(func.count(ProductMaster.id))),
            "suppliers": db.scalar(select(func.count(Supplier.id))),
            "quotes": db.scalar(select(func.count(PriceQuote.id))),
            "purchases": db.scalar(select(func.count(PurchaseFact.id))),
            "deviations": db.scalar(select(func.count(Deviation.id))),
            "unmapped": db.scalar(select(func.count(UnmappedItem.id))),
        }
    }


# ============ CATEGORIES ============

@app.get("/api/categories")
def list_categories(db: Session = Depends(get_db)):
    cats = db.execute(select(Category).order_by(Category.name)).scalars().all()
    return [{"id": c.id, "name": c.name, "unit_type": c.unit_type} for c in cats]


# ============ SUPPLIERS ============

@app.get("/api/suppliers")
def list_suppliers(
    db: Session = Depends(get_db),
    only_with_quotes: bool = Query(False),
):
    q = select(Supplier).where(Supplier.is_internal == False).order_by(Supplier.name)
    sups = db.execute(q).scalars().all()
    result = []
    for s in sups:
        q_count = db.scalar(select(func.count(PriceQuote.id)).where(PriceQuote.supplier_id == s.id))
        if only_with_quotes and not q_count:
            continue
        # сколько раз он Топ-1
        top1_count = db.execute(text_top1_count(s.id), db.connection())
        result.append({
            "id": s.id, "name": s.name,
            "quotes_count": q_count,
        })
    return result


def text_top1_count(supplier_id: int):
    """Заглушка чтобы не считать сейчас. Уберём в production-эндпойнте."""
    from sqlalchemy import text
    return text("SELECT 0")


# ============ PRODUCTS & TOP-2 ============

@app.get("/api/products")
def list_products(
    db: Session = Depends(get_db),
    category: Optional[str] = None,
    limit: int = 200,
):
    q = (
        select(ProductMaster, Category)
        .join(Category, ProductMaster.category_id == Category.id)
        .order_by(ProductMaster.name)
        .limit(limit)
    )
    if category:
        q = q.where(Category.name == category)
    rows = db.execute(q).all()
    return [
        {
            "id": p.id, "name": p.name,
            "category": c.name, "unit_label": p.unit_label,
        }
        for p, c in rows
    ]


@app.get("/api/top2")
def get_top2(
    db: Session = Depends(get_db),
    category: Optional[str] = None,
    limit: int = 100,
):
    """Топ-2 рекомендации: для каждого мастер-продукта берём 2 минимальные цены."""
    quotes_by_pm: dict[int, list] = {}

    q = (
        select(PriceQuote, ProductMaster, Category, Supplier)
        .join(ProductMaster, PriceQuote.product_master_id == ProductMaster.id)
        .join(Category, ProductMaster.category_id == Category.id)
        .join(Supplier, PriceQuote.supplier_id == Supplier.id)
    )
    if category:
        q = q.where(Category.name == category)

    rows = db.execute(q).all()
    items_map: dict[int, dict] = {}
    for pq, pm, cat, sup in rows:
        item = items_map.setdefault(pm.id, {
            "product_id": pm.id, "product": pm.name, "category": cat.name,
            "unit_type": pq.unit_type, "quotes": [],
        })
        item["quotes"].append({
            "supplier_id": sup.id, "supplier": sup.name,
            "price": pq.unit_price,
        })

    result = []
    for item in items_map.values():
        item["quotes"].sort(key=lambda x: x["price"])
        top1 = item["quotes"][0]
        top2 = item["quotes"][1] if len(item["quotes"]) > 1 else None
        result.append({
            "product_id": item["product_id"],
            "product": item["product"],
            "category": item["category"],
            "unit_type": item["unit_type"],
            "top1": top1,
            "top2": top2,
            "suppliers_count": len(item["quotes"]),
        })

    # Сортируем: сначала те у кого есть конкуренция (есть top2), потом одиночные
    result.sort(key=lambda x: (x["top2"] is None, x["category"], x["product"]))
    return result[:limit]


# ============ RESTAURANTS ============

@app.get("/api/restaurants")
def list_restaurants(db: Session = Depends(get_db)):
    rs = db.execute(select(Restaurant).order_by(Restaurant.name)).scalars().all()
    return [{"id": r.id, "name": r.name, "sh_code": r.sh_code} for r in rs]


# ============ DEVIATIONS ============

@app.get("/api/deviations")
def list_deviations(
    db: Session = Depends(get_db),
    status: Optional[str] = None,
    restaurant_id: Optional[int] = None,
    only_overpaid: bool = Query(False),
    limit: int = 100,
):
    q = (
        select(Deviation, PurchaseFact, Supplier, Restaurant)
        .join(PurchaseFact, Deviation.purchase_fact_id == PurchaseFact.id)
        .outerjoin(Supplier, PurchaseFact.supplier_id == Supplier.id)
        .outerjoin(Restaurant, PurchaseFact.restaurant_id == Restaurant.id)
        .order_by(desc(Deviation.overpayment))
    )
    if status:
        q = q.where(Deviation.status == status)
    if restaurant_id:
        q = q.where(PurchaseFact.restaurant_id == restaurant_id)
    if only_overpaid:
        q = q.where(Deviation.overpayment > 0)
    rows = db.execute(q.limit(limit)).all()
    return [
        {
            "id": dev.id,
            "date": pf.date.isoformat(),
            "product": pf.raw_product,
            "supplier": sup.name if sup else pf.raw_supplier,
            "restaurant": rest.name if rest else pf.raw_restaurant,
            "quantity": pf.quantity,
            "unit_price": pf.unit_price,
            "top2_price": dev.top2_price,
            "delta_per_unit": dev.delta_per_unit,
            "delta_pct": dev.delta_pct,
            "overpayment": dev.overpayment,
            "status": dev.status,
            "reason": dev.reason_text,
        }
        for dev, pf, sup, rest in rows
    ]


# ============ DASHBOARD SUMMARY ============

@app.get("/api/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db)):
    """Сводка для главной страницы дашборда."""
    total_overpayment = db.scalar(
        select(func.coalesce(func.sum(Deviation.overpayment), 0))
        .where(Deviation.overpayment > 0)
    ) or 0

    status_counts = {
        row[0]: row[1]
        for row in db.execute(
            select(Deviation.status, func.count(Deviation.id)).group_by(Deviation.status)
        ).all()
    }

    total_external = (
        status_counts.get("green_top2", 0)
        + status_counts.get("yellow", 0)
        + status_counts.get("red", 0)
        + status_counts.get("no_top2", 0)
        + status_counts.get("no_quotes", 0)
        + status_counts.get("unmapped_product", 0)
    )
    matched_with_top2 = (
        status_counts.get("green_top2", 0)
        + status_counts.get("yellow", 0)
        + status_counts.get("red", 0)
    )
    discipline_pct = (
        (status_counts.get("green_top2", 0) / matched_with_top2 * 100)
        if matched_with_top2 else 0
    )

    # Топ-категорий по числу позиций в Топ-2
    top_categories = db.execute(
        select(Category.name, func.count(PriceQuote.id).label("c"))
        .join(ProductMaster, PriceQuote.product_master_id == ProductMaster.id)
        .join(Category, ProductMaster.category_id == Category.id)
        .group_by(Category.name)
        .order_by(desc("c"))
    ).all()

    return {
        "total_overpayment": float(total_overpayment),
        "status_counts": status_counts,
        "total_external_purchases": total_external,
        "discipline_pct": round(discipline_pct, 1),
        "categories": [{"name": n, "quotes_count": c} for n, c in top_categories],
        "last_import": (
            db.execute(select(ImportRun).order_by(desc(ImportRun.id)).limit(1))
            .scalar()
        ).finished_at.isoformat() if db.scalar(select(func.count(ImportRun.id))) else None,
    }


# ============ UNMAPPED ITEMS ============

@app.get("/api/unmapped")
def list_unmapped(db: Session = Depends(get_db), limit: int = 50):
    rows = db.execute(
        select(UnmappedItem).order_by(desc(UnmappedItem.occurrence_count)).limit(limit)
    ).scalars().all()
    return [
        {
            "id": u.id, "raw_name": u.raw_name, "source": u.source,
            "occurrence_count": u.occurrence_count,
            "last_seen": u.last_seen.isoformat(),
        }
        for u in rows
    ]


# ============ STATIC FRONTEND ============

PROTOTYPE_DIR = Path(__file__).parent.parent / "prototype"
if PROTOTYPE_DIR.exists():
    app.mount("/", StaticFiles(directory=str(PROTOTYPE_DIR), html=True), name="prototype")
