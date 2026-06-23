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

from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks, Response, UploadFile, File
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
    PriceQuote, PriceHistory, PriceChange, PendingPriceChange,
    PurchaseFact, Deviation,
    ImportRun, UnmappedItem, User,
)
from backend.auth import (
    current_user, require_role, verify_password, hash_password,
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
    return {"username": user.username, "role": user.role, "full_name": user.full_name,
            "supplier_id": user.supplier_id}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)):
    return {"username": user.username, "role": user.role, "full_name": user.full_name,
            "supplier_id": user.supplier_id}


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

# In-memory кэш контактов поставщиков из Google Sheets (TTL 5 минут).
# Файл "Контакты поставщиков" в той же папке Drive, обновляется заказчиком раз в неделю.
_CONTACTS_FILE_ID = "1YSPGoqCYvB9U-H7Pd5tMwbI3fE8mWqlXl0_elUXieqg"
_contacts_cache = {"data": {}, "fetched_at": 0.0, "error": None}


def _norm_supplier_name(s: str) -> str:
    """Нормализация имени поставщика для сопоставления (без кавычек, пробелов, регистра)."""
    if not s:
        return ""
    out = str(s).lower().strip()
    for ch in ('"', "'", "«", "»", "ё"):
        out = out.replace(ch, "е" if ch == "ё" else "")
    import re
    return re.sub(r"\s+", " ", out)


def _load_supplier_contacts(force: bool = False) -> dict[str, dict]:
    """Возвращает {normalized_name: {min_order, contact_name, phone, comment}}.
    TTL 5 минут — чтобы не дёргать Google Sheets на каждый запрос."""
    import time as _t
    if not force and _contacts_cache["data"] and (_t.time() - _contacts_cache["fetched_at"] < 300):
        return _contacts_cache["data"]
    try:
        import sys as _sys
        etl_path = str(Path(__file__).parent.parent / "etl")
        if etl_path not in _sys.path:
            _sys.path.insert(0, etl_path)
        from sync_master_to_suppliers import _get_services  # noqa
        _, sheets = _get_services()
        # Берём только первую вкладку — там всё
        meta = sheets.spreadsheets().get(spreadsheetId=_CONTACTS_FILE_ID, fields="sheets.properties").execute()
        title = meta["sheets"][0]["properties"]["title"]
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=_CONTACTS_FILE_ID, range=f"'{title}'!A2:E200"
        ).execute()
        data = {}
        for row in resp.get("values", []):
            if not row or not row[0]:
                continue
            name = str(row[0]).strip()
            data[_norm_supplier_name(name)] = {
                "raw_name": name,
                "min_order": (row[1] if len(row) > 1 else "") or "",
                "contact_name": (row[2] if len(row) > 2 else "") or "",
                "phone": (row[3] if len(row) > 3 else "") or "",
                "comment": (row[4] if len(row) > 4 else "") or "",
            }
        _contacts_cache["data"] = data
        _contacts_cache["fetched_at"] = _t.time()
        _contacts_cache["error"] = None
    except Exception as e:
        _contacts_cache["error"] = f"{type(e).__name__}: {e}"
    return _contacts_cache["data"]


@app.post("/api/suppliers/contacts/refresh")
def refresh_supplier_contacts(user: User = Depends(require_role("buyer"))):
    """Принудительно перечитывает контакты из Google Sheets (раз в неделю заказчик
    обновляет файл, а кэш TTL 5 минут — этот эндпоинт сбрасывает кэш моментально)."""
    _load_supplier_contacts(force=True)
    return {
        "loaded": len(_contacts_cache["data"]),
        "error": _contacts_cache["error"],
    }


@app.get("/api/suppliers")
def list_suppliers(
    db: Session = Depends(get_db),
    only_with_quotes: bool = Query(False),
):
    """Список поставщиков + сколько у них позиций + дата последнего обновления + сколько раз они Топ-1
    + контакты из Google Sheets (имя, телефон, мин. сумма заказа, комментарий)."""
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

    contacts = _load_supplier_contacts()

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
        contact = contacts.get(_norm_supplier_name(s.name)) or {}
        result.append({
            "id": s.id, "name": s.name,
            "quotes_count": q_count,
            "top1_count": top1_counter.get(s.id, 0),
            "categories": sorted(cats_by_supplier.get(s.id, [])),
            "last_updated": last_updated.isoformat() if last_updated else None,
            "min_order": contact.get("min_order", ""),
            "contact_name": contact.get("contact_name", ""),
            "phone": contact.get("phone", ""),
            "comment": contact.get("comment", ""),
            "has_contact": bool(contact),
        })
    # Сначала те у кого есть цены
    result.sort(key=lambda x: (-x["quotes_count"], x["name"]))
    return result


# ============ ПОРТАЛ ПОСТАВЩИКА (этап 3) ============
# Поставщик логинится своим аккаунтом и заполняет цены прямо в приложении,
# вместо Google Sheets. Видит все позиции мастер-матрицы, разложенные по
# категориям-вкладкам. Названия только для чтения, вводит цену + комментарий.

import re as _re_portal


def _translit(s: str) -> str:
    table = {
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z',
        'и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
        'с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'c','ч':'ch','ш':'sh','щ':'sch',
        'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
    }
    out = []
    for ch in (s or "").lower():
        out.append(table.get(ch, ch if ch.isalnum() else ""))
    return "".join(out)


