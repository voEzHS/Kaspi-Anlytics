"""Analytics calculation engine — pure Python, no DB queries here."""
import math
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

# Canonical month order for chronological sorting
MONTH_ORDER = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

# Aliases: any form → canonical index (0-based)
_MONTH_IDX: dict[str, int] = {}
for _i, _full in enumerate(MONTH_ORDER):
    _MONTH_IDX[_full.lower()] = _i          # "январь" → 0
    _MONTH_IDX[_full[:3].lower()] = _i       # "янв" → 0 (first 3 chars)
# manual overrides for ambiguous 3-char prefixes
_MONTH_IDX.update({
    "янв": 0, "фев": 1, "мар": 2, "апр": 3, "май": 4,
    "июн": 5, "июл": 6, "авг": 7, "сен": 8, "окт": 9, "ноя": 10, "дек": 11,
    "март": 2,  # "март" is 4 chars but common abbreviation
})


def _month_sort_key(m: str) -> tuple:
    """Sort month strings chronologically, handles any abbreviation or case."""
    parts = m.strip().split()
    month_raw = parts[0] if parts else m
    year = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    key = month_raw.lower()
    idx = _MONTH_IDX.get(key, _MONTH_IDX.get(key[:3], 99))
    return (year, idx)


def _month_name(m: str) -> str:
    """Extract just the month word from 'Январь 2025' → 'Январь'."""
    return m.split()[0] if m else m


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


# ── Business rules ────────────────────────────────────────────────────────────

def _vetka_lower_bound(vetka: str) -> int:
    """Extract lower litre bound from vetka string.
    '400-500' → 400,  '2500' → 2500,  'до 100' → 0
    """
    if not vetka:
        return 0
    s = vetka.strip().replace('–', '-').replace(' ', '')
    # take first numeric token
    import re as _re
    m = _re.search(r'\d+', s)
    return int(m.group()) if m else 0


# Rule 1 (see apply_business_rules): below this many litres a freezer is
# household equipment, not commercial — with the exemptions below.
_MIN_FREEZER_LITRES = 400

# Subtypes the 400 L floor does NOT apply to (business owner, 11.08.2026).
# Both are commercial equipment at any capacity — there is no household
# version of either, so a small one is a small commercial unit, not a
# consumer product:
#   бонета — open-top retail display freezer for shop floors;
#   шок    — blast chiller / shock freezer, professional kitchen equipment
#            (deliberately low capacity: it freezes fast, not much).
# Spelling variants included because several appear across historical
# uploads ("Бонета" / "Боета", "шок" / "шокер" / "шоковая заморозка").
_VOLUME_RULE_EXEMPT_TIPS = {
    "бонета", "боета",
    "шок", "шокер", "шоковая заморозка", "шоковая",
}

_TABLETOP_RE = re.compile(r"настольн", re.IGNORECASE)

# Matches a month string that's a word followed by a trailing 1-2 digit
# number (with or without a dangling apostrophe) — e.g. "Июнь 15'",
# "июнь 15", "Июль 20'". Deliberately capped at 2 digits (day-of-month
# range, 1-31) rather than any \d+, so this does NOT match the legitimate
# "<Месяц> <ГГГГ>" year-suffixed format used elsewhere (e.g. "Январь 2026")
# — only the mid-period export artifact described in Rule 3 below.
_MIDPERIOD_SNAPSHOT_RE = re.compile(r"^[а-яё]+\s+\d{1,2}'?$", re.IGNORECASE)

# Тип values that do NOT belong in the "refrigerated" (Холод. витрины)
# department — see Rule 4 below. Lowercased for case-insensitive matching.
_NON_VITRINA_TYPES = {"боета", "бонета", "винный шкаф", "ларь", "холодильник", "шкаф"}


def apply_business_rules(rows: list[dict]) -> list[dict]:
    """
    Global business rules applied before any analytics calculation. This is
    the single choke point used by every analytics endpoint (see _fetch_rows
    in analytics.py) and by the AI strategy router — a rule added here
    applies everywhere (all tabs, all dashboards, AI context) with no risk
    of a dashboard being missed.

    Rule 1 — 400 L minimum volume, all freezer subtypes EXCEPT бонета/шок:
        In the "freezers" department, exclude any row whose vetka (liter
        range) starts below 400 L, with two deliberate exceptions —
        бонета and шок (see _VOLUME_RULE_EXEMPT_TIPS).
        Rationale (business owner, 11.08.2026): below 400 L the product is
        household equipment regardless of subtype, and household units
        distort market share, ветка segmentation and procurement priority
        for the whole department. The two exempt subtypes are commercial
        by construction at any capacity — a retail open-top display
        freezer and a professional blast chiller have no household
        equivalent, so a small one is a small commercial unit rather than
        a consumer product.

        Two safeguards on top of the threshold:

        (a) A row with NO vetka at all is NOT excluded. An empty vetka
            means "we failed to classify this SKU", not "this SKU is
            small" — _vetka_lower_bound returns 0 for an empty string,
            which would otherwise silently delete every unclassified row.
            Found live 11.08: Polair CM110-S and LUX 2X (commercial
            cabinets, 1.1M ₸ + 0.45M ₸ July revenue) have no vetka yet and
            were being dropped as if they were sub-400L household units.
            Unclassified rows stay visible so the gap is fixable instead
            of invisible.

        (b) Scoped to department == "freezers". The same тип strings can
            legitimately appear elsewhere and 400 L is a freezer-specific
            business threshold, not a universal one.

        Previously this rule was scoped to тип == "Ларь" only, which let
        sub-400 L шкаф/шок rows through while dropping every small Ларь.

    Rule 2 — Ice maker tabletop exclusion:
        For ice makers ("Льдогенератор"), exclude tabletop units entirely.
        Ice makers have no structured subtype field yet (тип is always
        "Льдогенератор") — tabletop vs floor-standing only shows up in the
        product name, e.g. "Настольный льдогенератор ...". Matched via
        substring on "настольн" (case-insensitive) rather than a strict
        prefix, since it's free text.
        Rationale (per business owner, July 2026): tabletop units are a
        consumer/home-segment product, not the commercial floor-standing
        equipment TorgStore actually sells — including them skews revenue,
        market share and the кг/сутки (production capacity) segmentation.

    Rule 3 — Duplicate mid-month snapshot exclusion (01.08):
        Some Kaspi export files include a SECOND, partial pull for a month
        taken mid-period — observed as month == "Июнь 15'" across multiple
        independent source files (Морозильники "по типам", Матрица Холод
        ветрины) and confirmed by row-level kod matching: every single
        "Июнь 15'" row is the SAME product as its "Июнь" counterpart with
        strictly lower units/revenue — a partial snapshot of the same
        period, not a distinct month. Counting both as if they were
        independent months double-counts June and produces a bogus extra
        bar in every Тренды chart between May and June. Matched structurally
        (any "<месяц> <число>'?" string) rather than hardcoding "июнь 15'"
        specifically, since the export tool that produces this isn't
        specific to June and could resurface for any month.

        Also excludes rows whose month value doesn't resolve to any
        recognized calendar month at all — e.g. a single stray "Пар" row
        found in Матрица Холод ветрины (kod=132720762, AVANGARD LC-1200FS),
        almost certainly a corrupted/mistyped export label (values are close
        to, but not identical to, that same SKU's legitimate "Март" row).
        Since it can't be confidently merged into any specific month, and a
        garbled month string otherwise renders as its own bogus bar in
        Тренды, it's dropped rather than guessed at. This is a general
        catch-all (any unrecognized month token, not just "Пар") so any
        future one-off typo of this kind is excluded automatically instead
        of requiring another manual patch.

    Rule 4 — Non-vitrina types excluded from "refrigerated" department (01.08):
        The "refrigerated" (Холод. витрины) department is meant to hold only
        genuine commercial display cases. The brand-based extraction from
        Kaspi's general "Холодильник" category (per business owner request,
        31.07) pulled in some rows whose тип is actually a different
        equipment class entirely — "Боета"/"Бонета" (chest-freezer subtypes
        that belong to the "freezers" department instead — see DeptEnum),
        "Ларь" (chest freezer), "Винный шкаф" (wine cabinet), "Холодильник"
        (generic fridge, not a display case), "Шкаф" (generic cabinet).
        Per business owner (01.08), these are excluded entirely from the
        refrigerated department's analytics — they're a different product
        class, not a mis-labeled display case. Scoped strictly to
        department == "refrigerated" so it does NOT touch the same тип
        strings when they're legitimate elsewhere (e.g. "Ларь" in
        "freezers" is still governed only by Rule 1's 400L threshold, and
        "Бонета" in "freezers" is untouched).
    """
    result = []
    for r in rows:
        tip = (r.get("tip") or "").strip().lower()
        dept = (r.get("department") or "")
        # Rule 1 — see docstring. Бонета exempt; empty vetka exempt (unknown
        # ≠ small); freezers only.
        if dept == "freezers" and tip not in _VOLUME_RULE_EXEMPT_TIPS:
            vetka_raw = (r.get("vetka") or "").strip()
            if vetka_raw and _vetka_lower_bound(vetka_raw) < _MIN_FREEZER_LITRES:
                continue
        if tip == "льдогенератор":
            if _TABLETOP_RE.search(r.get("name") or ""):
                continue
        if (r.get("department") or "") == "refrigerated" and tip in _NON_VITRINA_TYPES:
            continue
        month = str(r.get("month") or "").strip()
        if month:
            if _MIDPERIOD_SNAPSHOT_RE.match(month):
                continue
            month_word = month.split()[0].lower()
            if month_word not in _MONTH_IDX and month_word[:3] not in _MONTH_IDX:
                continue
        result.append(r)
    return result


def sku_key(r: dict) -> str:
    """
    Unique identifier for a product (SKU).
    Priority: kod → name. Normalised to uppercase, stripped.
    Multiple rows for the same product (different months/vetkas) share the same key.
    """
    kod = str(r.get("kod") or "").strip().upper()
    name = str(r.get("name") or "").strip().upper()
    key = kod if kod else name
    return key if key else "__UNKNOWN__"


def dedup_skus(rows: list[dict]) -> dict[str, dict]:
    """
    Collapse multiple rows for the same SKU into one representative record.
    Aggregates revenue/units; keeps the ABC and rating from the highest-revenue row.
    """
    skus: dict[str, dict] = {}
    for r in rows:
        k = sku_key(r)
        if k not in skus:
            skus[k] = {**r, "_revenue_sum": r["revenue"] or 0, "_units_sum": r["units"] or 0}
        else:
            existing = skus[k]
            existing["_revenue_sum"] += r["revenue"] or 0
            existing["_units_sum"] += r["units"] or 0
            # Keep ABC/rating from the row with highest revenue
            if (r["revenue"] or 0) > (existing["revenue"] or 0):
                existing["abc"] = r["abc"]
                existing["rating"] = r["rating"]
                existing["revenue"] = r["revenue"]
    return skus


def calc_overview(rows: list[dict], our_brands: set[str]) -> dict:
    if not rows:
        return {}

    total_rev = sum(r["revenue"] for r in rows)
    total_units = sum(r["units"] for r in rows)

    # Unique SKUs across the whole market (deduplicated)
    all_skus = dedup_skus(rows)
    unique_products = len(all_skus)
    unique_brands = len({r["brand"] for r in rows if r["brand"]})

    seller_rows = [r for r in rows if r["sellers"] > 0]
    avg_sellers = safe_div(sum(r["sellers"] for r in seller_rows), len(seller_rows))

    rrc_rows = [r for r in rows if r["rrc"] > 0]
    avg_rrc = safe_div(sum(r["rrc"] for r in rrc_rows), len(rrc_rows))

    # Our brands — rows
    our_rows = [r for r in rows if r["brand"].upper() in our_brands]
    our_rev = sum(r["revenue"] for r in our_rows)
    our_units = sum(r["units"] for r in our_rows)
    our_share = safe_div(our_rev, total_rev) * 100

    # Our SKUs — deduplicated
    our_skus = dedup_skus(our_rows)
    our_sku = len(our_skus)

    # ABC-A: count unique SKUs (not rows) where ABC == "A"
    our_abc_a = sum(1 for s in our_skus.values() if s.get("abc") == "A")

    our_rating_rows = [r for r in our_rows if r["rating"] > 0]
    avg_our_rating = safe_div(sum(r["rating"] for r in our_rating_rows), len(our_rating_rows))
    our_reviews = sum(r["reviews"] for r in our_rows)

    # Market ABC: count unique SKUs per category
    all_skus_list = list(all_skus.values())
    abc_counts = {"A": 0, "B": 0, "C": 0}
    abc_revenue = {"A": 0.0, "B": 0.0, "C": 0.0}
    for s in all_skus_list:
        k = s["abc"] if s.get("abc") in ("A", "B", "C") else "C"
        abc_counts[k] += 1
        abc_revenue[k] += s.get("_revenue_sum", s["revenue"] or 0)

    return {
        "total_revenue": total_rev,
        "total_units": total_units,
        "unique_products": unique_products,
        "unique_brands": unique_brands,
        "avg_sellers": round(avg_sellers, 1),
        "avg_rrc": round(avg_rrc, 2),
        "our_revenue": our_rev,
        "our_units": our_units,
        "our_sku": our_sku,
        "our_share_pct": round(our_share, 2),
        "our_avg_rating": round(avg_our_rating, 2),
        "our_reviews": our_reviews,
        "our_abc_a": our_abc_a,
        "abc_counts": abc_counts,
        "abc_revenue": abc_revenue,
    }


def calc_brands(rows: list[dict], our_brands: set[str]) -> list[dict]:
    total_rev = sum(r["revenue"] for r in rows) or 1
    brand_map: dict[str, dict] = {}

    for r in rows:
        b = r["brand"]
        if not b:
            continue
        if b not in brand_map:
            brand_map[b] = {
                "brand": b,
                "is_ours": b.upper() in our_brands,
                "revenue": 0.0, "units": 0.0,
                # sku_keys → set for deduplication
                "sku_rows": {},      # sku_key → best row (for ABC/rating)
                "reviews": 0.0,
                "sellers_sum": 0.0, "sellers_cnt": 0,
                "ratings": [],
            }
        m = brand_map[b]
        m["revenue"] += r["revenue"] or 0
        m["units"] += r["units"] or 0
        m["reviews"] += r["reviews"] or 0

        # Deduplicate SKUs per brand
        k = sku_key(r)
        if k not in m["sku_rows"] or (r["revenue"] or 0) > (m["sku_rows"][k]["revenue"] or 0):
            m["sku_rows"][k] = r

        if r["sellers"] > 0:
            m["sellers_sum"] += r["sellers"]
            m["sellers_cnt"] += 1
        if r["rating"] > 0:
            m["ratings"].append(r["rating"])

    result = []
    for m in brand_map.values():
        avg_rating = safe_div(sum(m["ratings"]), len(m["ratings"]))
        avg_sellers = safe_div(m["sellers_sum"], m["sellers_cnt"])

        # ABC-A: unique SKUs with ABC=A
        abc_a = sum(1 for s in m["sku_rows"].values() if s.get("abc") == "A")

        # Top SKU by revenue for direct Kaspi link
        top_sku = max(m["sku_rows"].values(), key=lambda s: s.get("revenue") or 0) if m["sku_rows"] else {}

        result.append({
            "brand": m["brand"],
            "is_ours": m["is_ours"],
            "revenue": round(m["revenue"], 2),
            "units": round(m["units"]),
            "skus": len(m["sku_rows"]),        # unique SKU count
            "reviews": round(m["reviews"]),
            "avg_rating": round(avg_rating, 2),
            "avg_sellers": round(avg_sellers, 1),
            "abc_a": abc_a,
            "market_share_pct": round(safe_div(m["revenue"], total_rev) * 100, 2),
            "top_kod": top_sku.get("kod") or "",
            "top_name": top_sku.get("name") or "",
        })

    return sorted(result, key=lambda x: x["revenue"], reverse=True)


def calc_vetka(rows: list[dict], our_brands: set[str]) -> list[dict]:
    total_rev = sum(r["revenue"] for r in rows) or 1
    vmap: dict[str, dict] = {}

    # Per-vetka brand revenue totals for correct leader calculation
    brand_rev_by_vetka: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    brand_top_sku: dict[str, dict[str, dict]] = defaultdict(dict)  # vetka → brand → top sku row

    for r in rows:
        k = r["vetka"] or "—"
        if k not in vmap:
            vmap[k] = {
                "vetka": k, "revenue": 0.0, "units": 0.0,
                "sku_keys": set(), "brands": set(), "rrcs": [], "our_rev": 0.0,
                "our_units": 0.0, "our_rrcs": [], "our_sku_keys": set(),
            }
        v = vmap[k]
        v["revenue"] += r["revenue"] or 0
        v["units"] += r["units"] or 0
        v["sku_keys"].add(sku_key(r))
        v["brands"].add(r["brand"])
        if r["rrc"] > 0:
            v["rrcs"].append(r["rrc"])
        if r["brand"].upper() in our_brands:
            v["our_rev"] += r["revenue"] or 0
            v["our_units"] += r["units"] or 0
            v["our_sku_keys"].add(sku_key(r))
            if r["rrc"] > 0:
                v["our_rrcs"].append(r["rrc"])
        # Track brand revenue totals and top SKU per brand per vetka
        b = r["brand"] or ""
        brand_rev_by_vetka[k][b] += r["revenue"] or 0
        row_rev = r["revenue"] or 0
        if b not in brand_top_sku[k] or row_rev > (brand_top_sku[k][b].get("revenue") or 0):
            brand_top_sku[k][b] = r

    result = []
    for v in vmap.values():
        k = v["vetka"]
        # Leader = competitor brand with highest TOTAL revenue in this vetka
        comp_brands = [(b, rev) for b, rev in brand_rev_by_vetka[k].items()
                       if b.upper() not in our_brands]
        if comp_brands:
            leader_b, _ = max(comp_brands, key=lambda x: x[1])
            leader_row = brand_top_sku[k].get(leader_b, {})
        else:
            leader_b, leader_row = "", {}
        mkt_avg_rrc = safe_div(sum(v["rrcs"]), len(v["rrcs"]))
        our_avg_rrc = safe_div(sum(v["our_rrcs"]), len(v["our_rrcs"]))
        rrc_pos_pct = round(safe_div(our_avg_rrc - mkt_avg_rrc, mkt_avg_rrc) * 100, 1) if mkt_avg_rrc and our_avg_rrc else None
        result.append({
            "vetka": k,
            "revenue": round(v["revenue"], 2),
            "units": round(v["units"]),
            "our_units": round(v["our_units"]),
            "skus": len(v["sku_keys"]),
            "our_skus": len(v["our_sku_keys"]),
            "brands": len(v["brands"]),
            "avg_rrc": round(mkt_avg_rrc, 0) if mkt_avg_rrc else None,
            "our_avg_rrc": round(our_avg_rrc, 0) if our_avg_rrc else None,
            "rrc_position_pct": rrc_pos_pct,
            "market_share_pct": round(safe_div(v["revenue"], total_rev) * 100, 2),
            "our_revenue": round(v["our_rev"], 2),
            "our_share_pct": round(safe_div(v["our_rev"], v["revenue"]) * 100, 2),
            "leader_brand": leader_b,
            "leader_kod": leader_row.get("kod") or "",
            "leader_name": leader_row.get("name") or "",
        })

    return sorted(result, key=lambda x: x["revenue"], reverse=True)


