"""
Мост между двумя пространствами идентификаторов товара:

  SKU  — артикул CRM (Zymyran): остатки, продажи по каналам, закуп;
  kod  — код листинга Kaspi: матрицы категорий, доля рынка, ветки.

Зачем: без моста Закуп слеп к рынку. Сверка kaspi_divergence была
отключена 06.08 именно из-за отсутствия моста (см. комментарий в
calc_procurement_v2) — 3/3 проверенных вручную SKU активно продавались на
Kaspi, но получали ложный флаг «нет в Kaspi», потому что kod и SKU просто
никогда не совпадают строково. 11.08 директорский разбор нашёл 6 наших
исчезнувших листингов (~10 млн ₸/мес) — обнаружить такое автоматически
можно только через мост.

Дизайн — та же философия, что tip_classifier.py: НИКОГДА не гадать.
Каждое сопоставление имеет уровень уверенности, всё сомнительное — в
unresolved на ручную разметку. Ошибочный мост хуже отсутствующего: он
тихо портит рекомендации закупа.

Ярусы уверенности (по убыванию):

  sku_in_name   — CRM-артикул (≥6 цифр) буквально встречается токеном в
                  названии Kaspi-листинга. Реальный паттерн наших карточек:
                  «Морозильник Leadbros 9300023 700 л» ↔ SKU 9300023.
                  Практически невозможно получить случайно.
  exact_name    — нормализованные названия (без цветов/регистра/пунктуации)
                  байт-в-байт совпали, и совпадение единственное с обеих
                  сторон.
  unique_token  — модельный токен (буквенно-цифровой, с цифрой, ≥4 знаков,
                  например BCBD217AT из «BC/BD-217AT») встречается ровно у
                  одного SKU и ровно у одного kod. Комбо-карточки CRM
                  («АКЦИЯ! ... + ...») автоматически выпадают из этого
                  яруса: их токен не уникален — и это правильно, комбо не
                  равно одиночному товару.
  token_single_sku — токен указывает ровно на ОДНУ карточку CRM, но на
                  несколько листингов Kaspi. Живой паттерн 11.08: CRM ведёт
                  одну карточку «BC/BD-217AT», Kaspi — отдельные листинги
                  на белый и тёмно-серый; или один физический SRS-262CBP
                  продаётся листингами Leadbros И Muxxed (пере-брендинг).
                  Многие-к-одному здесь корректно: физический сток у всех
                  этих листингов общий. Если у токена несколько карточек
                  CRM ОДНОГО бренда — сначала пробуем развязать цветом.
  token_brand   — токен коллизится между брендами (XINGX и Leadbros выпускают
                  BC/BD375LS), но после фильтра по бренду Kaspi-листинга
                  остаётся ровно одна карточка CRM.
  token_color   — карточки CRM различаются только цветом (BD-378 белый/серый/
                  тёмно-серый), цвет листинга Kaspi однозначно выбирает одну.
  (unresolved)  — ничего из перечисленного. НЕ сопоставляем.

Чисто-Python, без БД — тестируется офлайн на реальных выгрузках, как и
весь engine.py.
"""
import re
from collections import defaultdict

# Цветовые слова — тот же список, что в tip_classifier (расширенный формами
# из CRM-названий); вычищаются перед сравнением названий.
_COLOR_WORDS = {
    "белый", "черный", "чёрный", "серый", "серебристый", "золотистый", "красный",
    "синий", "зеленый", "зелёный", "коричневый", "оранжевый", "бордовый",
    "мультиколор", "темно-серый", "темно‑серый", "голубой", "стальной", "желтый",
    "жёлтый", "розовый", "фиолетовый", "бежевый", "бронзовый", "инокс",
    "бело-черный", "серо-черный", "черно-серый", "белая", "серая", "черная",
    "чёрная",
}

