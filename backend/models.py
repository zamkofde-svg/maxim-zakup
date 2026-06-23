"""
SQLAlchemy модели — схема БД.

Архитектурный принцип: всё нормализовано, без дублирования.
products_master — мастер-номенклатура из мастер-матрицы.
price_quotes — текущая цена поставщика на товар.
price_history — версионирование цен (для исторической сверки факта).
"""
from __future__ import annotations
from datetime import datetime, date
from typing import Optional

from sqlalchemy import (
    ForeignKey, String, Integer, Float, DateTime, Date,
    Boolean, Text, UniqueConstraint, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


# ============ СПРАВОЧНИКИ ============

class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    unit_type: Mapped[str] = mapped_column(String(16), default="pkg")
    # 'kg_or_l' для Сыры/Молочка, 'pkg' для остальных


class AccountingSystem(Base):
    """Система учёта: SH (StoreHouse), Chees, TEHNIKUM, Sorrento."""
    __tablename__ = "accounting_systems"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)


class Restaurant(Base):
    __tablename__ = "restaurants"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    sh_code: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True)
    # короткий код в выгрузке SH: Чум, Максимыч, Коф_СП, ...
    accounting_system_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounting_systems.id"))


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    name_normalized: Mapped[str] = mapped_column(String(256), index=True)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    # для Цех/Производство — это не внешний поставщик
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    aliases: Mapped[list["SupplierAlias"]] = relationship(back_populates="supplier", cascade="all, delete-orphan")


class SupplierAlias(Base):
    """Альтернативные написания поставщика (как в iiko/SH-выгрузках)."""
    __tablename__ = "supplier_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    alias: Mapped[str] = mapped_column(String(256))
    alias_normalized: Mapped[str] = mapped_column(String(256), index=True)

    supplier: Mapped[Supplier] = relationship(back_populates="aliases")
    __table_args__ = (UniqueConstraint("supplier_id", "alias_normalized"),)


# ============ МАСТЕР-НОМЕНКЛАТУРА ============

