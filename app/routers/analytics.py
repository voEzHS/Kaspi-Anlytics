"""Analytics endpoints — query DB, run engine, return JSON."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import DeptEnum, KaspiRow
from app.analytics import engine
# "Our brands" logic lives in one place — app/routers/settings.py — and is
# imported here rather than duplicated, so it can't silently drift out of
# sync with what the Settings UI shows/edits.
from app.routers.settings import get_our_brands as _get_our_brands

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])

# Canonical month order for chronological sorting
MONTH_ORDER = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def _month_sort_key(m: str) -> tuple:
    """Sort months chronologically: year first, then month index within year."""
    parts = m.split()
    month_name = parts[0] if parts else m
    # parts[1] isn't always a clean year — contaminated exports can leave
    # artifacts like "Июнь 15'" in the raw DB rows (filtered out of actual
    # analytics by engine.apply_business_rules, but this function is also
    # used by /months, a raw DISTINCT query that never goes through that
    # filter). int("15'") raises ValueError and 500s the endpoint — found
    # via full-site numbers audit, 03.08.2026. Default to year=0 instead of
    # crashing; the entry gets filtered out below anyway.
    try:
        year = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        year = 0
    idx = MONTH_ORDER.index(month_name) if month_name in MONTH_ORDER else 99
    return (year, idx)


def _validate_department(department: str) -> "DeptEnum":
    try:
        return DeptEnum[department]
    except KeyError:
        raise HTTPException(400, f"Unknown department: '{department}'. Use: {list(DeptEnum.__members__)}")


async def _fetch_rows(
    db: AsyncSession,
    department: str,
    month: Optional[str],
    subtype: Optional[str],
) -> list[dict]:
    dept = _validate_department(department)
    q = select(KaspiRow).where(KaspiRow.department == dept)
    if month:
        q = q.where(KaspiRow.month == month)
    if subtype:
        q = q.where(KaspiRow.tip == subtype)
    result = await db.execute(q)
    rows = result.scalars().all()
    raw = [
        {
            "kod": r.kod, "tip": r.tip, "name": r.name, "brand": r.brand,
            "volume": r.volume, "vetka": r.vetka, "month": r.month,
            "rrc": r.rrc or 0, "units": r.units or 0, "revenue": r.revenue or 0,
            "abc": r.abc, "sellers": r.sellers or 0,
            "rating": r.rating or 0, "reviews": r.reviews or 0,
            "department": department,
        }
        for r in rows
    ]
    return engine.apply_business_rules(raw)


@router.get("/overview")
async def get_overview(
    department: str = Query(...),
    month: Optional[str] = Query(None),
    subtype: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    rows = await _fetch_rows(db, department, month, subtype)
    if not rows:
        return None

    our_brands = await _get_our_brands(db)
    ov = engine.calc_overview(rows, our_brands)
    monthly = engine.calc_monthly(rows, our_brands)
    top_brands = engine.calc_brands(rows, our_brands)[:10]
    for b in top_brands:
        b["is_ours"] = b["brand"].upper() in our_brands

    # When no subtype filter: show type comparison (Ларь vs Бонета etc.)
    # When subtype is selected: show vetka (liter range) breakdown within that type
    subtype_compare = []
    if not subtype:
        subtype_compare = engine.calc_subtype_compare(rows, our_brands)

    # Flat structure — frontend reads top-level keys
    return {
        **ov,
        "monthly": monthly,
        "top_brands": top_brands,
        "subtype_compare": subtype_compare,
    }


@router.get("/brands")
async def get_brands(
    department: str = Query(...),
    month: Optional[str] = Query(None),
    subtype: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    rows = await _fetch_rows(db, department, month, subtype)
    if not rows:
        return {"brands": []}
    our_brands = await _get_our_brands(db)
    brands = engine.calc_brands(rows, our_brands)
    return {"brands": brands}


@router.get("/vetka")
async def get_vetka(
    department: str = Query(...),
    month: Optional[str] = Query(None),
    subtype: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    rows = await _fetch_rows(db, department, month, subtype)
    if not rows:
        return {"vetka": []}
    our_brands = await _get_our_brands(db)
    return {"vetka": engine.calc_vetka(rows, our_brands)}


@router.get("/products")
async def get_products(
    department: str = Query(...),
    month: Optional[str] = Query(None),
    subtype: Optional[str] = Query(None),
    brand: Optional[str] = Query(None),
    abc: Optional[str] = Query(None),
    vetka: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort: str = Query("revenue"),
    limit: int = Query(50, le=500),
    offset: int = Query(0),
    ours_only: Optional[bool] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    rows = await _fetch_rows(db, department, month, subtype)
    our_brands = await _get_our_brands(db)

    # Available brands for filter dropdown
    available_brands = sorted({r["brand"] for r in rows if r["brand"]})

    if ours_only:
        rows = [r for r in rows if r["brand"].upper() in our_brands]
    if brand:
        rows = [r for r in rows if r["brand"] == brand.upper()]
    if abc:
        rows = [r for r in rows if r["abc"] == abc.upper()]
    if vetka:
        rows = [r for r in rows if (r["vetka"] or "") == vetka]
    if search:
        s = search.lower()
        rows = [r for r in rows if
                s in (r["name"] or "").lower() or
                s in (r["brand"] or "").lower() or
                s in (r["kod"] or "").lower()]

    valid_sort = {"revenue", "units", "rrc", "rating", "reviews", "sellers"}
    sort_key = sort if sort in valid_sort else "revenue"
    rows.sort(key=lambda r: r.get(sort_key) or 0, reverse=True)

    total = len(rows)
    page_items = rows[offset: offset + limit]
    for r in page_items:
        r["is_ours"] = r["brand"].upper() in our_brands

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": page_items,
        "available_brands": available_brands,
    }


@router.get("/abc")
async def get_abc(
    department: str = Query(...),
    month: Optional[str] = Query(None),
    subtype: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    rows = await _fetch_rows(db, department, month, subtype)
    if not rows:
        return {"market": {"A": 0, "B": 0, "C": 0}, "ours": {"A": 0, "B": 0, "C": 0},
                "our_a_items": [], "our_c_items": []}

    our_brands = await _get_our_brands(db)

    # Deduplicate by SKU — one product across multiple months = one SKU
    all_skus = engine.dedup_skus(rows)
    our_rows = [r for r in rows if r["brand"].upper() in our_brands]
    our_skus = engine.dedup_skus(our_rows)

    market_counts = {"A": 0, "B": 0, "C": 0}
    for s in all_skus.values():
        abc = s["abc"] if s.get("abc") in ("A", "B", "C") else "C"
        market_counts[abc] += 1

    ours_counts = {"A": 0, "B": 0, "C": 0}
    our_a_items = []
    our_c_items = []
    for s in our_skus.values():
        abc = s["abc"] if s.get("abc") in ("A", "B", "C") else "C"
        ours_counts[abc] += 1
        item = {"name": s["name"] or s["kod"], "brand": s["brand"],
                "kod": s.get("kod") or "",
                "revenue": s.get("_revenue_sum", s["revenue"]),
                "units": s.get("_units_sum", s["units"]), "abc": abc}
        if abc == "A":
            our_a_items.append(item)
        elif abc == "C":
            our_c_items.append(item)

    our_a_items.sort(key=lambda x: x["revenue"], reverse=True)
    our_c_items.sort(key=lambda x: x["revenue"], reverse=True)

    return {
        "market": market_counts,
        "ours": ours_counts,
        "our_a_items": our_a_items[:20],
        "our_c_items": our_c_items[:20],
    }


@router.get("/strategy")
async def get_strategy(
    department: str = Query(...),
    month: Optional[str] = Query(None),
    subtype: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Strategic analysis: review deficit, segment gaps, low-rating SKUs, competitor benchmarks."""
    rows = await _fetch_rows(db, department, month, subtype)
    if not rows:
        return {}
    our_brands = await _get_our_brands(db)
    return engine.calc_strategy(rows, our_brands)


