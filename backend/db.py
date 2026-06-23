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
    """Создаёт все таблицы (если их нет) + лёгкие in-place миграции для SQLite."""
    from . import models  # noqa: F401 — регистрирует все модели в Base.metadata
    Base.metadata.create_all(engine)
    _apply_lightweight_migrations()


def _apply_lightweight_migrations() -> None:
    """Добавляет колонки, которых ещё нет в существующей БД.
    SQLite ALTER TABLE ADD COLUMN не падает на nullable-полях — это безопасная операция.
    Для серьёзных миграций потом перейдём на Alembic.
    """
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    expected = {
        "price_quotes": [
            ("supplier_comment", "TEXT"),
        ],
        "users": [
            ("supplier_id", "INTEGER"),
        ],
        "products_master": [
            ("has_photo", "BOOLEAN DEFAULT 0"),
        ],
    }
    with engine.begin() as conn:
        for table, cols in expected.items():
            if table not in insp.get_table_names():
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col_name, col_type in cols:
                if col_name in existing:
                    continue
                # Безопасный ALTER — добавляет nullable колонку
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{col_name}" {col_type}'))
