"""
FastAPI приложение — backend для прототипа.

Запуск: uvicorn backend.app:app --reload --port 8000
"""
from __future__ import annotations
import os
import sys
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

print(f"[boot] loading backend.app … PORT env={os.environ.get('PORT')} HOST={os.uname().nodename}", flush=True)


def _log_signal(signum, frame):
    print(f"[signal] received signal {signum}", flush=True)
    # Возвращаем дефолтный обработчик чтобы uvicorn нормально завершился
    signal.signal(signum, signal.SIG_DFL)
    signal.raise_signal(signum)


for sig_name in ("SIGTERM", "SIGINT", "SIGHUP"):
    try:
        signal.signal(getattr(signal, sig_name), _log_signal)
    except Exception as e:
        print(f"[boot] couldn't install {sig_name} handler: {e}", flush=True)

from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks, Response
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
    PriceQuote, PriceHistory, PriceChange,
    PurchaseFact, Deviation,
    ImportRun, UnmappedItem, User,
)
from backend.auth import (
    current_user, require_role, verify_password,
    make_session_token, COOKIE_NAME, COOKIE_MAX_AGE,
    ensure_default_users,
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
    """МИНИМАЛЬНЫЙ startup: init_db + дефолтные юзеры + scheduler. Всё в try."""
    try:
        init_db()
        print(f"[startup] init_db OK", flush=True)
    except Exception as e:
        import traceback
        print(f"[startup] init_db FAILED (продолжаем): {e}", flush=True)
        traceback.print_exc()
    try:
        ensure_default_users()
    except Exception as e:
        print(f"[startup] ensure_default_users FAILED: {e}", flush=True)
    try:
        _setup_scheduler()
    except Exception as e:
        print(f"[startup] scheduler FAILED (продолжаем): {e}", flush=True)


# ============ AUTH ENDPOINTS ============

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.execute(select(User).where(User.username == body.username)).scalar_one_or_none()
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Неверный логин или пароль")
    token = make_session_token(user.id, user.role, user.username)
    response.set_cookie(
        key=COOKIE_NAME, value=token,
        max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax",
        secure=False,  # на HTTPS поменять на True
    )
    return {"username": user.username, "role": user.role, "full_name": user.full_name}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)):
    return {"username": user.username, "role": user.role, "full_name": user.full_name}


# ============ HEALTH / STATS ============

@app.get("/healthz")
def healthz():
    """Light healthcheck для Timeweb — без БД, без зависимостей. Всегда 200."""
    return {"ok": True}


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


# ============ MAPPING (KARTA POZICIJ) ============

@app.get("/api/mapping/summary")
def mapping_summary(db: Session = Depends(get_db)):
    """Сводка покрытия: сколько маппингов на категорию × систему."""
    from collections import defaultdict
    rows = db.execute(
        select(
            Category.name, AccountingSystem.name, func.count(AccountingAlias.id)
        )
        .join(ProductMaster, ProductMaster.category_id == Category.id)
        .join(AccountingAlias, AccountingAlias.product_master_id == ProductMaster.id)
        .join(AccountingSystem, AccountingSystem.id == AccountingAlias.system_id)
        .group_by(Category.name, AccountingSystem.name)
    ).all()
    by_cat: dict = defaultdict(dict)
    for cat, sys_name, n in rows:
        by_cat[cat][sys_name] = n
    return {
        "categories": [{"name": c, "by_system": d} for c, d in sorted(by_cat.items())],
        "total_aliases": db.scalar(select(func.count(AccountingAlias.id))),
        "total_master_products": db.scalar(select(func.count(ProductMaster.id))),
        "total_unmapped": db.scalar(select(func.count(UnmappedItem.id))),
    }


