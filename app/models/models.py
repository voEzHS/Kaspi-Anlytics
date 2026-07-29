from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Text, JSON, ForeignKey, Index, Enum
)
from sqlalchemy.orm import relationship
import enum

from app.core.database import Base


class DeptEnum(str, enum.Enum):
    freezers = "freezers"       # Морозильники (Лари + Бонеты)
    refrigerated = "refrigerated"  # Холодильные витрины
    ovens = "ovens"             # Жарочные шкафы
    ice_makers = "ice_makers"   # Льдогенераторы


class Upload(Base):
    """One uploaded Excel file = one Upload record."""
    __tablename__ = "uploads"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    department = Column(Enum(DeptEnum), nullable=False)
    row_count = Column(Integer, default=0)
    months = Column(JSON, default=list)       # ["Январь 2025", "Февраль 2025"]
    subtypes = Column(JSON, default=list)     # ["Ларь", "Бонета"] – unique Тип values
    # Колонка DateTime без timezone=True в Postgres — это TIMESTAMP WITHOUT
    # TIME ZONE. datetime.now(timezone.utc) даёт tz-aware объект — asyncpg
    # падает с "can't subtract offset-naive and offset-aware datetimes" при
    # попытке его записать. .replace(tzinfo=None) убирает метку, само время
    # остаётся в UTC (как и было задумано), просто без явного tzinfo.
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    rows = relationship("KaspiRow", back_populates="upload", cascade="all, delete-orphan", lazy="select")


class KaspiRow(Base):
    """One product row from the matrix."""
    __tablename__ = "kaspi_rows"

    id = Column(Integer, primary_key=True, index=True)
    upload_id = Column(Integer, ForeignKey("uploads.id", ondelete="CASCADE"), nullable=False)

    # Core identifiers
    kod = Column(String(100), nullable=True)
    department = Column(Enum(DeptEnum), nullable=False)
    tip = Column(String(100), nullable=True)    # Тип: "Ларь" | "Бонета" | null
    name = Column(Text, nullable=True)
    brand = Column(String(200), nullable=True, index=True)
    volume = Column(String(100), nullable=True)
    vetka = Column(String(300), nullable=True, index=True)
    color = Column(String(100), nullable=True)
    month = Column(String(50), nullable=True, index=True)

    # Metrics
    rrc = Column(Float, default=0)
    units = Column(Float, default=0)
    revenue = Column(Float, default=0)
    abc = Column(String(5), nullable=True)
    sellers = Column(Float, default=0)
    rating = Column(Float, default=0)
    reviews = Column(Float, default=0)
    thaw = Column(Float, default=0)

    upload = relationship("Upload", back_populates="rows")

    __table_args__ = (
        Index("ix_kaspi_dept_month", "department", "month"),
        Index("ix_kaspi_dept_brand", "department", "brand"),
        Index("ix_kaspi_dept_tip", "department", "tip"),
        Index("ix_kaspi_dept_tip_month", "department", "tip", "month"),
    )


class AppSettings(Base):
    """Key-value settings store."""
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(JSON, nullable=True)
    # Тот же tz-aware/tz-naive конфликт, что и у Upload.created_at выше.
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
