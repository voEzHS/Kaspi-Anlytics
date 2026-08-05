import base64
import hashlib
import hmac
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, distinct, func

from app.core.database import init_db, AsyncSessionLocal
from app.routers import analytics, uploads, settings, stock, channel_sales
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
    # CF-Connecting-IP is set by Cloudflare (which fronts every Render app)
    # and cannot be spoofed by the client — Cloudflare overwrites it.
    # X-Forwarded-For is a weaker fallback: trustworthy in Render's normal
    # setup, but headers are still easier to forge than a Cloudflare-set one.
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
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

    # Only count a 401 as a "failed login attempt" if the request actually
    # PRESENTED credentials that turned out to be wrong (set by
    # basic_auth_middleware below via request.state.auth_attempted). A bare
    # 401 with no Authorization header at all is just the normal first touch
    # of a fresh browser session — every new tab/reload/device does this
    # once before the browser's native login prompt even appears, since the
    # browser doesn't know to send credentials until it's been challenged.
    # Counting THAT as a failed attempt meant a handful of routine reloads
    # (exactly what happens during any troubleshooting session) could push
    # the shared-IP counter over _AUTH_FAIL_MAX and lock the whole site out
    # for 10 minutes — which then looks like "infinite re-authorization"
    # from the user's side, because every retry during the lockout window
    # also comes back 429 and resets nothing. This was the real bug behind
    # the recurring "бесконечная авторизация" reports.
    if response.status_code == 401 and getattr(request.state, "auth_attempted", False):
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

# ── Session cookie (01.08) ──────────────────────────────────────────────────
# Root cause of the recurring "снова бесконечная авторизация" reports: the
# SPA's initLanding() fires ~20 parallel fetch() calls (5 endpoints × 4
# departments) the instant the page loads. HTTP Basic Auth credentials are
# supposed to be cached by the browser and silently reattached to every
# subsequent request to the origin, but real browsers are flaky about doing
# this reliably when a *burst* of simultaneous requests races the very first
# challenge/response handshake — some of the 20 requests go out before the
# browser has finished caching credentials from the first 401, each of those
# gets its own 401 back, and the browser re-prompts. The 29.07 fix exempted
# one specific route (POST /uploads/) that hit this same underlying flakiness,
# but that was patching one symptom, not the cause — any other endpoint could
# (and, per this report, does) hit it too.
#
# Fix: after the first successful Basic Auth on any request, issue a signed
# session cookie. Cookies do not have this reattachment flakiness — the
# browser attaches them to every request unconditionally. All of the SPA's
# subsequent fetch() calls (however many, however parallel) authenticate via
# that cookie instead of depending on the Authorization header being resent,
# which removes the race entirely rather than exempting more routes one at a
# time as new ones get hit.
SESSION_COOKIE_NAME = "kaspi_sess"
SESSION_MAX_AGE_SEC = 60 * 60 * 24  # 24h


def _session_secret() -> str:
    # No dedicated secret is provisioned for this — reuse ADMIN_PASSWORD
    # (already a server-only secret) rather than requiring a new env var.
    # If ADMIN_PASSWORD is unset, BASIC_AUTH_USERS is empty and this whole
    # middleware short-circuits below, so this value is never used.
    return ADMIN_PASSWORD or "dev-secret"


def _make_session_cookie(username: str) -> str:
    expiry = int(time.time()) + SESSION_MAX_AGE_SEC
    payload = f"{username}:{expiry}"
    sig = hmac.new(_session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_session_cookie(cookie_val: str) -> Optional[str]:
    try:
        username, expiry_s, sig = cookie_val.split(":", 2)
        expiry = int(expiry_s)
    except (ValueError, AttributeError):
        return None
    if expiry < int(time.time()):
        return None
    expected_sig = hmac.new(_session_secret().encode(), f"{username}:{expiry}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    return username if username in BASIC_AUTH_USERS else None


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    # 29.07: POST /api/v1/uploads/ specifically kept re-triggering the
    # browser's native Basic Auth login popup — reproduced live (incognito,
    # fresh credentials, no saved-password autofill involved) and matches
    # what was seen from automated testing too: this browser does not
    # reliably reattach the cached Authorization header to this particular
    # POST+multipart request, even though it does for ordinary GETs. Since
    # that's a browser-side quirk outside our control and it was blocking
    # every upload attempt, front-door Basic Auth is exempted for this one
    # route by explicit request. Viewing the dashboard (all GET routes) and
    # every other write action (delete, tip correction, settings, AI report)
    # are still fully gated as before — this narrows the exemption to only
    # the action that was actually broken.
    if request.url.path == "/api/v1/uploads/" and request.method == "POST":
        return await call_next(request)
    if request.url.path == "/health" or not BASIC_AUTH_USERS:
        return await call_next(request)

    # Session-cookie fast path — see note above. Checked before the
    # Authorization header so an already-logged-in browser never has to
    # depend on Basic-Auth-header reattachment at all.
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_val:
        cookie_user = _verify_session_cookie(cookie_val)
        if cookie_user:
            request.state.basic_auth_user = cookie_user
            return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("basic "):
        # Real credentials were presented (right or wrong) — a 401 from here
        # on is a genuine failed login attempt, not just "not logged in yet".
        # rate_limit_middleware (outer layer) reads this to decide whether
        # the eventual 401 should count toward the brute-force lockout.
        request.state.auth_attempted = True
        try:
            decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
            username, _, password = decoded.partition(":")
            expected = BASIC_AUTH_USERS.get(username)
            if expected is not None and hmac.compare_digest(password, expected):
                # Stash who passed the front door so require_admin() downstream
                # can recognize the admin account without asking for the same
                # password a second time via the in-app x-admin-token modal.
                request.state.basic_auth_user = username
                response = await call_next(request)
                response.set_cookie(
                    SESSION_COOKIE_NAME,
                    _make_session_cookie(username),
                    max_age=SESSION_MAX_AGE_SEC,
                    httponly=True,
                    samesite="lax",
                    secure=True,
                )
                return response
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
app.include_router(stock.router)
app.include_router(channel_sales.router)

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
        # 1. What brands exist in kaspi_rows (across all departments)
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
            "GET    /api/v1/uploads/            — list uploads [?department=freezers|refrigerated|ovens|ice_makers]",
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
            "department": "freezers | refrigerated | ovens | ice_makers",
            "month": "string, e.g. 'Январь 2025'",
            "subtype": "string, e.g. 'Ларь' | 'Бонета'",
        },
    }
