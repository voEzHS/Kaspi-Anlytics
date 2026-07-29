"""App settings (our brands, etc.)."""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import AppSettings

import os

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
# Must match main.py's default — used only to recognize the admin account
# that already passed the front-door Basic Auth check (see require_admin).
BASIC_AUTH_USER = os.getenv("BASIC_AUTH_USER", "torgstore")

DEFAULTS = {
    "our_brands": ["AOLIEGE", "FRIGGIER", "LEADBROS", "XINGX", "MUXXED"],
}

# Brands that are ALWAYS ours regardless of DB settings
MANDATORY_BRANDS: set[str] = {"AOLIEGE", "FRIGGIER", "LEADBROS", "XINGX", "MUXXED"}


class SettingsPayload(BaseModel):
    our_brands: list[str]


async def require_admin(request: Request, x_admin_token: Optional[str] = Header(None)):
    """Allow write operations only if the caller is the admin.

    Same two-path check as uploads.require_admin: an in-app x-admin-token,
    OR having already passed the front-door Basic Auth middleware as the
    admin account (see basic_auth_middleware in main.py) — so the admin
    never has to re-enter the same password a second time.
    """
    if not ADMIN_PASSWORD:
        return  # No password set — open access (dev mode)
    if getattr(request.state, "basic_auth_user", None) == BASIC_AUTH_USER:
        return
    if x_admin_token != ADMIN_PASSWORD:
        raise HTTPException(403, "Неверный пароль администратора")


@router.get("/")
async def get_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AppSettings))
    rows = result.scalars().all()
    data = {**DEFAULTS}
    for r in rows:
        data[r.key] = r.value
    return data


@router.put("/")
async def save_settings(
    payload: SettingsPayload,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_admin),
):
    brands = [b.strip().upper() for b in payload.our_brands if b.strip()]

    row = await db.execute(select(AppSettings).where(AppSettings.key == "our_brands"))
    setting = row.scalar_one_or_none()

    if setting:
        setting.value = brands
        setting.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    else:
        db.add(AppSettings(key="our_brands", value=brands))

    await db.commit()
    return {"our_brands": brands}
