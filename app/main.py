import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, distinct, func

from app.core.database import init_db, AsyncSessionLocal
from app.routers import analytics, uploads, settings
from app.routers import ai_router
from app.models.models import KaspiRow, AppSettings


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Kaspi Analytics API",
    description="Backend for TorgStore Kaspi Marketplace analytics",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow all origins in dev (file:// protocol sends null origin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(uploads.router)
app.include_router(analytics.router)
app.include_router(ai_router.router)
app.include_router(settings.router)

# ── Static frontend (single HTML file) ────────────────────────────────────────
_STATIC = Path(__file__).parent.parent / "static"
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

@app.get("/", include_in_schema=False)
async def serve_frontend():
    """Serve the dashboard HTML at root."""
    html = _STATIC / "kaspi_analytics.html"
    if html.exists():
        return FileResponse(str(html), media_type="text/html")
    return {"error": "Frontend not found. Put kaspi_analytics.html in backend/static/"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "kaspi-analytics"}


@app.get("/api/v1/debug")
async def debug():
    """Diagnostic: shows what brands are in DB vs what our_brands setting contains."""
    async with AsyncSessionLocal() as db:
        # 1. What brands exist in kaspi_rows (freezers)
        q = select(KaspiRow.brand, func.count(KaspiRow.id).label("rows")).group_by(KaspiRow.brand).order_by(func.count(KaspiRow.id).desc()).limit(30)
        res = await db.execute(q)
        brands_in_db = [{"brand": r.brand, "rows": r.rows} for r in res.all()]

        # 2. What our_brands is set to
        q2 = select(AppSettings).where(AppSettings.key == "our_brands")
        res2 = await db.execute(q2)
        setting = res2.scalar_one_or_none()
        our_brands_in_db = setting.value if setting else None

        # 3. Total rows per department
        q3 = select(KaspiRow.department, func.count(KaspiRow.id)).group_by(KaspiRow.department)
        res3 = await db.execute(q3)
        dept_counts = {str(r[0]): r[1] for r in res3.all()}

        # 4. Cross-check
        defaults = ["AOLIEGE", "BACKERCRAFT", "FRIGGIER", "LEADBROS"]
        active_brands = set(b.strip().upper() for b in (our_brands_in_db or defaults))
        brands_found = [b["brand"] for b in brands_in_db if b["brand"] and b["brand"].upper() in active_brands]
        brands_missing = [b for b in active_brands if b not in {x["brand"].upper() for x in brands_in_db if x["brand"]}]

        # 5. Sample rows for our brands — show name/kod fields
        q5 = select(KaspiRow).where(KaspiRow.brand.in_(list(active_brands))).limit(10)
        res5 = await db.execute(q5)
        our_sample = [{"brand": r.brand, "name": r.name, "kod": r.kod, "units": r.units, "revenue": r.revenue, "tip": r.tip} for r in res5.scalars()]

    return {
        "brands_in_db_top30": brands_in_db,
        "our_brands_setting_in_db": our_brands_in_db,
        "active_our_brands": sorted(active_brands),
        "our_brands_FOUND_in_data": brands_found,
        "our_brands_NOT_found_in_data": sorted(brands_missing),
        "dept_row_counts": dept_counts,
        "our_brand_sample_rows": our_sample,
    }


@app.get("/api/v1/endpoints")
async def list_endpoints():
    """Quick reference of all endpoints."""
    return {
        "uploads": [
            "POST   /api/v1/uploads/           — upload Excel (form: file + department)",
            "GET    /api/v1/uploads/            — list uploads [?department=freezers]",
            "DELETE /api/v1/uploads/{id}        — delete upload + its rows",
        ],
        "analytics": [
            "GET    /api/v1/analytics/overview  — overview KPIs + monthly + top brands",
            "GET    /api/v1/analytics/brands    — all brands ranked by revenue",
            "GET    /api/v1/analytics/vetka     — vetka analysis",
            "GET    /api/v1/analytics/products  — paginated product list with filters",
            "GET    /api/v1/analytics/abc       — ABC analysis with top/bottom lists",
            "GET    /api/v1/analytics/months    — available months for a department",
            "GET    /api/v1/analytics/subtypes  — available subtypes (Ларь, Бонета…)",
        ],
        "ai": [
            "POST   /api/v1/ai/report           — AI analysis report via Claude API",
        ],
        "settings": [
            "GET    /api/v1/settings/           — get settings (our_brands)",
            "PUT    /api/v1/settings/           — save settings",
        ],
        "common_params": {
            "department": "freezers | refrigerated",
            "month": "string, e.g. 'Январь 2025'",
            "subtype": "string, e.g. 'Ларь' | 'Бонета'",
        },
    }