class ProductMaster(Base):
    """
    Мастер-позиция — единая точка истины для всех поставщиков и систем учёта.
    Источник: мастер-матрица. Имя = как в колонке A матриц поставщиков.
    """
    __tablename__ = "products_master"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), index=True)
    name: Mapped[str] = mapped_column(String(512))
    name_normalized: Mapped[str] = mapped_column(String(512), index=True)
    unit_label: Mapped[Optional[str]] = mapped_column(String(16))  # КГ/ШТ для Овощифрукты
    group_abc: Mapped[Optional[str]] = mapped_column(String(1))   # A/B/C — задаём руками

    category: Mapped[Category] = relationship()
    accounting_aliases: Mapped[list["AccountingAlias"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    quotes: Mapped[list["PriceQuote"]] = relationship(back_populates="product")

    __table_args__ = (UniqueConstraint("category_id", "name_normalized"),)


class AccountingAlias(Base):
    """
    Как мастер-позиция называется в конкретной системе учёта.
    Одна product_master может иметь N alias по разным системам,
    одно имя в одной системе может ссылаться на одну мастер (n-к-1).
    """
    __tablename__ = "accounting_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_master_id: Mapped[int] = mapped_column(ForeignKey("products_master.id"))
    system_id: Mapped[int] = mapped_column(ForeignKey("accounting_systems.id"))
    name: Mapped[str] = mapped_column(String(512))
    name_normalized: Mapped[str] = mapped_column(String(512))

    product: Mapped[ProductMaster] = relationship(back_populates="accounting_aliases")
    system: Mapped[AccountingSystem] = relationship()

    __table_args__ = (
        Index("ix_acct_alias_lookup", "system_id", "name_normalized"),
    )


# ============ ЦЕНЫ ============

class PriceQuote(Base):
    """Текущая (последняя) цена поставщика на позицию."""
    __tablename__ = "price_quotes"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    product_master_id: Mapped[int] = mapped_column(ForeignKey("products_master.id"), index=True)
    unit_price: Mapped[float] = mapped_column(Float)
    unit_type: Mapped[str] = mapped_column(String(16))   # kg_or_l / pkg
    pkg_net: Mapped[Optional[float]] = mapped_column(Float)
    pkg_gross: Mapped[Optional[float]] = mapped_column(Float)
    # Свободный текст, который ВПИСЫВАЕТ САМ ПОСТАВЩИК в свою матрицу
    # (колонка с заголовком «Комментарий» — типичные значения: страна происхождения,
    # фасовка, особенности партии).
    supplier_comment: Mapped[Optional[str]] = mapped_column(String, default=None)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    supplier: Mapped[Supplier] = relationship()
    product: Mapped[ProductMaster] = relationship(back_populates="quotes")

    __table_args__ = (UniqueConstraint("supplier_id", "product_master_id"),)


class PriceHistory(Base):
    """История изменений цен — старые версии PriceQuote."""
    __tablename__ = "price_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    product_master_id: Mapped[int] = mapped_column(ForeignKey("products_master.id"), index=True)
    unit_price: Mapped[float] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class PriceChange(Base):
    """Аудит реальных изменений цен (НЕ snapshot'ы). Для отчёта «что поменялось»."""
    __tablename__ = "price_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    product_master_id: Mapped[int] = mapped_column(ForeignKey("products_master.id"), index=True)
    old_price: Mapped[float] = mapped_column(Float)
    new_price: Mapped[float] = mapped_column(Float)
    delta_pct: Mapped[float] = mapped_column(Float)  # (new - old) / old * 100
    changed_at: Mapped[datetime] = mapped_column(DateTime, index=True, default=datetime.utcnow)


# ============ ФАКТ ЗАКУПОК ============

class PurchaseFact(Base):
    """Строка из выгрузки iiko/StoreHouse."""
    __tablename__ = "purchases_fact"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))     # iiko / storehouse
    source_file: Mapped[str] = mapped_column(String(256))
    date: Mapped[date] = mapped_column(Date, index=True)
    raw_product: Mapped[str] = mapped_column(String(512))     # как в выгрузке
    raw_supplier: Mapped[str] = mapped_column(String(256))
    raw_restaurant: Mapped[Optional[str]] = mapped_column(String(128))  # только в SH
    quantity: Mapped[float] = mapped_column(Float)
    unit_price: Mapped[float] = mapped_column(Float)
    total: Mapped[float] = mapped_column(Float)

    # Сопоставленные id-шки (могут быть NULL если не нашли)
    product_master_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products_master.id"), index=True)
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"), index=True)
    restaurant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("restaurants.id"), index=True)
    accounting_system_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounting_systems.id"))

    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Deviation(Base):
    """Сверка факта с Топ-2 — отклонения и переплаты."""
    __tablename__ = "deviations"
    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_fact_id: Mapped[int] = mapped_column(ForeignKey("purchases_fact.id"), unique=True)

    top1_supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"))
    top1_price: Mapped[Optional[float]] = mapped_column(Float)
    top2_supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"))
    top2_price: Mapped[Optional[float]] = mapped_column(Float)

    delta_per_unit: Mapped[Optional[float]] = mapped_column(Float)
    delta_pct: Mapped[Optional[float]] = mapped_column(Float)
    overpayment: Mapped[Optional[float]] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(32), index=True)
    # green_top2 / green_cheaper / yellow / red / no_top2 / no_quotes / unmapped_product / internal

    reason_text: Mapped[Optional[str]] = mapped_column(Text)
    reason_category: Mapped[Optional[str]] = mapped_column(String(64))


# ============ ИМПОРТ-СЛУЖЕБНОЕ ============

class ImportRun(Base):
    """Лог запусков импорта — для observability."""
    __tablename__ = "import_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64))      # 'drive_sync' / 'fact_iiko' / ...
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running / ok / error
    files_processed: Mapped[int] = mapped_column(Integer, default=0)
    rows_processed: Mapped[int] = mapped_column(Integer, default=0)
    error_text: Mapped[Optional[str]] = mapped_column(Text)


class UnmappedItem(Base):
    """Позиции из факта, которые мы не смогли сопоставить — список «надо добавить в карту»."""
    __tablename__ = "unmapped_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    raw_name: Mapped[str] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(16))  # iiko / storehouse
    system_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounting_systems.id"))
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("raw_name", "source", "system_id"),)


# ============ ПОЛЬЗОВАТЕЛИ ============

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))  # chef / buyer / ceo / supplier
    restaurant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("restaurants.id"))
    # Для роли supplier — к какому поставщику привязан аккаунт (логин = «портал поставщика»)
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"), index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    restaurant: Mapped[Optional[Restaurant]] = relationship()
    supplier: Mapped[Optional[Supplier]] = relationship()