# Токен «похоже на CRM-артикул»: чисто цифровой, 6+ знаков. Короче — риск
# поймать литраж/габарит (700, 1200 и т.п.).
_SKU_LIKE_RE = re.compile(r"^\d{6,}$")


def _norm_name(name: str) -> str:
    tokens = re.split(r"[\s,()]+", str(name or "").strip().lower())
    return " ".join(t for t in tokens if t and t not in _COLOR_WORDS)


def _model_tokens(name: str) -> set:
    """
    Модельные токены из названия: буквенно-цифровые куски с хотя бы одной
    цифрой, слитые через удаление разделителей -/., длиной ≥4. «BC/BD-217AT»
    и «BC/BD217AT» дают одинаковый токен BCBD217AT. Чисто числовые короткие
    куски (литраж «700», год) отбрасываются — они не модель.

    Дополнительно — составной токен «буквы + число»: «BC/BD 601» пишется
    через пробел, по отдельности «BCBD» (без цифры) и «601» (короткое
    число) выпадают, но их пара — полноценное имя модели BCBD601. Пара
    склеивается только когда буквенный кусок (≥2 букв, без цифр) стоит
    НЕПОСРЕДСТВЕННО перед числом из ≥3 цифр — обе стороны моста токенизируются
    одинаково, все гейты уникальности применяются как обычно.
    """
    s = str(name or "").upper()
    # склеиваем конструкции вида BC/BD-217AT в непрерывный токен
    raw = re.findall(r"[A-ZА-Я0-9][A-ZА-Я0-9\-/\.]*", s)
    out = set()
    for t in raw:
        merged = re.sub(r"[\-/\.]", "", t)
        if len(merged) < 4:
            continue
        if not any(ch.isdigit() for ch in merged):
            continue
        if merged.isdigit() and not _SKU_LIKE_RE.match(merged):
            continue  # «1200», «2026» — габарит/год, не модель
        out.add(merged)
    # составные: буквенный кусок + следующее за ним число («BC/BD 601»)
    for m in re.finditer(r"\b([A-Z][A-Z\-/\.]{1,})\s+(\d{3,})\b", s):
        letters = re.sub(r"[\-/\.]", "", m.group(1))
        if len(letters) >= 2 and not any(ch.isdigit() for ch in letters):
            out.add(letters + m.group(2))
    return out


def _color_of(name: str) -> str | None:
    """Первый цвет из названия, нормализованный (ё→е, все виды дефисов и
    пробелы убраны): «темно‑серый» (nb-hyphen), «темно серый» и
    «тёмно-серый» дают одно значение. None — цвета в названии нет."""
    low = str(name or "").lower().replace("ё", "е")
    low = re.sub(r"[\s\-‑–—]+", "", low)
    found = [c for c in _COLOR_KEYS if c in low]
    return max(found, key=len) if found else None


_COLOR_KEYS = sorted({re.sub(r"[\s\-‑–—]+", "", w.replace("ё", "е"))
                      for w in _COLOR_WORDS}, key=len, reverse=True)


def _brand_in_name(brand: str, name: str) -> bool:
    """Бренд присутствует в названии как ЦЕЛОЕ слово (не подстрока: «мороз»
    не должен находиться внутри «морозильник»). Для латиницы допускается
    расхождение написания на 1-2 символа с общего префикса (XING ↔ XINGX —
    реальный кейс: CRM пишет XING, Kaspi — XINGX)."""
    b = str(brand or "").strip().lower().replace("ё", "е")
    if not b:
        return False
    for t in re.findall(r"[a-zа-яё0-9]+", str(name or "").lower().replace("ё", "е")):
        if t == b:
            return True
        if len(b) >= 4 and len(t) >= 4 and abs(len(t) - len(b)) <= 2 \
                and (t.startswith(b) or b.startswith(t)):
            return True
    return False


