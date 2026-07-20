"""AI report generation via Anthropic Claude API."""
import asyncio
import os
from typing import Optional

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import AppSettings, DeptEnum, KaspiRow
from app.analytics import engine
from app.routers.uploads import require_admin
from sqlalchemy import select

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])

DEPT_LABELS = {
    "freezers": "Морозильники (Лари и Бонеты)",
    "refrigerated": "Холодильные витрины",
}


_MANDATORY_BRANDS: set[str] = {"AOLIEGE", "FRIGGIER", "LEADBROS", "XINGX", "MUXXED"}


async def _get_our_brands(db: AsyncSession) -> set[str]:
    row = await db.execute(select(AppSettings).where(AppSettings.key == "our_brands"))
    setting = row.scalar_one_or_none()
    if setting and setting.value:
        return {b.strip().upper() for b in setting.value} | _MANDATORY_BRANDS
    return _MANDATORY_BRANDS


@router.post("/report")
async def generate_report(
    department: str = Query(..., description="freezers | refrigerated"),
    month: Optional[str] = Query(None),
    subtype: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_admin),
):
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")

    dept = DeptEnum[department]
    q = select(KaspiRow).where(KaspiRow.department == dept)
    if month:
        q = q.where(KaspiRow.month == month)
    if subtype:
        q = q.where(KaspiRow.tip == subtype)
    result = await db.execute(q)
    rows = result.scalars().all()

    if not rows:
        raise HTTPException(404, "No data found for selected filters")

    our_brands = await _get_our_brands(db)
    row_dicts = engine.apply_business_rules([
        {"brand": r.brand, "tip": r.tip, "vetka": r.vetka, "revenue": r.revenue or 0,
         "units": r.units or 0, "abc": r.abc, "rating": r.rating or 0,
         "reviews": r.reviews or 0, "sellers": r.sellers or 0, "month": r.month,
         "kod": r.kod or "", "name": r.name or "", "rrc": r.rrc or 0,
         "sellers": r.sellers or 0, "volume": r.volume}
        for r in rows
    ])

    overview = engine.calc_overview(row_dicts, our_brands)
    brands = engine.calc_brands(row_dicts, our_brands)[:15]
    vetka = engine.calc_vetka(row_dicts, our_brands)[:10]
    subtype_compare = engine.calc_subtype_compare(row_dicts, our_brands) if department == "freezers" and not subtype else []

    our_brands_list = ", ".join(sorted(our_brands))
    dept_label = DEPT_LABELS.get(department, department)
    period = f"{month}" if month else "все месяцы"
    sub_label = f" · {subtype}" if subtype else ""

    prompt = f"""Ты Senior Product Manager с опытом работы на Kaspi Marketplace.

Проанализируй данные по категории **{dept_label}{sub_label}** за период **{period}**.
Наши бренды TorgStore: {our_brands_list}

## Ключевые метрики рынка
- Выручка рынка: {overview.get('total_revenue', 0):,.0f} ₸
- Наша выручка: {overview.get('our_revenue', 0):,.0f} ₸ ({overview.get('our_share_pct', 0):.1f}% доли)
- Товаров на рынке: {overview.get('unique_products', 0)}
- Брендов: {overview.get('unique_brands', 0)}
- Наших SKU: {overview.get('our_sku', 0)}
- ABC-A наших: {overview.get('our_abc_a', 0)}
- Наш рейтинг: {overview.get('our_avg_rating', 0):.1f}

## Топ-10 брендов по выручке
{chr(10).join(f"- {b['brand']}{'[НАШ]' if b['is_ours'] else ''}: {b['revenue']:,.0f}₸ ({b['market_share_pct']:.1f}%)" for b in brands[:10])}

## Топ-10 веток
{chr(10).join(f"- {v['vetka']}: {v['revenue']:,.0f}₸ (наша доля {v['our_share_pct']:.1f}%)" for v in vetka[:10])}

{"## Сравнение подтипов" + chr(10) + chr(10).join(f"- {s['subtype']}: {s['revenue']:,.0f}₸ ({s['market_share_pct']:.1f}% рынка, наша доля {s['our_share_pct']:.1f}%)" for s in subtype_compare) if subtype_compare else ""}

Дай структурированный анализ:
1. **Позиция TorgStore** — как мы стоим относительно рынка
2. **Ключевые выводы** — что критично важно (3-5 пунктов)
3. **Белые пятна** — где нас нет, но рынок есть
4. **Приоритетные действия** — конкретные шаги на следующий месяц

Будь конкретным, используй цифры. Пиши на русском языке."""

    # Use asyncio.to_thread so the blocking I/O doesn't stall the event loop
    client = anthropic.Anthropic(api_key=api_key)
    message = await asyncio.to_thread(
        client.messages.create,
        model="claude-opus-4-8",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )

    return {
        "department": department,
        "month": month,
        "subtype": subtype,
        "report": message.content[0].text,
        "tokens_used": message.usage.output_tokens,
    }
