"""Остатки CRM (torgstore.zymyran.com, "Экспорт товаров с надстройками") —
загрузка снимка склада, используется вкладкой «Закуп».

Формат подтверждён вживую 05.08.2026 (см. CRM_Logistika_Sklady.md и Замечание
7 в истории Plan_Zakupa): колонки — SKU, Название, Статус, Цена, 4 продаваемых
на Kaspi склада (Первомай/Астана/Шымкент/Туздыбастау), плюс произвольный набор
"пайплайн"-столбцов (YMC-хабы, 2_ordered, 3_left_factory — набор и порядок
может меняться от выгрузки к выгрузке), плюс MAS/Общий объём — это НЕ сток
(товарная характеристика, объём/литраж), не читаем их вообще.
"""
import os
from datetime import datetime, timezone
from typing import Optional

import openpyxl
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import StockRow, StockUpload
from app.routers.uploads import require_admin, require_admin_or_ingest, MAX_UPLOAD_BYTES

router = APIRouter(prefix="/api/v1/stock", tags=["stock"])

# Явно опознаваемые колонки. Всё, что не сюда — считается пайплайном
# (см. ymc_transit) и суммируется, КРОМЕ явно исключённых ниже.
_COL_SKU = "sku"
_COL_NAME = "название"
_COL_STATUS = "статус"
_COL_PRICE = "цена"
# ⚠ ТОЧНЫЕ имена, НЕ подстроки. Причина — инцидент 17.08.2026:
# в выгрузке есть «Дефекты (Хаб Первомай)», «Уценка Астана», «Ремонт Шымкент»,
# «Шымкент Витрина Уценка» и т.п. При поиске по подстроке каждая следующая
# такая колонка ПЕРЕЗАПИСЫВАЛА настоящий склад, и в остаток попадал брак вместо
# товара. По SKU 8300341 вместо 244 шт читалось 3. Ошибка была во всех снимках
# с самого начала и никак не проявлялась внешне.
_COL_WH = {
    "хаб первомай": "wh_pervomay",
    "склад астана": "wh_astana",
    "склад шымкент": "wh_shymkent",
    "склад туздыбастау": "wh_tuzdybastau",
}
# "ordered" встречается в латинице внутри заголовка "2_ordered (已订购, заказаны)"
_ORDERED_HINT = "ordered"

# ─────────────────────────────────────────────────────────────────────────────
# ПОЧЕМУ ЗДЕСЬ БЕЛЫЙ СПИСОК, А НЕ ЧЁРНЫЙ (инцидент 17.08.2026)
#
# Раньше логика была «всё, что не опознано — это пайплайн, суммируем».
# 14.08 в выгрузку добавили колонки габаритов (Вес/Длина/Ширина/Высота/
# Глубина) — парсер молча сложил сантиметры и килограммы в «товар в пути».
# Результат: по SKU 9300637 «в пути» стало 2934 шт при реальном нуле, снимок
# остатков целиком стал негодным, и построенный на нём план закупа на 15,9
# млн ₸ пришлось отменить. Ни одна проверка этого не поймала, потому что
# числа выглядели правдоподобно.
#
# Теперь наоборот: суммируется ТОЛЬКО то, что опознано как склад или транзит.
# Незнакомая колонка не портит цифры — она попадает в отчёт unknown_columns,
# чтобы её заметили и осознанно классифицировали.
# ─────────────────────────────────────────────────────────────────────────────

# Транзит/пайплайн: китайские хабы YMC и статусы производства.
_TRANSIT_HINTS = (
    "gs-cai", "dlt-ylh", "gs-xx", "gs-xz", "sk-ylh",   # YMC-подсклады
    "d-169946", "d-225604", "d-444508", "d-981812",    # прочие YMC-коды
    "left_factory", "出厂", "ordered", "已订购",
    "ymc",
)

# Прочие склады Казахстана — реальный товар, но Kaspi их не видит.
# Суммируются в ymc_transit как «есть, но не продаётся на Kaspi».
_OTHER_WH_HINTS = (
    "витрина", "запчаст", "ремонт", "списан", "дефект", "свх",
    "возврат", "уценка", "midou", "цех", "магазин", "туздыбастау",
)