def calc_subtype_compare(rows: list[dict], our_brands: set[str]) -> list[dict]:
    """
    If rows contain multiple types → compare types (Ларь vs Бонета).
    If rows are already filtered to one type → compare by vetka (liter range).
    """
    types = {r["tip"] for r in rows if r["tip"]}
    total_rev = sum(r["revenue"] for r in rows) or 1

    # Choose grouping key
    if len(types) > 1:
        # Multiple types → group by tip
        groups = sorted(types)
        key_fn = lambda r: r["tip"]
    else:
        # Single type (filtered) → group by vetka (liter range)
        groups = sorted({r["vetka"] for r in rows if r["vetka"]},
                        key=lambda v: _vetka_sort_key(v))
        key_fn = lambda r: r["vetka"]

    result = []
    for g_val in groups:
        tr = [r for r in rows if key_fn(r) == g_val]
        if not tr:
            continue
        rev = sum(r["revenue"] for r in tr)
        our_rev = sum(r["revenue"] for r in tr if r["brand"].upper() in our_brands)
        result.append({
            "subtype": g_val,
            "revenue": round(rev, 2),
            "units": round(sum(r["units"] for r in tr)),
            "skus": len({sku_key(r) for r in tr}),
            "our_skus": len({sku_key(r) for r in tr if r["brand"].upper() in our_brands}),
            "brands": len({r["brand"] for r in tr}),
            "market_share_pct": round(safe_div(rev, total_rev) * 100, 2),
            "our_revenue": round(our_rev, 2),
            "our_share_pct": round(safe_div(our_rev, rev) * 100, 2),
        })
    return result


def _vetka_sort_key(v: str) -> tuple:
    """Sort vetka strings like '100-200', '500-600', 'до 100' numerically."""
    nums = re.findall(r"\d+", str(v))
    return (int(nums[0]),) if nums else (9999,)


def calc_monthly(rows: list[dict], our_brands: set[str]) -> list[dict]:
    months: dict[str, dict] = {}
    for r in rows:
        m = r["month"] or "—"
        if m not in months:
            months[m] = {"month": m, "revenue": 0.0, "units": 0.0, "our_revenue": 0.0,
                         "sku_keys": set(), "our_sku_keys": set()}
        months[m]["revenue"] += r["revenue"] or 0
        months[m]["units"] += r["units"] or 0
        months[m]["sku_keys"].add(sku_key(r))
        if r["brand"].upper() in our_brands:
            months[m]["our_revenue"] += r["revenue"] or 0
            months[m]["our_sku_keys"].add(sku_key(r))

    result = sorted(months.values(), key=lambda x: _month_sort_key(x["month"]))
    out = []
    for m in result:
        total = m["revenue"] or 1
        out.append({
            "month": m["month"],
            "revenue": round(m["revenue"], 2),
            "units": round(m["units"]),
            "skus": len(m["sku_keys"]),
            "our_skus": len(m["our_sku_keys"]),
            "our_revenue": round(m["our_revenue"], 2),
            "our_share_pct": round(safe_div(m["our_revenue"], total) * 100, 2),
        })
    return out


def calc_intelligence(rows: list[dict], our_brands: set[str]) -> dict:
    """
    World-class analytics engine:
    market position · review ROI · segment penetrability · time-to-leadership
    SKU momentum · cannibalization · competitive threats · Kaspi rank score · seasonal forecast
    """
    if not rows:
        return {}

    total_rev = sum(r["revenue"] for r in rows) or 1

    # ── Grouping helpers ──────────────────────────────────────────────────────
    vetka_rows: dict[str, list] = defaultdict(list)
    for r in rows:
        vetka_rows[r["vetka"] or "—"].append(r)

    # ── 1. Market position ────────────────────────────────────────────────────
    brand_rev: dict[str, float] = defaultdict(float)
    brand_reviews: dict[str, float] = defaultdict(float)
    brand_sku_keys: dict[str, set] = defaultdict(set)
    for r in rows:
        b = r["brand"]
        brand_rev[b] += r["revenue"] or 0
        brand_reviews[b] += r["reviews"] or 0
        brand_sku_keys[b].add(sku_key(r))

    our_combined_rev = sum(v for b, v in brand_rev.items() if b.upper() in our_brands)

    # Build combined ranking: TorgStore vs individual competitors
    combined_ranking: dict[str, float] = {"__OURS__": our_combined_rev}
    for b, rev in brand_rev.items():
        if b.upper() not in our_brands:
            combined_ranking[b] = rev
    sorted_combined = sorted(combined_ranking.items(), key=lambda x: -x[1])
    our_rank = next((i + 1 for i, (k, _) in enumerate(sorted_combined) if k == "__OURS__"), None)

    # Competitors only (for gap calculations)
    comp_sorted = sorted(
        [(b, rev) for b, rev in brand_rev.items() if b.upper() not in our_brands],
        key=lambda x: -x[1],
    )
    leader_brand = comp_sorted[0][0] if comp_sorted else None
    leader_rev = comp_sorted[0][1] if comp_sorted else 0
    second_brand = comp_sorted[1][0] if len(comp_sorted) > 1 else None
    second_rev = comp_sorted[1][1] if len(comp_sorted) > 1 else 0

    market_position = {
        "our_combined_revenue": round(our_combined_rev, 2),
        "our_rank": our_rank or len(combined_ranking),
        "total_brands": len(brand_rev),
        "market_share_pct": round(safe_div(our_combined_rev, total_rev) * 100, 2),
        "leader_brand": leader_brand,
        "leader_revenue": round(leader_rev, 2),
        "second_brand": second_brand,
        "second_revenue": round(second_rev, 2),
        "gap_to_leader": round(max(0, leader_rev - our_combined_rev), 2),
        "gap_to_second": round(max(0, second_rev - our_combined_rev), 2),
    }

    # ── 2. Review ROI (₸ per review, by segment) ──────────────────────────────
    def _regression_slope(points: list[tuple]) -> float:
        """Linear regression slope for (x, y) pairs."""
        n = len(points)
        if n < 3:
            return 0.0
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        mx = sum(xs) / n
        my = sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        var_x = sum((x - mx) ** 2 for x in xs)
        return cov / var_x if var_x > 0 else 0.0

    review_roi_by_vetka: dict[str, int] = {}
    all_roi_points: list[tuple] = []

    for vetka, vrows in vetka_rows.items():
        # Dedup SKUs in this vetka → reviews vs total revenue
        sku_agg: dict[str, dict] = {}
        for r in vrows:
            k = sku_key(r)
            if k not in sku_agg:
                sku_agg[k] = {"rev": 0.0, "reviews": r["reviews"] or 0}
            sku_agg[k]["rev"] += r["revenue"] or 0

        points = [(s["reviews"], s["rev"]) for s in sku_agg.values()
                  if s["reviews"] > 0 and s["rev"] > 0]
        slope = _regression_slope(points)
        if slope > 0:
            review_roi_by_vetka[vetka] = int(slope)
        all_roi_points.extend(points)

    overall_roi: int = max(0, int(_regression_slope(all_roi_points)))

    # ── 3. Segment intelligence (penetrability + time-to-leadership) ──────────
    segment_intelligence = []

    for vetka, vrows in sorted(vetka_rows.items(),
                                key=lambda x: -sum(r["revenue"] for r in x[1])):
        vtotal_rev = sum(r["revenue"] for r in vrows) or 1

        # Per-brand in this vetka
        vb_rev: dict[str, float] = defaultdict(float)
        vb_reviews: dict[str, float] = defaultdict(float)
        vb_skus: dict[str, set] = defaultdict(set)
        # Track top SKU per brand for direct Kaspi links
        vb_top_sku: dict[str, dict] = {}
        for r in vrows:
            b = r["brand"]
            vb_rev[b] += r["revenue"] or 0
            vb_reviews[b] += r["reviews"] or 0
            vb_skus[b].add(sku_key(r))
            row_rev = r["revenue"] or 0
            if b not in vb_top_sku or row_rev > (vb_top_sku[b].get("revenue") or 0):
                vb_top_sku[b] = r

        # Top-3 concentration
        top3_vals = sorted(vb_rev.values(), reverse=True)[:3]
        top3_rev = sum(top3_vals)
        top3_concentration = safe_div(top3_rev, vtotal_rev)

        # Competitor leader in this vetka
        comp_vb = [(b, rev) for b, rev in vb_rev.items() if b.upper() not in our_brands]
        leader_b = leader_b_rev = leader_b_reviews = None
        if comp_vb:
            leader_b, leader_b_rev = max(comp_vb, key=lambda x: x[1])
            leader_b_reviews = vb_reviews.get(leader_b, 0)

        our_rev_v = sum(vb_rev[b] for b in vb_rev if b.upper() in our_brands)
        our_reviews_v = sum(vb_reviews[b] for b in vb_reviews if b.upper() in our_brands)
        our_skus_v: set = set()
        for b in vb_skus:
            if b.upper() in our_brands:
                our_skus_v |= vb_skus[b]

        # Penetrability (0–100): higher = easier to enter/grow
        review_barrier = min(1.0, (leader_b_reviews or 0) / 150.0)
        penetrability = int((1 - top3_concentration * 0.5) * (1 - review_barrier * 0.5) * 100)
        penetrability = max(5, min(95, penetrability))

        # Reviews gap and time-to-leadership
        reviews_gap = max(0, (leader_b_reviews or 0) - our_reviews_v)
        months_organic = math.ceil(reviews_gap / 2) if reviews_gap > 0 else 0
        months_campaign = math.ceil(reviews_gap / 10) if reviews_gap > 0 else 0

        # Revenue opportunity: matching leader share
        leader_share = safe_div(leader_b_rev or 0, vtotal_rev)
        our_share = safe_div(our_rev_v, vtotal_rev)
        rev_opportunity = max(0, (leader_share - our_share) * vtotal_rev)

        # ROI: ₸ per review in this segment
        roi = review_roi_by_vetka.get(vetka, overall_roi)

        leader_top = vb_top_sku.get(leader_b, {}) if leader_b else {}
        segment_intelligence.append({
            "vetka": vetka,
            "market_revenue": round(vtotal_rev, 2),
            "our_revenue": round(our_rev_v, 2),
            "our_share_pct": round(our_share * 100, 2),
            "our_skus": len(our_skus_v),
            "leader_brand": leader_b,
            "leader_kod": leader_top.get("kod") or "",
            "leader_name": leader_top.get("name") or "",
            "leader_revenue": round(leader_b_rev or 0, 2),
            "leader_share_pct": round(leader_share * 100, 2),
            "leader_reviews": int(leader_b_reviews or 0),
            "our_reviews": int(our_reviews_v),
            "reviews_gap": int(reviews_gap),
            "penetrability_score": penetrability,
            "months_organic": months_organic,
            "months_campaign": months_campaign,
            "revenue_opportunity": round(rev_opportunity, 2),
            "roi_per_review": int(roi),
            "top3_concentration_pct": round(top3_concentration * 100, 1),
        })

    # ── 4. SKU momentum (revenue trend across months) ─────────────────────────
    sku_monthly: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    sku_meta: dict[str, dict] = {}
    for r in rows:
        k = sku_key(r)
        m = r["month"] or "—"
        sku_monthly[k][m] += r["revenue"] or 0
        # Keep metadata from highest-revenue row
        if k not in sku_meta or (r["revenue"] or 0) > (sku_meta[k].get("revenue") or 0):
            sku_meta[k] = {
                "name": r["name"] or r["kod"] or k,
                "brand": r["brand"],
                "kod": r["kod"] or "",
                "vetka": r["vetka"] or "—",
                "tip": r["tip"] or "—",
                "revenue": r["revenue"] or 0,
                "is_ours": r["brand"].upper() in our_brands,
            }

    momentum_list = []
    for k, mdata in sku_monthly.items():
        if len(mdata) < 2:
            continue
        sorted_m = sorted(mdata.keys())
        revenues = [mdata[m] for m in sorted_m]
        n = len(revenues)
        slope = _regression_slope(list(zip(range(n), revenues)))
        avg_rev = sum(revenues) / n
        pct = safe_div(slope, avg_rev) * 100

        meta = sku_meta.get(k, {})
        momentum_list.append({
            "name": meta.get("name", k),
            "brand": meta.get("brand", ""),
            "kod": meta.get("kod", ""),
            "vetka": meta.get("vetka", "—"),
            "tip": meta.get("tip", "—"),
            "is_ours": meta.get("is_ours", False),
            "avg_monthly_revenue": round(avg_rev, 2),
            "momentum_pct": round(pct, 1),
            "months": n,
            "latest_revenue": round(revenues[-1], 2),
            "peak_revenue": round(max(revenues), 2),
        })

    rising_ours = sorted([m for m in momentum_list if m["is_ours"] and m["momentum_pct"] > 5],
                         key=lambda x: -x["momentum_pct"])[:10]
    declining_ours = sorted([m for m in momentum_list if m["is_ours"] and m["momentum_pct"] < -10],
                             key=lambda x: x["momentum_pct"])[:10]
    comp_rising = sorted([m for m in momentum_list if not m["is_ours"] and m["momentum_pct"] > 10],
                         key=lambda x: -x["momentum_pct"])[:10]

    # ── 5. Brand cannibalization (our brands competing in same vetka) ──────────
    our_by_vetka: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r["brand"].upper() in our_brands:
            our_by_vetka[r["vetka"] or "—"][r["brand"]] += r["revenue"] or 0

    cannibalization = []
    for vetka, brand_revs in our_by_vetka.items():
        if len(brand_revs) < 2:
            continue
        vtotal = sum(r["revenue"] for r in vetka_rows[vetka]) or 1
        sorted_br = sorted(brand_revs.items(), key=lambda x: -x[1])
        cannibalization.append({
            "vetka": vetka,
            "market_revenue": round(vtotal, 2),
            "combined_share_pct": round(safe_div(sum(brand_revs.values()), vtotal) * 100, 1),
            "brands": [{"brand": b, "revenue": round(r, 2),
                        "share_pct": round(safe_div(r, vtotal) * 100, 1)}
                       for b, r in sorted_br],
        })
    cannibalization.sort(key=lambda x: -x["market_revenue"])

    # ── 6. Competitive threat radar ───────────────────────────────────────────
    # Competitors gaining presence in segments where WE have revenue
    our_active_vetkas = {s["vetka"] for s in segment_intelligence if s["our_revenue"] > 0}

    threat_rev: dict[str, float] = defaultdict(float)
    threat_skus: dict[str, set] = defaultdict(set)
    threat_vetkas: dict[str, set] = defaultdict(set)
    threat_top_sku: dict[str, dict] = {}
    for r in rows:
        if r["brand"].upper() in our_brands:
            continue
        v = r["vetka"] or "—"
        if v in our_active_vetkas:
            b = r["brand"]
            threat_rev[b] += r["revenue"] or 0
            threat_skus[b].add(sku_key(r))
            threat_vetkas[b].add(v)
            row_rev = r["revenue"] or 0
            if b not in threat_top_sku or row_rev > (threat_top_sku[b].get("revenue") or 0):
                threat_top_sku[b] = r

    brand_momentum_avg: dict[str, float] = defaultdict(float)
    brand_momentum_cnt: dict[str, int] = defaultdict(int)
    for m in momentum_list:
        if not m["is_ours"]:
            brand_momentum_avg[m["brand"]] += m["momentum_pct"]
            brand_momentum_cnt[m["brand"]] += 1

    competitive_threats = sorted([
        {
            "brand": b,
            "revenue": round(threat_rev[b], 2),
            "sku_count": len(threat_skus[b]),
            "vetkas_count": len(threat_vetkas[b]),
            "avg_momentum_pct": round(
                safe_div(brand_momentum_avg[b], brand_momentum_cnt[b]), 1
            ) if brand_momentum_cnt[b] else 0,
            "top_kod": threat_top_sku.get(b, {}).get("kod") or "",
            "top_name": threat_top_sku.get(b, {}).get("name") or "",
        }
        for b in threat_rev
    ], key=lambda x: -x["revenue"])[:10]

    # ── 7. Kaspi rank scores for our SKUs ─────────────────────────────────────
    kaspi_scores = []
    seen_sku = set()
    for r in rows:
        if r["brand"].upper() not in our_brands:
            continue
        k = sku_key(r)
        if k in seen_sku:
            continue
        seen_sku.add(k)
        v = r["vetka"] or "—"
        vtotal = sum(rr["revenue"] for rr in vetka_rows[v]) or 1
        rev_share = (r["revenue"] or 0) / vtotal
        rating = r["rating"] or 0
        reviews = r["reviews"] or 0
        score = (rating ** 2) * math.log(reviews + 1) * (rev_share ** 0.5) * 100
        kaspi_scores.append({
            "name": r["name"] or r["kod"] or "—",
            "brand": r["brand"],
            "vetka": v,
            "reviews": int(reviews),
            "rating": round(rating, 2),
            "kaspi_score": round(score, 1),
            "rev_share_pct": round(rev_share * 100, 1),
        })
    kaspi_scores.sort(key=lambda x: -x["kaspi_score"])

    # ── 8. Seasonal forecast ─────────────────────────────────────────────────
    month_totals: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "ours": 0.0})
    for r in rows:
        m = r["month"] or "—"
        month_totals[m]["total"] += r["revenue"] or 0
        if r["brand"].upper() in our_brands:
            month_totals[m]["ours"] += r["revenue"] or 0

    sorted_months = sorted(month_totals.keys())
    forecast = None
    if len(sorted_months) >= 3:
        last3 = sorted_months[-3:]
        lt = [month_totals[m]["total"] for m in last3]
        lo = [month_totals[m]["ours"] for m in last3]
        st = _regression_slope(list(zip(range(3), lt)))
        so = _regression_slope(list(zip(range(3), lo)))
        forecast = {
            "next_month_total": round(max(0, lt[-1] + st), 2),
            "next_month_ours": round(max(0, lo[-1] + so), 2),
            "trend_total": round(st, 2),
            "trend_ours": round(so, 2),
            "based_on": last3,
        }

    return {
        "market_position": market_position,
        "review_roi_overall": overall_roi,
        "review_roi_by_vetka": review_roi_by_vetka,
        "segment_intelligence": segment_intelligence[:20],
        "sku_momentum": {
            "rising_ours": rising_ours,
            "declining_ours": declining_ours,
            "comp_rising": comp_rising,
        },
        "cannibalization": cannibalization[:8],
        "competitive_threats": competitive_threats,
        "kaspi_scores": kaspi_scores[:20],
        "seasonal_forecast": forecast,
    }