def _resolve_portal_supplier(db: Session, user: User, supplier_id: Optional[int]) -> Supplier:
    """Возвращает поставщика, чей портал смотрим.
    - supplier: всегда свой (param игнорируется)
    - buyer: обязателен supplier_id (мастер-вход — закупщик заходит за поставщика)
    """
    if user.role == "supplier":
        if not user.supplier_id:
            raise HTTPException(400, "Аккаунт не привязан к поставщику")
        sup = db.get(Supplier, user.supplier_id)
    elif user.role == "buyer":
        if not supplier_id:
            raise HTTPException(400, "Нужен supplier_id")
        sup = db.get(Supplier, supplier_id)
    else:
        raise HTTPException(403, "Нет доступа к порталу")
    if not sup:
        raise HTTPException(404, "Поставщик не найден")
    return sup


@app.get("/api/portal/positions")
def portal_positions(db: Session = Depends(get_db),
                     supplier_id: Optional[int] = None,
                     user: User = Depends(current_user)):
    """Все мастер-позиции по категориям + цена/комментарий поставщика.
    Если у поставщика есть НЕподтверждённое изменение (pending) — показываем его
    значение с флагом pending=true (поставщик видит что ввёл, ждёт проверки)."""
    sup = _resolve_portal_supplier(db, user, supplier_id)

    # живые цены: {pm_id: (price, comment)}
    live = {}
    for pq in db.execute(select(PriceQuote).where(PriceQuote.supplier_id == sup.id)).scalars():
        live[pq.product_master_id] = (pq.unit_price, pq.supplier_comment or "")
    # pending (на проверке): {pm_id: (price, comment)}
    pend = {}
    for pc in db.execute(select(PendingPriceChange).where(PendingPriceChange.supplier_id == sup.id)).scalars():
        pend[pc.product_master_id] = (pc.new_price, pc.new_comment or "")

    rows = db.execute(
        select(ProductMaster, Category)
        .join(Category, Category.id == ProductMaster.category_id)
        .order_by(Category.name, ProductMaster.name)
    ).all()

    cats: dict[str, dict] = {}
    for pm, cat in rows:
        c = cats.setdefault(cat.name, {
            "category": cat.name, "unit_type": cat.unit_type, "positions": [],
        })
        is_pending = pm.id in pend
        if is_pending:
            price, comment = pend[pm.id]
        else:
            price, comment = live.get(pm.id, (None, ""))
        c["positions"].append({
            "product_id": pm.id,
            "product": pm.name,
            "price": price,
            "comment": comment,
            "pending": is_pending,
            "has_photo": pm.has_photo,
        })

    cats_list = sorted(cats.values(), key=lambda x: x["category"].casefold())
    return {
        "supplier": sup.name,
        "supplier_id": sup.id,
        "is_buyer": user.role == "buyer",   # закупщик в режиме мастер-входа
        "categories": cats_list,
        "total_positions": sum(len(c["positions"]) for c in cats_list),
        "filled": sum(1 for c in cats_list for p in c["positions"] if p["price"] is not None),
        "pending_count": len(pend),
    }


@app.get("/api/portal/market")
def portal_market(db: Session = Depends(get_db),
                  supplier_id: Optional[int] = None,
                  user: User = Depends(current_user)):
    """Анонимный Топ-3 рынка для поставщика: по каждой позиции 3 минимальные цены
    БЕЗ имён конкурентов + своя цена и своё место. Мотивирует снижать цену."""
    sup = _resolve_portal_supplier(db, user, supplier_id)

    # все цены по позициям: {pm_id: [(price, supplier_id), ...]}
    by_pm: dict[int, list] = {}
    for pq in db.execute(select(PriceQuote)).scalars():
        by_pm.setdefault(pq.product_master_id, []).append((pq.unit_price, pq.supplier_id))

    rows = db.execute(
        select(ProductMaster, Category)
        .join(Category, Category.id == ProductMaster.category_id)
        .order_by(Category.name, ProductMaster.name)
    ).all()

    cats: dict[str, dict] = {}
    for pm, cat in rows:
        quotes = sorted(by_pm.get(pm.id, []), key=lambda x: x[0])
        prices = [q[0] for q in quotes]
        my_price = next((p for p, sid in quotes if sid == sup.id), None)
        my_rank = None
        if my_price is not None:
            my_rank = next((i + 1 for i, (p, sid) in enumerate(quotes) if sid == sup.id), None)
        # Показываем ТОЛЬКО позиции с реальной конкуренцией (>=2 поставщика).
        # Где поставщик монополист — не светим, иначе он догадается поднять цену.
        if len(prices) < 2:
            continue
        c = cats.setdefault(cat.name, {"category": cat.name, "unit_type": cat.unit_type, "positions": []})
        c["positions"].append({
            "product_id": pm.id,
            "product": pm.name,
            "top1": prices[0] if len(prices) > 0 else None,
            "top2": prices[1] if len(prices) > 1 else None,
            "top3": prices[2] if len(prices) > 2 else None,
            "my_price": my_price,
            "my_rank": my_rank,
            "suppliers_count": len(prices),
        })
    cats_list = sorted(cats.values(), key=lambda x: x["category"].casefold())
    return {"supplier": sup.name, "categories": cats_list}