def _candidate_brand_ok(crm_name: str, kaspi_brand: str,
                        known_brands: set, brand_family: set) -> bool:
    """
    Предохранитель от межбрендовой склейки (найден вживую 11.08: листинг
    конкурента «МОРОЗ BC/BD 217» приклеивался к нашей карточке Leadbros
    через общий модельный токен BCBD217).

    Кандидат CRM допустим, если:
      * бренд листинга Kaspi есть в названии карточки CRM, ИЛИ
      * в названии карточки CRM нет НИ ОДНОГО известного бренда
        (безымянные карточки вида «SRS-262CBP»), ИЛИ
      * оба бренда — из brand_family (наши бренды): пере-брендинг внутри
        семьи реален (SRS-262CBP продаётся и как Leadbros, и как Muxxed,
        физический сток общий).
    Иначе — кандидат отклоняется: чужой бренд в названии карточки при
    несовпадающем бренде листинга это чужой товар, а не вариант нашего.
    """
    kb = str(kaspi_brand or "").strip().lower().replace("ё", "е")
    if not kb:
        return True  # у листинга нет бренда — нечем конфликтовать
    if _brand_in_name(kb, crm_name):
        return True
    conflicting = [b for b in known_brands
                   if b != kb and _brand_in_name(b, crm_name)]
    if not conflicting:
        return True  # карточка безымянная
    fam = {str(b).strip().lower().replace("ё", "е") for b in (brand_family or set())}
    return kb in fam and all(b in fam for b in conflicting)