# Явно НЕ сток — характеристики товара и служебные поля. Не суммируем никуда.
_EXCLUDE_HINTS = (
    "объем", "объём", "mas",
    "вес", "длина", "ширина", "высота", "глубина", "габарит",  # ← причина инцидента 17.08
    "supplier", "поставщик", "отзыв", "ссылк", "link", "url",
    "рейтинг", "бренд", "категор", "артикул", "фото", "изображ",
)

# Если «в пути» превышает сток более чем во столько раз при непустом стоке —
# почти наверняка в транзит попало что-то посторонее (как габариты 17.08).
_TRANSIT_SANITY_RATIO = 20.0


def _norm(s) -> str:
    return str(s or "").strip().lower()


def _num(v) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (ValueError, TypeError):
        return 0.0


def parse_stock_excel(filepath: str) -> tuple[list[dict], dict]:
    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows_raw = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows_raw:
        return [], {"unknown_columns": [], "warnings": ["Файл пустой"]}

    # Заголовок почти всегда строка 1, но на всякий случай ищем строку с "sku"
    header_idx = 0
    for i, row in enumerate(rows_raw[:5]):
        if any(_norm(c) == "sku" for c in row):
            header_idx = i
            break
    headers = rows_raw[header_idx]

    col_sku = col_name = col_status = col_price = col_ordered = None
    col_wh: dict[str, int] = {}
    transit_cols: list[int] = []
    unknown_cols: list[str] = []

    for idx, h in enumerate(headers):
        n = _norm(h)
        if not n:
            continue
        if n == _COL_SKU:
            col_sku = idx
        elif _COL_NAME in n:
            col_name = idx
        elif _COL_STATUS in n:
            col_status = idx
        elif _COL_PRICE in n:
            col_price = idx
        elif n in _COL_WH:                      # ТОЧНОЕ совпадение, см. комментарий выше
            col_wh[_COL_WH[n]] = idx
        elif n.startswith("2_") and _ORDERED_HINT in n:
            col_ordered = idx
        elif any(k in n for k in _EXCLUDE_HINTS):
            continue  # характеристика товара — не сток, никуда не суммируем
        elif any(k in n for k in _TRANSIT_HINTS) or any(k in n for k in _OTHER_WH_HINTS):
            transit_cols.append(idx)          # опознанный транзит / прочий склад
        else:
            unknown_cols.append(str(h).strip())  # НЕ суммируем — только сообщаем

    if col_sku is None:
        raise ValueError("Колонка SKU не найдена в файле")
    if not col_wh:
        raise ValueError(
            "Ни одна колонка Kaspi-склада не распознана (ожидались Первомай / "
            "Астана / Шымкент). Формат выгрузки изменился — снимок не принят."
        )

    result = []
    for row in rows_raw[header_idx + 1:]:
        if not row or col_sku >= len(row):
            continue
        sku_raw = row[col_sku]
        if sku_raw is None or str(sku_raw).strip() == "":
            continue
        sku = str(sku_raw).strip().upper()

        def get(i):
            return row[i] if i is not None and i < len(row) else None

        transit_sum = sum(_num(get(i)) for i in transit_cols)

        result.append({
            "sku": sku,
            "name": str(get(col_name) or "").strip(),
            "status": str(get(col_status) or "").strip(),
            "price": _num(get(col_price)),
            "wh_pervomay": _num(get(col_wh.get("wh_pervomay"))),
            "wh_astana": _num(get(col_wh.get("wh_astana"))),
            "wh_shymkent": _num(get(col_wh.get("wh_shymkent"))),
            "wh_tuzdybastau": _num(get(col_wh.get("wh_tuzdybastau"))),
            "ymc_transit": transit_sum,
            "ordered": _num(get(col_ordered)),
        })

    # ── санитарный контроль снимка ───────────────────────────────────────────
    warnings: list[str] = []
    if unknown_cols:
        warnings.append(
            "Нераспознанные колонки НЕ учтены в остатках: "
            + ", ".join(unknown_cols[:12])
            + (f" (и ещё {len(unknown_cols)-12})" if len(unknown_cols) > 12 else "")
            + ". Если это склад или транзит — добавьте подсказку в _TRANSIT_HINTS."
        )
    tot_stock = sum(r["wh_pervomay"] + r["wh_astana"] + r["wh_shymkent"] for r in result)
    tot_transit = sum(r["ymc_transit"] for r in result)
    if tot_stock > 0 and tot_transit > tot_stock * _TRANSIT_SANITY_RATIO:
        raise ValueError(
            f"Транзит ({tot_transit:.0f} шт) превышает остаток на Kaspi-складах "
            f"({tot_stock:.0f} шт) более чем в {_TRANSIT_SANITY_RATIO:.0f} раз. "
            "Почти наверняка в транзит попала посторонняя колонка (так 17.08.2026 "
            "туда сложились габариты). Снимок не принят."
        )
    frac = [r for r in result if r["ymc_transit"] and abs(r["ymc_transit"] - round(r["ymc_transit"])) > 1e-6]
    if frac:
        warnings.append(
            f"У {len(frac)} позиций дробное количество «в пути» — штуки дробными не бывают. "
            "Вероятно, в транзит попала колонка с измерением (вес/объём). Примеры: "
            + ", ".join(f'{r["sku"]}={r["ymc_transit"]}' for r in frac[:5])
        )
    diag = {
        "unknown_columns": unknown_cols,
        "warehouses_recognized": sorted(col_wh.keys()),
        "transit_columns_count": len(transit_cols),
        "total_kaspi_stock": tot_stock,
        "total_transit": tot_transit,
        "warnings": warnings,
    }
    return result, diag