class PortalSaveItem(BaseModel):
    product_id: int
    price: Optional[float] = None
    comment: Optional[str] = None


class PortalSaveBody(BaseModel):
    items: list[PortalSaveItem]
    supplier_id: Optional[int] = None   # для мастер-входа закупщика


def _cat_unit_map(db: Session) -> dict[int, str]:
    return {pm.id: cat.unit_type for pm, cat in db.execute(
        select(ProductMaster, Category).join(Category, Category.id == ProductMaster.category_id)
    ).all()}


def _apply_live_price(db: Session, sup_id: int, pm_id: int, price: Optional[float],
                      comment: Optional[str], unit_type: str, now: datetime):
    """Записывает цену в ЖИВЫЕ price_quotes (видят шефы) + история/изменения.
    price None/<=0 → удаляет позицию у поставщика. Возвращает 'saved'/'removed'/None."""
    existing = db.execute(
        select(PriceQuote).where(PriceQuote.supplier_id == sup_id)
        .where(PriceQuote.product_master_id == pm_id)
    ).scalar_one_or_none()
    if price is None or price <= 0:
        if existing:
            db.delete(existing)
            return "removed"
        return None
    if existing:
        if existing.unit_price != price:
            db.add(PriceHistory(supplier_id=sup_id, product_master_id=pm_id,
                                unit_price=existing.unit_price, captured_at=existing.captured_at))
            if existing.unit_price and existing.unit_price >= 2 and price >= 2:
                delta = (price - existing.unit_price) / existing.unit_price * 100
                if abs(delta) <= 200:
                    db.add(PriceChange(supplier_id=sup_id, product_master_id=pm_id,
                                       old_price=existing.unit_price, new_price=price,
                                       delta_pct=delta, changed_at=now))
        existing.unit_price = price
        existing.unit_type = unit_type
        existing.supplier_comment = comment
        existing.captured_at = now
    else:
        db.add(PriceQuote(supplier_id=sup_id, product_master_id=pm_id,
                          unit_price=price, unit_type=unit_type,
                          supplier_comment=comment, captured_at=now))
    return "saved"


