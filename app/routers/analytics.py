"""Analytics endpoints — query DB, run engine, return JSON."""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import AppSettings, DeptEnum, KaspiRow
from app.analytics import engine

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
    year = int(parts[1]) if len(parts) > 1 else 0
    idx = MONTH_ORDER.index(month_name) if month_name in MONTH_ORDER else 99
    return (year, idx)


async def _get_our_brands(db: AsyncSession) -> set[str]:
    row = await db.execute(select(AppSettings).where(AppSettings.key == "our_brands"))
    setting = row.scalar_one_or_none()
    if setting and setting.value:
        return {b.strip().upper() for b in setting.value}
    return {"AOLIEGE", "FRIGGIER", "LEADBROS"}


async def _fetch_rows(
    db: AsyncSession,
    department: str,
    month: Optional[str],
    subtype: Optional[str],
) -> list[dict]:
    dept = DeptEnum[department]
    q = select(KaspiRow).where(KaspiRow.department == dept)
    if month:
        q = q.where(KaspiRow.month == month)
    if subtype:
        q = q.where(KaspiRow.tip == subtype)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [
        {
            "kod": r.kod, "tip": r.tip, "name": r.name, "brand": r.brand,
            "volume": r.volume, "vetka": r.vetka, "month": r.month,
            "rrc": r.rrc or 0, "units": r.units or 0, "revenue": r.revenue or 0,
            "abc": r.abc, "sellers": r.sellers or 0,
            "rating": r.rating or 0, "reviews": r.reviews or 0,
        }
        for r in rows
    ]


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
    db: AsyncSession = Depends(get_db),
):
    rows = await _fetch_rows(db, department, month, subtype)
    our_brands = await _get_our_brands(db)

    # Available brands for filter dropdown
    available_brands = sorted({r["brand"] for r in rows if r["brand"]})

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
    dept = DeptEnum[department]
    q = select(distinct(KaspiRow.month)).where(
        KaspiRow.department == dept,
        KaspiRow.month.isnot(None),
        KaspiRow.month != "",
    ).order_by(KaspiRow.month)
    result = await db.execute(q)
    months = sorted((r[0] for r in result.all()), key=_month_sort_key)
    return {"months": months}


@router.get("/subtypes")
async def get_subtypes(
    department: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    dept = DeptEnum[department]
    q = select(distinct(KaspiRow.tip)).where(
        KaspiRow.department == dept,
        KaspiRow.tip.isnot(None),
        KaspiRow.tip != "",
    )
    result = await db.execute(q)
    subtypes = sorted(r[0] for r in result.all())
    return {"subtypes": subtypes}


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