@router.post("/", summary="Загрузить снимок остатков (заменяет предыдущий целиком)")
async def upload_stock(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_admin_or_ingest),
):
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Файл слишком большой (максимум {MAX_UPLOAD_BYTES // 1024 // 1024} МБ)")

    import tempfile
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        parsed, diag = parse_stock_excel(tmp_path)
    except Exception as e:
        raise HTTPException(422, f"Ошибка разбора файла: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if not parsed:
        raise HTTPException(422, "В файле не найдено строк с данными")

    # Остатки — не накопительные (это состояние "на сейчас"), поэтому старый
    # снимок удаляется целиком перед вставкой нового, а не суммируется с ним.
    old_uploads = (await db.execute(select(StockUpload))).scalars().all()
    for u in old_uploads:
        await db.delete(u)
    await db.flush()

    upload = StockUpload(filename=file.filename, row_count=len(parsed))
    db.add(upload)
    await db.flush()

    CHUNK = 500
    for i in range(0, len(parsed), CHUNK):
        chunk = parsed[i: i + CHUNK]
        db.add_all([StockRow(upload_id=upload.id, **row) for row in chunk])

    await db.commit()
    await db.refresh(upload)

    return {
        "id": upload.id,
        "filename": upload.filename,
        "row_count": upload.row_count,
        "created_at": upload.created_at.isoformat(),
        "replaced_previous": len(old_uploads) > 0,
        "diagnostics": diag,
    }


@router.get("/", summary="Текущий снимок остатков (метаданные)")
async def get_stock_status(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(StockUpload).order_by(StockUpload.created_at.desc()))
    upload = result.scalars().first()
    if not upload:
        return {"loaded": False}
    return {
        "loaded": True,
        "id": upload.id,
        "filename": upload.filename,
        "row_count": upload.row_count,
        "created_at": upload.created_at.isoformat(),
    }


@router.delete("/", summary="Удалить текущий снимок остатков")
async def delete_stock(db: AsyncSession = Depends(get_db), _: None = Depends(require_admin)):
    old_uploads = (await db.execute(select(StockUpload))).scalars().all()
    for u in old_uploads:
        await db.delete(u)
    await db.commit()
    return {"deleted": len(old_uploads)}
