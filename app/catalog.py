"""Katalog: CSV fayl yoki Google Sheets'dan mahsulotlarni yuklash."""
from __future__ import annotations

import csv
import io
import logging
import re

import httpx

from .db import DB

log = logging.getLogger("catalog")

# CSV ustun nomlari (o'zbekcha yoki inglizcha bo'lishi mumkin)
COLUMNS = {
    "name": ("nomi", "nom", "mahsulot", "name"),
    "category": ("kategoriya", "toifa", "category"),
    "price": ("narx", "narxi", "price"),
    "price_note": ("narx_izoh", "birlik", "price_note"),
    "description": ("tavsif", "izoh", "description"),
    "variants": ("variantlar", "rang", "ranglar", "variants"),
    "in_stock": ("mavjud", "bor", "in_stock"),
    "keywords": ("kalit_sozlar", "kalit so'zlar", "keywords"),
    "template": ("shablon", "javob_shabloni", "template"),
    "post_ids": ("post_idlar", "postlar", "post_ids"),
}

CSV_TEMPLATE = (
    "nomi,kategoriya,narx,narx_izoh,tavsif,variantlar,mavjud,kalit_sozlar,shablon,post_idlar\n"
    "Saodat asri,Kitoblar,269000,to'plam,\"I, II, III va IV kitoblar bitta to'plamda\",,ha,\"saodat, 4 kitob\","
    "\"«Saodat asri» 4 kitobli to'plam — 269 000 so'm. Oldindan to'lov yo'q.\",\n"
)


def format_price(price: int | None) -> str:
    return f"{price:,}".replace(",", " ") + " so'm" if price else "narxi ko'rsatilmagan"


def parse_price(value: str | None) -> int | None:
    """"149 000", "149,000 so'm", "149000.00", "149 000,50" → 149000."""
    text = (value or "").strip()
    text = re.sub(r"[.,]\d{1,2}(?!\d)\s*(so'm|сум|sum|uzs)?\s*$", "", text, flags=re.I)  # tiyinlarni tashlaymiz
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def parse_bool(value: str | None) -> int:
    return 0 if (value or "").strip().lower() in ("yo'q", "yoq", "no", "0", "false", "tugagan", "нет") else 1


def _pick(row: dict, field: str) -> str:
    lowered = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
    for alias in COLUMNS[field]:
        if alias in lowered:
            return lowered[alias]
    return ""


def import_csv(db: DB, text: str, source: str = "csv") -> tuple[int, list[str]]:
    """CSV matnini o'qib, mahsulotlarni qo'shadi/yangilaydi. (soni, xatolar) qaytaradi."""
    text = text.lstrip("﻿")
    sample = text[:2000]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    count, errors = 0, []
    for i, row in enumerate(reader, start=2):
        name = _pick(row, "name")
        if not name:
            continue
        try:
            posts = [p.strip() for p in re.split(r"[,\s]+", _pick(row, "post_ids")) if p.strip()]
            db.upsert_product(
                name=name,
                category=_pick(row, "category") or "Umumiy",
                price=parse_price(_pick(row, "price")),
                price_note=_pick(row, "price_note"),
                description=_pick(row, "description"),
                variants=_pick(row, "variants"),
                in_stock=parse_bool(_pick(row, "in_stock")),
                keywords=_pick(row, "keywords"),
                template=_pick(row, "template"),
                post_ids=posts,
                source=source,
            )
            count += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{i}-qator: {e}")
    return count, errors


def sheets_csv_url(url: str) -> str:
    """Google Sheets havolasini CSV eksport havolasiga aylantiradi."""
    m = re.search(r"docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if m and "export" not in url and "output=csv" not in url:
        gid = re.search(r"[#&?]gid=(\d+)", url)
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")
    return url


async def sync_from_url(db: DB, url: str) -> tuple[int, list[str]]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        r = await http.get(sheets_csv_url(url))
        r.raise_for_status()
    count, errors = import_csv(db, r.text, source="sheets")
    log.info("Katalog yangilandi: %d mahsulot", count)
    return count, errors