def calc_monthly_trends(rows: list[dict], our_brands: set[str]) -> dict:
    """
    Deep monthly trend analysis:
    - Overview per month (revenue, share, units)
    - Heatmap: vetka × month
    - Subtype breakdown per month
    - Top brands with monthly sparklines
    - Segments where we're losing / gaining share
    - Auto-generated trend insights
    """
    if not rows:
        return {}

    raw_months = sorted(
        {r["month"] for r in rows if r["month"]},
        key=_month_sort_key,
    )

    # ── 1. Monthly overview ────────────────────────────────────────────────────
    m_data: dict[str, dict] = defaultdict(lambda: {
        "revenue": 0.0, "our_revenue": 0.0, "units": 0.0,
        "sku_keys": set(), "our_sku_keys": set(),
    })
    for r in rows:
        m = r["month"] or "—"
        m_data[m]["revenue"]   += r["revenue"] or 0
        m_data[m]["units"]     += r["units"]   or 0
        m_data[m]["sku_keys"].add(sku_key(r))
        if r["brand"].upper() in our_brands:
            m_data[m]["our_revenue"]   += r["revenue"] or 0
            m_data[m]["our_sku_keys"].add(sku_key(r))

    monthly_overview = []
    for m in raw_months:
        d = m_data[m]
        prev = monthly_overview[-1] if monthly_overview else None
        total = d["revenue"] or 1
        rev    = round(d["revenue"], 2)
        our_r  = round(d["our_revenue"], 2)
        share  = round(safe_div(d["our_revenue"], total) * 100, 2)
        monthly_overview.append({
            "month":        m,
            "revenue":      rev,
            "our_revenue":  our_r,
            "units":        round(d["units"]),
            "our_share_pct": share,
            "skus":         len(d["sku_keys"]),
            "our_skus":     len(d["our_sku_keys"]),
            "mom_revenue_pct":   round(safe_div(rev - prev["revenue"],   prev["revenue"]) * 100, 1) if prev else None,
            "mom_our_pct":       round(safe_div(our_r - prev["our_revenue"], prev["our_revenue"]) * 100, 1) if prev else None,
            "share_delta":       round(share - prev["our_share_pct"], 2) if prev else None,
        })

    # ── 2. By vetka × month (heatmap) ─────────────────────────────────────────
    vm_data: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {
        "revenue": 0.0, "our_revenue": 0.0, "units": 0.0,
    }))
    vtotal_rev: dict[str, float] = defaultdict(float)

    for r in rows:
        v = r["vetka"] or "—"
        m = r["month"] or "—"
        vm_data[v][m]["revenue"]  += r["revenue"] or 0
        vm_data[v][m]["units"]    += r["units"]   or 0
        vtotal_rev[v]             += r["revenue"] or 0
        if r["brand"].upper() in our_brands:
            vm_data[v][m]["our_revenue"] += r["revenue"] or 0

    top_vetkas = sorted(vtotal_rev, key=lambda x: -vtotal_rev[x])[:15]
    by_vetka = {}
    for v in top_vetkas:
        by_vetka[v] = []
        for m in raw_months:
            d = vm_data[v][m]
            total_vm = d["revenue"] or 1
            by_vetka[v].append({
                "month":        m,
                "revenue":      round(d["revenue"], 2),
                "our_revenue":  round(d["our_revenue"], 2),
                "our_share_pct": round(safe_div(d["our_revenue"], total_vm) * 100, 2),
            })

    # ── 3. By subtype (tip) × month ───────────────────────────────────────────
    tm_data: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {
        "revenue": 0.0, "our_revenue": 0.0, "units": 0.0,
    }))
    for r in rows:
        t = r["tip"] or "—"
        m = r["month"] or "—"
        tm_data[t][m]["revenue"]  += r["revenue"] or 0
        tm_data[t][m]["units"]    += r["units"]   or 0
        if r["brand"].upper() in our_brands:
            tm_data[t][m]["our_revenue"] += r["revenue"] or 0

    by_subtype = {}
    for t, mmap in tm_data.items():
        if t == "—":
            continue
        by_subtype[t] = []
        for m in raw_months:
            d = mmap[m]
            total_tm = d["revenue"] or 1
            by_subtype[t].append({
                "month":        m,
                "revenue":      round(d["revenue"], 2),
                "our_revenue":  round(d["our_revenue"], 2),
                "our_share_pct": round(safe_div(d["our_revenue"], total_tm) * 100, 2),
            })

    # ── 4. Top brands × month (sparklines) ────────────────────────────────────
    bm_data: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    brand_total: dict[str, float] = defaultdict(float)
    for r in rows:
        b = r["brand"]
        m = r["month"] or "—"
        bm_data[b][m]  += r["revenue"] or 0
        brand_total[b] += r["revenue"] or 0

    top_brands = sorted(brand_total, key=lambda x: -brand_total[x])[:20]
    by_brand = []
    for b in top_brands:
        monthly_vals = [round(bm_data[b].get(m, 0), 2) for m in raw_months]
        first_v = next((v for v in monthly_vals if v > 0), 0)
        last_v  = monthly_vals[-1] if monthly_vals else 0
        total_b = round(brand_total[b], 2)
        trend_pct = round(safe_div(last_v - first_v, first_v) * 100, 1) if first_v else None
        by_brand.append({
            "brand":     b,
            "is_ours":   b.upper() in our_brands,
            "total":     total_b,
            "months":    monthly_vals,
            "trend_pct": trend_pct,  # first → last % change
        })

    # ── 5. Where we're losing / gaining share (per vetka) ─────────────────────
    losing: list[dict] = []
    winning: list[dict] = []

    for v in top_vetkas:
        series = by_vetka[v]
        active = [s for s in series if s["our_revenue"] > 0]
        if len(active) < 2:
            continue
        first_s = active[0]["our_share_pct"]
        last_s  = active[-1]["our_share_pct"]
        delta   = round(last_s - first_s, 2)
        market_rev = sum(s["revenue"] for s in series)
        our_rev    = sum(s["our_revenue"] for s in series)
        entry = {
            "vetka":           v,
            "first_month":     active[0]["month"],
            "last_month":      active[-1]["month"],
            "first_share_pct": round(first_s, 2),
            "last_share_pct":  round(last_s, 2),
            "delta_pct":       delta,
            "market_revenue":  round(market_rev, 2),
            "our_revenue":     round(our_rev, 2),
            "monthly":         series,
        }
        if delta <= -1.0:
            losing.append(entry)
        elif delta >= 1.0:
            winning.append(entry)

    losing.sort(key=lambda x: x["delta_pct"])
    winning.sort(key=lambda x: -x["delta_pct"])

    # ── 6. Same analysis for subtypes ─────────────────────────────────────────
    losing_subtypes: list[dict] = []
    for t, series in [(t, by_subtype[t]) for t in by_subtype]:
        active = [s for s in series if s["our_revenue"] > 0]
        if len(active) < 2:
            continue
        first_s = active[0]["our_share_pct"]
        last_s  = active[-1]["our_share_pct"]
        delta   = round(last_s - first_s, 2)
        if delta <= -1.0:
            losing_subtypes.append({
                "subtype":         t,
                "first_share_pct": round(first_s, 2),
                "last_share_pct":  round(last_s, 2),
                "delta_pct":       delta,
                "market_revenue":  round(sum(s["revenue"] for s in series), 2),
                "monthly":         series,
            })
    losing_subtypes.sort(key=lambda x: x["delta_pct"])

    # ── 7. Auto insights ──────────────────────────────────────────────────────
    insights: list[dict] = []
    if monthly_overview:
        peak = max(monthly_overview, key=lambda x: x["revenue"])
        peak_month_name = _month_name(peak["month"])
        peak_idx = MONTH_ORDER.index(peak_month_name) if peak_month_name in MONTH_ORDER else -1
        # БАГ 06.08 (директорский аудит): пик — это просто max() по загруженным
        # месяцам, без разбора "это реально пик" vs "это последний загруженный
        # месяц, а сезон мог и не закончиться". Если peak — последний элемент
        # monthly_overview, мы НЕ знаем, что было дальше (следующих месяцев
        # просто нет в базе) — заявлять "летний пик" уверенным тоном в этом
        # случае неверно: сайт может отставать от календаря на недели, и
        # реальный пик ещё может быть впереди. Формулировка смягчена, только
        # если данные реально обрываются на пике (не на более позднем спаде).
        is_last_loaded_month = peak is monthly_overview[-1] and len(monthly_overview) >= 1
        if peak_idx in (3, 4):   # Апрель, Май
            peak_comment = "Пик типичен для сезона охлаждения."
        elif peak_idx in (5, 6, 7):  # Июнь–Август
            peak_comment = "Летний пик — высокий сезон продаж."
        elif peak_idx in (10, 11, 0):  # Ноябрь–Январь
            peak_comment = "Пик в период распродаж и новогоднего сезона."
        else:
            peak_comment = ""
        if is_last_loaded_month and peak_comment:
            peak_comment = (f"{peak_comment} Внимание: это последний загруженный месяц — "
                             f"неизвестно, продолжится ли рост дальше, если данные за "
                             f"следующие месяцы ещё не загружены.")
        insights.append({"type": "info", "text": f"Пик рынка — {peak['month']} ({peak['revenue']/1e6:.0f} млн ₸). {peak_comment}".strip()})
        first_o, last_o = monthly_overview[0], monthly_overview[-1]
        rev_growth = round(safe_div(last_o["revenue"] - first_o["revenue"], first_o["revenue"]) * 100, 1)
        our_growth = round(safe_div(last_o["our_revenue"] - first_o["our_revenue"], first_o["our_revenue"]) * 100, 1)
        share_delta = round(last_o["our_share_pct"] - first_o["our_share_pct"], 2)
        if share_delta < -0.5:
            insights.append({"type": "danger",
                "text": f"Рынок вырос на {rev_growth}%, наша выручка — на {our_growth}%. "
                        f"Рынок растёт быстрее → доля снизилась на {abs(share_delta):.1f}% за период."})
        elif share_delta > 0.5:
            insights.append({"type": "success",
                "text": f"Доля рынка выросла на {share_delta:.1f}% (с {first_o['our_share_pct']:.1f}% до {last_o['our_share_pct']:.1f}%). "
                        f"Мы растём быстрее рынка — хороший сигнал."})
        else:
            insights.append({"type": "info",
                "text": f"Доля рынка стабильна: {first_o['our_share_pct']:.1f}% → {last_o['our_share_pct']:.1f}% за период."})

    if losing:
        top_l = losing[0]
        insights.append({"type": "danger",
            "text": f"Наибольшая потеря доли: сегмент «{top_l['vetka']}» — "
                    f"{top_l['first_share_pct']:.1f}% → {top_l['last_share_pct']:.1f}% ({top_l['delta_pct']:+.1f}%). "
                    f"Объём рынка: {top_l['market_revenue']/1e6:.0f} млн ₸ — критично."})
    if winning:
        top_w = winning[0]
        insights.append({"type": "success",
            "text": f"Лучший рост доли: сегмент «{top_w['vetka']}» — "
                    f"{top_w['first_share_pct']:.1f}% → {top_w['last_share_pct']:.1f}% (+{top_w['delta_pct']:.1f}%). "
                    f"Масштабировать стратегию этого сегмента."})
    if losing_subtypes:
        ls = losing_subtypes[0]
        insights.append({"type": "warning",
            "text": f"Тип «{ls['subtype']}» теряет долю: {ls['first_share_pct']:.1f}% → {ls['last_share_pct']:.1f}% ({ls['delta_pct']:+.1f}%). "
                    f"Конкуренты усиливаются в этой категории."})

    return {
        "months": raw_months,
        "monthly_overview": monthly_overview,
        "by_vetka": by_vetka,
        "by_subtype": by_subtype,
        "by_brand": by_brand,
        "losing_segments": losing[:10],
        "winning_segments": winning[:10],
        "losing_subtypes": losing_subtypes[:5],
        "insights": insights,
    }


def calc_strategy(rows: list[dict], our_brands: set[str]) -> dict:
    """
    Strategic analysis: review deficit, segment gaps, low-rating SKUs,
    SKU efficiency, monthly trend, competitor benchmarks.
    """
    if not rows:
        return {}

    total_rev = sum(r["revenue"] for r in rows) or 1

    # ── Dedup SKUs ─────────────────────────────────────────────────────────────
    all_skus = dedup_skus(rows)
    our_rows = [r for r in rows if r["brand"].upper() in our_brands]
    our_skus = dedup_skus(our_rows)

    # ── 1. Review deficit ──────────────────────────────────────────────────────
    no_reviews: list[dict] = []
    few_reviews: list[dict] = []   # 1-4 reviews
    low_rating: list[dict] = []    # rating < 4.5 and rating > 0

    for s in our_skus.values():
        rev_cnt = s.get("reviews") or 0
        rat = s.get("rating") or 0
        item = {
            "name": s.get("name") or s.get("kod") or "—",
            "brand": s.get("brand", ""),
            "kod": s.get("kod") or "",
            "revenue": round(s.get("_revenue_sum", s.get("revenue") or 0), 2),
            "reviews": int(rev_cnt),
            "rating": round(rat, 2),
            "abc": s.get("abc") or "?",
            "vetka": s.get("vetka") or "—",
        }
        if rev_cnt == 0:
            no_reviews.append(item)
        elif rev_cnt < 5:
            few_reviews.append(item)
        if 0 < rat < 4.5:
            low_rating.append(item)

    no_reviews.sort(key=lambda x: -x["revenue"])
    few_reviews.sort(key=lambda x: -x["revenue"])
    low_rating.sort(key=lambda x: x["rating"])

    # ── 2. Segment gaps (by vetka) ─────────────────────────────────────────────
    vetka_map: dict[str, dict] = {}
    for r in rows:
        v = r["vetka"] or "—"
        if v not in vetka_map:
            vetka_map[v] = {"rev": 0.0, "our_rev": 0.0, "our_sku_keys": set(), "sku_keys": set(), "brands": set()}
        vetka_map[v]["rev"] += r["revenue"] or 0
        vetka_map[v]["sku_keys"].add(sku_key(r))
        vetka_map[v]["brands"].add(r["brand"])
        if r["brand"].upper() in our_brands:
            vetka_map[v]["our_rev"] += r["revenue"] or 0
            vetka_map[v]["our_sku_keys"].add(sku_key(r))

    # Competitors per vetka (top 3 by revenue)
    vetka_brand_rev: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    # Also track top SKU per vetka for direct Kaspi links
    vetka_top_sku: dict[str, dict] = {}
    for r in rows:
        v = r["vetka"] or "—"
        vetka_brand_rev[v][r["brand"]] += r["revenue"] or 0
        row_rev = r["revenue"] or 0
        if v not in vetka_top_sku or row_rev > (vetka_top_sku[v].get("revenue") or 0):
            if r["brand"].upper() not in our_brands:  # prefer competitor leader
                vetka_top_sku[v] = r

    segment_gaps = []
    for v, d in sorted(vetka_map.items(), key=lambda x: -x[1]["rev"]):
        our_share = safe_div(d["our_rev"], d["rev"]) * 100
        mkt_share = safe_div(d["rev"], total_rev) * 100
        top_competitors = sorted(
            [(b, rev) for b, rev in vetka_brand_rev[v].items() if b.upper() not in our_brands],
            key=lambda x: -x[1]
        )[:3]
        top_sku = vetka_top_sku.get(v, {})
        segment_gaps.append({
            "vetka": v,
            "market_revenue": round(d["rev"], 2),
            "market_share_pct": round(mkt_share, 2),
            "our_revenue": round(d["our_rev"], 2),
            "our_share_pct": round(our_share, 2),
            "our_skus": len(d["our_sku_keys"]),
            "total_skus": len(d["sku_keys"]),
            "is_gap": our_share < 10 and d["rev"] > 30_000_000,
            "leader_brand": top_competitors[0][0] if top_competitors else "",
            "leader_kod": top_sku.get("kod") or "",
            "leader_name": top_sku.get("name") or "",
            "top_competitors": [
                {"brand": b, "revenue": round(r, 2), "share_pct": round(safe_div(r, d["rev"]) * 100, 1)}
                for b, r in top_competitors
            ],
        })

    # ── 3. SKU efficiency ──────────────────────────────────────────────────────
    # Our SKUs per brand
    brand_skus: dict[str, dict] = {}
    for r in rows:
        b = r["brand"]
        if b not in brand_skus:
            brand_skus[b] = {"rev": 0.0, "skus": set()}
        brand_skus[b]["rev"] += r["revenue"] or 0
        brand_skus[b]["skus"].add(sku_key(r))

    our_total_skus = len(our_skus)
    our_total_rev = sum(r["revenue"] for r in our_rows)
    our_rev_per_sku = safe_div(our_total_rev, our_total_skus)

    # Best competitor efficiency
    comp_efficiency = [
        {
            "brand": b,
            "revenue": round(d["rev"], 2),
            "skus": len(d["skus"]),
            "rev_per_sku": round(safe_div(d["rev"], len(d["skus"])), 2),
        }
        for b, d in brand_skus.items()
        if b.upper() not in our_brands and len(d["skus"]) >= 1
    ]
    comp_efficiency.sort(key=lambda x: -x["rev_per_sku"])

    # ── 4. Monthly trend ───────────────────────────────────────────────────────
    month_data: dict[str, dict] = {}
    for r in rows:
        m = r["month"] or "—"
        if m not in month_data:
            month_data[m] = {"rev": 0.0, "our_rev": 0.0}
        month_data[m]["rev"] += r["revenue"] or 0
        if r["brand"].upper() in our_brands:
            month_data[m]["our_rev"] += r["revenue"] or 0

    monthly_share = [
        {
            "month": m,
            "market_revenue": round(d["rev"], 2),
            "our_revenue": round(d["our_rev"], 2),
            "our_share_pct": round(safe_div(d["our_rev"], d["rev"]) * 100, 2),
        }
        for m, d in sorted(month_data.items())
    ]

    # Share trend: last month vs previous
    share_delta = None
    if len(monthly_share) >= 2:
        share_delta = round(monthly_share[-1]["our_share_pct"] - monthly_share[-2]["our_share_pct"], 2)

    # ── 5. Competitor review benchmark ────────────────────────────────────────
    comp_review_bench: dict[str, dict] = {}
    for s in all_skus.values():
        b = s["brand"]
        if b not in comp_review_bench:
            comp_review_bench[b] = {"reviews_sum": 0, "skus": 0, "revenue": 0.0, "ratings": []}
        comp_review_bench[b]["reviews_sum"] += s.get("reviews") or 0
        comp_review_bench[b]["skus"] += 1
        comp_review_bench[b]["revenue"] += s.get("_revenue_sum", s.get("revenue") or 0)
        if s.get("rating"):
            comp_review_bench[b]["ratings"].append(s["rating"])

    review_benchmarks = sorted(
        [
            {
                "brand": b,
                "is_ours": b.upper() in our_brands,
                "revenue": round(d["revenue"], 2),
                "skus": d["skus"],
                "avg_reviews": round(safe_div(d["reviews_sum"], d["skus"]), 1),
                "avg_rating": round(
                    safe_div(sum(d["ratings"]), len(d["ratings"])), 2
                ) if d["ratings"] else 0,
            }
            for b, d in comp_review_bench.items()
        ],
        key=lambda x: -x["revenue"]
    )[:15]

    # ── 6. Priority actions (computed) ────────────────────────────────────────
    priority_actions = []

    # Action 1: Reviews for no-review SKUs
    if no_reviews:
        revenue_at_risk = sum(x["revenue"] for x in no_reviews)
        priority_actions.append({
            "priority": 1,
            "category": "reviews",
            "title": f"Получить отзывы на {len(no_reviews)} SKU без отзывов",
            "description": f"Эти SKU алгоритмически невидимы на Kaspi. Суммарная выручка: {revenue_at_risk/1e6:.1f} млн ₸. Запустить кампанию сбора отзывов.",
            "impact": "high",
            "skus_affected": len(no_reviews),
        })

    # Action 2: Biggest segment gap
    big_gaps = [s for s in segment_gaps if s["is_gap"]]
    if big_gaps:
        top_gap = big_gaps[0]
        potential = top_gap["market_revenue"] * 0.10 - top_gap["our_revenue"]
        if potential > 0:
            priority_actions.append({
                "priority": 2,
                "category": "segments",
                "title": f"Войти в сегмент {top_gap['vetka']} — {top_gap['market_revenue']/1e6:.0f} млн рынок",
                "description": f"Текущая доля {top_gap['our_share_pct']:.1f}%. Лидеры: {', '.join(c['brand'] for c in top_gap['top_competitors'][:2])}. 10% доли = +{potential/1e6:.1f} млн ₸.",
                "impact": "high",
                "skus_affected": 0,
            })

    # Action 3: Fix low-rating SKUs
    if low_rating:
        priority_actions.append({
            "priority": 3,
            "category": "rating",
            "title": f"Исправить рейтинг {len(low_rating)} проблемных SKU",
            "description": f"SKU с рейтингом < 4.5 теряют позиции в поиске. Проанализировать отзывы и устранить причины.",
            "impact": "medium",
            "skus_affected": len(low_rating),
        })

    # Action 4: Few-reviews SKUs
    if few_reviews:
        revenue_boost = sum(x["revenue"] for x in few_reviews[:10])
        priority_actions.append({
            "priority": 4,
            "category": "reviews",
            "title": f"Усилить {len(few_reviews)} SKU с 1–4 отзывами",
            "description": f"Минимальный порог доверия на Kaspi — 5+ отзывов. Топ-10 по выручке в этой группе: {revenue_boost/1e6:.1f} млн ₸.",
            "impact": "medium",
            "skus_affected": len(few_reviews),
        })

    return {
        "review_deficit": {
            "no_reviews": no_reviews,
            "few_reviews": few_reviews,
            "no_reviews_count": len(no_reviews),
            "few_reviews_count": len(few_reviews),

            "avg_our_reviews": round(
                safe_div(sum(s.get("reviews") or 0 for s in our_skus.values()), len(our_skus)), 1
            ) if our_skus else 0,
        },
        "low_rating_skus": low_rating,
        "segment_gaps": segment_gaps,
        "sku_efficiency": {
            "our_skus": our_total_skus,
            "our_revenue": round(our_total_rev, 2),
            "our_rev_per_sku": round(our_rev_per_sku, 2),
            "top_competitors": comp_efficiency[:10],
        },
        "monthly_trend": {
            "months": monthly_share,
            "share_delta_vs_prev": share_delta,
        },
        "review_benchmarks": review_benchmarks,
        "priority_actions": priority_actions,
    }