def build_bridge(crm_items: list[dict], kaspi_items: list[dict],
                 brand_family: set | None = None) -> dict:
    """
    crm_items:   [{sku, name}, ...]  — каталог CRM (остатки/продажи).
    kaspi_items: [{kod, name, brand}, ...] — листинги Kaspi (дедуп по kod).
    brand_family: наши бренды — внутри семьи допускается пере-брендинг
                  (см. _candidate_brand_ok); None = строгий режим.

    Возвращает:
      sku_to_kods: {sku: [{kod, confidence}]}
      kod_to_skus: {kod: [{sku, confidence}]}
      stats: счётчики по ярусам
    Многие-ко-многим сознательно: один листинг Kaspi может соответствовать
    нескольким карточкам CRM (цветовые варианты) и наоборот.
    """
    crm = [{"sku": str(c.get("sku") or "").strip(),
            "name": str(c.get("name") or "").strip()} for c in crm_items]
    crm = [c for c in crm if c["sku"] and c["name"]]
    kaspi = [{"kod": str(k.get("kod") or "").strip(),
              "name": str(k.get("name") or "").strip(),
              "brand": str(k.get("brand") or "").strip()} for k in kaspi_items]
    kaspi = [k for k in kaspi if k["kod"] and k["name"]]

    sku_to_kods: dict[str, list] = defaultdict(list)
    kod_to_skus: dict[str, list] = defaultdict(list)
    stats = {"sku_in_name": 0, "exact_name": 0, "unique_token": 0,
             "token_single_sku": 0, "token_brand": 0, "token_color": 0}
    matched_skus: set = set()
    matched_kods: set = set()

    def _link(sku: str, kod: str, conf: str):
        sku_to_kods[sku].append({"kod": kod, "confidence": conf})
        kod_to_skus[kod].append({"sku": sku, "confidence": conf})
        stats[conf] += 1
        matched_skus.add(sku)
        matched_kods.add(kod)

    # ── Ярус 0: CRM-артикул внутри названия Kaspi ────────────────────────
    crm_by_sku = {c["sku"]: c for c in crm}
    for k in kaspi:
        for tok in _model_tokens(k["name"]):
            if _SKU_LIKE_RE.match(tok) and tok in crm_by_sku:
                _link(tok, k["kod"], "sku_in_name")

    # ── Ярус 1: точное совпадение нормализованного названия ──────────────
    crm_by_norm: dict[str, list] = defaultdict(list)
    for c in crm:
        crm_by_norm[_norm_name(c["name"])].append(c["sku"])
    kaspi_by_norm: dict[str, list] = defaultdict(list)
    for k in kaspi:
        kaspi_by_norm[_norm_name(k["name"])].append(k["kod"])
    for nn, skus in crm_by_norm.items():
        kods = kaspi_by_norm.get(nn)
        if not nn or not kods:
            continue
        if len(skus) == 1 and len(kods) == 1:
            sku, kod = skus[0], kods[0]
            if kod not in {m["kod"] for m in sku_to_kods.get(sku, [])}:
                _link(sku, kod, "exact_name")

    # ── Ярусы 2-5: модельные токены ──────────────────────────────────────
    crm_by_token: dict[str, set] = defaultdict(set)
    for c in crm:
        for tok in _model_tokens(c["name"]):
            crm_by_token[tok].add(c["sku"])
    kaspi_by_token: dict[str, set] = defaultdict(set)
    for k in kaspi:
        for tok in _model_tokens(k["name"]):
            kaspi_by_token[tok].add(k["kod"])
    kaspi_by_kod = {k["kod"]: k for k in kaspi}
    known_brands = {str(k["brand"]).strip().lower().replace("ё", "е")
                    for k in kaspi if k.get("brand") and len(str(k["brand"]).strip()) >= 4}

    def _already(sku, kod):
        return kod in {m["kod"] for m in sku_to_kods.get(sku, [])}

    for tok, skus in crm_by_token.items():
        kods = kaspi_by_token.get(tok)
        if not kods:
            continue
        if _SKU_LIKE_RE.match(tok):
            continue  # чистые артикулы обработаны ярусом 0

        crm_cands_all = [crm_by_sku[s] for s in skus if s in crm_by_sku]

        for kod in kods:
            k = kaspi_by_kod[kod]
            kbrand = (k.get("brand") or "").strip().lower()
            kcolor = _color_of(k["name"])

            # Брендовый предохранитель — на ВСЕ токен-ярусы разом.
            cands = [c for c in crm_cands_all
                     if _candidate_brand_ok(c["name"], kbrand,
                                            known_brands, brand_family or set())]
            if not cands:
                continue

            # Ярус 2/3: единственная допустимая карточка CRM. Если при этом
            # kod единственный на токен — unique_token; если листингов
            # несколько (цвета/пере-брендинг одной карточки) —
            # token_single_sku: физический сток у них общий.
            if len(cands) == 1:
                conf = "unique_token" if (len(kods) == 1 and len(crm_cands_all) == 1) \
                       else "token_single_sku"
                if not _already(cands[0]["sku"], kod):
                    _link(cands[0]["sku"], kod, conf)
                continue

            # Ярус 4: буквальный фильтр по бренду листинга (токен коллизится
            # между брендами — XINGX и Leadbros выпускают BC/BD375LS).
            if kbrand:
                brand_hits = [c for c in cands if _brand_in_name(kbrand, c["name"])]
                if len(brand_hits) == 1:
                    if not _already(brand_hits[0]["sku"], kod):
                        _link(brand_hits[0]["sku"], kod, "token_brand")
                    continue
                cands_after_brand = brand_hits if brand_hits else cands
            else:
                cands_after_brand = cands

            # Ярус 5: развязка цветом (карточки CRM — цветовые варианты).
            if kcolor:
                color_hits = [c for c in cands_after_brand
                              if _color_of(c["name"]) == kcolor]
                if len(color_hits) == 1:
                    if not _already(color_hits[0]["sku"], kod):
                        _link(color_hits[0]["sku"], kod, "token_color")

    stats["skus_matched"] = len(matched_skus)
    stats["kods_matched"] = len(matched_kods)
    stats["skus_total"] = len(crm)
    stats["kods_total"] = len(kaspi)
    return {"sku_to_kods": dict(sku_to_kods), "kod_to_skus": dict(kod_to_skus),
            "stats": stats}