@app.post("/api/portal/save")
def portal_save(body: PortalSaveBody, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    """Сохранение цен из портала.
    - Поставщик: цены идут в pending_price_changes (на проверку закупщику),
      в price_quotes НЕ попадают пока закупщик не подтвердит.
    - Закупщик (мастер-вход): пишет сразу в живые price_quotes (он же и есть
      контроль) — без очереди на проверку.
    """
    sup = _resolve_portal_supplier(db, user, body.supplier_id)
    cat_unit = _cat_unit_map(db)
    now = datetime.utcnow()
    is_buyer = user.role == "buyer"
    saved = pending = removed = 0

    for it in body.items:
        pm = db.get(ProductMaster, it.product_id)
        if not pm:
            continue
        unit_type = cat_unit.get(it.product_id, "pkg")
        price = it.price
        comment = (it.comment or "").strip() or None

        if is_buyer:
            r = _apply_live_price(db, sup.id, it.product_id, price, comment, unit_type, now)
            if r == "saved": saved += 1
            elif r == "removed": removed += 1
        else:
            # поставщик → pending (upsert). Сохраняем старую живую цену для сравнения.
            live = db.execute(
                select(PriceQuote).where(PriceQuote.supplier_id == sup.id)
                .where(PriceQuote.product_master_id == it.product_id)
            ).scalar_one_or_none()
            existing_pending = db.execute(
                select(PendingPriceChange).where(PendingPriceChange.supplier_id == sup.id)
                .where(PendingPriceChange.product_master_id == it.product_id)
            ).scalar_one_or_none()
            new_price = price if (price and price > 0) else None
            old_price = live.unit_price if live else None
            # Если значение совпадает с живым и нет смысла в pending — пропускаем/чистим
            if new_price == old_price and (comment or "") == ((live.supplier_comment if live else "") or ""):
                if existing_pending:
                    db.delete(existing_pending)
                continue
            if existing_pending:
                existing_pending.new_price = new_price
                existing_pending.new_comment = comment
                existing_pending.old_price = old_price
                existing_pending.unit_type = unit_type
                existing_pending.created_at = now
            else:
                db.add(PendingPriceChange(
                    supplier_id=sup.id, product_master_id=it.product_id,
                    new_price=new_price, new_comment=comment,
                    old_price=old_price, unit_type=unit_type, created_at=now,
                ))
            pending += 1

    db.commit()
    return {"saved": saved, "removed": removed, "pending": pending, "is_buyer": is_buyer}


# ============ АЦЕПТ: ревью изменений цен закупщиком ============

@app.get("/api/pending-changes")
def list_pending_changes(db: Session = Depends(get_db),
                         user: User = Depends(require_role("buyer"))):
    """Все НЕподтверждённые изменения цен от поставщиков, сгруппированы по
    поставщику. Закупщик проверяет (↑↓) и принимает/отклоняет."""
    rows = db.execute(
        select(PendingPriceChange, Supplier, ProductMaster, Category)
        .join(Supplier, Supplier.id == PendingPriceChange.supplier_id)
        .join(ProductMaster, ProductMaster.id == PendingPriceChange.product_master_id)
        .join(Category, Category.id == ProductMaster.category_id)
        .order_by(Supplier.name, Category.name, ProductMaster.name)
    ).all()
    by_sup: dict[int, dict] = {}
    for pc, sup, pm, cat in rows:
        s = by_sup.setdefault(sup.id, {"supplier_id": sup.id, "supplier": sup.name, "items": []})
        delta_pct = None
        if pc.old_price and pc.new_price and pc.old_price > 0:
            delta_pct = round((pc.new_price - pc.old_price) / pc.old_price * 100, 1)
        s["items"].append({
            "id": pc.id, "product_id": pm.id, "product": pm.name, "category": cat.name,
            "old_price": pc.old_price, "new_price": pc.new_price,
            "comment": pc.new_comment, "unit_type": pc.unit_type, "delta_pct": delta_pct,
        })
    result = sorted(by_sup.values(), key=lambda x: x["supplier"].casefold())
    return {"suppliers": result, "total": sum(len(s["items"]) for s in result)}


class PendingActionBody(BaseModel):
    ids: list[int]


@app.post("/api/pending-changes/approve")
def approve_pending(body: PendingActionBody, db: Session = Depends(get_db),
                    user: User = Depends(require_role("buyer"))):
    """Принимает выбранные pending-изменения → применяет к живым ценам."""
    now = datetime.utcnow()
    cat_unit = _cat_unit_map(db)
    applied = 0
    for pid in body.ids:
        pc = db.get(PendingPriceChange, pid)
        if not pc:
            continue
        unit_type = pc.unit_type or cat_unit.get(pc.product_master_id, "pkg")
        _apply_live_price(db, pc.supplier_id, pc.product_master_id, pc.new_price,
                          pc.new_comment, unit_type, now)
        db.delete(pc)
        applied += 1
    db.commit()
    return {"applied": applied}


@app.post("/api/pending-changes/reject")
def reject_pending(body: PendingActionBody, db: Session = Depends(get_db),
                   user: User = Depends(require_role("buyer"))):
    """Отклоняет выбранные pending-изменения (просто удаляет, живые цены не трогаются)."""
    rejected = 0
    for pid in body.ids:
        pc = db.get(PendingPriceChange, pid)
        if pc:
            db.delete(pc)
            rejected += 1
    db.commit()
    return {"rejected": rejected}


# ============ ФОТО-ЭТАЛОНЫ ПОЗИЦИЙ ============
# Закупщик грузит фото-эталон позиции (Пармезан vs Реджанито — чтобы не путали).
# Видят и шефы, и поставщики. Хранятся файлами в /data/photos.

_PHOTOS_DIR = Path(os.environ.get("DB_PATH", "/data/data.db")).parent / "photos"
_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/api/products/{product_id}/photo")
async def upload_product_photo(product_id: int, file: UploadFile = File(...),
                               db: Session = Depends(get_db),
                               user: User = Depends(require_role("buyer"))):
    """Загрузка фото-эталона позиции (только закупщик)."""
    pm = db.get(ProductMaster, product_id)
    if not pm:
        raise HTTPException(404, "Позиция не найдена")
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 8 МБ")
    if not data:
        raise HTTPException(400, "Пустой файл")
    (_PHOTOS_DIR / f"{product_id}.jpg").write_bytes(data)
    pm.has_photo = True
    db.commit()
    return {"ok": True, "product_id": product_id}


@app.get("/api/products/{product_id}/photo")
def get_product_photo(product_id: int):
    """Отдаёт фото позиции. Доступно всем залогиненным (шеф, поставщик, закупщик)."""
    p = _PHOTOS_DIR / f"{product_id}.jpg"
    if not p.exists():
        raise HTTPException(404, "Нет фото")
    return FileResponse(str(p), media_type="image/jpeg")


@app.delete("/api/products/{product_id}/photo")
def delete_product_photo(product_id: int, db: Session = Depends(get_db),
                         user: User = Depends(require_role("buyer"))):
    pm = db.get(ProductMaster, product_id)
    if pm:
        pm.has_photo = False
        db.commit()
    p = _PHOTOS_DIR / f"{product_id}.jpg"
    if p.exists():
        p.unlink()
    return {"ok": True}


# ============ АДМИНКА АККАУНТОВ ПОСТАВЩИКОВ (для закупщика) ============

@app.get("/api/supplier-accounts")
def list_supplier_accounts(db: Session = Depends(get_db),
                           user: User = Depends(require_role("buyer"))):
    """Список МАТРИЧНЫХ поставщиков (у кого есть цены) + есть ли у них логин.
    Фактовые поставщики из выгрузок iiko/SH (Инстамарт и т.п.) не показываем —
    им портал не нужен."""
    accounts = {}
    for u in db.execute(select(User).where(User.role == "supplier")).scalars():
        if u.supplier_id:
            accounts[u.supplier_id] = u
    # матричные = есть хоть одна цена ИЛИ уже есть логин
    sup_ids_with_quotes = {r[0] for r in db.execute(
        select(PriceQuote.supplier_id).distinct()
    ).all()}
    suppliers = db.execute(
        select(Supplier).where(Supplier.is_internal == False).order_by(Supplier.name)
    ).scalars().all()
    out = []
    for s in suppliers:
        if s.id not in sup_ids_with_quotes and s.id not in accounts:
            continue
        acc = accounts.get(s.id)
        out.append({
            "supplier_id": s.id,
            "name": s.name,
            "has_login": bool(acc),
            "username": acc.username if acc else None,
            "is_active": acc.is_active if acc else None,
        })
    return out


class CreateSupplierAccount(BaseModel):
    supplier_id: int
    password: Optional[str] = None  # если не задан — сгенерим


@app.post("/api/supplier-accounts")
def create_supplier_account(body: CreateSupplierAccount, db: Session = Depends(get_db),
                            user: User = Depends(require_role("buyer"))):
    """Создаёт (или сбрасывает пароль) логин поставщика для портала.
    Возвращает username + password в открытом виде ОДИН РАЗ — чтобы закупщик
    передал поставщику."""
    sup = db.get(Supplier, body.supplier_id)
    if not sup:
        raise HTTPException(404, "Поставщик не найден")

    import secrets as _secrets
    password = body.password or _secrets.token_urlsafe(6)

    existing = db.execute(
        select(User).where(User.role == "supplier").where(User.supplier_id == sup.id)
    ).scalar_one_or_none()

    if existing:
        existing.password_hash = hash_password(password)
        existing.is_active = True
        username = existing.username
    else:
        # генерим username из транслита имени, уникализируем
        base = _translit(sup.name)[:20] or f"sup{sup.id}"
        username = base
        n = 1
        while db.execute(select(User).where(User.username == username)).scalar_one_or_none():
            n += 1
            username = f"{base}{n}"
        db.add(User(
            username=username, password_hash=hash_password(password),
            role="supplier", supplier_id=sup.id, full_name=sup.name, is_active=True,
        ))
    db.commit()
    return {"username": username, "password": password, "supplier": sup.name}


@app.post("/api/supplier-accounts/{supplier_id}/toggle")
def toggle_supplier_account(supplier_id: int, db: Session = Depends(get_db),
                            user: User = Depends(require_role("buyer"))):
    """Включить/выключить доступ поставщика."""
    acc = db.execute(
        select(User).where(User.role == "supplier").where(User.supplier_id == supplier_id)
    ).scalar_one_or_none()
    if not acc:
        raise HTTPException(404, "Аккаунт не найден")
    acc.is_active = not acc.is_active
    db.commit()
    return {"supplier_id": supplier_id, "is_active": acc.is_active}


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
            "unit_type": pq.unit_type, "has_photo": pm.has_photo, "quotes": [],
        })
        item["quotes"].append({
            "supplier_id": sup.id, "supplier": sup.name,
            "price": pq.unit_price,
            "supplier_comment": pq.supplier_comment or "",
        })

    result = []
    for item in items_map.values():
        item["quotes"].sort(key=lambda x: x["price"])
        qs = item["quotes"]
        top1 = qs[0]
        top2 = qs[1] if len(qs) > 1 else None
        top3 = qs[2] if len(qs) > 2 else None
        result.append({
            "product_id": item["product_id"],
            "product": item["product"],
            "category": item["category"],
            "unit_type": item["unit_type"],
            "has_photo": item.get("has_photo", False),
            "top1": top1,
            "top2": top2,
            "top3": top3,
            "suppliers_count": len(qs),
        })

    # Сортировка: сплошной алфавитный список по наименованию (без учёта регистра).
    # Раньше делили на две группы (с top2 / без), но это давало два «прохода» алфавита
    # с разрывом посередине — заказчик ожидает один сплошной А→Я.
    result.sort(key=lambda x: (x["product"].casefold(), x["category"]))
    return result[:limit]