def calc_rrc_analytics(rows: list[dict], our_brands: set[str]) -> dict:
    """
    RRC analytics by vetka (liter range) and subtype (category).

    For each vetka / subtype returns:
      - market_min_rrc, market_avg_rrc, market_max_rrc
      - our_avg_rrc (average across our SKUs present in that segment)
      - position_pct  — how our avg RRC sits vs market avg (positive = above, negative = below)
      - leader_brand, leader_avg_rrc — top competitor by revenue in that segment
      - tactic         — "ниже_рынка" | "в_рынке" | "выше_рынка"
      - tactic_advice  — human-readable recommendation
      - our_items      — list of our SKUs with kod, name, rrc, abc, revenue
    """
    if not rows:
        return {}

    THRESHOLD_LOW = 0.92   # our_avg < market_avg * 0.92 → ниже рынка
    THRESHOLD_HIGH = 1.08  # our_avg > market_avg * 1.08 → выше рынка

    def _tactic(our_avg: float, market_avg: float) -> tuple[str, str]:
        if market_avg <= 0 or our_avg <= 0:
            return "нет_данных", "Недостаточно данных для рекомендации."
        ratio = our_avg / market_avg
        if ratio < THRESHOLD_LOW:
            pct = round((1 - ratio) * 100, 1)
            return "ниже_рынка", (
                f"Наш РРЦ на {pct}% ниже рынка. "
                "Есть пространство для повышения цены без риска потери позиций."
            )
        elif ratio > THRESHOLD_HIGH:
            pct = round((ratio - 1) * 100, 1)
            return "выше_рынка", (
                f"Наш РРЦ на {pct}% выше рынка. "
                "Убедитесь, что премиум оправдан рейтингом и отзывами."
            )
        else:
            diff = round((ratio - 1) * 100, 1)
            sign = "+" if diff >= 0 else ""
            return "в_рынке", (
                f"РРЦ в рынке ({sign}{diff}%). "
                "Держать позицию, следить за движением лидера."
            )

    # ─── collect data by vetka ────────────────────────────────────────────────
    v_market_rrc: dict[str, list[float]] = defaultdict(list)   # all non-zero rrc
    v_our_rrc: dict[str, list[float]] = defaultdict(list)
    v_revenue: dict[str, float] = defaultdict(float)
    v_our_revenue: dict[str, float] = defaultdict(float)
    v_comp_rev: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    v_comp_rrc: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    v_our_items: dict[str, dict] = {}   # vetka → sku_key → item

    # ─── collect data by subtype ──────────────────────────────────────────────
    t_market_rrc: dict[str, list[float]] = defaultdict(list)
    t_our_rrc: dict[str, list[float]] = defaultdict(list)
    t_revenue: dict[str, float] = defaultdict(float)
    t_our_revenue: dict[str, float] = defaultdict(float)
    t_comp_rev: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    t_comp_rrc: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

    seen_sku_vetka: set[tuple] = set()   # (sku_key, vetka) — deduplicate our items

    for r in rows:
        v = r["vetka"] or "—"
        t = r["tip"] or "—"
        rrc = r["rrc"] or 0
        rev = r["revenue"] or 0
        is_ours = r["brand"].upper() in our_brands

        # market RRC (all brands, deduplicated per SKU per vetka)
        sk = sku_key(r)
        pair = (sk, v)
        if rrc > 0 and pair not in seen_sku_vetka:
            v_market_rrc[v].append(rrc)
        if rrc > 0:
            t_market_rrc[t].append(rrc)   # subtypes: not deduped (aggregate view)

        v_revenue[v] += rev
        t_revenue[t] += rev

        if is_ours:
            if rrc > 0:
                v_our_rrc[v].append(rrc)
                t_our_rrc[t].append(rrc)
            v_our_revenue[v] += rev
            t_our_revenue[t] += rev

            # collect our SKU details (one entry per unique SKU per vetka)
            if pair not in seen_sku_vetka and rrc > 0:
                key = f"{sk}|{v}"
                if key not in v_our_items:
                    v_our_items[key] = {
                        "vetka": v,
                        "kod": r["kod"] or "",
                        "name": r["name"] or r["kod"] or "—",
                        "brand": r["brand"],
                        "rrc": rrc,
                        "abc": r["abc"] or "—",
                        "revenue": rev,
                    }
        else:
            b = r["brand"]
            v_comp_rev[v][b] += rev
            t_comp_rev[t][b] += rev
            if rrc > 0:
                v_comp_rrc[v][b].append(rrc)
                t_comp_rrc[t][b].append(rrc)

        if rrc > 0:
            seen_sku_vetka.add(pair)

    # ─── build by_vetka result ────────────────────────────────────────────────
    by_vetka = []
    for v in sorted(v_revenue, key=lambda x: -v_revenue[x]):
        mrrcs = v_market_rrc[v]
        orrcs = v_our_rrc[v]
        mkt_avg = safe_div(sum(mrrcs), len(mrrcs))
        our_avg = safe_div(sum(orrcs), len(orrcs))
        pos_pct = round(safe_div(our_avg - mkt_avg, mkt_avg) * 100, 1) if mkt_avg > 0 else None

        # leader competitor
        comp_brands_v = v_comp_rev[v]
        leader_b = max(comp_brands_v, key=lambda b: comp_brands_v[b]) if comp_brands_v else None
        leader_avg_rrc = (
            round(safe_div(sum(v_comp_rrc[v][leader_b]), len(v_comp_rrc[v][leader_b])), 0)
            if leader_b and v_comp_rrc[v][leader_b] else None
        )

        tac, advice = _tactic(our_avg, mkt_avg)

        our_items_v = sorted(
            [item for k, item in v_our_items.items() if item["vetka"] == v],
            key=lambda x: -(x["revenue"] or 0),
        )

        by_vetka.append({
            "vetka": v,
            "market_revenue": round(v_revenue[v], 2),
            "our_revenue": round(v_our_revenue[v], 2),
            "our_share_pct": round(safe_div(v_our_revenue[v], v_revenue[v]) * 100, 1),
            "market_min_rrc": round(min(mrrcs), 0) if mrrcs else None,
            "market_avg_rrc": round(mkt_avg, 0) if mkt_avg else None,
            "market_max_rrc": round(max(mrrcs), 0) if mrrcs else None,
            "our_avg_rrc": round(our_avg, 0) if our_avg else None,
            "position_pct": pos_pct,
            "leader_brand": leader_b,
            "leader_avg_rrc": int(leader_avg_rrc) if leader_avg_rrc is not None else None,
            "tactic": tac,
            "tactic_advice": advice,
            "our_items": our_items_v,
        })

    # ─── build by_subtype result ──────────────────────────────────────────────
    by_subtype = []
    for t in sorted(t_revenue, key=lambda x: -t_revenue[x]):
        mrrcs = t_market_rrc[t]
        orrcs = t_our_rrc[t]
        mkt_avg = safe_div(sum(mrrcs), len(mrrcs))
        our_avg = safe_div(sum(orrcs), len(orrcs))
        pos_pct = round(safe_div(our_avg - mkt_avg, mkt_avg) * 100, 1) if mkt_avg > 0 else None

        comp_brands_t = t_comp_rev[t]
        leader_b = max(comp_brands_t, key=lambda b: comp_brands_t[b]) if comp_brands_t else None
        leader_avg_rrc = (
            round(safe_div(sum(t_comp_rrc[t][leader_b]), len(t_comp_rrc[t][leader_b])), 0)
            if leader_b and t_comp_rrc[t][leader_b] else None
        )

        tac, advice = _tactic(our_avg, mkt_avg)

        by_subtype.append({
            "subtype": t,
            "market_revenue": round(t_revenue[t], 2),
            "our_revenue": round(t_our_revenue[t], 2),
            "our_share_pct": round(safe_div(t_our_revenue[t], t_revenue[t]) * 100, 1),
            "market_min_rrc": round(min(mrrcs), 0) if mrrcs else None,
            "market_avg_rrc": round(mkt_avg, 0) if mkt_avg else None,
            "market_max_rrc": round(max(mrrcs), 0) if mrrcs else None,
            "our_avg_rrc": round(our_avg, 0) if our_avg else None,
            "position_pct": pos_pct,
            "leader_brand": leader_b,
            "leader_avg_rrc": int(leader_avg_rrc) if leader_avg_rrc is not None else None,
            "tactic": tac,
            "tactic_advice": advice,
        })

    # ─── summary ──────────────────────────────────────────────────────────────
    below = sum(1 for x in by_vetka if x["tactic"] == "ниже_рынка")
    above = sum(1 for x in by_vetka if x["tactic"] == "выше_рынка")
    in_market = sum(1 for x in by_vetka if x["tactic"] == "в_рынке")

    return {
        "by_vetka": by_vetka,
        "by_subtype": by_subtype,
        "summary": {
            "total_vetkas": len(by_vetka),
            "below_market": below,
            "in_market": in_market,
            "above_market": above,
        },
    }


# ── IA redesign (audit, июль 2026): "diagnostic funnel" support ───────────────
# Level 1 "Пульс отдела" needs a ranked answer to "why did OUR revenue change",
# not just the total delta (that already exists as calc_monthly's mom_our_pct).
# This walks the same latest-vs-previous-month comparison down to the
# segment/brand/product level so the biggest contributors are visible without
# manually cross-referencing Ветки and Бренды by hand.

def calc_whats_changed(rows: list[dict], our_brands: set[str], top_n: int = 6) -> dict:
    """
    Rank the segments, brands and products that contributed most to the
    change in OUR revenue between the latest month present in `rows` and the
    one immediately before it (chronologically, not just "last uploaded").

    Returns {"month": latest, "prev_month": prev, "factors": [...]} where each
    factor is {type: 'сегмент'|'бренд'|'товар', label, current, previous,
    delta, delta_pct, vetka, brand, kod, name} — sorted by |delta| desc.
    If fewer than 2 months of data exist, returns {"factors": []} (nothing to
    compare yet — this is expected for a department's first month of data).
    """
    months = sorted({r["month"] for r in rows if r["month"]}, key=_month_sort_key)
    if len(months) < 2:
        return {"month": months[-1] if months else None, "prev_month": None, "factors": []}

    latest, prev = months[-1], months[-2]

    def _grouped_delta(key_fn) -> dict[str, dict]:
        """group→{'current':x,'previous':y} of OUR revenue for the two months."""
        acc: dict[str, dict] = defaultdict(lambda: {"current": 0.0, "previous": 0.0})
        for r in rows:
            if (r["brand"] or "").upper() not in our_brands:
                continue
            m = r["month"]
            if m not in (latest, prev):
                continue
            k = key_fn(r)
            if k is None:
                continue
            acc[k]["current" if m == latest else "previous"] += r["revenue"] or 0
        return acc

    def _to_factors(acc: dict[str, dict], ftype: str, extra_fn) -> list[dict]:
        out = []
        for k, d in acc.items():
            delta = d["current"] - d["previous"]
            if abs(delta) < 1:
                continue
            out.append({
                "type": ftype,
                "label": k,
                "current": round(d["current"], 2),
                "previous": round(d["previous"], 2),
                "delta": round(delta, 2),
                "delta_pct": round(safe_div(delta, d["previous"]) * 100, 1) if d["previous"] else None,
                **extra_fn(k),
            })
        return out

    seg_acc = _grouped_delta(lambda r: r["vetka"] or None)
    brand_acc = _grouped_delta(lambda r: r["brand"] or None)

    # Products need one representative row (for kod/name/vetka/brand) alongside
    # the aggregated delta — track the highest-revenue row seen per sku_key.
    sku_acc: dict[str, dict] = defaultdict(lambda: {"current": 0.0, "previous": 0.0, "rep": None})
    for r in rows:
        if (r["brand"] or "").upper() not in our_brands:
            continue
        m = r["month"]
        if m not in (latest, prev):
            continue
        k = sku_key(r)
        d = sku_acc[k]
        d["current" if m == latest else "previous"] += r["revenue"] or 0
        if d["rep"] is None or (r["revenue"] or 0) > (d["rep"]["revenue"] or 0):
            d["rep"] = r

    factors = []
    factors += _to_factors(seg_acc, "сегмент", lambda k: {"vetka": k, "brand": None, "kod": None, "name": None})
    factors += _to_factors(brand_acc, "бренд", lambda k: {"vetka": None, "brand": k, "kod": None, "name": None})
    for k, d in sku_acc.items():
        delta = d["current"] - d["previous"]
        if abs(delta) < 1:
            continue
        rep = d["rep"] or {}
        factors.append({
            "type": "товар",
            "label": rep.get("name") or rep.get("brand") or k,
            "current": round(d["current"], 2),
            "previous": round(d["previous"], 2),
            "delta": round(delta, 2),
            "delta_pct": round(safe_div(delta, d["previous"]) * 100, 1) if d["previous"] else None,
            "vetka": rep.get("vetka"), "brand": rep.get("brand"),
            "kod": rep.get("kod"), "name": rep.get("name"),
        })

    factors.sort(key=lambda f: -abs(f["delta"]))
    return {"month": latest, "prev_month": prev, "factors": factors[:top_n]}


def calc_sku_history(rows: list[dict], kod: str = "", name: str = "") -> dict:
    """
    Month-by-month history for ONE product, identified by kod (preferred) or
    exact name. Used by the product drill-down modal so a single SKU can be
    inspected further instead of dead-ending at a flat list (IA audit, июль
    2026 — Часть IV, Уровень 3 "Товар").

    Returns {"kod","name","brand","months":[{month,revenue,units,rrc,
    rating,reviews}...]} sorted chronologically, or {"months":[]} if the
    product can't be found in `rows`.
    """
    kod = (kod or "").strip()
    name = (name or "").strip()
    matches = [
        r for r in rows
        if (kod and (r.get("kod") or "").strip() == kod)
        or (not kod and name and (r.get("name") or "").strip().upper() == name.upper())
    ]
    if not matches:
        return {"kod": kod, "name": name, "brand": None, "months": []}

    by_month: dict[str, dict] = defaultdict(lambda: {"revenue": 0.0, "units": 0.0, "rrc": [], "rating": [], "reviews": 0.0})
    rep = matches[0]
    for r in matches:
        m = r["month"] or "—"
        d = by_month[m]
        d["revenue"] += r["revenue"] or 0
        d["units"] += r["units"] or 0
        if r.get("rrc"):
            d["rrc"].append(r["rrc"])
        if r.get("rating"):
            d["rating"].append(r["rating"])
        d["reviews"] = max(d["reviews"], r["reviews"] or 0)
        if (r["revenue"] or 0) > (rep["revenue"] or 0):
            rep = r

    months_sorted = sorted(by_month, key=_month_sort_key)
    series = []
    for m in months_sorted:
        d = by_month[m]
        series.append({
            "month": m,
            "revenue": round(d["revenue"], 2),
            "units": round(d["units"]),
            "rrc": round(sum(d["rrc"]) / len(d["rrc"]), 0) if d["rrc"] else None,
            "rating": round(sum(d["rating"]) / len(d["rating"]), 2) if d["rating"] else None,
            "reviews": round(d["reviews"]),
        })

    return {
        "kod": rep.get("kod") or kod,
        "name": rep.get("name") or name,
        "brand": rep.get("brand"),
        "vetka": rep.get("vetka"),
        "months": series,
    }


# ── Закуп (05.08.2026) ────────────────────────────────────────────────────────
# Перенос логики ручного директорского плана закупа (Plan_Zakupa_Prioritety) на
# сайт — только механическая часть (покрытие складом, тир T1-T4). Директорские
# поправки, которые в ручном файле требовали живой проверки в CRM конкретных
# SKU (скрытый сток ремонт/уценка/возврат/витрина) или ручного суждения —
# сюда НЕ перенесены, это осталось бы непроверенным автоматическим решением.
# Вместо жёстко зашитых категорий (Гастрономы/Фризеры для мороженого по имени)
# — два ОБЩИХ флага ниже, которые сработают на любую похожую категорию сами,
# без необходимости заново находить и чинить конкретный кейс каждый раз:
#   • made_to_order_group  — у категории (Тип) почти нет физического стока
#     ни у одного SKU → похоже на "под заказ", coverage-логика неприменима.
#   • dead_signal          — последний загруженный месяц = 0 продаж на Kaspi,
#     хотя раньше SKU продавался. Это МЕСЯЧНОЕ разрешение (сайт хранит только
#     помесячные продажи, не понедельные, как в ручном разборе фризеров) —
#     грубее, но честно помечено, не выдаётся за то же самое.
# ВАЖНО: сайт видит продажи только по Kaspi (загружаемые Excel — это Kaspi-
# экспорт). В отличие от ручного плана здесь НЕТ данных о продажах на других
# каналах — T3a ("сток есть, но не продаётся на Kaspi") здесь не может
# подтвердить/опровергнуть спрос на других каналах, только показать сам факт.

TARGET_MONTHS_COVER = 2.0          # тот же буфер, что и в ручном плане закупа
_TRAILING_MONTHS = 3               # окно для средней скорости продаж (mvel)
_MADE_TO_ORDER_MIN_GROUP = 3       # минимум SKU в группе, чтобы флаг не был шумом
_MADE_TO_ORDER_ZERO_STOCK_SHARE = 0.8  # доля SKU группы с нулевым физ. стоком


def _procurement_classify(mvel_kaspi: float, kaspi_stock: float, ymc_transit: float, ordered: float):
    has_pipe = (ordered or 0) > 0 or (ymc_transit or 0) > 0
    selling = mvel_kaspi >= 1.0
    cover = safe_div(kaspi_stock, mvel_kaspi, None) if mvel_kaspi > 0 else None
    cover_full = safe_div(kaspi_stock + ymc_transit + ordered, mvel_kaspi, None) if mvel_kaspi > 0 else None
    if selling and cover is not None and cover < TARGET_MONTHS_COVER:
        tier = "T1_CRITICAL" if not has_pipe else "T2_PIPELINE"
        if has_pipe and cover_full is not None and cover_full < TARGET_MONTHS_COVER:
            tier = "T1_CRITICAL"
    elif selling:
        tier = "T4_OK"
    else:
        tier = "T3A_LISTING" if kaspi_stock > 0 else ("T2_PIPELINE" if has_pipe else "T3B_LOWPRI")
    return tier, cover, cover_full


