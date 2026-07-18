"""Analytics calculation engine — pure Python, no DB queries here."""


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


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

    for r in rows:
        k = r["vetka"] or "—"
        if k not in vmap:
            vmap[k] = {
                "vetka": k, "revenue": 0.0, "units": 0.0,
                "sku_keys": set(), "brands": set(), "rrcs": [], "our_rev": 0.0,
                "our_sku_keys": set(),
                # track leader: brand+sku with highest revenue
                "_leader_rev": 0.0, "_leader_brand": "", "_leader_kod": "", "_leader_name": "",
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
            v["our_sku_keys"].add(sku_key(r))
        # Track the single highest-revenue SKU (any brand) for direct Kaspi link
        row_rev = r["revenue"] or 0
        if row_rev > v["_leader_rev"]:
            v["_leader_rev"] = row_rev
            v["_leader_brand"] = r["brand"] or ""
            v["_leader_kod"] = r["kod"] or ""
            v["_leader_name"] = r["name"] or ""

    result = []
    for v in vmap.values():
        result.append({
            "vetka": v["vetka"],
            "revenue": round(v["revenue"], 2),
            "units": round(v["units"]),
            "skus": len(v["sku_keys"]),
            "our_skus": len(v["our_sku_keys"]),
            "brands": len(v["brands"]),
            "avg_rrc": round(safe_div(sum(v["rrcs"]), len(v["rrcs"])), 2),
            "market_share_pct": round(safe_div(v["revenue"], total_rev) * 100, 2),
            "our_revenue": round(v["our_rev"], 2),
            "our_share_pct": round(safe_div(v["our_rev"], v["revenue"]) * 100, 2),
            "leader_brand": v["_leader_brand"],
            "leader_kod": v["_leader_kod"],
            "leader_name": v["_leader_name"],
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
    import re
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

    result = sorted(months.values(), key=lambda x: x["month"])
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
    import math
    from collections import defaultdict

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
    from collections import defaultdict

    if not rows:
        return {}

    # Sort months canonically
    MONTH_ORDER = ["Январь","Февраль","Март","Апрель","Май","Июнь",
                   "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]

    raw_months = sorted(
        {r["month"] for r in rows if r["month"]},
        key=lambda m: MONTH_ORDER.index(m) if m in MONTH_ORDER else 99,
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
        insights.append({"type": "info", "text": f"Пик рынка — {peak['month']} ({peak['revenue']/1e6:.0f} млн ₸). "
                         f"Апрельский пик типичен для сезона охлаждения."})
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
    from collections import defaultdict
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