@router.get("/intelligence")
async def get_intelligence(
    department: str = Query(...),
    month: Optional[str] = Query(None),
    subtype: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """World-class intelligence: market position, review ROI, penetrability, momentum, threats."""
    rows = await _fetch_rows(db, department, month, subtype)
    if not rows:
        return {}
    our_brands = await _get_our_brands(db)
    return engine.calc_intelligence(rows, our_brands)


@router.get("/months")
async def get_months(
    department: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    dept = _validate_department(department)
    q = select(distinct(KaspiRow.month)).where(
        KaspiRow.department == dept,
        KaspiRow.month.isnot(None),
        KaspiRow.month != "",
    ).order_by(KaspiRow.month)
    result = await db.execute(q)
    raw_months = [r[0] for r in result.all()]
    # Same bypass problem as /subtypes: raw DISTINCT query never goes
    # through engine.apply_business_rules, so contaminated month strings
    # ("Июнь 15'", "Пар") would otherwise show up as selectable options
    # even though they're excluded from every actual calculation.
    clean_months = []
    for m in raw_months:
        mm = (m or "").strip()
        if not mm or engine._MIDPERIOD_SNAPSHOT_RE.match(mm):
            continue
        word = mm.split()[0].lower()
        if word not in engine._MONTH_IDX and word[:3] not in engine._MONTH_IDX:
            continue
        clean_months.append(m)
    months = sorted(clean_months, key=_month_sort_key)
    return {"months": months}


@router.get("/subtypes")
async def get_subtypes(
    department: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    dept = _validate_department(department)
    q = select(distinct(KaspiRow.tip)).where(
        KaspiRow.department == dept,
        KaspiRow.tip.isnot(None),
        KaspiRow.tip != "",
    )
    result = await db.execute(q)
    subtypes = sorted(r[0] for r in result.all())
    # This bypasses apply_business_rules (it's a raw distinct query, not
    # row-level), so Rule 4's non-vitrina-type exclusion has to be mirrored
    # here explicitly — otherwise excluded types like "Боета"/"Ларь" would
    # still show up as selectable filter chips even though selecting them
    # returns empty analytics everywhere else.
    if department == "refrigerated":
        subtypes = [s for s in subtypes if s.strip().lower() not in engine._NON_VITRINA_TYPES]
    return {"subtypes": subtypes}


@router.get("/rrc")
async def get_rrc(
    department: str = Query(...),
    month: Optional[str] = Query(None),
    subtype: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """RRC analytics by vetka and subtype: market min/avg/max, our position, tactic."""
    rows = await _fetch_rows(db, department, month, subtype)
    if not rows:
        return {"by_vetka": [], "by_subtype": [], "summary": {}}
    our_brands = await _get_our_brands(db)
    return engine.calc_rrc_analytics(rows, our_brands)


@router.get("/monthly-trends")
async def get_monthly_trends(
    department: str = Query(...),
    subtype: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Full monthly trend breakdown — always fetches ALL months (no month filter).
    Optional subtype filter to drill into Ларь/Бонета/Витрина etc.
    """
    # No month filter — we need all months for the trend analysis
    rows = await _fetch_rows(db, department, month=None, subtype=subtype)
    if not rows:
        return {}
    our_brands = await _get_our_brands(db)
    return engine.calc_monthly_trends(rows, our_brands)


@router.get("/whats-changed")
async def get_whats_changed(
    department: str = Query(...),
    subtype: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Ranked list of segments/brands/products that moved OUR revenue the most
    between the latest month and the one before it. Powers the "Пульс отдела"
    ranked "что изменилось" list (IA audit, июль 2026) so a manager doesn't
    have to manually diff Ветки and Бренды tables by hand.
    """
    rows = await _fetch_rows(db, department, month=None, subtype=subtype)
    if not rows:
        return {"month": None, "prev_month": None, "factors": []}
    our_brands = await _get_our_brands(db)
    return engine.calc_whats_changed(rows, our_brands)


@router.get("/sku-history")
async def get_sku_history(
    department: str = Query(...),
    kod: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    subtype: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Month-by-month history for a single product (by kod, or exact name as a
    fallback). Lets the product drill-down modal go one level deeper instead
    of dead-ending at a flat list (IA audit, июль 2026 — Уровень 3 «Товар»).
    """
    if not kod and not name:
        raise HTTPException(400, "Provide kod or name")
    rows = await _fetch_rows(db, department, month=None, subtype=subtype)
    return engine.calc_sku_history(rows, kod=kod or "", name=name or "")