def calc_procurement(rows: list[dict], stock_rows: list[dict]) -> dict:
    """
    rows: ВСЕ месяцы продаж этого отдела (после apply_business_rules) — нужна
          история за несколько месяцев, а не один выбранный месяц.
    stock_rows: [{sku,name,status,price,wh_pervomay,wh_astana,wh_shymkent,
          wh_tuzdybastau,ymc_transit,ordered}, ...] из StockRow (не привязаны
          к отделу — сопоставление по SKU происходит здесь).
    """
    if not rows:
        return {"items": [], "made_to_order_groups": [], "no_stock_data": [],
                "months_used": [], "note": "Нет загруженных продаж для этого отдела."}

    months_avail = sorted({r["month"] for r in rows if r.get("month")}, key=_month_sort_key)
    months_used = months_avail[-_TRAILING_MONTHS:]
    latest_month = months_used[-1] if months_used else None
    n_months = len(months_used) or 1

    stock_by_sku: dict[str, dict] = {}
    for s in stock_rows:
        k = str(s.get("sku") or "").strip().upper()
        if k:
            stock_by_sku[k] = s

    # units per SKU per month (только окно months_used) + метаданные (name/tip)
    per_sku: dict[str, dict] = {}
    for r in rows:
        k = sku_key(r)
        if k == "__UNKNOWN__":
            continue
        d = per_sku.setdefault(k, {"months": defaultdict(float), "name": r.get("name"), "tip": r.get("tip"),
                                    "kod": r.get("kod") or k, "_last_rev": -1})
        if (r.get("revenue") or 0) >= d["_last_rev"]:
            d["name"] = r.get("name") or d["name"]
            d["tip"] = r.get("tip") or d["tip"]
            d["_last_rev"] = r.get("revenue") or 0
        if r.get("month") in months_used:
            d["months"][r["month"]] += r.get("units") or 0

    items = []
    no_stock_data = []
    tip_groups: dict[str, list] = defaultdict(list)

    for k, d in per_sku.items():
        total_units = sum(d["months"].values())
        mvel_kaspi = total_units / n_months
        latest_units = d["months"].get(latest_month, 0) if latest_month else 0
        earlier_units = total_units - latest_units

        st = stock_by_sku.get(k)
        if st is None:
            no_stock_data.append({"kod": d["kod"], "name": d["name"], "tip": d["tip"],
                                   "mvel_kaspi": round(mvel_kaspi, 2)})
            continue

        kaspi_stock = (st.get("wh_pervomay") or 0) + (st.get("wh_astana") or 0) + (st.get("wh_shymkent") or 0)
        ymc_transit = st.get("ymc_transit") or 0
        ordered = st.get("ordered") or 0

        tier, cover, cover_full = _procurement_classify(mvel_kaspi, kaspi_stock, ymc_transit, ordered)
        need = TARGET_MONTHS_COVER * mvel_kaspi - kaspi_stock - ymc_transit - ordered
        suggest_qty = max(0, math.ceil(need)) if tier in ("T1_CRITICAL", "T2_PIPELINE") else 0

        # Месячное (не понедельное, как в ручном разборе) приближение "сигнал
        # пропал": в последнем загруженном месяце — 0 продаж, хотя раньше были.
        dead_signal = bool(latest_month) and latest_units == 0 and earlier_units > 0 and len(months_used) >= 2

        item = {
            "kod": d["kod"], "name": d["name"], "tip": d["tip"],
            "status": st.get("status"),
            "mvel_kaspi": round(mvel_kaspi, 2),
            "kaspi_stock": kaspi_stock,
            "wh_pervomay": st.get("wh_pervomay") or 0, "wh_astana": st.get("wh_astana") or 0,
            "wh_shymkent": st.get("wh_shymkent") or 0, "wh_tuzdybastau": st.get("wh_tuzdybastau") or 0,
            "ymc_transit": ymc_transit, "ordered": ordered,
            "cover_months": round(cover, 1) if cover is not None else None,
            "tier": tier, "suggest_qty": suggest_qty,
            "dead_signal": dead_signal,
        }
        items.append(item)
        if d["tip"]:
            tip_groups[d["tip"]].append(item)

    # made_to_order_group: категория (Тип), где почти весь физ. сток = 0 у
    # достаточно большой группы SKU — похоже на "под заказ", не на обычный склад.
    made_to_order_groups = []
    for tip, group in tip_groups.items():
        if len(group) < _MADE_TO_ORDER_MIN_GROUP:
            continue
        zero_stock = sum(1 for it in group if it["kaspi_stock"] <= 0)
        share = zero_stock / len(group)
        if share >= _MADE_TO_ORDER_ZERO_STOCK_SHARE:
            made_to_order_groups.append({
                "tip": tip, "sku_count": len(group), "zero_stock_count": zero_stock,
                "zero_stock_share": round(share, 2),
            })
            for it in group:
                it["made_to_order_group"] = True

    TIER_ORDER = {"T1_CRITICAL": 0, "T2_PIPELINE": 1, "T3A_LISTING": 2, "T3B_LOWPRI": 3, "T4_OK": 4}
    items.sort(key=lambda it: (TIER_ORDER.get(it["tier"], 9), -it["suggest_qty"], -it["mvel_kaspi"]))

    return {
        "items": items,
        "made_to_order_groups": made_to_order_groups,
        "no_stock_data": sorted(no_stock_data, key=lambda x: -x["mvel_kaspi"])[:50],
        "months_used": months_used,
        "counts": {t: sum(1 for it in items if it["tier"] == t) for t in TIER_ORDER},
    }


# ── Закуп v2 (05.08.2026) ────────────────────────────────────────────────────
# Дизайн-документ: Zakup_V2_Design_2026-08-05.md (корень репозитория) — 14
# разделов, включая 3 раунда самокритики с проверкой на реальных данных
# (119 SKU из ручного плана / 42039 транзакций из выгрузки CRM). Ниже —
# реализация того, что там согласовано. Ключевые решения (см. документ за
# цифрами и обоснованием):
#
#   1. Розница vs опт — НЕ усредняем в одно число. Опт (Дилеры/Супер-дилеры
#      Байсат/Айдын Опт/Корпоративные) даёт 46.5% объёма всего каталога, но
#      лумпи (разовые партии по 20-200 шт — напр. SKU 9309484: 87% объёма
#      через один канал "Айдын Опт"). Усреднение по 3 мес маскирует чужой
#      закупочный цикл под "стабильный спрос". Розничный сигнал ведёт
#      буфер/тир/сезонность; оптовый — отдельный контекст-паттерн.
#   2. Kaspi-канал ЭТОГО файла vs mvel_kaspi сайта (KaspiRow, отдельная
#      загрузка Kaspi-матриц) — сверка, не замена: расхождение >20% -> флаг
#      на карточке, а не молчаливый выбор одного источника (порог не
#      откалиброван на повторных загрузках — открытый пробел, см. п.14).
#   3. Тир считается по РОЗНИЧНОМУ mvel, не по Kaspi-only и не по мешанине
#      с опт — чинит канальную слепоту (см. документ п.12: 76/119 SKU
#      меняли тир при переходе с Kaspi-only на общий спрос; весь бакет
#      T3B_LOWPRI из 17 SKU был целиком построен на ложном сигнале).
#   4. Охват — composite score (выручка категории + гарантия видимости при
#      живом T1_CRITICAL внутри категории), не чистый ранг по выручке (см.
#      документ п.11 — чистая выручка прятала бы категории "Рисоварки
#      профессиональные" и "Слайсеры", где как раз известные T1 SKU).
#   5. Буфер — TARGET_COVER_DAYS = лид-тайм (45 дней, подтверждено
#      пользователем 05.08.2026: 30 произв. + 15 логистика до Хаб Первомай)
#      + периодичность проверки (допущение 15 дней, не измерено) = 60 дней
#      ≈ 2 мес — то же число, что и в v1 TARGET_MONTHS_COVER, но теперь
#      обосновано, а не произвольно.
#   6. Сезонность — по категории CRM, ТОЛЬКО на розничных транзакциях
#      (иначе те же лумпи-оптовые всплески читаются как "сезонность рынка"
#      — см. документ п.13).
#
# Открытые пробелы, которые НЕ закрыты этим кодом (нужен внешний вход, не
# ещё один проход по тем же файлам — см. документ п.14): MOQ по SKU/
# поставщику, себестоимость/маржа, точная дата размещения "2_ordered",
# плечо Первомай→Астана/Шымкент/Туздыбастау, статус канала "Айдын Опт"
# (независимый дилер или связанная структура), калибровка порога
# расхождения Kaspi vs CRM. Помечены как известные ограничения в API-ответе
# и должны быть видны в UI, а не скрыты.

RETAIL_CHANNELS = {
    "розничные продажи", "каспи", "каспи - магазин",
    "tiktok продажи", "tiktok продажи шымкент", "tiktok продажи астана",
    "инстаграм", "инстаграм шымкент", "инстаграм астана",
    "магазин шымкент",
}
WHOLESALE_CHANNELS = {
    "дилеры", "супер-дилеры байсат", "айдын опт", "корпоративные продажи",
    "мастер продаж",
}


def _channel_bucket(channel) -> str:
    c = (channel or "").strip().lower()
    if c in RETAIL_CHANNELS:
        return "retail"
    if c in WHOLESALE_CHANNELS:
        return "wholesale"
    return "other"  # неопознанный канал — не в основном сигнале, но не теряется молча


LEAD_TIME_DAYS = 45          # 30 произв. + 15 логистика (Первомай) — подтверждено пользователем 05.08.2026
REVIEW_CADENCE_DAYS = 15     # допущение о периодичности проверки вкладки — НЕ измерено, см. документ п.14
TARGET_COVER_DAYS = LEAD_TIME_DAYS + REVIEW_CADENCE_DAYS   # 60 дней ≈ 2.0 мес
NEAR_PIPELINE_ETA_DAYS = 15  # left_factory + китайские хаб-коды — осталась только логистика
FAR_PIPELINE_ETA_DAYS = 45   # "ordered" — верхняя граница, дата заказа неизвестна (открытый пробел)

KASPI_DIVERGENCE_THRESHOLD = 0.20   # прикидка, не откалибровано на повторных загрузках — см. документ п.14
CATEGORY_MATERIALITY_CUM_PCT = 0.90  # топ-категории, дающие 90% суммарной выручки → "Расширенный охват"


def calc_category_seasonality(channel_rows: list[dict]) -> dict[str, dict[int, float]]:
    """
    Сезонный индекс по категории CRM, ТОЛЬКО по розничным транзакциям (см.
    обоснование в шапке файла — опт искажает сезонность своим циклом
    закупок, не сезоном конечного спроса). Индекс месяца = средний объём
    этого календарного месяца / средний объём по наблюдаемым месяцам —
    1.0 = типичный месяц, <1 = просадка, >1 = пик.

    Фикс 11.08 (Ф1 Закуп v3) — три ошибки старой версии, которые выстрелили
    бы ровно в момент загрузки полного (~13 мес) файла:
      1) деление на жёсткие 12: при файле в 6-11 месяцев отсутствующие
         месяцы читались как «спрос = 0», занижали базу, а сами получали
         индекс 0.0 и попадали под порог сезонного подавления (<0.4) —
         ложные «не покупай» по живым категориям;
      2) повтор календарного месяца (13-месячный файл содержит один месяц
         дважды — прошлогодний и свежий) суммировался без нормализации и
         получал двойной вес в кривой;
      3) месяц, которого в покрытии файла нет вообще, был неотличим от
         месяца с реальным нулём продаж. Теперь такой месяц ОТСУТСТВУЕТ в
         результате: downstream обязан различать None («не знаем») и 0.0
         («знаем, что ноль») — и не подавлять закуп на «не знаем».

    Наблюдаемость месяца определяется покрытием ФАЙЛА (есть хоть какие-то
    розничные строки за (год, месяц)), а не продажами категории: месяц в
    покрытии файла без продаж категории — настоящий ноль спроса и остаётся
    нулём. Категория, появившаяся в ассортименте позже начала файла,
    получит заниженные индексы на «дожизненные» месяцы — известное
    упрощение: гейт ≥6 месяцев с продажами ограничивает искажение, а
    альтернатива (угадывать дату запуска категории) — гадание, которое
    протокол запрещает.

    Требует данные минимум за 6 разных (год, месяц) с продажами на
    категорию, иначе индекс не считается — недостаточно истории, не
    натягиваем кривую на шум (см. документ п.5).
    """
    retail = [r for r in channel_rows if _channel_bucket(r.get("channel")) == "retail"]

    # Покрытие файла: сколько раз каждый календарный месяц встречается
    # среди наблюдаемых (год, месяц) — нормализатор против двойного веса.
    file_ym: set = set()
    for r in retail:
        d = r.get("sale_date")
        if d:
            file_ym.add((d.year, d.month))
    month_occurrences: dict[int, int] = defaultdict(int)
    for (_y, m) in file_ym:
        month_occurrences[m] += 1

    by_cat_month: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    cat_months_seen: dict[str, set] = defaultdict(set)
    for r in retail:
        d = r.get("sale_date")
        if not d:
            continue
        cat = r.get("category") or ""
        by_cat_month[cat][d.month] += r.get("qty") or 0
        cat_months_seen[cat].add((d.year, d.month))

    result: dict[str, dict[int, float]] = {}
    for cat, by_month in by_cat_month.items():
        if len(cat_months_seen[cat]) < 6:
            continue
        # Средний объём календарного месяца, нормированный на число его
        # появлений в файле; месяцы вне покрытия файла не участвуют вовсе.
        month_avg = {m: by_month.get(m, 0.0) / occ
                     for m, occ in month_occurrences.items() if occ > 0}
        if not month_avg:
            continue
        avg = sum(month_avg.values()) / len(month_avg)
        if avg <= 0:
            continue
        result[cat] = {m: round(v / avg, 2) for m, v in month_avg.items()}
    return result


# ── Ф3 (11.08, Закуп v3): параметры сезонной модели спроса ────────────────
# Довес к закупу перед пиком сезона ограничен капом (решение Дамира 11.08:
# «да, с капом +50%»); срез вниз при мягком спаде — не глубже половины
# базового объёма (полное обнуление — только через T2S_SEASON_OFF).
SEASON_FACTOR_CAP_HIGH = 1.5
SEASON_FACTOR_CAP_LOW = 0.5
# Защита деления при десезонализации уровня: индекс месяца, на который
# делим, зажимается в эти пределы, чтобы один шумный месяц с индексом 0.1
# не раздул «уровень» спроса в 10 раз.
_DESEASON_CLAMP_LOW = 0.5
_DESEASON_CLAMP_HIGH = 2.0

# ── Справочная сезонность (12.08) ──────────────────────────────────────────
# Мёртвые месяцы по категориям, известные БИЗНЕСУ, а не выведенные из
# текущего файла. Нужна ровно потому, что файловая сезонность требует ≥6
# месяцев истории: пока файл узкий, у категорий нет НИКАКОЙ сезонной
# защиты, и мороженщики честно шли в T1 в августе (кейс Дамира 12.08:
# «закупил бы мороженщики — в корне неверно»; заказ августа приезжает в
# октябре, к мёртвому сезону). Источник цифр: полногодовая проверка
# сезонности фризеров 05.08 (задача #112) + прямое указание Дамира.
# Используется ТОЛЬКО для подавления (T2S) и ТОЛЬКО когда файловой
# сезонности по категории нет — файл, когда появится, главнее. Довес по
# справочнику не делается никогда (для довеса нужны величины, а не
# календарь — не гадаем).
SEASONAL_FALLBACK_OFF_MONTHS: dict[str, set] = {
    "Фризеры для мороженого": {9, 10, 11, 12, 1, 2},
}

# ── Порядок тиров и ценность позиции (12.08) ──────────────────────────────
PROC_TIER_ORDER = {"T1_CRITICAL": 0, "T2_PIPELINE": 1, "T2S_SEASON_OFF": 2,
                   "T2M_MADE_TO_ORDER": 3, "T2X_STOP": 4, "T3A_LISTING": 5,
                   "T3B_LOWPRI": 6, "T4_OK": 7}


def procurement_sort_value(it: dict) -> float:
    """
    Ценность позиции для сортировки = ЗАЩИЩАЕМЫЙ БИЗНЕС, ₸/мес
    (цена × эффективная скорость продаж), усиленный ростом рынка ветки.

    Редизайн 12.08, вопрос Дамира «по какому принципу рисоварка стоит
    вверху, если приносит копейки»: прежний критерий revenue_potential =
    цена × Купить мерил размер ПОКУПКИ, а не размер бизнеса. Морозильник
    ядрового отдела (бизнес 611 тыс ₸/мес), которому нужна 1 штука
    доливки, проигрывал рисоварке за 27.8 тыс (бизнес 334 тыс/мес),
    которой нужно 14 штук. Теперь решает, какой месячный оборот позиция
    защищает — сколько штук докупаем, вопрос логистики, а не приоритета.
    Когда появится себестоимость — заменить выручку на маржу.
    """
    base = it.get("monthly_value") or 0
    m = it.get("market")
    if base > 0 and m and (m.get("vetka_trend_pct") or 0) > 0:
        return base * (1 + min(m["vetka_trend_pct"], 50) / 100.0)
    return base


def _procurement_classify_v2(mvel_retail, kaspi_stock, near_pipeline, far_pipeline,
                              recent_ok: bool = True):
    """
    Как _procurement_classify (v1), но: (а) вход — розничная скорость
    продаж, не смешанная с опт; (б) буфер — TARGET_COVER_DAYS (лид-тайм +
    периодичность проверки), не голые 2 месяца; (в) пайплайн учитывается по
    стадиям — считается "покрывающим" только если долетит в пределах
    буфера. При текущих константах (буфер 60д >= обе ETA-стадии) это всегда
    true, то есть поведение сегодня совпадает с плоским суммированием —
    стадийность оставлена ради прозрачности расчёта и на случай если буфер
    когда-то станет короче лид-тайма (см. документ п.14, честно об этом).
    """
    mvel_daily = (mvel_retail or 0) / 30.0
    has_pipe = (near_pipeline or 0) > 0 or (far_pipeline or 0) > 0
    # 12.08 (кейс CF-E4F): «продаётся» = скорость есть И спрос подтверждён
    # свежими продажами (recent_ok = были продажи в последнем завершённом
    # или текущем месяце). Скорость без свежести — затухший товар: ему
    # дорога в T3A/T3B (наблюдение), а не в T1 («критично купить»).
    selling = (mvel_retail or 0) >= 1.0 and recent_ok

    days_stock = safe_div(kaspi_stock, mvel_daily, None) if mvel_daily > 0 else None

    covered_pipeline = 0
    if near_pipeline and NEAR_PIPELINE_ETA_DAYS <= TARGET_COVER_DAYS:
        covered_pipeline += near_pipeline
    if far_pipeline and FAR_PIPELINE_ETA_DAYS <= TARGET_COVER_DAYS:
        covered_pipeline += far_pipeline
    days_stock_full = safe_div(kaspi_stock + covered_pipeline, mvel_daily, None) if mvel_daily > 0 else None

    if selling and days_stock is not None and days_stock < TARGET_COVER_DAYS:
        tier = "T1_CRITICAL" if not has_pipe else "T2_PIPELINE"
        if has_pipe and days_stock_full is not None and days_stock_full < TARGET_COVER_DAYS:
            tier = "T1_CRITICAL"
    elif selling:
        tier = "T4_OK"
    else:
        tier = "T3A_LISTING" if kaspi_stock > 0 else ("T2_PIPELINE" if has_pipe else "T3B_LOWPRI")

    cover_months = round(days_stock / 30, 1) if days_stock is not None else None
    cover_months_full = round(days_stock_full / 30, 1) if days_stock_full is not None else None
    # covered_pipeline возвращается наружу (Ф1, 11.08), чтобы формула need в
    # calc_procurement_v2 вычитала РОВНО тот пайплайн, который тир считает
    # «покрывающим» — раньше need вычитал весь пайплайн безусловно, и при
    # изменении констант ETA/буфера формула разъехалась бы с тиром молча.
    return tier, cover_months, cover_months_full, covered_pipeline


