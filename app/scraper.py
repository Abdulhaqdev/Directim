"""Saytlardan mahsulot ma'lumotini yig'ish (shoxmarket.uz va shunga o'xshash do'konlar).

Har xil saytlarda ishlashi uchun ma'lumot shu tartibda qidiriladi:
  1) JSON-LD (schema.org/Product)  2) OpenGraph meta teglar  3) sahifa tuzilishi (sarlavha, narx, tavsif)
  4) hech biri yetmasa — Claude sahifa matnidan ajratib oladi (ixtiyoriy).
Mahsulot sahifalari: boshlang'ich sahifadagi havolalar, /product/N kabi raqamli manzillarni ketma-ket tekshirish,
sitemap.xml va mahsulot sahifalaridagi "o'xshash mahsulotlar" havolalari orqali topiladi.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .catalog import parse_price
from .db import DB

log = logging.getLogger("scraper")

USER_AGENT = "Mozilla/5.0 (compatible; SotuvAgentCatalog/1.0)"
PRODUCT_PATH = re.compile(r"/(product|products|item|items|p|mahsulot|mahsulotlar|tovar|goods|catalog/item)[/-]", re.I)
NUMERIC_TAIL = re.compile(r"^(.*?/(?:product|products|item|p|mahsulot|tovar)/)(\d+)/?$", re.I)
MONEY = re.compile(r"(\d[\d\s.,  ]{2,})\s*(so'm|so‘m|сум|sum|uzs)", re.I)


@dataclass
class ScrapedProduct:
    name: str
    price: int | None = None
    old_price: int | None = None
    description: str = ""
    category: str = ""
    image_url: str = ""
    source_url: str = ""
    method: str = ""          # qaysi usul bilan olindi: jsonld | og | html | ai


@dataclass
class ImportReport:
    site: str
    found_urls: int = 0
    imported: int = 0
    updated: int = 0
    skipped: list[str] = field(default_factory=list)
    out_of_stock: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.site}: {self.imported} ta yangi, {self.updated} ta yangilandi"]
        if self.out_of_stock:
            parts.append(f"{self.out_of_stock} ta saytda yo'q → «tugagan»")
        if self.skipped:
            parts.append(f"{len(self.skipped)} ta o'tkazib yuborildi")
        if self.errors:
            parts.append(f"{len(self.errors)} ta xato")
        return ", ".join(parts)


def _clean(text: str | None, limit: int = 1500) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def _money(text: str | None) -> int | None:
    value = parse_price(text)
    return value if value and value >= 100 else None


# ---------------- ajratib olish ----------------
def _from_jsonld(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            stack.extend(item.get("@graph", []) if isinstance(item.get("@graph"), list) else [])
            types = item.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in types:
                continue
            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = offers.get("price") or offers.get("lowPrice")
            image = item.get("image")
            if isinstance(image, list):
                image = image[0] if image else ""
            if isinstance(image, dict):
                image = image.get("url", "")
            return {
                "name": item.get("name"), "description": item.get("description"),
                "price": _money(str(price)) if price else None, "image_url": image or "",
                "category": item.get("category") if isinstance(item.get("category"), str) else "",
            }
    return {}


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def _by_class(root, *needles: str):
    """Klass nomida berilgan so'zlardan biri bo'lgan elementlar."""
    def match(classes):
        if not classes:
            return False
        joined = " ".join(classes).lower() if isinstance(classes, list) else str(classes).lower()
        return any(n in joined for n in needles)
    return root.find_all(class_=match)


PRICE_CUR = ("current__price", "current-price", "price--current", "sale-price", "price-new", "special-price", "price_current")
PRICE_OLD = ("old__price", "old-price", "price--old", "price-old", "regular-price", "price_old")
TITLE_CLASSES = ("info__title", "product-title", "product_title", "product-name", "product__title", "product_name")


def _details_scope(soup: BeautifulSoup):
    """Mahsulot nomi turgan va ichida narx bor eng yaqin blok (o'xshash mahsulotlar kartochkalari emas)."""
    titles = _by_class(soup, *TITLE_CLASSES) or soup.find_all("h1")
    for title in titles:
        node = title
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            if _by_class(node, *PRICE_CUR, "price", "narx") or MONEY.search(node.get_text(" ")):
                return title, node
        return title, title.parent
    return None, None


def _from_html(soup: BeautifulSoup, url: str) -> dict:
    out: dict = {}
    title, details = _details_scope(soup)
    if title is None:
        return out
    out["name"] = _clean(title.get_text(" "), 200)
    out["_title_found"] = True
    cur = _by_class(details, *PRICE_CUR)
    old = _by_class(details, *PRICE_OLD)
    if cur:
        out["price"] = _money(cur[0].get_text(" "))
    if old:
        out["old_price"] = _money(old[0].get_text(" "))
    if not out.get("price"):
        for el in _by_class(details, "price", "narx") or [details]:
            m = MONEY.search(el.get_text(" "))
            if m:
                out["price"] = _money(m.group(1))
                break
    desc = _by_class(details, "info__desc", "product-description", "product__description", "description", "desc")
    if desc:
        out["description"] = _clean(desc[0].get_text(" "))
    # kategoriya: breadcrumb (oxiridan oldingisi) yoki "o'xshash mahsulotlar"dagi eng ko'p uchragan kategoriya
    crumbs = _by_class(soup, "breadcrumb")
    if crumbs:
        items = [_clean(a.get_text(" "), 80) for a in crumbs[0].find_all(["a", "li"]) if a.get_text(strip=True)]
        items = [i for i in dict.fromkeys(items) if i.lower() not in ("asosiy", "bosh sahifa", "главная", "home")]
        if len(items) >= 2:
            out["category"] = items[-2]
    if not out.get("category"):
        subs = [_clean(x.get_text(" "), 80) for x in _by_class(soup, "content__subtitle", "product-category", "product__category")]
        subs = [x for x in subs if x]
        if subs:
            out["category"] = Counter(subs).most_common(1)[0][0]
    # rasm: mahsulot galereyasi (nom turgan blokning yonida) yoki og:image
    media = _by_class(soup, "product__media", "product-gallery", "product-images", "product__gallery") or [details]
    imgs = [x.get("href") for x in media[0].find_all("a", href=True) if re.search(r"\.(jpe?g|png|webp)(\?|$)", x["href"], re.I)]
    imgs += [x.get("src") for x in media[0].find_all("img", src=True)]
    imgs = [i for i in imgs if i and not re.search(r"icon|logo|placeholder", i, re.I)]
    if imgs:
        out["image_url"] = urljoin(url, imgs[0])
    return out


TEST_NAMES = re.compile(r"^(test|тест|sinov)(\s+(offer|product|mahsulot|tovar))?\s*\d*$", re.I)


def parse_category_map(text: str | None) -> dict[str, str]:
    """ "Kitob = Kitoblar" qatorlari → {"kitob": "Kitoblar"}. "-" — bu kategoriyani olmaslik."""
    out = {}
    for line in re.split(r"[\n;]+", text or ""):
        if "=" in line:
            src, dst = line.split("=", 1)
            if src.strip():
                out[src.strip().lower()] = dst.strip()
    return out


def extract_product(html: str, url: str) -> ScrapedProduct | None:
    """HTML'dan mahsulot ma'lumotini ajratadi. Mahsulot sahifasi bo'lmasa None."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        if tag.get("type") != "application/ld+json":
            tag.decompose()
    data: dict = {}
    method = ""
    jsonld = _from_jsonld(soup)
    if jsonld.get("name"):
        data.update({k: v for k, v in jsonld.items() if v})
        method = "jsonld"
    og = {
        "name": _meta(soup, "og:title"), "description": _meta(soup, "og:description", "description"),
        "image_url": _meta(soup, "og:image"),
        "price": _money(_meta(soup, "product:price:amount", "og:price:amount")),
    }
    is_product_og = _meta(soup, "og:type").lower() in ("product", "og:product")
    html_data = _from_html(soup, url)
    for source, label in ((og, "og"), (html_data, "html")):
        for k, v in source.items():
            if v and not data.get(k):
                data[k] = v
                method = method or label
    # Sahifadagi sarlavha va chegirmali narx og'dagidan aniqroq (og:title ko'pincha sayt nomini ham qo'shadi)
    if html_data.get("name") and not jsonld.get("name"):
        data["name"] = html_data["name"]
    if html_data.get("price"):
        data["price"] = html_data["price"]
    if html_data.get("old_price"):
        data["old_price"] = html_data["old_price"]
    title_found = data.pop("_title_found", False) or html_data.pop("_title_found", False)
    if not data.get("name") or not (jsonld or is_product_og or (title_found and data.get("price"))):
        return None
    name = _clean(data["name"], 200)
    while len(name) > 2 and name[0] in '"\'«“„' and name[-1] in '"\'»”“':
        name = name[1:-1].strip()  # butun nom qo'shtirnoq ichida bo'lsa — olib tashlaymiz
    site_name = _meta(soup, "og:site_name")
    if site_name and name.lower().endswith(site_name.lower()):
        name = name[: -len(site_name)].rstrip(" -|–")
    if data.get("old_price") and data.get("price") and data["old_price"] <= data["price"]:
        data["old_price"] = None
    return ScrapedProduct(
        name=name, price=data.get("price"), old_price=data.get("old_price"),
        description=_clean(data.get("description")), category=_clean(data.get("category"), 80),
        image_url=urljoin(url, data["image_url"]) if data.get("image_url") else "", source_url=url, method=method or "html",
    )


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    return _clean(soup.get_text(" "), 8000)


# ---------------- sahifalarni topish ----------------
def _same_site(url: str, base: str) -> bool:
    return urlparse(url).netloc.replace("www.", "") == urlparse(base).netloc.replace("www.", "")


def product_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"].split("#")[0])
        if _same_site(url, base_url) and PRODUCT_PATH.search(urlparse(url).path):
            links.append(url.rstrip("/"))
    return list(dict.fromkeys(links))


class Fetcher:
    def __init__(self, concurrency: int = 4, delay: float = 0.3, http: httpx.AsyncClient | None = None):
        self.http = http or httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        self.sem = asyncio.Semaphore(concurrency)
        self.delay = delay
        self.cache: dict[str, tuple[int, str]] = {}

    async def get(self, url: str) -> tuple[int, str]:
        if url in self.cache:
            return self.cache[url]
        result = await self._get(url)
        self.cache[url] = result
        return result

    async def _get(self, url: str, retries: int = 3) -> tuple[int, str]:
        """Sahifani oladi; ulanish uzilsa yoki server xato bersa (5xx/429) — biroz kutib qayta urinadi."""
        status, text = 0, ""
        for attempt in range(retries):
            async with self.sem:
                try:
                    r = await self.http.get(url)
                    status, text = r.status_code, (r.text if r.status_code == 200 else "")
                except httpx.HTTPError as e:
                    log.warning("%s: %s (urinish %d)", url, e, attempt + 1)
                    status, text = 0, ""
                finally:
                    await asyncio.sleep(self.delay)  # saytga yuk tushirmaslik uchun
            if status not in (0, 429) and status < 500:
                break
            await asyncio.sleep(1.5 * (attempt + 1))
        return status, text

    async def aclose(self) -> None:
        await self.http.aclose()


async def discover(fetcher: Fetcher, start_url: str, max_products: int) -> list[str]:
    """Mahsulot sahifalari ro'yxati."""
    found: list[str] = []
    status, html = await fetcher.get(start_url)
    if status == 200:
        if PRODUCT_PATH.search(urlparse(start_url).path):
            return [start_url.rstrip("/")]  # bitta mahsulot sahifasi berilgan
        found += product_links(html, start_url)
    root = f"{urlparse(start_url).scheme}://{urlparse(start_url).netloc}"
    # sitemap.xml
    s_status, s_xml = await fetcher.get(f"{root}/sitemap.xml")
    if s_status == 200:
        found += [u.rstrip("/") for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", s_xml)
                  if _same_site(u, root) and PRODUCT_PATH.search(urlparse(u).path)]
    found = list(dict.fromkeys(found))
    # /product/49 kabi raqamli manzillar — 1 dan boshlab ketma-ket tekshiramiz
    numeric = [NUMERIC_TAIL.match(urlparse(u).path) for u in found]
    numeric = [m for m in numeric if m]
    if numeric:
        prefix = Counter(m.group(1) for m in numeric).most_common(1)[0][0]
        top = max(int(m.group(2)) for m in numeric if m.group(1) == prefix)
        candidates = [f"{root}{prefix}{i}" for i in range(1, min(top + 30, max_products * 2) + 1)]
        statuses = await asyncio.gather(*(fetcher.get(u) for u in candidates))
        alive = [u for u, (st, _) in zip(candidates, statuses) if st == 200]
        # raqamli ro'yxatda yo'qlarini (masalan sitemap'dagi eski havolalar) ham tekshirib qo'shamiz
        others = [u for u in found if not NUMERIC_TAIL.match(urlparse(u).path)]
        found = alive + others
    return found[:max_products]


# ---------------- import ----------------
async def import_site(db: DB, start_url: str, ai=None, category: str = "", max_products: int = 300,
                      fetcher: Fetcher | None = None, category_map: dict[str, str] | None = None) -> ImportReport:
    """Saytdagi mahsulotlarni katalogga yuklaydi/yangilaydi.
    category berilsa — hammasi shu kategoriyaga; category_map — saytdagi kategoriya → bizdagi ("-" = olmaslik)."""
    category_map = {k.lower(): v for k, v in (category_map or {}).items()}
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher()
    site = urlparse(start_url).netloc.replace("www.", "")
    report = ImportReport(site=site)
    try:
        urls = await discover(fetcher, start_url, max_products)
        report.found_urls = len(urls)
        pages = await asyncio.gather(*(fetcher.get(u) for u in urls))
        seen: set[str] = set()
        for url, (status, html) in zip(urls, pages):
            if status == 0:
                seen.add(url)  # tarmoq xatosi — "tugagan" deb belgilamaymiz
                report.errors.append(f"{url}: ochilmadi")
                continue
            if status != 200:
                continue
            product = extract_product(html, url)
            if product is None and ai is not None:
                product = await ai_extract(ai, html, url)
            if product is None or TEST_NAMES.match(product.name.strip()):
                report.skipped.append(url)
                continue
            target = category or category_map.get((product.category or "").lower(), product.category) or "Umumiy"
            if target.strip() == "-":
                report.skipped.append(url)  # egasi bu kategoriyani olmaslikni so'ragan
                continue
            seen.add(url)
            created = db.upsert_scraped(
                source_url=url, name=product.name, category=target,
                price=product.price, old_price=product.old_price, description=product.description,
                image_url=product.image_url, source=f"sayt:{site}", keep_category=True,
            )
            if created:
                report.imported += 1
            else:
                report.updated += 1
        # Avval shu saytdan olingan, endi topilmagan mahsulotlar — "tugagan"
        if urls:
            report.out_of_stock = db.mark_missing_scraped(f"sayt:{site}", seen)
    except Exception as e:  # noqa: BLE001
        log.exception("Import xatosi")
        report.errors.append(str(e))
    finally:
        if own_fetcher:
            await fetcher.aclose()
    db.log("katalog", report.summary())
    return report


async def ai_extract(ai, html: str, url: str) -> ScrapedProduct | None:
    """Tuzilishi notanish saytlar uchun: Claude sahifa matnidan mahsulotni ajratadi."""
    data = await ai.extract_product(page_text(html), url)
    if not data or not data.is_product or not data.name:
        return None
    return ScrapedProduct(name=data.name, price=data.price, old_price=data.old_price, description=data.description or "",
                          category=data.category or "", source_url=url, method="ai")


def report_dict(report: ImportReport) -> dict:
    d = asdict(report)
    d["skipped"] = d["skipped"][:20]
    return d
