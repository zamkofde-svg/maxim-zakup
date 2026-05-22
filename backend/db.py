"""SQLite + SQLAlchemy: подключение и фабрика сессий."""
import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# Путь к БД через env (полезно в облаке где /app read-only). По дефолту — рядом с backend/
DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "data.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DB_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DB_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},  # SQLite + многопоточный FastAPI
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Создаёт все таблицы (если их нет). Для миграций потом — alembic."""
    from . import models  # noqa: F401 — регистрирует все модели в Base.metadata
    Base.metadata.create_all(engine)
