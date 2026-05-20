"""
FastAPI приложение — backend для прототипа.

Запуск: uvicorn backend.app:app --reload --port 8000
"""
from __future__ import annotations
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
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
    """Список поставщиков + сколько у них позиций + дата последнего обновления + сколько раз они Топ-1."""
    sups = db.execute(
        select(Supplier).where(Supplier.is_internal == False).order_by(Supplier.name)
    ).scalars().all()

    # Топ-1 кеш: для каждого мастер-продукта найти поставщика с минимальной ценой
    top1_supplier_by_pm: dict[int, int] = {}
    quotes_by_pm: dict[int, list] = {}
    for pq in db.execute(select(PriceQuote)).scalars():
        quotes_by_pm.setdefault(pq.product_master_id, []).append(pq)
    for pm_id, qs in quotes_by_pm.items():
        top1 = min(qs, key=lambda x: x.unit_price)
        top1_supplier_by_pm[pm_id] = top1.supplier_id

    from collections import Counter
    top1_counter = Counter(top1_supplier_by_pm.values())

    # Категории по поставщику (через мастер-позиции)
    cats_by_supplier: dict[int, set[str]] = {}
    for row in db.execute(
        select(Supplier.id, Category.name)
        .join(PriceQuote, PriceQuote.supplier_id == Supplier.id)
        .join(ProductMaster, ProductMaster.id == PriceQuote.product_master_id)
        .join(Category, Category.id == ProductMaster.category_id)
        .distinct()
    ).all():
        cats_by_supplier.setdefault(row[0], set()).add(row[1])

    result = []
    for s in sups:
        q_count = db.scalar(
            select(func.count(PriceQuote.id)).where(PriceQuote.supplier_id == s.id)
        ) or 0
        if only_with_quotes and not q_count:
            continue
        last_updated = db.scalar(
            select(func.max(PriceQuote.captured_at)).where(PriceQuote.supplier_id == s.id)
        )
        result.append({
            "id": s.id, "name": s.name,
            "quotes_count": q_count,
            "top1_count": top1_counter.get(s.id, 0),
            "categories": sorted(cats_by_supplier.get(s.id, [])),
            "last_updated": last_updated.isoformat() if last_updated else None,
        })
    # Сначала те у кого есть цены
    result.sort(key=lambda x: (-x["quotes_count"], x["name"]))
    return result


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


# ============ RESTAURANTS DISCIPLINE ============

@app.get("/api/restaurants/discipline")
def restaurants_discipline(db: Session = Depends(get_db)):
    """Доля по Топ-2 в разрезе ресторанов."""
    rows = db.execute(
        select(
            Restaurant.id, Restaurant.name,
            func.sum(func.iif(Deviation.status == "green_top2", 1, 0)).label("green"),
            func.sum(func.iif(Deviation.status.in_(["green_top2", "yellow", "red"]), 1, 0)).label("with_top2"),
            func.coalesce(func.sum(Deviation.overpayment), 0).label("overpayment"),
        )
        .join(PurchaseFact, PurchaseFact.restaurant_id == Restaurant.id)
        .join(Deviation, Deviation.purchase_fact_id == PurchaseFact.id)
        .group_by(Restaurant.id, Restaurant.name)
        .order_by(desc("overpayment"))
    ).all()
    return [
        {
            "id": r[0], "name": r[1],
            "discipline_pct": round((r[2] / r[3] * 100), 1) if r[3] else None,
            "purchases_with_top2": r[3],
            "overpayment": float(r[4]),
        }
        for r in rows
    ]


# ============ REASONS ============

class ReasonIn(BaseModel):
    reason_text: str
    reason_category: Optional[str] = None


@app.post("/api/deviations/{deviation_id}/reason")
def set_reason(
    deviation_id: int,
    body: ReasonIn,
    db: Session = Depends(get_db),
):
    dev = db.get(Deviation, deviation_id)
    if not dev:
        raise HTTPException(404, "Deviation not found")
    dev.reason_text = body.reason_text
    dev.reason_category = body.reason_category
    db.commit()
    return {"ok": True, "id": deviation_id}


# ============ SYNC (Drive → DB) ============

_sync_state = {"status": "idle", "started_at": None, "finished_at": None, "log": []}


def _do_sync():
    """Запускается в фоне — синхронизирует Drive и пересчитывает БД."""
    _sync_state["status"] = "running"
    _sync_state["started_at"] = datetime.utcnow().isoformat()
    _sync_state["finished_at"] = None
    _sync_state["log"] = []

    root = Path(__file__).parent.parent

    def step(name, cmd):
        _sync_state["log"].append({"step": name, "started": datetime.utcnow().isoformat()})
        result = subprocess.run(
            cmd, cwd=str(root), capture_output=True, text=True
        )
        _sync_state["log"][-1]["finished"] = datetime.utcnow().isoformat()
        _sync_state["log"][-1]["returncode"] = result.returncode
        if result.returncode != 0:
            _sync_state["log"][-1]["error"] = result.stderr[-500:]

    try:
        step("drive_sync", [sys.executable, "-m", "etl.sync_from_drive"])
        step("importer", [sys.executable, "-m", "backend.importer"])
        _sync_state["status"] = "ok"
    except Exception as e:
        _sync_state["status"] = "error"
        _sync_state["log"].append({"error": str(e)})
    finally:
        _sync_state["finished_at"] = datetime.utcnow().isoformat()


@app.post("/api/sync")
def trigger_sync(background_tasks: BackgroundTasks):
    if _sync_state["status"] == "running":
        raise HTTPException(409, "Уже идёт синхронизация")
    background_tasks.add_task(_do_sync)
    return {"status": "started"}


@app.get("/api/sync/status")
def sync_status():
    return _sync_state


# ============ EXTENDED DASHBOARD ============

@app.get("/api/dashboard/top_overpayments")
def top_overpayments(db: Session = Depends(get_db), limit: int = 5):
    """Топ-N переплат для блока на главной."""
    rows = db.execute(
        select(Deviation, PurchaseFact, Supplier, Restaurant)
        .join(PurchaseFact, Deviation.purchase_fact_id == PurchaseFact.id)
        .outerjoin(Supplier, PurchaseFact.supplier_id == Supplier.id)
        .outerjoin(Restaurant, PurchaseFact.restaurant_id == Restaurant.id)
        .where(Deviation.overpayment > 0)
        .order_by(desc(Deviation.overpayment))
        .limit(limit)
    ).all()
    return [
        {
            "id": dev.id, "product": pf.raw_product,
            "supplier": sup.name if sup else pf.raw_supplier,
            "restaurant": rest.name if rest else pf.raw_restaurant,
            "overpayment": dev.overpayment,
            "delta_pct": dev.delta_pct,
            "status": dev.status,
        }
        for dev, pf, sup, rest in rows
    ]


# ============ STATIC FRONTEND ============

PROTOTYPE_DIR = Path(__file__).parent.parent / "prototype"
if PROTOTYPE_DIR.exists():
    app.mount("/", StaticFiles(directory=str(PROTOTYPE_DIR), html=True), name="prototype")