# ============ СВОДНАЯ ПО ПОСТАВЩИКАМ ============

@app.get("/api/summary")
def get_summary(
    db: Session = Depends(get_db),
    category: Optional[str] = None,
):
    """Сводная таблица: каждое мастер-наименование × каждый поставщик → цена.

    Возвращает:
        {
          "suppliers": ["Поставщик 1", ...],   // отсортированы по числу заполнений (desc)
          "categories": [...],
          "rows": [{
              "product_id", "product", "category", "unit_type",
              "prices": {"Поставщик 1": 123.0, ...},  // только те у кого есть цена
              "min_price", "max_price", "avg_price", "suppliers_count"
          }, ...]
        }
    """
    # Подтянем все мастер-позиции и все цены к ним
    q = (
        select(PriceQuote, ProductMaster, Category, Supplier)
        .join(ProductMaster, PriceQuote.product_master_id == ProductMaster.id)
        .join(Category, ProductMaster.category_id == Category.id)
        .join(Supplier, PriceQuote.supplier_id == Supplier.id)
    )
    if category:
        q = q.where(Category.name == category)

    by_product: dict[int, dict] = {}
    sup_freq: dict[str, int] = {}
    for pq, pm, cat, sup in db.execute(q).all():
        item = by_product.setdefault(pm.id, {
            "product_id": pm.id, "product": pm.name, "category": cat.name,
            "unit_type": pq.unit_type, "prices": {}, "comments": {},
        })
        item["prices"][sup.name] = pq.unit_price
        if pq.supplier_comment:
            item["comments"][sup.name] = pq.supplier_comment
        # последняя unit_type «победит» — но по факту они одинаковы внутри одной позиции
        item["unit_type"] = pq.unit_type
        sup_freq[sup.name] = sup_freq.get(sup.name, 0) + 1

    # Список поставщиков — по убыванию числа заполненных цен (самый «полный» левее)
    suppliers_sorted = [s for s, _ in sorted(sup_freq.items(), key=lambda x: (-x[1], x[0].casefold()))]

    rows = []
    for item in by_product.values():
        prices = item["prices"]
        if not prices:
            continue
        vals = list(prices.values())
        rows.append({
            "product_id": item["product_id"],
            "product": item["product"],
            "category": item["category"],
            "unit_type": item["unit_type"],
            "prices": prices,
            "comments": item.get("comments", {}),
            "min_price": min(vals),
            "max_price": max(vals),
            "avg_price": sum(vals) / len(vals),
            "suppliers_count": len(vals),
        })
    rows.sort(key=lambda x: (x["category"], x["product"].casefold()))

    # Все категории (даже без цен — чтобы UI показал все вкладки)
    categories = sorted({c.name for c in db.execute(select(Category)).scalars()})

    return {
        "suppliers": suppliers_sorted,
        "categories": categories,
        "rows": rows,
    }


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


