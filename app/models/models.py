from datetime import datetime
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


class Upload(Base):
    """One uploaded Excel file = one Upload record."""
    __tablename__ = "uploads"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    department = Column(Enum(DeptEnum), nullable=False)
    row_count = Column(Integer, default=0)
    months = Column(JSON, default=list)       # ["Январь 2025", "Февраль 2025"]
    subtypes = Column(JSON, default=list)     # ["Ларь", "Бонета"] – unique Тип values
    created_at = Column(DateTime, default=datetime.utcnow)

    rows = relationship("KaspiRow", back_populates="upload", cascade="all, delete-orphan", lazy="dynamic")


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
    )


class AppSettings(Base):
    """Key-value settings store."""
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(JSON, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