# 08.08 — 8 SKU в T1/T2 с пустым "Название" в файле экспорта (не баг парсера
# — проверено, поле реально пустое в CRM, сама CRM помечает их внутренним
# тегом "NONAME"). Найдено и решено директорским аудитом: зашёл в CRM
# (Склад → Товары, поиск по артикулу) и вручную посмотрел карточку каждого
# — у всех 8 есть настоящее описание модели, просто не попадающее в
# "Название" при выгрузке. Это заплатка на ТЕКУЩИЙ снимок данных (08.08),
# не постоянное решение — при новой выгрузке с другими пустыми SKU нужно
# повторить руками; корневая причина (пустая колонка "Название" в CRM для
# этих карточек) лежит на стороне CRM, не чинится кодом дашборда.
MANUAL_NAME_OVERRIDES = {
    "8300376": "Холодильный шкаф (LSC-2d), серый",
    "8300382": "Холодильная витрина (LSC-3d), белый",
    "8300389": "Морозильный шкаф (LSC-2d), белый",
    "8300410": "Холодильный шкаф (LSC-2d), белый",
    "8300431": "Холодильная витрина (LSC-2d), белый",
    "8300381": "Бонета, серый",
    "8300372": "Комбинированная тумба 150х60, серый",
    "8300394": "Комбинированная тумба 150х60, серый",
}


def _kaspi_month_key(mstr) -> Optional[tuple]:
    """«Июль 2026» → (2026, 7); «Июль» → (0, 7); мусор → None. Год-less
    месяцы сортируются по номеру — на стыке годов (Дек→Янв) сравнение
    станет неверным, к этому моменту в выгрузках должен появиться год."""
    parts = str(mstr or "").strip().split()
    if not parts:
        return None
    w = parts[0].lower()
    idx = _MONTH_IDX.get(w, _MONTH_IDX.get(w[:3]))
    if idx is None:
        return None
    year = 0
    if len(parts) > 1 and parts[1].isdigit():
        year = int(parts[1])
    return (year, idx)


def calc_kaspi_lost_listings(rows: list[dict], stock_by_sku: dict,
                              crm_catalog: dict, our_brands: set,
                              bridge: Optional[dict] = None) -> tuple[list, dict]:
    """
    Ф2 Закуп v3 (11.08) — «пропавшие листинги»: наши kod'ы, которые были в
    предыдущем месяце Kaspi-матрицы отдела и исчезли в последнем. Ровно так
    11.08 вручную нашлись 6 SKU на ~10 млн ₸/мес (Leadbros 9300023, XINGX
    XF-850, Friggier LD-998/SD-730B, XINGX SD/SC103B, AOLIEGE BC/BD 801) —
    теперь проверка автоматическая при каждом расчёте закупа.

    Исчезновение из выгрузки Kaspi = либо слетела карточка, либо ноль на
    всех 3 видимых Kaspi складах. Мост SKU↔kod (sku_bridge) позволяет
    различить: если по сопоставленной карточке CRM сток ЕСТЬ — проблема в
    листинге, это самый дорогой и самый быстрый в починке случай.

    Месяцы сравниваются ВНУТРИ отдела (форматы разные: «Июль 2026» у
    морозильников, «Июль» у витрин). Год-less месяцы сортируются по номеру
    месяца — на стыке годов (Дек→Янв) сравнение станет неверным, к этому
    моменту в выгрузках должен появиться год (см. known_gaps).

    Возвращает (список пропавших, статистика моста).
    """
    from app.analytics.sku_bridge import build_bridge

    if not rows or not our_brands:
        return [], {}
    ob = {str(b).strip().upper() for b in our_brands}
    _mkey = _kaspi_month_key

    if bridge is None:
        # standalone-режим (тесты): мост строится здесь; из calc_procurement_v2
        # передаётся готовый — он там нужен и для рыночного контекста items.
        kaspi_seen: dict[str, dict] = {}
        for r in rows:
            kod = str(r.get("kod") or "").strip()
            if kod and kod not in kaspi_seen:
                kaspi_seen[kod] = {"kod": kod, "name": r.get("name"), "brand": r.get("brand")}
        bridge = build_bridge(
            [{"sku": s, "name": n} for s, n in crm_catalog.items()],
            list(kaspi_seen.values()), brand_family=ob)

    by_dept: dict[str, list] = defaultdict(list)
    for r in rows:
        by_dept[r.get("department") or ""].append(r)

    lost = []
    for dept, drows in by_dept.items():
        month_keys = {}
        for r in drows:
            mk = _mkey(r.get("month"))
            if mk:
                month_keys.setdefault(mk, r.get("month"))
        if len(month_keys) < 2:
            continue
        ordered = sorted(month_keys)
        last_k, prev_k = ordered[-1], ordered[-2]
        last_label, prev_label = month_keys[last_k], month_keys[prev_k]

        prev_by_kod: dict[str, dict] = {}
        last_kods: set = set()
        for r in drows:
            mk = _mkey(r.get("month"))
            kod = str(r.get("kod") or "").strip()
            if not kod or not mk:
                continue
            if mk == last_k:
                last_kods.add(kod)
            elif mk == prev_k:
                e = prev_by_kod.setdefault(kod, {"units": 0.0, "revenue": 0.0, "r": r})
                e["units"] += r.get("units") or 0
                e["revenue"] += r.get("revenue") or 0

        for kod, e in prev_by_kod.items():
            r = e["r"]
            if (str(r.get("brand") or "").strip().upper() not in ob
                    or e["units"] <= 0 or kod in last_kods):
                continue
            links = bridge["kod_to_skus"].get(kod) or []
            crm_stock = crm_pipe = 0.0
            linked_skus = []
            for l in links:
                st = stock_by_sku.get(str(l["sku"]).strip().upper())
                if st:
                    crm_stock += (st.get("wh_pervomay") or 0) + (st.get("wh_astana") or 0) + (st.get("wh_shymkent") or 0)
                    crm_pipe += (st.get("ymc_transit") or 0) + (st.get("ordered") or 0)
                linked_skus.append({"sku": l["sku"], "confidence": l["confidence"]})
            if not links:
                verdict = "не сопоставлен с CRM — проверить вручную (мост не нашёл карточку)"
            elif crm_stock > 0:
                verdict = ("товар на складе ЕСТЬ — похоже, слетел сам листинг: "
                           "проверить карточку на Kaspi, это самая быстрая починка")
            elif crm_pipe > 0:
                verdict = "стока нет, но товар в пути/заказан — вернуть листинг к приходу"
            else:
                verdict = "стока нет и не заказано — потерян и товар, и листинг"
            lost.append({
                "kod": kod, "name": r.get("name"), "brand": r.get("brand"),
                "department": dept,
                "prev_month": prev_label, "last_month": last_label,
                "prev_units": round(e["units"], 1), "prev_revenue": round(e["revenue"], 0),
                "crm_links": linked_skus, "crm_stock": round(crm_stock, 1),
                "crm_pipeline": round(crm_pipe, 1), "verdict": verdict,
            })

    lost.sort(key=lambda x: -x["prev_revenue"])
    return lost, bridge["stats"]