# ============ АВТО-СОПОСТАВЛЕНИЯ (этап 2) ============

def _normalize_for_match(s: str) -> str:
    """Глубокая нормализация для fuzzy-сравнения: убираем '*', единицы измерения,
    слова 'свежий/свежие/охл./с/м/охлаждённый' и т.п."""
    import re as _re
    if not s:
        return ""
    out = str(s).lower()
    out = out.replace("*", " ").replace("ё", "е")
    # Числа+единицы
    out = _re.sub(r"\d+[\.,]?\d*\s*(кг|г|мл|л|шт|уп|гр|грамм|литр)\b", " ", out)
    out = _re.sub(r"\b(\d+[\.,]?\d*)\b", " ", out)
    # Стоп-слова
    stop = ["свежий", "свежие", "свежая", "свежее", "охлажденный", "охл.", "охл",
            "с/м", "с/с", "с/о", "б/у", "б/к", "б\\к", "б/г",
            "филе", "натур.", "натур", "конс.", "конс",
            "марк.", "марк", "штучн.", "штучн", "размер"]
    for w in stop:
        out = out.replace(w, " ")
    # Знаки препинания + слэши
    out = _re.sub(r"[^a-zа-я0-9 ]+", " ", out)
    out = _re.sub(r"\s+", " ", out).strip()
    return out


@app.get("/api/mapping/suggest")
def mapping_suggest(
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
    min_confidence: float = Query(0.40, ge=0.0, le=1.0),
):
    """Для топа нераспознанных позиций предлагаем кандидатов из мастер-матрицы
    с confidence-скорингом через difflib.SequenceMatcher. Возвращаем по 3 варианта."""
    from difflib import SequenceMatcher

    # Кеш всех ProductMaster (~750 шт) для fuzzy match
    masters = db.execute(
        select(ProductMaster.id, ProductMaster.name, Category.name)
        .join(Category, Category.id == ProductMaster.category_id)
    ).all()
    master_keys = [(pm_id, name, cat, _normalize_for_match(name)) for pm_id, name, cat in masters]

    unmapped = db.execute(
        select(UnmappedItem).order_by(desc(UnmappedItem.occurrence_count)).limit(limit)
    ).scalars().all()

    out = []
    for u in unmapped:
        raw_norm = _normalize_for_match(u.raw_name)
        if not raw_norm:
            continue
        scored = []
        for pm_id, pm_name, cat, pm_norm in master_keys:
            r = SequenceMatcher(None, raw_norm, pm_norm).ratio()
            if r >= min_confidence:
                scored.append((r, pm_id, pm_name, cat))
        scored.sort(key=lambda x: -x[0])
        candidates = [
            {"product_id": pm_id, "product": pm_name, "category": cat, "confidence": round(r, 3)}
            for r, pm_id, pm_name, cat in scored[:3]
        ]
        out.append({
            "id": u.id,
            "raw_name": u.raw_name,
            "source": u.source,
            "occurrence_count": u.occurrence_count,
            "candidates": candidates,
        })
    return out


class MappingItem(BaseModel):
    unmapped_id: int
    master_product_id: int


class MappingBulkConfirm(BaseModel):
    items: list[MappingItem]