@app.get("/api/mapping/items")
def mapping_items(
    db: Session = Depends(get_db),
    category: Optional[str] = None,
    limit: int = 200,
):
    """Реальный список маппингов: мастер-позиция → имена в системах учёта."""
    q = (
        select(ProductMaster, Category)
        .join(Category, ProductMaster.category_id == Category.id)
    )
    if category:
        q = q.where(Category.name == category)
    q = q.order_by(ProductMaster.name).limit(limit)
    rows = db.execute(q).all()
    result = []
    for pm, cat in rows:
        aliases = db.execute(
            select(AccountingAlias, AccountingSystem)
            .join(AccountingSystem, AccountingSystem.id == AccountingAlias.system_id)
            .where(AccountingAlias.product_master_id == pm.id)
        ).all()
        by_sys = {sys.name: alias.name for alias, sys in aliases}
        result.append({
            "id": pm.id,
            "product": pm.name,
            "category": cat.name,
            "SH": by_sys.get("SH"),
            "Chees": by_sys.get("Chees"),
            "TEHNIKUM": by_sys.get("TEHNIKUM"),
            "Sorrento": by_sys.get("Sorrento"),
            "has_any": bool(by_sys),
        })
    return result


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
    user: User = Depends(require_role("buyer", "chef")),
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
_master_sync_state = {"status": "idle", "started_at": None, "finished_at": None, "result": None, "error": None}


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
def trigger_sync(background_tasks: BackgroundTasks, user: User = Depends(require_role("buyer"))):
    if _sync_state["status"] == "running":
        raise HTTPException(409, "Уже идёт синхронизация")
    background_tasks.add_task(_do_sync)
    return {"status": "started"}


@app.get("/api/sync/status")
def sync_status():
    return _sync_state


# Отдельный sync только фактов (быстрее, не трогает мастер и матрицы)
_facts_sync_state = {"status": "idle", "started_at": None, "finished_at": None, "log": []}


def _do_facts_sync():
    _facts_sync_state["status"] = "running"
    _facts_sync_state["started_at"] = datetime.utcnow().isoformat()
    _facts_sync_state["finished_at"] = None
    _facts_sync_state["log"] = []
    root = Path(__file__).parent.parent

    def step(name, cmd):
        _facts_sync_state["log"].append({"step": name, "started": datetime.utcnow().isoformat()})
        result = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=300)
        _facts_sync_state["log"][-1]["finished"] = datetime.utcnow().isoformat()
        _facts_sync_state["log"][-1]["returncode"] = result.returncode
        if result.returncode != 0:
            _facts_sync_state["log"][-1]["error"] = result.stderr[-500:]

    try:
        # Полный sync (он тащит и подпапку facts/) + переимпорт (он подхватит факты)
        step("drive_sync", [sys.executable, "-m", "etl.sync_from_drive"])
        step("importer", [sys.executable, "-m", "backend.importer"])
        _facts_sync_state["status"] = "ok"
    except Exception as e:
        _facts_sync_state["status"] = "error"
        _facts_sync_state["log"].append({"error": str(e)})
    finally:
        _facts_sync_state["finished_at"] = datetime.utcnow().isoformat()


@app.post("/api/sync-facts")
def trigger_facts_sync(background_tasks: BackgroundTasks, user: User = Depends(require_role("buyer"))):
    """Подтягивает выгрузки iiko/SH из подпапки Drive «Факты iiko-SH»
    и пересчитывает отклонения. Сохранённые шефами причины не теряются."""
    if _facts_sync_state["status"] == "running":
        raise HTTPException(409, "Уже идёт обновление выгрузок")
    background_tasks.add_task(_do_facts_sync)
    return {"status": "started"}


@app.get("/api/sync-facts/status")
def facts_sync_status():
    return _facts_sync_state


# ============ MASTER → SUPPLIERS SYNC ============