def calc_procurement_v2(rows: list[dict], stock_rows: list[dict], channel_rows: list[dict],
                         scope_categories: Optional[set] = None,
                         our_brands: Optional[set] = None,
                         today: Optional[date] = None) -> dict:
    """
    rows: продажи Kaspi этого отдела из KaspiRow (после apply_business_rules)
          — используются ТОЛЬКО для сверки с Kaspi-каналом нового файла
          (kaspi_divergence), не как основной сигнал спроса.
    stock_rows: как в calc_procurement (v1) — остатки CRM.
    channel_rows: [{sku,name,qty,revenue,sale_date,channel,category,subgroup},
          ...] из ChannelSalesRow — ВЕСЬ каталог CRM, не только 4 отдела сайта.
    scope_categories: если задано — ограничить расчёт этими категориями.
          При None считается весь каталог (нужно для гарантии видимости
          категорий с живым T1 в calc_category_scope — см. её docstring).
    """
    if not channel_rows:
        return {"items": [], "made_to_order_groups": [], "no_stock_data": [], "months_used": [],
                "note": "Файл «Продажи всех каналов» не загружен."}

    _TRAILING_MONTHS = 3
    all_dates = sorted({(r["sale_date"].year, r["sale_date"].month)
                         for r in channel_rows if r.get("sale_date")})
    # Исключаем текущий (незавершённый) календарный месяц из окна тренда —
    # иначе усреднение по неполному месяцу (напр. выгрузка 4 числа = 4 дня
    # из ~30) занижает скорость продаж именно в момент, когда решение
    # нужнее всего. Найдено тестированием на реальном файле 05.08.2026.
    # 11.08: параметр today (для тестов и воспроизводимости) — НЕ затирать.
    today = today or date.today()
    if all_dates and all_dates[-1] == (today.year, today.month):
        all_dates = all_dates[:-1]
    months_used = all_dates[-_TRAILING_MONTHS:]
    n_months = len(months_used) or 1

    # 08.08 — Дамир: "твоя цель сейчас разделение суммы продаж и плана
    # закупа по городам и всю логику в целом" (цифры плана продаж по
    # городам придут позже отдельно — эта функция готовит факт-разбивку,
    # план встанет поверх неё, когда появятся цифры). Разбивка ПО ВСЕМ
    # каналам (не только retail-бакету, который используется для скорости
    # продаж/тира ниже) — это топлайн "сколько продаём в каждом городе",
    # не сигнал спроса для закупа.
    CITIES = ("Алматы", "Астана", "Шымкент")
    city_sales_totals = {c: {"revenue": 0.0, "qty": 0.0} for c in CITIES}
    city_sales_totals["Не определено"] = {"revenue": 0.0, "qty": 0.0}
    for r in channel_rows:
        d = r.get("sale_date")
        if not d or (d.year, d.month) not in months_used:
            continue
        c = r.get("city") if r.get("city") in CITIES else "Не определено"
        city_sales_totals[c]["revenue"] += r.get("revenue") or 0
        city_sales_totals[c]["qty"] += r.get("qty") or 0
    for c in city_sales_totals:
        city_sales_totals[c]["revenue"] = round(city_sales_totals[c]["revenue"], 0)
        city_sales_totals[c]["qty"] = round(city_sales_totals[c]["qty"], 1)

    seasonality = calc_category_seasonality(channel_rows)

    # 08.08 — root-cause guard (Дамир: "мы уже 10 раз переделывали логику и
    # он всё ещё предлагает фризер" — расследование живых данных проды
    # показало, что сама логика верна и подтверждена тестом на полном файле
    # (42к строк, 3 города): фризер корректно уходит в T2S_SEASON_OFF,
    # suggest_qty=0. Причина расхождения prod vs тест — на проде оказался
    # загружен УЗКИЙ тестовый файл (1988 строк, только Алматы, короткий
    # период вместо полного «Продажи всех каналов»), из-за чего
    # calc_category_seasonality() выше молча пропускает почти все категории
    # (< 6 месяцев истории на категорию) и city_sales_totals/
    # city_purchase_split вырождаются в один город. Раньше это было
    # НЕВИДИМО — расчёт просто тихо давал другой, менее строгий результат
    # без единого сигнала о том, что входные данные недостаточны. Этот блок
    # делает такую ситуацию видимой прямо в ответе API (data_quality), а не
    # только при ручном сравнении с локальным тестом — чтобы ошибка
    # «загрузили не тот/слишком узкий файл» не повторялась в 11-й раз.
    retail_rows_dq = [r for r in channel_rows if _channel_bucket(r.get("channel")) == "retail"]
    dq_cities_seen = {r.get("city") for r in retail_rows_dq if r.get("city")}
    dq_categories_seen = {r.get("category") or "" for r in retail_rows_dq if r.get("sale_date")}
    dq_categories_seen.discard("")
    dq_months_total = len(all_dates)
    dq_seasonality_coverage = (round(len(seasonality) / len(dq_categories_seen), 2)
                                if dq_categories_seen else None)

    dq_warnings = []
    if len(dq_cities_seen) < 3:
        dq_warnings.append(
            f"Загруженный файл содержит данные только по {len(dq_cities_seen)} город(ам) из 3"
            f"{(' (' + ', '.join(sorted(dq_cities_seen)) + ')') if dq_cities_seen else ''} — "
            "«По городам» и разбивка закупа по городам будут неполными или неверными."
        )
    if dq_months_total < 6:
        dq_warnings.append(
            f"Загруженный файл покрывает только {dq_months_total} календарных месяцев — "
            "сезонное подавление (тир T2S_SEASON_OFF) требует минимум 6 и НЕ сработает ни для "
            "одной категории. Все рекомендации «Купить» в этом снимке — БЕЗ поправки на сезон."
        )
    elif dq_seasonality_coverage is not None and dq_seasonality_coverage < 0.5:
        dq_warnings.append(
            f"Сезонность посчиталась только для {len(seasonality)} из {len(dq_categories_seen)} "
            f"категорий ({int(dq_seasonality_coverage * 100)}%) — остальным не хватает 6 месяцев "
            "истории в загруженном файле, для них сезонное подавление не сработает."
        )
    data_quality = {
        "cities_seen": sorted(dq_cities_seen),
        "months_total": dq_months_total,
        "categories_total": len(dq_categories_seen),
        "categories_with_seasonality": len(seasonality),
        "warnings": dq_warnings,
    }

    per_sku: dict[str, dict] = {}
    for r in channel_rows:
        sku = r["sku"]
        d = r.get("sale_date")
        if not d:
            continue
        cat = r.get("category") or ""
        if scope_categories is not None and cat not in scope_categories:
            continue
        entry = per_sku.setdefault(sku, {
            "name": r.get("name"), "category": cat, "subgroup": r.get("subgroup"),
            "retail_by_month": defaultdict(float), "wholesale_orders": [],
            "retail_by_city": defaultdict(float),
            "qty_recent": 0.0,   # продажи в последнем ЗАВЕРШЁННОМ месяце + текущем частичном
            "_last_rev": -1,
        })
        if (r.get("revenue") or 0) >= entry["_last_rev"]:
            entry["name"] = r.get("name") or entry["name"]
            entry["_last_rev"] = r.get("revenue") or 0
        bucket = _channel_bucket(r.get("channel"))
        ym = (d.year, d.month)
        if bucket == "retail":
            # 12.08, кейс Дамира «CF-E4F в T1»: свежесть спроса. Товар мог
            # набрать mvel в начале трейлинг-окна и умереть — 0 продаж в
            # последнем завершённом И в текущем частичном месяце значит
            # «спрос не подтверждён сейчас», в T1 такому не место.
            if (months_used and ym == months_used[-1]) or ym == (today.year, today.month):
                entry["qty_recent"] += r.get("qty") or 0
            if ym in months_used:
                entry["retail_by_month"][ym] += r.get("qty") or 0
                # 08.08 — город СКЛАДА, обслужившего продажу (см. докстринг
                # router/channel_sales.py), не город доставки клиенту. Нужен
                # для будущей разбивки закупа/перемещения по городам
                # (Дамир 08.08: "план продаж на 3 города — Алматы/Астана/
                # Шымкент"). Пока чисто информационный срез в item — тир и
                # suggest_qty ниже по-прежнему считаются по ОБЩЕЙ скорости
                # продаж, город НЕ влияет на решение (это отдельная, ещё не
                # спроектированная логика "Перемещение").
                city = r.get("city")
                if city:
                    entry["retail_by_city"][city] += r.get("qty") or 0
        elif bucket == "wholesale":
            entry["wholesale_orders"].append(
                {"date": d.date().isoformat(), "qty": r.get("qty") or 0, "channel": r.get("channel")})

    # Kaspi-канал ЭТОГО файла (для сверки с KaspiRow сайта)
    kaspi_channel_by_sku: dict[str, float] = defaultdict(float)
    for r in channel_rows:
        d = r.get("sale_date")
        if d and (d.year, d.month) in months_used and (r.get("channel") or "").strip().lower() == "каспи":
            kaspi_channel_by_sku[r["sku"]] += r.get("qty") or 0

    # mvel_kaspi сайта (KaspiRow, отдельная загрузка Kaspi-матриц) — для сверки
    months_used_labels = {f"{MONTH_ORDER[m-1]} {y}" for (y, m) in months_used}
    site_kaspi_by_sku: dict[str, float] = defaultdict(float)
    for r in rows:
        if r.get("month") in months_used_labels:
            site_kaspi_by_sku[sku_key(r)] += r.get("units") or 0

    stock_by_sku: dict[str, dict] = {}
    for s in stock_rows:
        k = str(s.get("sku") or "").strip().upper()
        if k:
            stock_by_sku[k] = s

    # ── Ф2 (11.08): мост SKU↔kod + рыночный контекст Kaspi ───────────────
    # Каталог CRM для моста: имена из остатков + имена из файла продаж
    # (остатки — основной источник, продажи добирают SKU без строки стока).
    from app.analytics.sku_bridge import build_bridge
    _ob_set = {str(b).strip().upper() for b in (our_brands or set())}
    _crm_catalog: dict[str, str] = {}
    for s in stock_rows:
        _sk = str(s.get("sku") or "").strip()
        _nm = str(s.get("name") or "").strip()
        if _sk and _nm:
            _crm_catalog[_sk] = _nm
    for r in channel_rows:
        _sk = str(r.get("sku") or "").strip()
        if _sk and _sk not in _crm_catalog and r.get("name"):
            _crm_catalog[_sk] = str(r.get("name")).strip()
    _kaspi_seen: dict[str, dict] = {}
    for r in rows:
        _kd = str(r.get("kod") or "").strip()
        if _kd and _kd not in _kaspi_seen:
            _kaspi_seen[_kd] = {"kod": _kd, "name": r.get("name"), "brand": r.get("brand")}
    _bridge = build_bridge(
        [{"sku": s, "name": n} for s, n in _crm_catalog.items()],
        list(_kaspi_seen.values()), brand_family=_ob_set) if _kaspi_seen and _crm_catalog \
        else {"sku_to_kods": {}, "kod_to_skus": {}, "stats": {}}

    kaspi_lost_listings, sku_bridge_stats = calc_kaspi_lost_listings(
        rows, stock_by_sku, _crm_catalog, our_brands or set(), bridge=_bridge)
    sku_bridge_stats = sku_bridge_stats or _bridge["stats"]

    # ── C3 (11.08, «исправляй» П4): рыночные карты по последнему месяцу
    # Kaspi-матрицы каждого отдела — ёмкость ветки, наша доля, тренд.
    _dept_mkeys: dict[str, dict] = {}
    for r in rows:
        _mk = _kaspi_month_key(r.get("month"))
        if _mk:
            _dept_mkeys.setdefault(r.get("department") or "", {})[_mk] = True
    _dept_last_prev: dict[str, tuple] = {}
    for _dp, _mks in _dept_mkeys.items():
        _oo = sorted(_mks)
        _dept_last_prev[_dp] = (_oo[-1], _oo[-2] if len(_oo) > 1 else None)

    _vetka_rev_last: dict[tuple, float] = defaultdict(float)
    _vetka_rev_prev: dict[tuple, float] = defaultdict(float)
    _vetka_our_last: dict[tuple, float] = defaultdict(float)
    _kod_latest: dict[str, dict] = {}
    for r in rows:
        _dp = r.get("department") or ""
        _lp = _dept_last_prev.get(_dp)
        if not _lp:
            continue
        _mk = _kaspi_month_key(r.get("month"))
        _vet = (r.get("vetka") or "").strip()
        _kd = str(r.get("kod") or "").strip()
        if _mk == _lp[0]:
            if _vet:
                _vetka_rev_last[(_dp, _vet)] += r.get("revenue") or 0
                if str(r.get("brand") or "").strip().upper() in _ob_set:
                    _vetka_our_last[(_dp, _vet)] += r.get("revenue") or 0
            if _kd:
                _e = _kod_latest.setdefault(_kd, {"dept": _dp, "vetka": _vet,
                                                   "units": 0.0, "sellers": 0.0})
                _e["units"] += r.get("units") or 0
                _e["sellers"] = max(_e["sellers"], r.get("sellers") or 0)
                if _vet and not _e["vetka"]:
                    _e["vetka"] = _vet
        elif _lp[1] is not None and _mk == _lp[1]:
            if _vet:
                _vetka_rev_prev[(_dp, _vet)] += r.get("revenue") or 0

    # ── Ф3-фикс из самокритики аудита 11.08: окно прибытия якорится на
    # РЕАЛЬНОЕ «сегодня», а не на последний месяц файла — файл, залитый с
    # лагом, сдвигал окно в прошлое. Заказ размещается сегодня → приезжает
    # t+45д → продаётся до t+105д; берём календарные месяцы трёх точек
    # окна (начало/середина/конец).
    _today = today  # уже разрешён выше (параметр или date.today())
    _arrival_months: list[int] = []
    for _off in (LEAD_TIME_DAYS, LEAD_TIME_DAYS + TARGET_COVER_DAYS // 2,
                 LEAD_TIME_DAYS + TARGET_COVER_DAYS):
        _am = (_today + timedelta(days=_off)).month
        if _am not in _arrival_months:
            _arrival_months.append(_am)

    # 08.08 — «под заказ» категории определяются ЗАРАНЕЕ, отдельным проходом,
    # ДО расчёта тира/suggest_qty (не после, как было). Раньше
    # made_to_order_group был чисто декоративным флагом поверх уже
    # посчитанного 2-месячного складского буфера — то есть система всё равно
    # советовала "купи 20 шт на склад" для категории, где 96% SKU физически
    # не лежат на складе (сделано под заказ). Директорский аудит 08.08: 3 из
    # топ-4 позиций T1 по ₸-потенциалу были из «Гастрономы и кондитерки»
    # (96% нулевого стока) — тот же класс ошибки, что и сезонный баг с
    # фризерами: тир считается по модели (складской буфер), которая не
    # подходит категории.
    cat_group_skus: dict[str, list[str]] = defaultdict(list)
    for sku, d in per_sku.items():
        if stock_by_sku.get(sku) is None:
            continue
        cat_group_skus[d["category"]].append(sku)

    made_to_order_categories: set[str] = set()
    made_to_order_groups = []
    for cat, skus in cat_group_skus.items():
        if len(skus) < 3:
            continue
        zero_stock = sum(
            1 for s in skus
            if ((stock_by_sku[s].get("wh_pervomay") or 0) + (stock_by_sku[s].get("wh_astana") or 0)
                + (stock_by_sku[s].get("wh_shymkent") or 0)) <= 0
        )
        share = zero_stock / len(skus)
        if share >= 0.8:
            made_to_order_categories.add(cat)
            made_to_order_groups.append({"category": cat, "sku_count": len(skus),
                                          "zero_stock_count": zero_stock, "zero_stock_share": round(share, 2)})

    items = []
    no_stock_data = []

    for sku, d in per_sku.items():
        total_retail = sum(d["retail_by_month"].values())
        mvel_retail = total_retail / n_months

        st = stock_by_sku.get(sku)
        if st is None:
            if mvel_retail >= 0.5:  # не шумим товарами вообще без сигнала
                no_stock_data.append({"sku": sku, "name": d["name"], "category": d["category"],
                                       "mvel_retail": round(mvel_retail, 2)})
            continue

        kaspi_stock = (st.get("wh_pervomay") or 0) + (st.get("wh_astana") or 0) + (st.get("wh_shymkent") or 0)
        near_pipeline = st.get("ymc_transit") or 0
        far_pipeline = st.get("ordered") or 0

        # ── Ф3 (11.08, Закуп v3): сезонная модель спроса ─────────────────
        # Спрос для тира и целевого объёма = десезонализированный уровень ×
        # сезонный индекс окна ПРИБЫТИЯ товара. Товар, заказанный сегодня,
        # приезжает через LEAD_TIME_DAYS (~45д) и продаётся следующие
        # TARGET_COVER_DAYS (~60д) — значит правильный множитель берётся не
        # из текущего месяца, а из окна t+45..t+105 дней ≈ календарные
        # месяцы cur+2..cur+4 (cur_month — последний ЗАВЕРШЁННЫЙ месяц,
        # «сегодня» — начало cur+1). Именно это делает возможным довес ДО
        # пика: в феврале трейлинг-скорость фризеров зимняя, но окно
        # прибытия — апрель-июнь, и тир/объём считаются уже под них.
        # Уровень чистится от сезона трейлинг-месяцев (qty / индекс месяца,
        # индекс зажат в _DESEASON_CLAMP), иначе «низкий сезон в знаменателе»
        # занижал бы базу ровно перед пиком, а пиковые месяцы — завышали
        # после него. Если сезонность категории не посчиталась (нет 6
        # месяцев истории) или окно прибытия известно меньше чем на 2 месяца
        # — factor = 1.0, уровень = сырой mvel, поведение в точности
        # прежнее. Модель включается сама по мере появления данных — та же
        # философия, что у data_quality guard.
        cur_month = months_used[-1][1] if months_used else None
        cat_season = seasonality.get(d["category"])

        season_level = mvel_retail
        season_factor = 1.0
        arrival_idx = None
        if cat_season and cur_month:
            deseason_terms = []
            for (_yy, _mm), _qty in d["retail_by_month"].items():
                s = cat_season.get(_mm)
                if s is None:
                    deseason_terms.append(_qty)
                else:
                    s_cl = min(max(s, _DESEASON_CLAMP_LOW), _DESEASON_CLAMP_HIGH)
                    deseason_terms.append(_qty / s_cl)
            if deseason_terms:
                # месяцы без продаж отсутствуют в retail_by_month — делим на
                # n_months, как и sum/n_months у сырого mvel_retail
                season_level = sum(deseason_terms) / n_months
            win = []
            for m in _arrival_months:
                v = cat_season.get(m)
                if v is not None:
                    win.append(v)
            if len(win) >= 2:
                arrival_idx = round(sum(win) / len(win), 2)
                season_factor = min(max(arrival_idx, SEASON_FACTOR_CAP_LOW), SEASON_FACTOR_CAP_HIGH)

        mvel_effective = round(season_level * season_factor, 2)

        stale_demand = mvel_retail >= 1.0 and (d["qty_recent"] or 0) <= 0
        tier, cover_months, cover_months_full, covered_pipeline = _procurement_classify_v2(
            mvel_effective, kaspi_stock, near_pipeline, far_pipeline,
            recent_ok=not stale_demand)

        # ── Сезонное подавление, редизайн 12.08 (кейс Дамира «система
        # закупила бы мороженщики в августе»). Прежний триггер смотрел на
        # ТЕКУЩИЙ месяц («сезон уже кончился?») — но заказ, размещённый в
        # августе, приезжает в октябре: текущий индекс ещё пиковый, а окно
        # прибытия уже мёртвое. Правильный вопрос один: «будет ли сезон,
        # когда товар ПРИЕДЕТ и будет продаваться?» — то есть решает окно
        # прибытия (_arrival_months, t+45..t+105 от сегодня), а не текущий
        # месяц. Подавляем, если ВСЁ окно известно и максимум индекса < 0.4.
        # Неполное окно не подавляет (Ф1: «не знаем» ≠ «ноль»).
        #
        # Второй контур — справочная сезонность (fallback): пока файл продаж
        # короче 6 месяцев, файловой сезонности нет ВООБЩЕ, и категории с
        # известной бизнесу мёртвой зимой шли в T1 без единого флага. Для
        # категорий из SEASONAL_FALLBACK_OFF_MONTHS (подтверждены полным
        # годом 05.08 + прямое указание Дамира 12.08) подавляем по
        # календарю, если всё окно прибытия внутри мёртвых месяцев.
        # Файловая сезонность, когда появится, имеет приоритет.
        season_note = None
        season_conflict = False
        season_suppressed = False
        season_suppress_src = None
        if cat_season and cur_month:
            idx = cat_season.get(cur_month)
            if idx is not None:
                season_note = {"month_index": idx}
                if idx < 0.4 and tier in ("T1_CRITICAL", "T2_PIPELINE"):
                    season_conflict = True  # информационный флаг, решает окно ниже
        if tier in ("T1_CRITICAL", "T2_PIPELINE"):
            if cat_season:
                win_vals = [cat_season.get(m) for m in _arrival_months]
                if all(v is not None for v in win_vals) and win_vals \
                        and max(win_vals) < 0.4:
                    season_suppressed = True
                    season_suppress_src = "file"
            else:
                _off = SEASONAL_FALLBACK_OFF_MONTHS.get(d["category"])
                if _off and all(m in _off for m in _arrival_months):
                    season_suppressed = True
                    season_suppress_src = "fallback"

        if season_suppressed:
            tier = "T2S_SEASON_OFF"

        is_made_to_order = d["category"] in made_to_order_categories
        if is_made_to_order and tier in ("T1_CRITICAL", "T2_PIPELINE"):
            # Категория живёт "под заказ" (см. комментарий у
            # made_to_order_categories выше) — 2-месячный складской буфер
            # здесь не имеет смысла. Спрос реальный (виден по mvel_retail),
            # но "купи N штук на склад" — неверная рекомендация для модели
            # без склада. Не гадаем правильное количество (нет данных о
            # реальном лид-тайме/MOQ у поставщика под заказ) — честно
            # показываем suggest_qty=0 и оставляем скорость продаж как
            # сигнал спроса, а не как "купи ровно столько".
            tier = "T2M_MADE_TO_ORDER"

        # ── Стоп-лист, 12.08 (кейс Дамира «CF-E4F на стоп-листе в T1»):
        # статус CRM читался в item, но НИКОГДА не участвовал в решении —
        # на живом проде треть T1+T2 (19 из 56) оказалась «Стоп лист».
        # Товар, который человек сознательно остановил, не может быть
        # рекомендацией «срочно купить». Перекрывает ЛЮБОЙ тир. Если при
        # стопе есть свежие продажи — отдельный флаг противоречия (стоп
        # может быть автоматическим из-за нуля остатка — тогда решение о
        # возврате товара принимает человек, видя флаг, а не молчаливый T1).
        _status_norm = str(st.get("status") or "").strip().lower()
        status_blocked = bool(_status_norm) and not _status_norm.startswith("продается")
        status_conflict = status_blocked and (d["qty_recent"] or 0) > 0
        if status_blocked:
            tier = "T2X_STOP"

        # Целевой объём — по ЭФФЕКТИВНОЙ скорости (уровень × сезон окна
        # прибытия); вычитаем ровно «покрывающий» пайплайн из тира (Ф1).
        target_units = (TARGET_COVER_DAYS / 30.0) * mvel_effective
        need = target_units - kaspi_stock - covered_pipeline
        suggest_qty = max(0, math.ceil(need)) if tier in ("T1_CRITICAL", "T2_PIPELINE") else 0

        # Сезонный довес отдельным числом (решение Дамира 11.08: довес
        # виден отдельной колонкой, финальное слово за закупщиком):
        # сколько штук добавила/убрала сезонная модель против плоской
        # математики при том же тире.
        _baseline_target = (TARGET_COVER_DAYS / 30.0) * mvel_retail
        _baseline_need = _baseline_target - kaspi_stock - covered_pipeline
        _baseline_qty = max(0, math.ceil(_baseline_need)) if tier in ("T1_CRITICAL", "T2_PIPELINE") else 0
        season_uplift_units = suggest_qty - _baseline_qty

        # ── C3 (11.08, аудит П4): рыночный контекст через мост SKU↔kod ──
        # Ветка листинга, ёмкость её рынка в последнем месяце Kaspi-матрицы,
        # наша доля и тренд к предыдущему месяцу. Не меняет тир — участвует
        # в сортировке внутри тира (растущий сегмент поднимается выше при
        # равном ₸-потенциале, см. сортировку ниже).
        market = None
        demand_underest = None
        _links = _bridge["sku_to_kods"].get(sku) or []
        if _links:
            _cands = [(_l["kod"], _kod_latest[_l["kod"]])
                      for _l in _links if _l["kod"] in _kod_latest]
            _vk = next(((e["dept"], e["vetka"]) for _k, e in _cands if e["vetka"]), None)
            if _vk:
                _mr = _vetka_rev_last.get(_vk, 0.0)
                _mp = _vetka_rev_prev.get(_vk, 0.0)
                market = {
                    "department": _vk[0], "vetka": _vk[1],
                    "vetka_market_month": round(_mr, 0),
                    "our_share_pct": round(_vetka_our_last.get(_vk, 0.0) / _mr * 100, 1)
                                     if _mr > 0 else None,
                    "vetka_trend_pct": round((_mr - _mp) / _mp * 100, 1) if _mp > 0 else None,
                }
            # ── Аудит П3: детектор заниженного спроса (стокаут-искажение).
            # Kaspi-матрица показывает продажи ЛИСТИНГА за последний месяц
            # независимо от того, был ли у нас товар — если листинг продал
            # заметно больше, чем CRM-скорость этого SKU, CRM-спрос почти
            # наверняка занижен стокаутом/дырой в данных. Гейт sellers<=2:
            # на листинге с многими продавцами штуки не только наши — не
            # приписываем себе чужие продажи. НЕ раздуваем suggest_qty
            # автоматически (урок отката v1-фикса) — показываем обе цифры
            # и альтернативный объём, решение за человеком.
            _site_units = sum(e["units"] for _k, e in _cands)
            _max_sellers = max((e["sellers"] for _k, e in _cands), default=0)
            if (_site_units >= 2 and _max_sellers <= 2
                    and _site_units > 1.5 * max(mvel_retail, 0.1)):
                _alt_rate = max(_site_units, mvel_effective)
                _alt_need = (TARGET_COVER_DAYS / 30.0) * _alt_rate - kaspi_stock - covered_pipeline
                demand_underest = {
                    "site_kaspi_mvel": round(_site_units, 1),
                    "crm_mvel": round(mvel_retail, 2),
                    "alt_suggest_qty": max(0, math.ceil(_alt_need)),
                }

        # ── Аудит П5: опт жрёт тот же склад, но исключён из спроса (лумпи).
        # Буфер, посчитанный только по рознице, может сгореть одним дилерским
        # заказом. Считаем скорость опта в том же трейлинг-окне и флагуем,
        # когда покрытие «достаточно» по рознице, но проваливается с учётом
        # опта. Тир не меняем (прозрачность) — флаг + честное покрытие.
        _ws_window_qty = 0.0
        for _o in d["wholesale_orders"]:
            try:
                _oy, _om = int(str(_o["date"])[0:4]), int(str(_o["date"])[5:7])
            except (ValueError, TypeError):
                continue
            if (_oy, _om) in months_used:
                _ws_window_qty += _o.get("qty") or 0
        wholesale_rate = round(_ws_window_qty / n_months, 2)
        wholesale_risk = None
        _comb = mvel_effective + wholesale_rate
        if wholesale_rate > 0 and _comb > 0:
            _ds_ws = kaspi_stock / (_comb / 30.0)
            if (cover_months is not None and cover_months * 30 >= TARGET_COVER_DAYS
                    and _ds_ws < TARGET_COVER_DAYS):
                wholesale_risk = {
                    "wholesale_rate_monthly": wholesale_rate,
                    "cover_months_with_wholesale": round(_ds_ws / 30, 1),
                }

        wholesale_orders = sorted(d["wholesale_orders"], key=lambda o: o["date"])[-5:]
        wholesale_total = sum(o["qty"] for o in d["wholesale_orders"])

        # ОТКЛЮЧЕНО 06.08 — родовой баг, не откалиброванный порог. Найдено
        # директорским аудитом прямо на живых данных прода: site_kaspi_by_sku
        # ключуется по sku_key() -> приоритет "kod" из KaspiRow — это код
        # ЛИСТИНГА KASPI (напр. "119264283"), а kaspi_channel_by_sku ключуется
        # по CRM "SKU" из транзакционной выгрузки (напр. "9304067") — ДРУГОЕ
        # пространство идентификаторов, между ними нет моста в имеющихся
        # данных. Проверено на 3/3 реальных примерах ("MUXXED SD/SC-105Y",
        # "Friggier LSC-145W", "AOLIEGE BC/BD 601") — все активно продаются
        # на Kaspi по данным самого сайта (12/9/7, 59/18, 6 шт по месяцам),
        # но из-за несовпадения кода получали file=X/site=0/100% — то есть
        # ложный сигнал "нет в Kaspi" на товарах, которые там реально есть.
        # Из 351 сработавших на проде divergence-флагов 272 вообще вне 4
        # отделов сайта (site_kaspi_by_sku тривиально пуст), у оставшихся 79
        # — 100% site=0, что для активно продающихся SKU статистически
        # невозможно без ошибки ключа. Показывать заведомо ложный "красный
        # флаг" хуже, чем не показывать сигнал вообще — включать обратно
        # только после реального моста SKU(CRM) <-> kod(Kaspi-листинг).
        kaspi_divergence = None

        # 07.08 — цель роли "директор по продажам": максимизировать СУММУ
        # продаж, не штуки. Тир T1-T4 остаётся честным сигналом "есть ли
        # риск дефицита по факту" (не трогаем — иначе теряем понятность
        # "почему это T1"), но ранжирование ВНУТРИ тира было по штукам
        # (-suggest_qty), из-за чего дешёвая мелочь (овощерезки, костерезки
        # и т.п.) визуально соревновалась с холодильным/морозильным
        # оборудованием, которое реально двигает сумму плана. Цена ("Цена"
        # из остатков CRM) уже была в базе, но нигде не читалась в v1/v2 —
        # довожу её до items и считаю revenue_potential = цена × Купить,
        # это и есть новый первичный критерий сортировки внутри тира.
        price = st.get("price") or 0
        revenue_potential = round(price * suggest_qty, 0) if suggest_qty > 0 else 0

        resolved_name = MANUAL_NAME_OVERRIDES.get(sku) or d["name"]
        # 08.08 — по городам, чисто информационно (см. комментарий выше в
        # цикле накопления retail_by_city). mvel_retail_by_city — скорость
        # продаж отдельно по городу-складу; by_city_stock — реальный сток
        # именно этого склада (уже был в st, просто не разложен по item).
        # Не участвует в tier/suggest_qty — только для будущей вкладки
        # "Перемещение" (задача #141/#142).
        mvel_retail_by_city = {
            city: round(qty / n_months, 2) for city, qty in d["retail_by_city"].items()
        }
        by_city_stock = {
            "Алматы": st.get("wh_pervomay") or 0,
            "Астана": st.get("wh_astana") or 0,
            "Шымкент": st.get("wh_shymkent") or 0,
        }
        item = {
            "sku": sku, "name": resolved_name, "category": d["category"], "subgroup": d["subgroup"],
            "status": st.get("status"),
            "mvel_retail": round(mvel_retail, 2),
            "mvel_effective": mvel_effective,
            "season_model": ({
                "level": round(season_level, 2),
                "arrival_index": arrival_idx,
                "factor": round(season_factor, 2),
                "uplift_units": season_uplift_units,
            } if arrival_idx is not None else None),
            "mvel_retail_by_city": mvel_retail_by_city,
            "by_city_stock": by_city_stock,
            "kaspi_stock": kaspi_stock,
            "near_pipeline": near_pipeline, "far_pipeline": far_pipeline,
            "cover_months": cover_months, "cover_months_full": cover_months_full,
            "tier": tier, "suggest_qty": suggest_qty,
            "price": round(price, 0), "revenue_potential": revenue_potential,
            "monthly_value": round(price * mvel_effective, 0),
            "season_note": season_note, "season_conflict": season_conflict,
            "season_suppress_src": season_suppress_src,
            "stale_demand": stale_demand,
            "status_blocked": status_blocked, "status_conflict": status_conflict,
            "qty_recent": round(d["qty_recent"] or 0, 1),
            "wholesale_pattern": {"total_13mo": round(wholesale_total, 0), "recent_orders": wholesale_orders}
                                  if wholesale_orders else None,
            "kaspi_divergence": kaspi_divergence,
            "made_to_order_group": is_made_to_order,
            "market": market,
            "demand_underest": demand_underest,
            "wholesale_risk": wholesale_risk,
        }
        items.append(item)

    # 08.08 — «Перемещение»: план закупа/остатков по городам. Дамир 08.08:
    # срок внутреннего плеча Первомай→Астана/Шымкент считать T+0/1 (почти
    # мгновенным) — это ГИПОТЕЗА (в CRM и в двух документах логистики точных
    # цифр нет, "используется допущение «быстро» без цифры"), уточнить
    # позже реальным числом. При T+0/1 логика простая: если в одном городе
    # дефицит (сток < целевого покрытия по ЕГО собственному спросу), а в
    # другом — избыток, выгоднее СНАЧАЛА переместить внутри страны (дни),
    # чем заказывать у поставщика (~45 дней). suggest_qty/tier выше НЕ
    # трогаем (они остаются честным сигналом "есть риск дефицита в целом по
    # компании") — это отдельный, дополнительный слой поверх него.
    city_transfers = []
    for it in items:
        by_city = it["by_city_stock"]
        demand = it["mvel_retail_by_city"]
        target_days_frac = TARGET_COVER_DAYS / 30.0
        gaps = {}
        for c in CITIES:
            city_target = target_days_frac * (demand.get(c) or 0)
            city_stock = by_city.get(c) or 0
            gaps[c] = round(city_target - city_stock, 2)  # >0 дефицит, <0 избыток

        shortages = sorted([c for c in CITIES if gaps[c] > 0.5], key=lambda c: -gaps[c])
        surpluses = sorted([c for c in CITIES if gaps[c] < -0.5], key=lambda c: gaps[c])
        remaining_surplus = {c: -gaps[c] for c in surpluses}
        unresolved_gap = {c: (gaps[c] if gaps[c] > 0.5 else 0.0) for c in CITIES}
        # 08.08 — директорский аудит: доля Kaspi в спросе SKU, честности ради.
        # Живая проверка в CRM ("Склад → Бронь товаров") показала, что ВСЕ
        # исторические Kaspi-резервы (72 акта, все до дек. 2024, похоже на
        # устаревший/неактивный механизм) помечены единообразно "Каспи
        # (Алматы)" — ни разу "Каспи (Астана)"/"Каспи (Шымкент)", хотя
        # экспорт продаж явно показывает Kaspi-строки во всех 3 городах.
        # Не удалось ни подтвердить, ни опровергнуть, что "Город канала
        # продаж" на Kaspi-заказе = реальный физический склад отгрузки (лог
        # брони слишком старый/неактивный, чтобы служить перекрёстной
        # проверкой). Для розницы/Instagram/TikTok/Магазин уверенность выше
        # — там город прямо зашит в название канала в самой CRM. Поэтому
        # помечаем каждую рекомендацию перемещения долей Kaspi в спросе —
        # чем выше доля, тем ниже уверенность, что "Город канала продаж"
        # точно отражает физический склад.
        total_retail_units = it["mvel_retail"] * n_months
        kaspi_units = kaspi_channel_by_sku.get(it["sku"], 0)
        kaspi_share_pct = round(100 * kaspi_units / total_retail_units, 0) if total_retail_units > 0 else None

        sku_transfers = []
        for to_city in shortages:
            need = gaps[to_city]
            for from_city in surpluses:
                if need <= 0.5:
                    break
                avail = remaining_surplus.get(from_city, 0)
                if avail <= 0.5:
                    continue
                qty = math.floor(min(need, avail))
                if qty <= 0:
                    continue
                sku_transfers.append({
                    "sku": it["sku"], "name": it["name"], "category": it["category"],
                    "from_city": from_city, "to_city": to_city, "qty": qty,
                    "tier": it["tier"], "value": round((it["price"] or 0) * qty, 0),
                    "kaspi_share_pct": kaspi_share_pct,
                })
                need -= qty
                remaining_surplus[from_city] -= qty
            unresolved_gap[to_city] = round(max(0.0, need), 2)
        if sku_transfers:
            city_transfers.extend(sku_transfers)
        it["city_plan"] = {
            c: {"target_units": round(target_days_frac * (demand.get(c) or 0), 1),
                "stock": by_city.get(c) or 0, "gap": gaps[c]}
            for c in CITIES
        }

        # 08.08 — «Закуп по городам»: Дамир прямо указал, что вся логика закупа
        # должна учитывать фактический маршрут — из Китая товар всегда сначала
        # приезжает на Хаб Первомай (Алматы), и уже ОТТУДА распределяется в
        # Астану/Шымкент. Поэтому "Купить" (suggest_qty) — это не финальная
        # рекомендация, а только ОБЪЁМ заказа у поставщика; city_transfers выше
        # решает только то, что можно закрыть перемещением УЖЕ существующего
        # стока между городами. Если после этого остаётся нерешённый дефицит
        # (unresolved_gap > 0) в конкретном городе — именно под него нужно
        # закладывать долю НОВОЙ закупки, чтобы после приезда в Алматы часть
        # партии сразу переслать дальше, а не ждать, пока Алматы "наестся" и
        # излишек когда-нибудь переместят. Делим suggest_qty пропорционально
        # нерешённым дефицитам; если по формуле дефицита нет ни в одном городе
        # (buy пришёл из другой логики — буфер/пайплайн, не сырая городская
        # скорость продаж) — вся партия по умолчанию идёт в Алматы (это и
        # физически верно: приедет туда в любом случае, а куда двигать дальше
        # без выраженного городского дефицита — решение вручную, не автоматика).
        buy_qty = it.get("suggest_qty") or 0
        city_purchase_split = {c: 0 for c in CITIES}
        if buy_qty > 0:
            total_unresolved = sum(v for v in unresolved_gap.values() if v > 0)
            if total_unresolved > 0.5:
                shares = {c: unresolved_gap[c] for c in CITIES if unresolved_gap[c] > 0}
                allocated = 0
                for c, g in shares.items():
                    q = math.floor(buy_qty * g / total_unresolved)
                    city_purchase_split[c] = q
                    allocated += q
                leftover = buy_qty - allocated
                if leftover > 0:
                    target_c = max(shares, key=lambda c: shares[c])
                    city_purchase_split[target_c] += leftover
            else:
                city_purchase_split["Алматы"] = buy_qty
        it["city_purchase_split"] = city_purchase_split

    # 08.08 — итог по городам НОВОЙ закупки (сумма city_purchase_split по всем
    # SKU с buy>0) — отвечает на прямой вопрос Дамира "после закупа понять, в
    # какие города и как это разделить", на уровне сводки, а не только по
    # каждому SKU в таблице.
    purchase_split_totals = {c: {"units": 0.0, "value": 0.0} for c in CITIES}
    for it in items:
        cps = it.get("city_purchase_split") or {}
        for c in CITIES:
            q = cps.get(c) or 0
            purchase_split_totals[c]["units"] += q
            purchase_split_totals[c]["value"] += q * (it.get("price") or 0)
    for c in purchase_split_totals:
        purchase_split_totals[c]["units"] = round(purchase_split_totals[c]["units"], 0)
        purchase_split_totals[c]["value"] = round(purchase_split_totals[c]["value"], 0)

    # 08.08 — по ценности (цена×кол-во), не по штукам: раньше сортировка шла
    # по qty, из-за чего дешёвая бытовая мелочь оптом (напр. 58 фритюрниц)
    # перекрывала в списке единичный дорогой T1-товар (холод. витрина
    # 1.8М₸) — тот же класс ошибки, что чинили в основной таблице тиров
    # (revenue_potential вместо suggest_qty), просто не перенесли сюда при
    # первой сборке.
    city_transfers.sort(key=lambda x: -x["value"])

    # 08.08 — обнаружение возможных задвоенных карточек: одинаковое
    # название+цена+категория под разными SKU в T1/T2. Не можем отличить
    # "это правда разные варианты (цвет/комплектация не в названии)" от
    # "карточка продублирована в CRM" по имеющимся данным — не мержим и не
    # душим автоматически, просто группируем для ручной проверки в CRM перед
    # закупом (см. философию файла: не глушим молча). Найдено директорским
    # аудитом 08.08: «Кондитерская витрина Cake 1.5 +2/+7» под 3 разными SKU
    # (9303199/9313199/9323199) — 13 шт, 6.27М₸ суммарно.
    dup_key_map: dict[tuple, list] = defaultdict(list)
    for it in items:
        nm = str(it["name"] or "").strip().lower()
        if not nm or nm == "не указано":
            continue
        if it["tier"] not in ("T1_CRITICAL", "T2_PIPELINE"):
            continue
        dup_key_map[(nm, it["price"], it["category"])].append(it)

    possible_duplicates = []
    for (nm, price, cat), group in dup_key_map.items():
        if len(group) < 2:
            continue
        skus = [it["sku"] for it in group]
        possible_duplicates.append({
            "name": group[0]["name"], "category": cat, "price": price, "skus": skus,
            "total_suggest_qty": sum(it["suggest_qty"] for it in group),
            "total_revenue_potential": sum(it["revenue_potential"] for it in group),
        })
        for it in group:
            it["possible_duplicate_skus"] = [s for s in skus if s != it["sku"]]

    TIER_ORDER = PROC_TIER_ORDER
    # Сортировка внутри тира — по ЗАЩИЩАЕМОМУ БИЗНЕСУ ₸/мес (см.
    # procurement_sort_value, редизайн 12.08 по вопросу Дамира про
    # рисоварку): размер покупки (цена × Купить) больше не решает порядок —
    # он мерил трату, а не бизнес. Рыночный буст растущей ветки сохранён
    # (+50% кап). Роутер после присвоения scope пересортирует ещё раз,
    # подняв профильные отделы (core) над сопутствующими (extended/tail)
    # внутри каждого тира — здесь scope ещё неизвестен.
    items.sort(key=lambda it: (TIER_ORDER.get(it["tier"], 9),
                                -procurement_sort_value(it),
                                -it["suggest_qty"], -it["mvel_retail"]))

    return {
        "items": items,
        "made_to_order_groups": made_to_order_groups,
        "possible_duplicates": sorted(possible_duplicates, key=lambda x: -x["total_revenue_potential"]),
        "no_stock_data": sorted(no_stock_data, key=lambda x: -x["mvel_retail"])[:50],
        "city_sales_totals": city_sales_totals,
        "city_transfers": city_transfers,
        "purchase_split_totals": purchase_split_totals,
        "kaspi_lost_listings": kaspi_lost_listings,
        "sku_bridge_stats": sku_bridge_stats,
        "data_quality": data_quality,
        "months_used": sorted(months_used_labels, key=_month_sort_key),
        "counts": {t: sum(1 for it in items if it["tier"] == t) for t in TIER_ORDER},
        "target_cover_days": TARGET_COVER_DAYS,
        "lead_time_days": LEAD_TIME_DAYS,
        "review_cadence_days": REVIEW_CADENCE_DAYS,
        "known_gaps": [
            "MOQ (минимальная партия) по SKU/поставщику неизвестен — рекомендованное количество может быть меньше MOQ",
            "Себестоимость/маржа по SKU недоступна — приоритизация по выручке, не по прибыли",
            "Дата размещения заказа (2_ordered) неизвестна — 45 дней это верхняя граница, не точный расчёт",
            "city_transfers считает плечо Первомай→Астана/Шымкент почти мгновенным (T+0/1) "
            "по прямому указанию Дамира 08.08 — точных цифр в CRM и документах логистики нет "
            "(\"используется допущение «быстро» без цифры\"), это рабочая гипотеза, не измеренный факт; "
            "уточнить реальным сроком, когда появится",
            "city_purchase_split/purchase_split_totals (08.08, прямое указание Дамира): суммы "
            "«Купить» делятся по городам пропорционально НЕРЕШЁННОМУ дефициту после city_transfers "
            "(т.е. после того, как учтена возможность закрыть нехватку перемещением уже существующего "
            "стока внутри страны). Если ни в одном городе такого дефицита по формуле нет (buy пришёл "
            "не из сырой городской скорости продаж, а из буфера/пайплайна) — вся партия по умолчанию "
            "уходит в Алматы (Хаб Первомай), это и физически верно (первая точка приезда), просто "
            "дальнейшее решение вручную. Наследует все ограничения city_plan/city_transfers выше: "
            "T+0/1-гипотеза плеча и (для SKU с высокой kaspi_share_pct) ненадёжность city-сигнала "
            "по Kaspi, пока не появится 3-файловый экспорт по городам вместо текущего.",
            "city_sales_totals/city_plan — факт-разбивка по городам готова; цифры ПЛАНА продаж "
            "по городам ещё не переданы Дамиром (обещал скинуть позже) — сравнения план vs факт "
            "пока нет, есть только факт",
            "ВАЖНО (08.08, аудит достоверности, уточнено повторной проверкой в тот же день): "
            "\"Город канала продаж\" (поле ChannelSalesRow.city, используется сейчас) точно означает "
            "город-склад для розницы/Instagram/TikTok/Магазин — эти каналы сами city-суффиксированы "
            "в CRM (\"Инстаграм Астана\" и т.п.). ДЛЯ КАНАЛА КАСПИ ЭТО ПОЛЕ ОПРОВЕРГНУТО: фильтр CRM "
            "Аналитика→Товары по Точка отгрузки=Склад Астана + Канал=Каспи + Раздел=Коммерческое "
            "оборудование (05.08-08.08) дал 1 позицию на 499 500₸; тот же фильтр БЕЗ Точки отгрузки, "
            "только по Город=Астана, дал 75 позиций на 24.18М₸ — \"Город канала продаж\"=Астана "
            "почти никогда не совпадает со складом отгрузки для Kaspi через это поле. НО (уточнение "
            "08.08, по прямому указанию Дамира — проверено вживую в фильтре \"Канал продажи\") в CRM "
            "у канала \"Каспи\" есть СКРЫТОЕ разделение по городам на уровне самого фильтра: "
            "выпадающий список \"Канал продажи\" группирует записи по optgroup (Алматы/Шымкент/Астана), "
            "и внутри каждой группы — своя, отдельная запись \"Каспи\" с собственным ID "
            "(sale_channel: Алматы=3, Шымкент=49, Астана=54; визуально во всех трёх подписано "
            "одинаково \"Каспи\", различаются только по ID в DOM/optgroup). Живой тест подтвердил "
            "100%-е совпадение с реальным складом: Канал=Каспи(id=54,Астана) без Точки отгрузки → "
            "185 позиций/1033 шт/142 724 234₸; тот же фильтр + Точка отгрузки=Склад Астана → "
            "ИДЕНТИЧНО 185/1033/142 724 234₸. Повтор для Шымкента: Канал=Каспи(id=49) → "
            "176/550/94 978 712₸; + Точка отгрузки=Склад Шымкент → ИДЕНТИЧНО 176/550/94 978 712₸. "
            "Алматы (id=3): 302 позиции/2019 шт/318 465 642₸ (без доп. проверки Точкой отгрузки, "
            "но по аналогии тоже надёжно). Итого: city-атрибуция Kaspi ВОЗМОЖНА и НАДЁЖНА — но не "
            "через поле \"Город канала продаж\" в стандартном экспорте, а через выбор конкретного "
            "city-варианта канала \"Каспи\" в фильтре при экспорте. ПРОБЛЕМА: обычный экспорт "
            "(файл, который сейчас парсит channel_sales.py) не хранит эту информацию — колонка "
            "\"Канал продажи\" в файле пишет текст \"Каспи\" одинаково для всех 3 городов (в отличие "
            "от Instagram/TikTok, где город прямо в тексте канала), т.к. экспортируется отображаемое "
            "имя канала, а не его ID/optgroup. РЕШЕНИЕ (не сделано): 3 отдельных экспорта — "
            "Аналитика→Товары→Фильтр→Раздел=Коммерческое оборудование→Канал продажи→Каспи (выбрать "
            "именно нужную city-группу)→Excel, по одному на город, затем склеить и проставить city "
            "вручную по источнику файла (так же как сейчас делается city-разбивка для "
            "Instagram/TikTok, только там город виден из текста, а тут — из того, какой файл "
            "скачан). До появления такого 3-файлового экспорта city_transfers для Kaspi-тяжёлых SKU "
            "по-прежнему построены на неверном поле \"Город канала продаж\" и НЕ должны исполняться "
            "как есть — но теперь понятно, что фикс возможен и не требует новых полей от CRM, "
            "только другой способ экспорта.",
            "Статус канала «Айдын Опт» (независимый дилер или связанная структура) не подтверждён",
            "Сверка Kaspi-канала файла с загрузкой сайта отключена: код Kaspi-листинга "
            "(kod, напр. 119264283) и SKU CRM (напр. 9304067) — разные пространства "
            "идентификаторов без моста в текущих данных; включать только после "
            "реального сопоставления SKU↔kod",
            "Тир T2S (сезон закончился) подавляет T1/T2, если ни текущий, ни ближайшие "
            "3 месяца индекса сезонности не показывают восстановления спроса (порог 0.4, "
            "не откалиброван статистически — эмпирический, проверен на фризерах для "
            "мороженого); категориям с историей <6 разных календарных месяцев индекс не "
            "считается вовсе, и подавление не сработает — тир останется как есть",
            "Тир T2M (под заказ) подавляет T1/T2 для категорий с ≥80% SKU без физического "
            "стока (порог из made_to_order_groups, не откалиброван) — suggest_qty=0, "
            "т.к. неизвестен реальный лид-тайм/MOQ поставщика под заказ; mvel_retail "
            "остаётся честным сигналом спроса, просто не переводится в 'купи N шт на склад'",
            "possible_duplicates — SKU с одинаковым названием+ценой+категорией в T1/T2: "
            "может быть реальный вариант (цвет/комплектация не в названии) или "
            "задвоенная карточка в CRM — не различить по имеющимся данным, проверять "
            "вручную перед закупом, иначе есть риск заказать в 2-3 раза больше нужного",
        ],
    }


