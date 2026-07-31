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
    "our_brands": ["AOLIEGE", "FRIGGIER", "LEADBROS", "XINGX", "MUXXED", "BACKERCRAFT"],
}

# Brands that are ALWAYS ours regardless of DB settings.
# Single source of truth — app/routers/analytics.py and app/routers/ai_router.py
# import get_our_brands() from here instead of keeping their own copy of this
# set, so adding/removing a mandatory brand only ever needs to happen in one
# place. (Previously this constant was duplicated in 5 places across the repo,
# which is how BACKERCRAFT ended up missing from calculations in July 2026 —
# it had been added to the DB-configurable list in one spot but the app was
# actually reading from a stale copy elsewhere.)
MANDATORY_BRANDS: set[str] = {"AOLIEGE", "FRIGGIER", "LEADBROS", "XINGX", "MUXXED", "BACKERCRAFT"}


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


async def get_our_brands(db: AsyncSession) -> set[str]:
    """Single source of truth for 'our brands' used in every calculation.

    Always the DB-configured list unioned with MANDATORY_BRANDS, so mandatory
    brands can never be dropped even if someone removes them via the Settings
    UI. Imported by analytics.py and ai_router.py — do not re-implement this
    logic locally in another router.
    """
    result = await db.execute(select(AppSettings).where(AppSettings.key == "our_brands"))
    setting = result.scalar_one_or_none()
    if setting and setting.value:
        return {b.strip().upper() for b in setting.value} | MANDATORY_BRANDS
    return set(MANDATORY_BRANDS)


@router.get("/")
async def get_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AppSettings))
    rows = result.scalars().all()
    data = {**DEFAULTS}
    for r in rows:
        data[r.key] = r.value
    # Always reflect the brand list actually used in calculations (DB ∪
    # mandatory) so the Settings UI never edits/displays a stale list that
    # has drifted from what get_our_brands() returns everywhere else.
    data["our_brands"] = sorted({b.strip().upper() for b in data.get("our_brands", [])} | MANDATORY_BRANDS)
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
