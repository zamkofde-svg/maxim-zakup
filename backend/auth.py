"""
Минимальная авторизация: 2 роли (buyer / chef), bcrypt-хеши, сессии через подписанные куки.

- buyer — закупщик/админ: видит всё
- chef  — шеф: видит только Анализ закупок + Рекомендации, может ставить причины
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional
import bcrypt
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import get_db, SessionLocal
from backend.models import User

# Секрет подписи токенов сессии. Если не задан в env — генерится случайно (рестарт = инвалидация всех сессий)
_SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
COOKIE_NAME = "mz_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 дней


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def make_session_token(user_id: int, role: str, username: str) -> str:
    """Подписанный JSON-токен: {payload}.{signature}"""
    payload = json.dumps({"u": user_id, "r": role, "n": username, "t": int(time.time())})
    sig = hmac.new(_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def parse_session_token(token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload)
        # проверка TTL (30 дней)
        if int(time.time()) - data.get("t", 0) > COOKIE_MAX_AGE:
            return None
        return data
    except Exception:
        return None


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: возвращает текущего пользователя или 401."""
    token = request.cookies.get(COOKIE_NAME)
    data = parse_session_token(token)
    if not data:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.get(User, data["u"])
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not active")
    return user


def require_role(*allowed_roles: str):
    """Dependency-фабрика для ограничения по ролям."""
    def _checker(user: User = Depends(current_user)) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail=f"Role {user.role} not allowed")
        return user
    return _checker


def ensure_default_users():
    """Создаёт дефолтных пользователей при первом старте если их нет."""
    db = SessionLocal()
    try:
        if db.scalar(select(User).limit(1)):
            return  # уже есть юзеры — не трогаем
        # Дефолтные креды можно переопределить через env
        buyer_pw = os.environ.get("DEFAULT_BUYER_PASSWORD", "zakupki2026")
        chef_pw = os.environ.get("DEFAULT_CHEF_PASSWORD", "chef2026")
        db.add(User(
            username="zakupki", password_hash=hash_password(buyer_pw),
            role="buyer", full_name="Отдел закупок", is_active=True,
        ))
        db.add(User(
            username="chef", password_hash=hash_password(chef_pw),
            role="chef", full_name="Шеф-повар", is_active=True,
        ))
        db.commit()
        print(f"[auth] созданы дефолтные пользователи: zakupki / chef", flush=True)
    finally:
        db.close()
