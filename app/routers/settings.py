"""App settings (our brands, etc.)."""
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import AppSettings

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

DEFAULTS = {
    "our_brands": ["AOLIEGE", "BACKERCRAFT", "FRIGGIER", "LEADBROS"],
}


class SettingsPayload(BaseModel):
    our_brands: list[str]


@router.get("/")
async def get_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AppSettings))
    rows = result.scalars().all()
    data = {**DEFAULTS}
    for r in rows:
        data[r.key] = r.value
    return data


@router.put("/")
async def save_settings(payload: SettingsPayload, db: AsyncSession = Depends(get_db)):
    brands = [b.strip().upper() for b in payload.our_brands if b.strip()]

    row = await db.execute(select(AppSettings).where(AppSettings.key == "our_brands"))
    setting = row.scalar_one_or_none()

    if setting:
        setting.value = brands
        setting.updated_at = datetime.utcnow()
    else:
        db.add(AppSettings(key="our_brands", value=brands))

    await db.commit()
    return {"our_brands": brands}
