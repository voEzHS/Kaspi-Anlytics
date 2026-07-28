"""
Auto-classifies the 'Тип' (subtype) field for uploads whose source no longer
provides it — e.g. the algatop refrigerated-department export, which used to
include a hand-picked Тип column and now only sends a generic category.

Design goal: NEVER guess. Every row is resolved with hard evidence from
historical data already sitting in our own database (rows previously
hand-classified by the team), or left unresolved for a human to decide.
An unresolved row is far better than a wrong one — a wrong Тип silently
corrupts market-share and vetka numbers for that whole subtype.

Confidence tiers, in order — pure Python, no DB access (the caller queries
historical (kod, tip, name, brand) rows and passes them in):

  1. exact_kod          — this exact Kaspi article code has a recorded Тип
                           before. It's the same physical product listing.
                           Always trusted.
  2. exact_name_nocolor — the product name, with color words stripped, is
                           byte-identical to a historical name (i.e. same
                           product, different color variant), AND every
                           historical row with that stripped name agrees on
                           one Тип. Validated via leave-one-out cross-
                           validation on 336 historical rows: 100% accuracy.
  3. brand_prefix        — brand + the single most distinctive model token
                           (first non-color word of the name) matches a
                           historical group with >=4 members, all agreeing
                           on one Тип. Also validated at 100% accuracy via
                           leave-one-out testing at this threshold — looser
                           thresholds (fewer required hits, or 2 tokens
                           instead of 1) measured lower (90-97%) and are
                           deliberately NOT used.
  4. unresolved          — no reliable signal. Left blank for manual review.

Tune MIN_PREFIX_HITS only after re-running the leave-one-out validation in
this module's test (see kaspi-backend/scripts or ask before changing it) —
lowering it trades away verified accuracy for coverage.
"""
import re
from collections import Counter, defaultdict

MIN_PREFIX_HITS = 4

COLOR_WORDS = {
    "белый", "черный", "чёрный", "серый", "серебристый", "золотистый", "красный",
    "синий", "зеленый", "зелёный", "коричневый", "оранжевый", "бордовый",
    "мультиколор", "темно-серый", "темно‑серый", "голубой", "стальной", "желтый",
    "жёлтый", "розовый", "фиолетовый",
}


def _norm_name(name: str) -> str:
    tokens = re.split(r"[\s,]+", (name or "").strip().lower())
    return " ".join(t for t in tokens if t and t not in COLOR_WORDS)


def _prefix_key(brand: str, name: str) -> str:
    b = (brand or "").strip().upper()
    tokens = re.split(r"[\s,]+", (name or "").strip())
    tokens = [t for t in tokens if t.upper() != b]
    model_tokens = [t for t in tokens if t.lower() not in COLOR_WORDS][:1]
    return b + "|" + " ".join(t.upper() for t in model_tokens)


class TipClassifier:
    """Build once per (department) from historical (kod, tip, name, brand) rows,
    then call .classify(kod, name, brand) for each row missing a Тип."""

    def __init__(self, historical: list[tuple[str, str, str, str]]):
        self.by_kod: dict[str, Counter] = defaultdict(Counter)
        self.by_name: dict[str, Counter] = defaultdict(Counter)
        self.by_prefix: dict[str, Counter] = defaultdict(Counter)
        for kod, tip, name, brand in historical:
            if not tip:
                continue
            if kod:
                self.by_kod[kod][tip] += 1
            self.by_name[_norm_name(name)][tip] += 1
            self.by_prefix[_prefix_key(brand, name)][tip] += 1

    def classify(self, kod: str, name: str, brand: str) -> tuple[str | None, str]:
        """Returns (tip_or_None, tier_name)."""
        if kod and kod in self.by_kod:
            return self.by_kod[kod].most_common(1)[0][0], "exact_kod"

        nn = _norm_name(name)
        c = self.by_name.get(nn)
        if c and len(c) == 1:
            return next(iter(c)), "exact_name_nocolor"

        pk = _prefix_key(brand, name)
        c2 = self.by_prefix.get(pk)
        if c2 and len(c2) == 1 and sum(c2.values()) >= MIN_PREFIX_HITS:
            return next(iter(c2)), "brand_prefix"

        return None, "unresolved"


def classify_rows(rows: list[dict], historical: list[tuple[str, str, str, str]]) -> dict:
    """
    Mutates `rows` in place: sets row['tip'] for every row currently missing
    one, wherever the classifier can resolve it with evidence.

    rows: parsed upload rows (dicts with at least kod/name/brand/tip keys).
    historical: (kod, tip, name, brand) tuples already in the DB for this
    department, with tip already set (the ground truth to learn from).

    Returns a summary: counts per tier + the list of rows still unresolved
    (same dicts as in `rows`, so the caller can report kod/name/brand/revenue).
    """
    clf = TipClassifier(historical)
    summary = {"exact_kod": 0, "exact_name_nocolor": 0, "brand_prefix": 0, "unresolved": []}

    for row in rows:
        if row.get("tip"):
            continue  # already has a Тип from the source file — leave it alone
        tip, tier = clf.classify(row.get("kod", ""), row.get("name", ""), row.get("brand", ""))
        if tip:
            row["tip"] = tip
            summary[tier] += 1
        else:
            summary["unresolved"].append(row)

    return summary