def calc_category_scope(channel_rows: list[dict], core_categories: set[str],
                         active_t1_categories: Optional[set] = None) -> dict:
    """
    Три уровня охвата (см. документ, разделы 3 и 11):
      - "core": 4 отдела сайта (core_categories — соответствие категория
        CRM -> отдел сайта, передаётся вызывающим кодом).
      - "extended": не core, но входит в кумулятивные CATEGORY_MATERIALITY_CUM_PCT
        (90%) выручки ИЛИ содержит хотя бы один T1_CRITICAL SKU прямо сейчас
        (active_t1_categories — гарантия видимости, см. документ п.11:
        "Рисоварки профессиональные"/"Слайсеры" не должны прятаться только
        из-за низкого места в рейтинге выручки).
      - "tail": всё остальное — видно только через переключатель "Показать всё".

    ВАЖНО: T1-гарантия требует знать тир КАЖДОЙ категории заранее — вызывающий
    код должен сначала посчитать calc_procurement_v2(scope_categories=None)
    по всему каталогу и передать сюда категории с активным T1_CRITICAL;
    охват здесь — фильтр ОТОБРАЖЕНИЯ поверх уже посчитанных данных, а не
    ограничение расчёта. Гистерезис (чтобы категория не мигала между
    загрузками у самой границы 90%) НЕ реализован — открытый пробел, см.
    документ п.14.
    """
    active_t1_categories = active_t1_categories or set()
    rev_by_cat: dict[str, float] = defaultdict(float)
    for r in channel_rows:
        rev_by_cat[r.get("category") or ""] += r.get("revenue") or 0
    total_rev = sum(rev_by_cat.values()) or 1

    ranked = sorted(rev_by_cat.items(), key=lambda x: -x[1])
    cum = 0.0
    scope_by_cat: dict[str, str] = {}
    cum_pct_by_cat: dict[str, float] = {}
    for cat, rev in ranked:
        cum += rev
        cum_pct = cum / total_rev
        cum_pct_by_cat[cat] = round(cum_pct, 3)
        if cat in core_categories:
            scope_by_cat[cat] = "core"
        elif cum_pct <= CATEGORY_MATERIALITY_CUM_PCT or cat in active_t1_categories:
            scope_by_cat[cat] = "extended"
        else:
            scope_by_cat[cat] = "tail"

    return {
        "scope_by_category": scope_by_cat,
        "revenue_by_category": dict(rev_by_cat),
        "cumulative_pct_by_category": cum_pct_by_cat,
    }