def _do_master_sync(dry_run: bool = False, prune: bool = True):
    import sys as _sys
    _master_sync_state["status"] = "running"
    _master_sync_state["started_at"] = datetime.utcnow().isoformat()
    _master_sync_state["finished_at"] = None
    _master_sync_state["result"] = None
    _master_sync_state["error"] = None
    try:
        # Импортируем здесь чтобы избежать конфликта при тестах
        etl_path = str(Path(__file__).parent.parent / "etl")
        if etl_path not in _sys.path:
            _sys.path.insert(0, etl_path)
        from sync_master_to_suppliers import sync as do_sync  # noqa
        result = do_sync(dry_run=dry_run, prune=prune)
        _master_sync_state["result"] = result
        _master_sync_state["status"] = "ok"
    except Exception as e:
        import traceback
        _master_sync_state["status"] = "error"
        _master_sync_state["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}"
    finally:
        _master_sync_state["finished_at"] = datetime.utcnow().isoformat()


@app.post("/api/sync-master")
def trigger_master_sync(background_tasks: BackgroundTasks,
                       dry_run: bool = Query(False),
                       prune: bool = Query(True),
                       confirm: str = Query(""),
                       user: User = Depends(require_role("buyer"))):
    """Раскатывает мастер-матрицу по матрицам всех поставщиков.

    prune=True (по умолчанию): приведение к единому виду — создаёт нужные вкладки
    из whitelist, дописывает позиции мастера, УДАЛЯЕТ у поставщиков позиции
    которых нет в мастере, и УДАЛЯЕТ вкладки которых нет в их whitelist.
    Цены в оставшихся позициях НЕ трогаются.

    prune=False: только добавление (старое поведение).

    Защита: при prune=True требуется query confirm=PRUNE-CONFIRM (защита от случайных POST).
    """
    if _master_sync_state["status"] == "running":
        raise HTTPException(409, "Уже идёт sync мастер→поставщики")
    if prune and confirm != "PRUNE-CONFIRM":
        raise HTTPException(400, "prune=true требует confirm=PRUNE-CONFIRM (защита от случайного запуска)")
    background_tasks.add_task(_do_master_sync, dry_run, prune)
    return {"status": "started", "dry_run": dry_run, "prune": prune}


@app.get("/api/sync-master/status")
def master_sync_status():
    return _master_sync_state


# ============ APSCHEDULER: cron 06:00 Тюмень (UTC+5) → 01:00 UTC ============

_scheduler = None


def _setup_scheduler():
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        if _scheduler is not None:
            return
        _scheduler = BackgroundScheduler(timezone="UTC")
        # 06:00 Asia/Yekaterinburg (Тюмень) = 01:00 UTC
        _scheduler.add_job(
            _do_sync,
            CronTrigger(hour=1, minute=0, timezone="UTC"),
            id="daily_drive_sync",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        _scheduler.start()
        print("[scheduler] APScheduler запущен. Daily sync at 01:00 UTC (06:00 Тюмень)", flush=True)
    except Exception as e:
        print(f"[scheduler] init failed: {e}", flush=True)


# ============ PERIODS ============

@app.get("/api/periods")
def periods(db: Session = Depends(get_db)):
    """Список месяцев, по которым есть данные в purchases_fact."""
    from sqlalchemy import extract
    rows = db.execute(
        select(
            extract("year", PurchaseFact.date).label("y"),
            extract("month", PurchaseFact.date).label("m"),
            func.count(PurchaseFact.id).label("n"),
        )
        .group_by("y", "m")
        .order_by(desc("y"), desc("m"))
    ).all()
    months_ru = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                 "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    return [
        {
            "key": f"{int(r[0])}-{int(r[1]):02d}",
            "label": f"{months_ru[int(r[1])]} {int(r[0])}",
            "year": int(r[0]),
            "month": int(r[1]),
            "count": r[2],
        }
        for r in rows
    ]


# ============ PRICE CHANGES (что изменилось) ============

@app.get("/api/price_changes")
def list_price_changes(
    db: Session = Depends(get_db),
    since: Optional[str] = Query(None, description="ISO datetime; default — последние 7 дней"),
    limit: int = 200,
):
    """Реальные изменения цен (не snapshot'ы). Сортировка: свежее сверху."""
    from datetime import timedelta
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", ""))
        except Exception:
            raise HTTPException(400, "Bad since format, expected ISO datetime")
    else:
        since_dt = datetime.utcnow() - timedelta(days=7)

    rows = db.execute(
        select(PriceChange, Supplier, ProductMaster, Category)
        .join(Supplier, Supplier.id == PriceChange.supplier_id)
        .join(ProductMaster, ProductMaster.id == PriceChange.product_master_id)
        .join(Category, Category.id == ProductMaster.category_id)
        .where(PriceChange.changed_at >= since_dt)
        .order_by(desc(PriceChange.changed_at))
        .limit(limit)
    ).all()
    return [
        {
            "id": pc.id,
            "supplier": s.name,
            "product": pm.name,
            "category": cat.name,
            "old_price": pc.old_price,
            "new_price": pc.new_price,
            "delta_pct": pc.delta_pct,
            "changed_at": pc.changed_at.isoformat(),
        }
        for pc, s, pm, cat in rows
    ]


@app.get("/api/price_changes/summary")
def price_changes_summary(
    db: Session = Depends(get_db),
    since: Optional[str] = Query(None),
):
    """Сводка изменений за период: сколько всего, ↑ и ↓, средняя дельта, топ-5 крупнейших."""
    from datetime import timedelta
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", ""))
        except Exception:
            raise HTTPException(400, "Bad since format")
    else:
        since_dt = datetime.utcnow() - timedelta(days=7)

    rows = db.execute(
        select(PriceChange, Supplier, ProductMaster)
        .join(Supplier, Supplier.id == PriceChange.supplier_id)
        .join(ProductMaster, ProductMaster.id == PriceChange.product_master_id)
        .where(PriceChange.changed_at >= since_dt)
        .order_by(desc(PriceChange.changed_at))
    ).all()

    total = len(rows)
    ups = [r for r in rows if r[0].delta_pct > 0]
    downs = [r for r in rows if r[0].delta_pct < 0]
    avg_up = (sum(r[0].delta_pct for r in ups) / len(ups)) if ups else 0
    avg_down = (sum(r[0].delta_pct for r in downs) / len(downs)) if downs else 0

    top_ups = sorted(ups, key=lambda r: -r[0].delta_pct)[:5]
    top_downs = sorted(downs, key=lambda r: r[0].delta_pct)[:5]

    def serialize(rows_subset):
        return [
            {
                "supplier": s.name, "product": pm.name,
                "old_price": pc.old_price, "new_price": pc.new_price,
                "delta_pct": pc.delta_pct,
            }
            for pc, s, pm in rows_subset
        ]

    last_ts = rows[0][0].changed_at.isoformat() if rows else None

    return {
        "since": since_dt.isoformat(),
        "total": total,
        "ups": len(ups),
        "downs": len(downs),
        "avg_up_pct": round(avg_up, 2),
        "avg_down_pct": round(avg_down, 2),
        "top_ups": serialize(top_ups),
        "top_downs": serialize(top_downs),
        "last_change_at": last_ts,
    }


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


# ============ AI: CEO SUMMARY via OpenRouter ============

import os
import json
import urllib.request
import urllib.error

OPENROUTER_KEY_FILE = Path.home() / ".config" / "maxim-zakup" / "openrouter.env"
_openrouter_key_cache: Optional[str] = None
_ai_cache: dict = {"summary": None, "generated_at": None}


def _load_openrouter_key() -> Optional[str]:
    global _openrouter_key_cache
    if _openrouter_key_cache:
        return _openrouter_key_cache
    # 1) ENV — приоритет (облако)
    env_key = os.environ.get("OPENROUTER_API_KEY")
    if env_key:
        _openrouter_key_cache = env_key.strip()
        return _openrouter_key_cache
    # 2) Файл (локалка)
    if OPENROUTER_KEY_FILE.exists():
        text = OPENROUTER_KEY_FILE.read_text().strip()
        for line in text.splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                _openrouter_key_cache = line.split("=", 1)[1].strip()
                return _openrouter_key_cache
    return None


def _call_openrouter(prompt: str, model: str = "anthropic/claude-sonnet-4") -> str:
    key = _load_openrouter_key()
    if not key:
        raise HTTPException(500, "OpenRouter API key не настроен")

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://maxim-zakup.local",
            "X-Title": "Maxim Zakup",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise HTTPException(502, f"OpenRouter error {e.code}: {body[:500]}")
    except Exception as e:
        raise HTTPException(502, f"OpenRouter call failed: {e}")


def _build_ceo_context(db: Session) -> dict:
    """Собираем фактическую сводку для AI."""
    summary_data = dashboard_summary(db)
    top_over = top_overpayments(db, limit=5)
    discipline = restaurants_discipline(db)
    sups = list_suppliers(db, only_with_quotes=True)
    unmapped = list_unmapped(db, limit=10)

    return {
        "period": "за весь период загруженных данных",
        "total_overpayment": summary_data["total_overpayment"],
        "total_external_purchases": summary_data["total_external_purchases"],
        "discipline_pct": summary_data["discipline_pct"],
        "status_counts": summary_data["status_counts"],
        "top_overpayments": top_over,
        "restaurants_discipline": discipline[:8],
        "suppliers_with_quotes": [{"name": s["name"], "quotes": s["quotes_count"], "top1": s["top1_count"]} for s in sups],
        "unmapped_examples": [{"name": u["raw_name"], "count": u["occurrence_count"]} for u in unmapped],
    }


@app.post("/api/ai/ceo_summary")
def generate_ceo_summary(db: Session = Depends(get_db), force: bool = Query(False),
                         user: User = Depends(require_role("buyer"))):
    """Генерирует CEO-сводку через OpenRouter (Claude). Кеширует на час."""
    if not force and _ai_cache.get("summary") and _ai_cache.get("generated_at"):
        age = (datetime.utcnow() - _ai_cache["generated_at"]).total_seconds()
        if age < 3600:
            return {"summary_md": _ai_cache["summary"], "generated_at": _ai_cache["generated_at"].isoformat(), "cached": True}

    ctx = _build_ceo_context(db)
    prompt = f"""Ты — управленческий аналитик закупок ресторанной сети «Максим» (Тюмень).
Сделай краткую сводку для CEO в формате Markdown.

Структура:
1. **Главная цифра** — одна-две строки (общая переплата vs Топ-2, доля закупок по Топ-2)
2. **Где теряем больше всего** — топ-3 позиции по переплате с пояснением что делать
3. **Дисциплина шефов** — топ-3 ресторана с худшей и лучшей дисциплиной
4. **Что требует внимания закупщика** — нераспознанные позиции, нужно дополнить карту
5. **Рекомендации** — 2-3 конкретных действия

Стиль: деловой, по делу, без воды. Цифры в рублях с пробелами как разделителями.
Если данных мало (например переплата = 0, мало совпадений) — честно скажи об этом, объясни почему
(период факта может не совпадать с актуальными ценами, не все поставщики ещё подгрузили цены).

ДАННЫЕ:
{json.dumps(ctx, ensure_ascii=False, indent=2)}
"""
    text = _call_openrouter(prompt)
    _ai_cache["summary"] = text
    _ai_cache["generated_at"] = datetime.utcnow()
    return {"summary_md": text, "generated_at": _ai_cache["generated_at"].isoformat(), "cached": False}


# ============ STATIC FRONTEND ============

PROTOTYPE_DIR = Path(__file__).parent.parent / "prototype"
if PROTOTYPE_DIR.exists():
    app.mount("/", StaticFiles(directory=str(PROTOTYPE_DIR), html=True), name="prototype")
