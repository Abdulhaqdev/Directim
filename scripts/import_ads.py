"""Meta Ad Library'dan yig'ilgan reklamalarni bazaga yozadi va Excel hisobot chiqaradi.

Ishlatish:
    .venv/bin/python scripts/import_ads.py config/ads/ilmbooks_ads.json [--out hisobot.xlsx]

JSON tuzilishi: config/ads/ilmbooks_ads.json ga qarang ("pages" — reklama beruvchi Facebook sahifalar,
"products" — reklama olib boradigan sahifalar (landing), "ads" — [ad_id, sahifa, sana, variantlar, products kaliti]).

Har bir reklama sahifasidan (landing) mahsulot nomi, narxi, eski narxi, rasmi va tavsifi olinadi:
- ilimbooks.uz — sahifaning ochiq API'si: /api/public/landing?path=/<slug>
- boshqa saytlar (creatium va h.k.) — HTML matnidan «ASL narxi» / «Aksiya Narxda».
Mahsulot katalogga (products) yoziladi, reklama esa ads jadvaliga mahsulotga bog'lanib saqlanadi.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import DB  # noqa: E402
from app.settings import Env  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130 Safari/537.36"}

# Boshqa saytlardagi xuddi shu mahsulotni topish uchun nom bo'lagi (taqqoslash ustuni uchun)
MATCH_HINTS = {
    "mukoshafatul-qulub": ["ukoshaf"],
    "100-tavsiya-erkak-ayol-ab": ["100 tavsiya"],
    "qutul-qulub-ab": ["utul qulub"],
    "katta-tajvidli-musaf-quroni-karim": ["tajvid"],
    "soyali-quron-ab": ["soyali qur"],
    "qashqirlar-makoni-ab": ["ashqirlar"],
    "ustoz-joynamoz-qt-ab": ["ustoz joynamoz", "ustoz"],
    "uzukli-tasbeh-ab": ["tasbeh", "zikr ring"],
    "pul-sarflash-sanati-ab": ["pul sarflash"],
    "uycha-palatka-ab": ["palatka"],
    "arencia-vitamin-c-ab": ["vitamin c"],
    "collagen-ampula-ab": ["ampula", "farmstay"],
    "collagen-shampon-ab": ["shampun", "shampon"],
    "rivojlantiruvchi-bolalar-kovrigi-ab": ["gilamcha", "kovrik"],
}


def money(text: str | None) -> int | None:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def landing_ilimbooks(url: str, cache_dir: Path) -> dict:
    slug = urlparse(url).path.strip("/")
    cached = cache_dir / f"api_{slug}.json"
    if cached.exists():
        data = json.loads(cached.read_text(encoding="utf-8"))
    else:
        r = httpx.get(f"https://ilimbooks.uz/api/public/landing", params={"path": f"/{slug}"}, headers=UA, timeout=30)
        r.raise_for_status()
        data = r.json()
        cached.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    c = data["content"]
    parts = []
    if c.get("subtitle"):
        parts.append(c["subtitle"])
    if c.get("benefits"):
        parts.append((c.get("benefitsTitle") or "Afzalliklari") + ":")
        parts += [f"- {b.get('title', '').strip()}: {b.get('text', '').strip()}" for b in c["benefits"]]
    if c.get("problems"):
        parts += [f"- {p.get('title', p) if isinstance(p, dict) else p}" for p in c["problems"]]
    if c.get("bundle"):
        parts.append("To'plamda: " + "; ".join(b.get("title", str(b)) if isinstance(b, dict) else str(b)
                                               for b in c["bundle"]))
    if c.get("faq"):
        parts.append("Savol-javob:")
        parts += [f"- {f.get('q', '').strip()} — {f.get('a', '').strip()}" for f in c["faq"]]
    if c.get("guaranteeText"):
        parts.append(c["guaranteeText"])
    image = (c.get("images") or [""])[0]
    return {
        "name": c.get("title", "").strip(),
        "price": money(c.get("priceNew")),
        "old_price": money(c.get("priceOld")),
        "description": "\n".join(parts),
        "image_url": f"https://ilimbooks.uz{image}" if image.startswith("/") else image,
    }


def landing_html(url: str, cache_dir: Path) -> dict:
    name = urlparse(url).netloc.replace(".", "_") + "_" + urlparse(url).path.strip("/").replace("/", "_") + ".html"
    cached = cache_dir / name
    if cached.exists():
        html = cached.read_text(encoding="utf-8", errors="ignore")
    else:
        r = httpx.get(url, headers=UA, timeout=30, follow_redirects=True)
        r.raise_for_status()
        html = r.text
        cached.write_text(html, encoding="utf-8")
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html)
    text = re.sub(r"<[^>]+>", " ", text).replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    old = re.search(r"ASL narxi\s*:?\s*([\d\s]+)", text, re.I)
    new = re.search(r"Aksiya\s+Narxda\s*:?\s*([\d\s]+)", text, re.I)
    og_img = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
    name = (title.group(1) if title else "").split("—")[0].strip()
    return {"name": name, "price": money(new.group(1)) if new else None,
            "old_price": money(old.group(1)) if old else None,
            "description": "", "image_url": og_img.group(1) if og_img else ""}


def fetch_landing(url: str, cache_dir: Path) -> dict:
    if urlparse(url).netloc.endswith("ilimbooks.uz"):
        return landing_ilimbooks(url, cache_dir)
    return landing_html(url, cache_dir)


def other_site_offers(db: DB, key: str, own_ids: set[int]) -> list[dict]:
    hints = MATCH_HINTS.get(key, [])
    seen, out = set(), []
    for hint in hints:
        for row in db._all("SELECT id, name, price, old_price, source, source_url FROM products "
                           "WHERE lower(name) LIKE ? AND in_stock=1", (f"%{hint.lower()}%",)):
            if row["id"] in own_ids or row["id"] in seen:
                continue
            seen.add(row["id"])
            out.append(row)
        if out:
            break
    return out


def normalize_ads(spec: dict) -> list[dict]:
    """ads: [id, sana, variantlar, kalit] (bitta sahifa — spec["page"]) yoki [id, sahifa, sana, variantlar, kalit]."""
    default_page = (spec.get("page") or {}).get("name", "")
    out = []
    for row in spec["ads"]:
        if len(row) == 4:
            ad_id, started, variants, key = row
            page = default_page
        else:
            ad_id, page, started, variants, key = row
        out.append({"id": ad_id, "page": page, "start": started, "variants": int(variants), "key": key})
    return out


def pages_of(spec: dict) -> dict[str, dict]:
    pages = dict(spec.get("pages") or {})
    if spec.get("page"):
        pages.setdefault(spec["page"]["name"], spec["page"])
    return pages


def base_key(spec: dict, key: str) -> str:
    return spec["products"][key].get("same_as") or key


def run(json_path: Path, out_path: Path | None) -> dict:
    spec = json.loads(json_path.read_text(encoding="utf-8"))
    cache_dir = ROOT / "data" / "ads"  # yuklab olingan sahifalar keshi (git'ga kirmaydi)
    cache_dir.mkdir(parents=True, exist_ok=True)
    db = DB(Env().db_path)
    pages = pages_of(spec)
    collected = spec.get("collected_at", "")

    product_ids: dict[str, int] = {}
    landings: dict[str, dict] = {}
    for key, p in spec["products"].items():
        if p.get("same_as"):
            continue
        info = fetch_landing(p["landing"], cache_dir)
        info["price"] = info["price"] or p.get("ad_price")
        info["old_price"] = info["old_price"] or p.get("ad_old_price")
        if not info["description"]:
            info["description"] = p.get("ad_text", "")
        landings[key] = info
        source = "reklama:" + urlparse(p["landing"]).netloc
        db.upsert_scraped(source_url=p["landing"], name=info["name"] or key, category=p["category"],
                          price=info["price"], old_price=info["old_price"], description=info["description"],
                          image_url=info["image_url"], source=source)
        row = db._one("SELECT id FROM products WHERE source_url=?", (p["landing"],))
        product_ids[key] = row["id"]
        db.update_product(row["id"], price_note="reklamadagi narx")
    for key, p in spec["products"].items():
        if p.get("same_as"):
            product_ids[key] = product_ids[p["same_as"]]
            landings[key] = fetch_landing(p["landing"], cache_dir)

    seen: dict[str, set] = {}
    for ad in normalize_ads(spec):
        p = spec["products"][ad["key"]]
        base = spec["products"][base_key(spec, ad["key"])]
        page = pages.get(ad["page"], {"page_id": ad["page"]})
        seen.setdefault(page["page_id"], set()).add(ad["id"])
        db.upsert_ad(ad_id=ad["id"], page_id=page["page_id"], page_name=ad["page"], instagram=page.get("instagram"),
                     started_at=ad["start"], variants=ad["variants"], landing_url=p["landing"],
                     product_id=product_ids[ad["key"]], ad_text=p.get("ad_text") or base.get("ad_text"),
                     ad_price=p.get("ad_price") or base.get("ad_price"),
                     ad_old_price=p.get("ad_old_price") or base.get("ad_old_price"), active=1, collected_at=collected)
    stopped = sum(db.deactivate_missing_ads(pid, ids) for pid, ids in seen.items())

    summary = {"products": len(set(product_ids.values())), "ads": sum(len(v) for v in seen.values()),
               "pages": len(seen), "stopped": stopped}
    if out_path:
        export_xlsx(db, spec, product_ids, landings, out_path)
        summary["xlsx"] = str(out_path)
    return summary


def _fmt(n) -> str:
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else ""


def export_xlsx(db: DB, spec: dict, product_ids: dict[str, int], landings: dict, out_path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    own = set(product_ids.values())
    ads = normalize_ads(spec)
    by_base: dict[str, list[dict]] = {}
    for ad in ads:
        by_base.setdefault(base_key(spec, ad["key"]), []).append(ad)

    wb = Workbook()
    ws = wb.active
    ws.title = "Mahsulotlar"
    ws.append(["Mahsulot", "Kategoriya", "Reklamadagi narx", "Saytdagi narx", "Saytdagi eski narx", "Farq",
               "Reklama sahifalari (landing)", "Qaysi sahifadan", "Reklamalar soni", "Birinchi reklama",
               "Boshqa saytlardagi narx", "Tavsif / yo'riqnoma"])
    rows = []
    for key, p in spec["products"].items():
        if p.get("same_as"):
            continue
        info = landings[key]
        group = by_base.get(key, [])
        ad_price, site_price = p.get("ad_price"), info.get("price")
        if ad_price is None:
            diff = "reklamada narx yo'q"
        elif site_price and ad_price != site_price:
            diff = f"reklamada {_fmt(ad_price)}, saytda {_fmt(site_price)}"
        else:
            diff = "bir xil"
        landing_urls = [p["landing"]] + [q["landing"] for q in spec["products"].values() if q.get("same_as") == key]
        page_counts: dict[str, int] = {}
        for ad in group:
            page_counts[ad["page"]] = page_counts.get(ad["page"], 0) + 1
        offers = other_site_offers(db, key, own)
        offers_txt = "\n".join(f"{o['source'].removeprefix('sayt:')}: {_fmt(o['price'])} — {o['name']}"
                               for o in offers if o.get("price"))
        product = db.get_product(product_ids[key]) or {}
        rows.append([product.get("name") or info.get("name") or key, p["category"], ad_price, site_price,
                     info.get("old_price"), diff, "\n".join(landing_urls),
                     "\n".join(f"{k}: {v}" for k, v in sorted(page_counts.items(), key=lambda x: -x[1])),
                     len(group), min((a["start"] for a in group), default=""), offers_txt,
                     product.get("description", "")])
    rows.sort(key=lambda r: -r[8])
    for r in rows:
        ws.append(r)

    ws2 = wb.create_sheet("Reklamalar")
    ws2.append(["Ad Library ID", "Sahifa", "Boshlangan", "Shu kreativdagi reklamalar", "Mahsulot", "Reklama narxi",
                "Reklama sahifasi (landing)", "Ad Library havola", "Reklama matni"])
    for ad in sorted(ads, key=lambda a: (a["start"], a["page"]), reverse=True):
        p = spec["products"][ad["key"]]
        base = spec["products"][base_key(spec, ad["key"])]
        product = db.get_product(product_ids[ad["key"]]) or {}
        ws2.append([ad["id"], ad["page"], ad["start"], ad["variants"], product.get("name") or ad["key"],
                    p.get("ad_price") or base.get("ad_price"), p["landing"],
                    f"https://www.facebook.com/ads/library/?id={ad['id']}", p.get("ad_text") or base.get("ad_text")])

    bold, fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0E3B2E")
    widths = {"Mahsulotlar": [38, 15, 13, 13, 13, 22, 44, 18, 10, 12, 48, 90],
              "Reklamalar": [20, 13, 12, 12, 38, 13, 44, 48, 90]}
    for sheet in (ws, ws2):
        for cell in sheet[1]:
            cell.font, cell.fill = bold, fill
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        for i, w in enumerate(widths[sheet.title], 1):
            sheet.column_dimensions[get_column_letter(i)].width = w
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                if isinstance(cell.value, int) and cell.value > 1000:
                    cell.number_format = "# ### ##0"
        sheet.freeze_panes = "B2"
        sheet.auto_filter.ref = sheet.dimensions
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="Excel hisobot yo'li (.xlsx)")
    args = ap.parse_args()
    print(json.dumps(run(args.json_path, args.out), ensure_ascii=False))