@app.post("/api/mapping/bulk_confirm")
def mapping_bulk_confirm(
    body: MappingBulkConfirm,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("buyer")),
):
    """Сохраняет подтверждённые сопоставления:
    1) В БД — AccountingAlias (system по source UnmappedItem).
    2) В Google Sheets «Карта сопоставлений» / вкладка «Авто-сопоставления»
       (дописывает новые строки чтобы заказчик мог их видеть и редактировать).
    """
    from backend.models import AccountingSystem, AccountingAlias as AA
    from backend.importer import normalize as _norm
    saved_db = 0
    sheets_rows = []
    sys_cache = {s.name.lower(): s for s in db.execute(select(AccountingSystem)).scalars()}

    for it in body.items:
        u = db.get(UnmappedItem, it.unmapped_id)
        pm = db.get(ProductMaster, it.master_product_id)
        if not u or not pm:
            continue
        # Определяем систему учёта по source
        src = (u.source or "").lower()
        sys = sys_cache.get("sh" if src == "storehouse" else "iiko") \
              or sys_cache.get(src) or list(sys_cache.values())[0]
        # Создаём alias если ещё нет
        existing = db.execute(
            select(AA).where(AA.system_id == sys.id).where(AA.name_normalized == _norm(u.raw_name))
        ).scalar_one_or_none()
        if not existing:
            db.add(AA(
                product_master_id=pm.id, system_id=sys.id,
                name=u.raw_name, name_normalized=_norm(u.raw_name),
            ))
            saved_db += 1
        sheets_rows.append([u.raw_name, pm.name, u.source, "✓", datetime.utcnow().date().isoformat()])
        # Убираем из нераспознанных
        db.delete(u)

    db.commit()

    # Дописываем в Google Sheets «Карта сопоставлений» / вкладка «Авто-сопоставления»
    sheets_appended = 0
    sheets_error = None
    if sheets_rows:
        try:
            import sys as _sys
            etl_path = str(Path(__file__).parent.parent / "etl")
            if etl_path not in _sys.path:
                _sys.path.insert(0, etl_path)
            from sync_master_to_suppliers import _get_services, _list_files  # noqa
            drive, sheets = _get_services()
            # Находим файл «Карта сопоставлений»
            files = _list_files(drive)
            mapping_file = next((f for f in files if f["name"].startswith("Карта сопоставлений")
                                 and f["mimeType"] == "application/vnd.google-apps.spreadsheet"), None)
            if mapping_file:
                meta = sheets.spreadsheets().get(spreadsheetId=mapping_file["id"], fields="sheets.properties").execute()
                titles = [s["properties"]["title"] for s in meta["sheets"]]
                AUTO_TAB = "Авто-сопоставления"
                if AUTO_TAB not in titles:
                    # Создаём вкладку и шапку
                    sheets.spreadsheets().batchUpdate(
                        spreadsheetId=mapping_file["id"],
                        body={"requests": [{"addSheet": {"properties": {"title": AUTO_TAB}}}]},
                    ).execute()
                    header = [["Из выгрузки (как пришло)", "Мастер-позиция", "Источник", "Подтверждено", "Дата"]]
                    sheets.spreadsheets().values().update(
                        spreadsheetId=mapping_file["id"],
                        range=f"'{AUTO_TAB}'!A1",
                        valueInputOption="USER_ENTERED",
                        body={"values": header},
                    ).execute()
                sheets.spreadsheets().values().append(
                    spreadsheetId=mapping_file["id"],
                    range=f"'{AUTO_TAB}'!A:E",
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": sheets_rows},
                ).execute()
                sheets_appended = len(sheets_rows)
        except Exception as e:
            sheets_error = f"{type(e).__name__}: {e}"

    return {
        "saved_db": saved_db,
        "sheets_appended": sheets_appended,
        "sheets_error": sheets_error,
    }


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
    _master_sync_state["db_refresh"] = None
    try:
        # Импортируем здесь чтобы избежать конфликта при тестах
        etl_path = str(Path(__file__).parent.parent / "etl")
        if etl_path not in _sys.path:
            _sys.path.insert(0, etl_path)
        from sync_master_to_suppliers import sync as do_sync  # noqa
        result = do_sync(dry_run=dry_run, prune=prune)
        _master_sync_state["result"] = result
        _master_sync_state["status"] = "ok"

        # После реального prune-sync — автоматически обновляем БД (drive_sync + importer),
        # чтобы Топ-2 и анализ сразу показывали свежие цены.
        if not dry_run:
            root = Path(__file__).parent.parent
            _master_sync_state["db_refresh"] = {"status": "running"}
            drive_rc, importer_rc = None, None
            drive_err, importer_err = "", ""
            stage = "drive_sync"
            try:
                # Таймауты подняли до 30 мин: при 30+ поставщиках и Sheets-throttle всё может занять много времени.
                r1 = subprocess.run(
                    [sys.executable, "-m", "etl.sync_from_drive"],
                    cwd=str(root), capture_output=True, text=True, timeout=1800,
                )
                drive_rc = r1.returncode
                drive_err = (r1.stderr or "")[-800:]
                stage = "importer"
                r2 = subprocess.run(
                    [sys.executable, "-m", "backend.importer"],
                    cwd=str(root), capture_output=True, text=True, timeout=1800,
                )
                importer_rc = r2.returncode
                importer_err = (r2.stderr or "")[-800:]
            except subprocess.TimeoutExpired as te:
                # Записываем что именно упало по таймауту
                _master_sync_state["db_refresh"] = {
                    "status": "error",
                    "error": f"{stage} timeout: {te}",
                    "drive_sync_rc": drive_rc,
                    "importer_rc": importer_rc,
                    "drive_sync_err": drive_err,
                    "importer_err": importer_err,
                }
            except Exception as e2:
                import traceback
                _master_sync_state["db_refresh"] = {
                    "status": "error",
                    "error": f"{stage}: {type(e2).__name__}: {e2}\n{traceback.format_exc()[-500:]}",
                    "drive_sync_rc": drive_rc,
                    "importer_rc": importer_rc,
                    "drive_sync_err": drive_err,
                    "importer_err": importer_err,
                }
            else:
                _master_sync_state["db_refresh"] = {
                    "status": "ok" if drive_rc == 0 and importer_rc == 0 else "error",
                    "drive_sync_rc": drive_rc,
                    "importer_rc": importer_rc,
                    "drive_sync_err": drive_err if drive_rc else "",
                    "importer_err": importer_err if importer_rc else "",
                }
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
    direction: str = Query("all", description="all | up | down"),
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Поиск по наименованию"),
    sort: str = Query("changed_at", description="changed_at | delta_pct"),
    limit: int = 500,
):
    """Реальные изменения цен (не snapshot'ы). По умолчанию — свежее сверху."""
    from datetime import timedelta
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", ""))
        except Exception:
            raise HTTPException(400, "Bad since format, expected ISO datetime")
    else:
        since_dt = datetime.utcnow() - timedelta(days=7)

    q = (
        select(PriceChange, Supplier, ProductMaster, Category)
        .join(Supplier, Supplier.id == PriceChange.supplier_id)
        .join(ProductMaster, ProductMaster.id == PriceChange.product_master_id)
        .join(Category, Category.id == ProductMaster.category_id)
        .where(PriceChange.changed_at >= since_dt)
        # Отсечь «фантомные» изменения: цены ниже 1₽ — это раньше парсер брал
        # мусор (фасовку, упаковку) из не той колонки. Сейчас парсер строгий,
        # но в истории остались такие фейки. Также скрываем delta_pct > 500%
        # — реальный рост цены так не происходит, это след старого мусора.
        # Жёсткий фильтр: цены меньше 2₽ — заведомо парсерный мусор (фасовка,
        # упаковка, единичка из старого шаблона). Реальные продукты столько
        # не стоят. Также |Δ%| ≤ 90% — реальное изменение цены за пару
        # месяцев не бывает больше; всё что выше — артефакт смены колонки.
        .where(PriceChange.old_price >= 2)
        .where(PriceChange.new_price >= 2)
        .where(func.abs(PriceChange.delta_pct) <= 90)
    )
    if direction == "up":
        q = q.where(PriceChange.delta_pct > 0)
    elif direction == "down":
        q = q.where(PriceChange.delta_pct < 0)
    if category:
        q = q.where(Category.name == category)
    if search:
        # SQLite LOWER не lowercase'ит кириллицу — используем name_normalized
        # (нормализованное Python'ом ещё при импорте мастер-матрицы).
        from backend.importer import normalize as _norm_search
        like = f"%{_norm_search(search)}%"
        q = q.where(ProductMaster.name_normalized.like(like))

    if sort == "delta_pct":
        q = q.order_by(desc(func.abs(PriceChange.delta_pct)))
    else:
        q = q.order_by(desc(PriceChange.changed_at))
    rows = db.execute(q.limit(limit)).all()
    return [
        {
            "id": pc.id,
            "supplier_id": s.id,
            "supplier": s.name,
            "product_id": pm.id,
            "product": pm.name,
            "category": cat.name,
            "old_price": pc.old_price,
            "new_price": pc.new_price,
            "delta_pct": pc.delta_pct,
            "changed_at": pc.changed_at.isoformat(),
        }
        for pc, s, pm, cat in rows
    ]


