import base64
import hmac
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, distinct, func

from app.core.database import init_db, AsyncSessionLocal
from app.routers import analytics, uploads, settings
from app.routers import ai_router
from app.routers.uploads import require_admin
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

# ── Rate limiting ───────────────────────────────────────────────────────────
# In-memory sliding-window limiter (fine for Render's single free-tier
# instance; if this ever runs on multiple instances, state won't be shared
# across them and would need a Redis-backed limiter instead).
#
#   - General limit: caps how fast any one IP can hit the API at all —
#     slows down bulk scraping of the confidential analytics data.
#   - Auth-failure limit: much stricter, counts only 401 responses — slows
#     down password brute-forcing specifically.
#
# Registered BEFORE the Basic Auth middleware below so it wraps AROUND it
# (outer layer): it needs to run first on every request (to block floods and
# locked-out IPs before they even reach the auth check) and needs to see the
# final response status (to count 401s from the auth layer).
_RATE_WINDOW_SEC = 60
_RATE_MAX_REQUESTS = 200          # per IP per minute, all requests
_AUTH_FAIL_WINDOW_SEC = 600       # 10 minutes
_AUTH_FAIL_MAX = 8                # failed logins per IP before temp block

_request_hits: dict = defaultdict(deque)
_auth_fail_hits: dict = defaultdict(deque)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _hit(log: dict, key: str, window: int, limit: int) -> bool:
    """Record a hit for key; returns False if it's now over the limit."""
    now = time.time()
    dq = log[key]
    while dq and dq[0] <= now - window:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def _recent_count(log: dict, key: str, window: int) -> int:
    now = time.time()
    dq = log.get(key)
    if not dq:
        return 0
    while dq and dq[0] <= now - window:
        dq.popleft()
    return len(dq)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)

    ip = _client_ip(request)

    # Brute-force lockout — check before doing anything else
    if _recent_count(_auth_fail_hits, ip, _AUTH_FAIL_WINDOW_SEC) >= _AUTH_FAIL_MAX:
        return Response(
            content="Слишком много неверных попыток входа. Попробуйте позже.",
            status_code=429,
        )

    # General request throttle
    if not _hit(_request_hits, ip, _RATE_WINDOW_SEC, _RATE_MAX_REQUESTS):
        return Response(
            content="Слишком много запросов. Попробуйте позже.",
            status_code=429,
        )

    response = await call_next(request)

    if response.status_code == 401:
        _auth_fail_hits[ip].append(time.time())

    return response


# ── Whole-site Basic Auth ──────────────────────────────────────────────────
# Data on this site is confidential business analytics. If any credentials are
# configured (production), every request except /health must present valid
# HTTP Basic credentials. If nothing is configured (local dev), auth is
# skipped entirely.
#
# Two independent tiers of secrets:
#   - Front-door login (who may open the site at all): BASIC_AUTH_USER/
#     ADMIN_PASSWORD (the main/admin account) plus any extra accounts listed
#     in BASIC_AUTH_VIEWERS — e.g. "artur:xxxx,baurzhan:yyyy". These extra
#     accounts can view the whole dashboard but do NOT know ADMIN_PASSWORD,
#     so they cannot unlock the separate in-app admin mode below.
#   - In-app admin mode (upload/delete data, change settings, generate AI
#     reports): gated by require_admin() in uploads.py, which checks
#     ADMIN_PASSWORD only. Viewer accounts are never given this value.
BASIC_AUTH_USER = os.getenv("BASIC_AUTH_USER", "torgstore")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")


def _load_basic_auth_users() -> dict:
    """Build the front-door username -> password map from env vars."""
    users: dict = {}
    if ADMIN_PASSWORD:
        users[BASIC_AUTH_USER] = ADMIN_PASSWORD
    for pair in os.getenv("BASIC_AUTH_VIEWERS", "").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        username, _, pw = pair.partition(":")
        username, pw = username.strip(), pw.strip()
        if username and pw:
            users[username] = pw
    return users


BASIC_AUTH_USERS = _load_basic_auth_users()


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if request.url.path == "/health" or not BASIC_AUTH_USERS:
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
            username, _, password = decoded.partition(":")
            expected = BASIC_AUTH_USERS.get(username)
            if expected is not None and hmac.compare_digest(password, expected):
                return await call_next(request)
        except Exception:
            pass

    return Response(
        content="Требуется авторизация / Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Kaspi Analytics"'},
    )


# CORS — restrict to known origins via ALLOWED_ORIGINS env var
_allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
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


@app.get("/api/v1/debug", dependencies=[Depends(require_admin)])
async def debug():
    """Diagnostic (admin-only): shows what brands are in DB vs what our_brands setting contains."""
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
        defaults = ["AOLIEGE", "FRIGGIER", "LEADBROS"]
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