@app.get("/api/price_history")
def get_price_history(
    db: Session = Depends(get_db),
    supplier_id: int = Query(...),
    product_id: int = Query(...),
    days: int = Query(90, ge=1, le=730),
):
    """История цен по конкретной паре (поставщик, товар) для мини-графика."""
    from datetime import timedelta
    since_dt = datetime.utcnow() - timedelta(days=days)
    rows = db.execute(
        select(PriceHistory)
        .where(PriceHistory.supplier_id == supplier_id)
        .where(PriceHistory.product_master_id == product_id)
        .where(PriceHistory.captured_at >= since_dt)
        .order_by(PriceHistory.captured_at)
    ).scalars().all()
    return [
        {"date": h.captured_at.date().isoformat(), "price": h.unit_price}
        for h in rows
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
        # Те же фильтры что и в списке: убрать фейковые «миллионные» дельты
        # из старой истории парсера.
        # Жёсткий фильтр: цены меньше 2₽ — заведомо парсерный мусор (фасовка,
        # упаковка, единичка из старого шаблона). Реальные продукты столько
        # не стоят. Также |Δ%| ≤ 90% — реальное изменение цены за пару
        # месяцев не бывает больше; всё что выше — артефакт смены колонки.
        .where(PriceChange.old_price >= 2)
        .where(PriceChange.new_price >= 2)
        .where(func.abs(PriceChange.delta_pct) <= 90)
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

    biggest_rise = serialize(top_ups[:1])[0] if top_ups else None
    return {
        "since": since_dt.isoformat(),
        "total": total,
        "ups": len(ups),
        "downs": len(downs),
        "avg_up_pct": round(avg_up, 2),
        "avg_down_pct": round(avg_down, 2),
        "top_ups": serialize(top_ups),
        "top_downs": serialize(top_downs),
        "biggest_rise": biggest_rise,
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
