"""Server: Instagram va Telegram webhooklari + boshqaruv paneli."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from urllib.parse import quote, urlparse

import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import catalog, scraper
from .ai import AI
from .db import DB, iso
from . import instagram as iglib
from .instagram import Instagram, verify_signature
from .logic import Agent
from .settings import CONFIG_DIR, ROOT, default_settings_yaml, env, validate_yaml_dict
from . import telegram as tgfmt
from .telegram import Telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx so'rov manzillarini (ichida Telegram bot tokeni bor) jurnalga yozmasin
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
log = logging.getLogger("main")

db = DB(env.db_path)

# Paneldagi «Ulanishlar» sahifasida kiritilgan kalitlar .env'dagidan ustun turadi
RUNTIME_KEYS = ("anthropic_api_key", "telegram_bot_token", "telegram_chat_id", "telegram_admin_ids", "ig_app_id",
                "ig_app_secret", "ig_config_id", "ig_verify_token", "public_url", "telegram_webhook_secret")
for _key in RUNTIME_KEYS:
    if db.kv_get(f"cfg:{_key}"):
        setattr(env, _key, db.kv_get(f"cfg:{_key}"))
for _key in ("ig_verify_token", "telegram_webhook_secret"):  # maxfiy so'zlarni o'zimiz yaratamiz
    if not getattr(env, _key):
        setattr(env, _key, secrets.token_urlsafe(18))
        db.kv_set(f"cfg:{_key}", getattr(env, _key))
env.public_url = (env.public_url or "").rstrip("/")

tg = Telegram(env.telegram_bot_token, env.telegram_chat_id)
tgfmt.PANEL_URL = env.public_url
ai = AI(env.anthropic_api_key, env.claude_model, env.claude_effort) if env.anthropic_api_key else None


_ig_clients: dict[tuple[str, str], Instagram] = {}


def make_ig(account: dict) -> Instagram:
    """Profil uchun Instagram klienti (bir xil token+ID uchun qayta ishlatiladi)."""
    key = (account.get("ig_token") or "", account.get("ig_user_id") or "me")
    if key not in _ig_clients:
        _ig_clients[key] = Instagram(key[0], key[1], env.ig_api_version)
    return _ig_clients[key]


def bootstrap() -> None:
    """Birinchi ishga tushish: Morechat'dan ko'chirilgan ma'lumotlar bilan boshlang'ich profil va katalog."""
    if db.list_accounts():
        return
    business = (CONFIG_DIR / "biznes.md").read_text(encoding="utf-8") if (CONFIG_DIR / "biznes.md").exists() else ""
    acc_id = db.add_account(
        name="Kitoblar", ig_user_id=env.ig_user_id or None, ig_username=env.ig_username or "ilmbooks.uz",
        ig_token=env.ig_access_token, tg_chat_id="", categories="Kitoblar, Dekor, Diniy buyumlar",
        business_text=business, active=1,
        settings_yaml=default_settings_yaml().replace('ism: "do\'kon menejeri"', 'ism: "ilm books menejeri"'),
    )
    seed_csv = CONFIG_DIR / "katalog_boshlangich.csv"
    if seed_csv.exists():
        catalog.import_csv(db, seed_csv.read_text(encoding="utf-8"), source="morechat")
    faq_path = CONFIG_DIR / "faq.yaml"
    if faq_path.exists():
        for item in yaml.safe_load(faq_path.read_text(encoding="utf-8")) or []:
            if isinstance(item, dict) and item.get("savol") and item.get("javob"):
                db.add_memory(item["savol"], item["javob"], acc_id, None, "morechat")
    gaps_path = CONFIG_DIR / "javobsiz_savollar.txt"
    if gaps_path.exists():
        for line in gaps_path.read_text(encoding="utf-8").splitlines():
            db.add_gap(acc_id, line)
    db.log("tizim", "Boshlang'ich profil «Kitoblar» va Morechat ma'lumotlari yuklandi", acc_id)


bootstrap()
agent = Agent(db, tg, ai, make_ig, env.reply_debounce_seconds)


def _on_tg_migrate(old: str, new: str) -> None:
    """Telegram guruh supergroup'ga aylandi — yangi ID'ni saqlaymiz."""
    db.rename_tg_chat(old, new)
    db.kv_set(f"tg_migrate:{old}", new)
    agent.reload()


tg.on_migrate = _on_tg_migrate
agent.on_main_chat = lambda chat_id: setattr(env, "telegram_chat_id", chat_id)
if env.telegram_chat_id and db.kv_get(f"tg_migrate:{env.telegram_chat_id}"):
    tg.migrations[env.telegram_chat_id] = db.kv_get(f"tg_migrate:{env.telegram_chat_id}")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))


def apply_runtime_config() -> None:
    """Kalitlar o'zgarganda AI va Telegram klientlarini yangilaydi."""
    global ai
    ai = AI(env.anthropic_api_key, env.claude_model, env.claude_effort) if env.anthropic_api_key else None
    agent.ai = ai
    tg.bot_token = env.telegram_bot_token
    tg.default_chat_id = env.telegram_chat_id
    tgfmt.PANEL_URL = env.public_url
TASHKENT = timezone(timedelta(hours=5))


def _local_time(value: str | None, fmt: str = "%d.%m %H:%M") -> str:
    """UTC ISO vaqtni Toshkent vaqtiga o'giradi."""
    from .db import parse_iso
    dt = parse_iso(value)
    return dt.astimezone(TASHKENT).strftime(fmt) if dt else ""


templates.env.filters["vaqt"] = _local_time


async def _background_loop() -> None:
    tick = 0
    while True:
        try:
            if tick % 360 == 0:
                await agent.refresh_posts()
            if tick % 1440 == 0:
                if await agent.refresh_tokens():
                    agent.reload()
            catalog_url = db.kv_get("catalog_url", env.catalog_csv_url)
            if catalog_url and tick % max(env.catalog_sync_minutes, 5) == 0:
                await catalog.sync_from_url(db, catalog_url)
            await agent.run_followups()
            await agent.retry_unsent_leads()
            await agent.send_due_reminders()
            if tick % max(env.site_sync_hours * 60, 60) == 30:
                for site in _sites():
                    await _run_site_import(site["url"], site.get("category", ""), site.get("max", 300), site.get("mapping", ""))
        except Exception:  # noqa: BLE001
            log.exception("Fon vazifasida xato")
        tick += 1
        await asyncio.sleep(60)


async def _telegram_poller() -> None:
    """Server manzili (PUBLIC_URL) bo'lmasa — Telegram'dan xabarlarni o'zimiz so'rab turamiz (kompyuterda ham ishlaydi)."""
    offset, webhook_cleared_for = 0, ""
    while True:
        try:
            if env.public_url or not env.telegram_bot_token:
                await asyncio.sleep(5)
                continue
            if webhook_cleared_for != env.telegram_bot_token:
                await tg.delete_webhook()  # getUpdates webhook bilan birga ishlamaydi
                webhook_cleared_for = env.telegram_bot_token
            for upd in await tg.get_updates(offset, timeout=25):
                offset = max(offset, upd.get("update_id", 0) + 1)
                _remember_tg_chat(upd)
                await agent.handle_telegram_update(upd, env.admin_ids())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Telegram polling xatosi")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if agent.ai is None:
        log.warning("Claude kaliti berilmagan — agent javob bermaydi (panel → Ulanishlar)")
    if not env.ig_app_secret:
        log.warning("IG_APP_SECRET berilmagan — webhook imzosi tekshirilmaydi (faqat sinov uchun!)")
    if not env.admin_password:
        log.warning("ADMIN_PASSWORD berilmagan — panelga kirib bo'lmaydi")
    if env.public_url and env.telegram_webhook_secret and env.telegram_bot_token:
        try:
            res = await tg.set_webhook(f"{env.public_url}/telegram/webhook", env.telegram_webhook_secret)
            log.info("Telegram webhook: %s", res.get("description") or res)
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram webhookni o'rnatib bo'lmadi: %s", e)
    status = json.loads(db.kv_get("import_status", "{}") or "{}")
    if status.get("holat") == "ishlayapti":  # server import paytida qayta ishga tushgan
        db.kv_set("import_status", json.dumps({**status, "holat": "to'xtadi"}, ensure_ascii=False))
    import os
    tasks = [] if os.getenv("AGENT_NO_BACKGROUND") == "1" else [asyncio.create_task(_background_loop()),
                                                                  asyncio.create_task(_telegram_poller())]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Sotuv agenti", lifespan=lifespan)

_PUBLIC_PATHS = ("/webhook", "/telegram/webhook", "/health")


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """Panel formalarini begona saytdan yuborishni (CSRF) bloklaydi."""
    if request.method == "POST" and not request.url.path.startswith(_PUBLIC_PATHS):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlparse(origin).netloc != request.url.netloc:
            return PlainTextResponse("Ruxsat yo'q", status_code=403)
    return await call_next(request)


# ===================== Webhooklar =====================
@app.get("/webhook")
async def webhook_verify(request: Request):
    q = request.query_params
    if env.ig_verify_token and q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == env.ig_verify_token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "verify token mos emas")


@app.post("/webhook")
async def webhook_receive(request: Request):
    body = await request.body()
    if not verify_signature(env.ig_app_secret, body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "imzo noto'g'ri")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "JSON emas")
    agent.spawn(agent.handle_webhook(payload))
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not env.telegram_webhook_secret or request.headers.get("x-telegram-bot-api-secret-token") != env.telegram_webhook_secret:
        raise HTTPException(401)
    update = await request.json()
    _remember_tg_chat(update)
    agent.spawn(agent.handle_telegram_update(update, env.admin_ids()))
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True, "ai": agent.ai is not None, "profillar": len(agent.profiles)}


# ===================== Panel =====================
SESSION_COOKIE = "sotuv_agent"


def _session_token() -> str:
    return hmac.new(env.admin_password.encode(), b"panel-session-v1", hashlib.sha256).hexdigest()


def is_logged_in(request: Request) -> bool:
    if not env.admin_password:
        return False
    token = request.cookies.get(SESSION_COOKIE, "")
    if token and hmac.compare_digest(token, _session_token()):
        return True
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):  # skriptlar va testlar uchun
        try:
            password = base64.b64decode(header[6:]).decode().split(":", 1)[1]
        except Exception:  # noqa: BLE001
            return False
        return hmac.compare_digest(password.encode(), env.admin_password.encode())
    return False


def admin(request: Request) -> str:
    if not is_logged_in(request):
        raise HTTPException(303, "Kirish kerak", headers={"Location": "/kirish"})
    return "admin"


@app.get("/kirish")
async def login_page(request: Request, xato: str = ""):
    return templates.TemplateResponse(request, "login.html", {"xato": xato, "no_password": not env.admin_password})


@app.post("/kirish")
async def login(parol: str = Form("")):
    if env.admin_password and hmac.compare_digest(parol.encode(), env.admin_password.encode()):
        resp = RedirectResponse("/", 303)
        resp.set_cookie(SESSION_COOKIE, _session_token(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
        return resp
    return RedirectResponse("/kirish?xato=1", 303)


@app.get("/chiqish")
async def logout():
    resp = RedirectResponse("/kirish", 303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


def setup_status() -> dict:
    """Ishga tushirish uchun nimalar tayyor."""
    accounts = db.list_accounts()
    connected = [a for a in accounts if a.get("ig_user_id") and a.get("ig_token")]
    return {
        "claude": agent.ai is not None,
        "telegram_bot": bool(env.telegram_bot_token),
        "telegram_group": bool(env.telegram_chat_id),
        "instagram_app": bool(env.ig_app_id and env.ig_app_secret),
        "public_url": bool(env.public_url),
        "profiles_total": len(accounts),
        "profiles_connected": len(connected),
    }


def render(request: Request, name: str, **ctx):
    status = setup_status()
    return templates.TemplateResponse(request, name, {
        "page": name, "accounts": db.list_accounts(), "setup": status,
        "pending_gaps": len(db.list_gaps(status="kutilmoqda")) + len(db.list_gaps(status="taklif")), **ctx})


def back(url: str, xato: str | None = None, ok: str | None = None) -> RedirectResponse:
    url, _, fragment = url.partition("#")  # xabar #bo'lim dan OLDIN turishi kerak, aks holda yo'qoladi
    sep = "&" if "?" in url else "?"
    if xato:
        url += f"{sep}xato={quote(xato[:300])}"
    elif ok:
        url += f"{sep}ok={quote(ok[:300])}"
    return RedirectResponse(url + (f"#{fragment}" if fragment else ""), 303)


def _opt_int(value: str | None) -> int | None:
    return int(value) if value and value.strip().isdigit() else None


@app.get("/")
async def dashboard(request: Request, _: str = Depends(admin)):
    stats = db.stats()
    for s_ in stats:
        prof = agent.profiles.get(s_["account"]["id"])
        s_["products"] = len(agent.products_for(prof)) if prof else 0
    totals = {k: sum(x[k] for x in stats) for k in ("dialoglar", "lidlar", "kommentlar", "javobsiz")}
    return render(request, "dashboard.html", stats=stats, totals=totals, leads=db.list_leads(8), events=db.events(12),
                  sales=db.lead_stats(30),
                  product_count=len(db.list_products()))


# --- Ulanishlar (kalitlar paneldan) ---
def _mask(value: str) -> str:
    return (value[:4] + "•••" + value[-4:]) if value and len(value) > 10 else ("•••" if value else "")


def _remember_tg_chat(update: dict) -> None:
    """Bot qo'shilgan guruhlarni eslab qolamiz — panelda tanlash uchun."""
    msg = update.get("message") or update.get("my_chat_member") or (update.get("callback_query") or {}).get("message") or {}
    chat = msg.get("chat") or {}
    if chat.get("type") in ("group", "supergroup"):
        chats = json.loads(db.kv_get("tg_chats", "{}") or "{}")
        chats[str(chat["id"])] = chat.get("title") or str(chat["id"])
        db.kv_set("tg_chats", json.dumps(chats, ensure_ascii=False))


@app.get("/ulanishlar")
async def connections(request: Request, _: str = Depends(admin), xato: str = "", ok: str = ""):
    return render(request, "connections.html", xato=xato, ok=ok, env=env, mask=_mask, bot_username=db.kv_get("tg_bot_username"),
                  tg_chats=json.loads(db.kv_get("tg_chats", "{}") or "{}"),
                  webhook_url=f"{env.public_url}/webhook" if env.public_url else "",
                  callback_url=f"{env.public_url}/instagram/callback" if env.public_url else "")


@app.post("/ulanishlar")
async def connections_save(request: Request, _: str = Depends(admin)):
    form = await request.form()
    changed = []
    for key in RUNTIME_KEYS:
        if key not in form:
            continue
        value = str(form.get(key) or "").strip()
        if not value and key in ("anthropic_api_key", "telegram_bot_token", "ig_app_secret"):
            continue  # maxfiy maydon bo'sh qoldirilsa — eskisi saqlanadi
        if key == "public_url":
            value = value.rstrip("/")
            if value and not value.startswith("http"):
                value = "https://" + value
        db.kv_set(f"cfg:{key}", value)
        setattr(env, key, value)
        changed.append(key)
    apply_runtime_config()
    note = ""
    if "telegram_bot_token" in changed and env.telegram_bot_token:
        me = await tg.get_me()
        if me.get("username"):
            db.kv_set("tg_bot_username", me["username"])
            note += f" Bot: @{me['username']}."
        else:
            note += " ⚠️ Bot token noto'g'ri ko'rinadi."
    if "telegram_bot_token" in changed or "public_url" in changed:
        if env.public_url and env.telegram_bot_token:
            res = await tg.set_webhook(f"{env.public_url}/telegram/webhook", env.telegram_webhook_secret)
            note = " Telegram javoblari ulandi." if res.get("ok") else f" Telegram webhook: {res.get('description')}"
    return back("/ulanishlar", ok="Saqlandi." + note)


@app.get("/telegram/ulash")
async def telegram_connect(profil: int = 0, _: str = Depends(admin)):
    """Botni guruhga bitta havola bilan qo'shish: guruh tanlanadi va o'zi ulanadi (/start bind_...)."""
    if not env.telegram_bot_token:
        return back("/ulanishlar#telegram", xato="Avval Telegram bot tokenini kiriting.")
    bot = db.kv_get("tg_bot_username")
    if not bot:
        bot = (await tg.get_me()).get("username", "")
        if not bot:
            return back("/ulanishlar#telegram", xato="Bot token ishlamayapti — @BotFather'dan qayta oling.")
        db.kv_set("tg_bot_username", bot)
    code = "bind_" + secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    db.kv_set(f"tg_bind:{code}", str(profil) if profil else "main")
    return RedirectResponse(f"https://t.me/{bot}?startgroup={code}", 303)


@app.post("/telegram/namuna")
async def telegram_samples(_: str = Depends(admin)):
    """Guruhga namuna xabarlar (tugmalari ishlaydi, lekin hech narsani o'zgartirmaydi)."""
    if not (env.telegram_bot_token and env.telegram_chat_id):
        return back("/ulanishlar#telegram", xato="Avval bot va guruhni ulang.")
    acc = (db.list_accounts() or [{"name": "Kitoblar", "ig_username": "ilmbooks.uz"}])[0]
    conv = {"id": "namuna", "username": "namuna_mijoz", "name": "Malika"}

    def demo(a: str, b: str):
        return [[(a, "demo:1"), (b, "demo:2")]]

    ok = 0
    ok += bool(await tg.send("🧪 <b>Namuna xabarlar</b> — guruhda nima kelishini ko'rsatadi. Tugmalarni bosib ko'ring.", env.telegram_chat_id))
    ok += bool(await tg.send(tgfmt.lead_message(acc, conv, {"phone": "+998 90 123 45 67", "interest": "Saodat asri", "region": "Samarqand",
                                                            "for_whom": "o'g'limga", "summary": "Saodat asri to'plamini so'radi, raqam qoldirdi",
                                                            "temperature": "issiq", "lead_reason": "aniq xarid-niyati · raqam berdi"}, "dm"),
                             env.telegram_chat_id, buttons=demo("💰 Sotildi", "🚫 Bo'lmadi")))
    ok += bool(await tg.send(tgfmt.gap_message(acc, conv, "Kitobning muqovasi qattiqmi?", None, 0, 1, iso()), env.telegram_chat_id,
                             buttons=demo("❌ Kerak emas", "⏰ Keyinroq")))
    ok += bool(await tg.send(tgfmt.handoff_message(acc, conv, "Mijoz shikoyat qildi", "Boshqa nashr kelibdi"), env.telegram_chat_id,
                             buttons=demo("✅ Men gaplashdim", "🤖 AI davom etsin")))
    ok += bool(await tg.send(tgfmt.moderation_message(acc, "namuna_mijoz", "haqoratli so'z", "HAQORAT", "yashirildi", None),
                             env.telegram_chat_id, buttons=demo("↩️ Qayta ochish", "🗑 O'chirish")))
    if ok < 5:
        return back("/ulanishlar#telegram", xato=f"{5 - ok} ta xabar ketmadi — bot guruhda adminmi?")
    hint = "" if env.public_url else " Tugmalar ishlashi uchun platforma ochiq turishi kerak (kompyuterda — server yoqiq bo'lsin)."
    return back("/ulanishlar#telegram", ok="Guruhga 5 ta namuna xabar yuborildi." + hint)


@app.post("/ulanishlar/claude-test")
async def connections_claude_test(_: str = Depends(admin)):
    if agent.ai is None:
        return back("/ulanishlar", xato="Avval Claude kalitini kiriting.")
    try:
        r = await agent.ai.client.messages.create(model=agent.ai.model, max_tokens=50,
                                                  messages=[{"role": "user", "content": "Salom! Bir so'z bilan javob ber."}])
        text = next((b.text for b in r.content if b.type == "text"), "")
        return back("/ulanishlar", ok=f"Claude ishlayapti ✓ ({agent.ai.model}): {text[:60]}")
    except Exception as e:  # noqa: BLE001
        return back("/ulanishlar", xato=f"Claude ishlamadi: {str(e)[:250]}")


@app.post("/ulanishlar/telegram-guruhlar")
async def connections_tg_groups(_: str = Depends(admin)):
    """Bot qo'shilgan guruhlarni topadi (webhook o'rnatilmagan bo'lsa getUpdates orqali)."""
    if not env.telegram_bot_token:
        return back("/ulanishlar", xato="Avval Telegram bot tokenini kiriting.")
    chats = json.loads(db.kv_get("tg_chats", "{}") or "{}")
    try:
        r = await tg.http.get(f"{tg.base}/getUpdates", params={"allowed_updates": json.dumps(["message", "my_chat_member"])})
        data = r.json()
        for upd in data.get("result", []):
            _remember_tg_chat(upd)
        chats = json.loads(db.kv_get("tg_chats", "{}") or "{}")
        if not data.get("ok") and not chats:
            return back("/ulanishlar", xato=f"Telegram: {data.get('description')}. Botni guruhga qo'shib, guruhda xabar yozing.")
    except Exception as e:  # noqa: BLE001
        return back("/ulanishlar", xato=f"Telegram bilan aloqa yo'q: {e}")
    if not chats:
        return back("/ulanishlar", xato="Guruh topilmadi. Botni guruhga qo'shing, guruhda bitta xabar yozing va qayta bosing.")
    return back("/ulanishlar", ok=f"{len(chats)} ta guruh topildi — pastdan tanlang.")


@app.post("/ulanishlar/telegram-guruh")
async def connections_tg_pick(chat_id: str = Form(...), _: str = Depends(admin)):
    db.kv_set("cfg:telegram_chat_id", chat_id)
    env.telegram_chat_id = chat_id
    apply_runtime_config()
    ok = await tg.send("✅ Sotuv agenti shu guruhga ulandi. Lidlar va savollar shu yerga keladi.", chat_id)
    return back("/ulanishlar", ok="Asosiy guruh tanlandi va test xabar yuborildi." if ok else "Guruh tanlandi, lekin xabar ketmadi — bot guruhda adminmi?")


# --- Agentni sinash (Instagram'ga hech narsa ketmaydi) ---
def _sim_key(pid: int) -> str:
    return f"sinov:{pid}"


def _sim_load(pid: int) -> dict:
    try:
        return json.loads(db.kv_get(_sim_key(pid), "") or "{}") or {"chat": [], "state": {}}
    except json.JSONDecodeError:
        return {"chat": [], "state": {}}


@app.get("/sinash")
async def simulate_page(request: Request, _: str = Depends(admin), profil: int = 0, xato: str = ""):
    profiles = list(agent.profiles.values())
    if not profiles:
        return back("/profillar", xato="Avval profil yarating.")
    prof = agent.profiles.get(profil) or profiles[0]
    sim = _sim_load(prof.id)
    return render(request, "simulate.html", prof=prof, sim=sim, xato=xato, products=len(agent.products_for(prof)),
                  comment=json.loads(db.kv_get(f"sinov-komment:{prof.id}", "") or "{}"))


@app.post("/sinash/{pid}/yozish")
async def simulate_send(pid: int, matn: str = Form(""), _: str = Depends(admin)):
    prof = agent.profiles.get(pid)
    if not prof or not matn.strip():
        return RedirectResponse(f"/sinash?profil={pid}", 303)
    if agent.ai is None:
        return back(f"/sinash?profil={pid}", xato="Claude kaliti kiritilmagan — «Ulanishlar» sahifasida kiriting.")
    sim = _sim_load(pid)
    sim["chat"].append({"role": "user", "text": matn.strip()})
    out = await agent.simulate_dm(prof, sim["chat"], sim["state"])
    if out.get("error"):
        sim["chat"].pop()
        return back(f"/sinash?profil={pid}", xato=f"AI javob bermadi: {out['error']}")
    sim["chat"].append({"role": "assistant", "text": out["reply"], "events": out["events"], "info": out["info"]})
    db.kv_set(_sim_key(pid), json.dumps(sim, ensure_ascii=False))
    return RedirectResponse(f"/sinash?profil={pid}#oxiri", 303)


@app.post("/sinash/{pid}/komment")
async def simulate_comment(pid: int, matn: str = Form(""), _: str = Depends(admin)):
    prof = agent.profiles.get(pid)
    if not prof or not matn.strip():
        return RedirectResponse(f"/sinash?profil={pid}", 303)
    if agent.ai is None:
        return back(f"/sinash?profil={pid}", xato="Claude kaliti kiritilmagan — «Ulanishlar» sahifasida kiriting.")
    out = await agent.simulate_comment(prof, matn.strip())
    db.kv_set(f"sinov-komment:{pid}", json.dumps({"text": matn.strip(), **out}, ensure_ascii=False))
    return RedirectResponse(f"/sinash?profil={pid}#komment", 303)


@app.post("/sinash/{pid}/tozalash")
async def simulate_reset(pid: int, _: str = Depends(admin)):
    db.kv_set(_sim_key(pid), "")
    db.kv_set(f"sinov-komment:{pid}", "")
    return RedirectResponse(f"/sinash?profil={pid}", 303)


# --- Profillar ---
@app.get("/profillar")
async def profiles(request: Request, _: str = Depends(admin), xato: str = "", ok: str = ""):
    return render(request, "profiles.html", categories=db.product_categories(), xato=xato, ok=ok,
                  profiles=agent.profiles)


@app.post("/profillar/yangi")
async def profile_new(name: str = Form(...), ig_username: str = Form(""), categories: str = Form("*"), _: str = Depends(admin)):
    acc_id = db.add_account(name=name.strip(), ig_username=ig_username.strip().lstrip("@"), categories=categories.strip() or "*",
                            business_text="", settings_yaml=default_settings_yaml(), active=1)
    agent.reload()
    return back(f"/profil/{acc_id}", ok="Profil yaratildi. Instagram token va biznes matnini kiriting.")


@app.get("/profil/{acc_id}")
async def profile_page(acc_id: int, request: Request, _: str = Depends(admin), xato: str = "", ok: str = ""):
    acc = db.get_account(acc_id)
    if not acc:
        raise HTTPException(404)
    products = db.list_products(agent.profiles[acc_id].categories) if acc_id in agent.profiles else []
    token = acc.get("ig_token") or ""
    cfg = agent.profiles[acc_id].cfg if acc_id in agent.profiles else None
    from .settings import Config
    cfg = cfg or Config.from_yaml(acc.get("settings_yaml"))
    simple = [{"path": p_, "kind": k_, "label": l_, "group": g_, "value": cfg.get(p_)} for p_, k_, l_, g_ in SIMPLE_SETTINGS]
    groups: dict[str, list] = {}
    for item in simple:
        groups.setdefault(item["group"], []).append(item)
    history = json.loads(db.kv_get(f"tarix:{acc_id}", "") or "{}")
    return render(request, "profile.html", acc=acc, history=history, masked=(token[:6] + "…" + token[-4:]) if len(token) > 12 else "", simple=simple, groups=list(groups.items()),
                  products=products, categories=db.product_categories(), xato=xato, ok=ok,
                  posts=agent.profiles[acc_id].posts if acc_id in agent.profiles else [])


@app.post("/profil/{acc_id}")
async def profile_save(acc_id: int, name: str = Form(...), ig_username: str = Form(""), ig_user_id: str = Form(""),
                       ig_token: str = Form(""), tg_chat_id: str = Form(""), categories: str = Form("*"),
                       business_text: str = Form(""), settings_yaml: str = Form(""), active: str = Form(""),
                       _: str = Depends(admin)):
    try:
        validate_yaml_dict(settings_yaml)
    except (yaml.YAMLError, ValueError) as e:
        return back(f"/profil/{acc_id}", xato=f"Sozlamalarda xato: {e}")
    fields = dict(name=name.strip(), ig_username=ig_username.strip().lstrip("@"), ig_user_id=ig_user_id.strip() or None,
                  tg_chat_id=tg_chat_id.strip(), categories=categories.strip() or "*",
                  business_text=business_text.replace("\r\n", "\n"), settings_yaml=settings_yaml.replace("\r\n", "\n"),
                  active=1 if active else 0)
    if ig_token.strip():
        fields["ig_token"] = ig_token.strip()
    try:
        db.update_account(acc_id, **fields)
    except Exception as e:  # noqa: BLE001
        return back(f"/profil/{acc_id}", xato=f"Saqlanmadi: {e}")
    agent.reload()
    return back(f"/profil/{acc_id}", ok="Saqlandi")


# --- «Instagram bilan ulash» (bir bosishda, Morechat'dagidek) ---
def _redirect_uri() -> str:
    return f"{env.public_url}/instagram/callback"


@app.get("/instagram/ulash")
async def instagram_connect(profil: int = 0, _: str = Depends(admin)):
    missing = [name for name, val in (("Meta App ID", env.ig_app_id), ("Meta App secret", env.ig_app_secret),
                                      ("Server manzili", env.public_url)) if not val]
    if missing:
        return back("/ulanishlar#instagram", xato=f"Instagram'ni ulash uchun kiritilmagan: {', '.join(missing)}. "
                                                   "Shu sahifadagi Instagram bo'limida to'ldiring.")
    state = secrets.token_urlsafe(24)
    db.kv_set(f"oauth:{state}", json.dumps({"profil": profil, "at": iso(), "unified": bool(env.ig_config_id)}))
    if env.ig_config_id:  # yangi Meta ilovalari: Facebook orqali (Unified login)
        return RedirectResponse(iglib.oauth_url_unified(env.ig_app_id, env.ig_config_id, _redirect_uri(), state,
                                                        env.ig_api_version), 303)
    return RedirectResponse(iglib.oauth_url(env.ig_app_id, _redirect_uri(), state), 303)


async def _connect_ig_account(ig_id: str, username: str, token: str, prefer_profile: int | None) -> int:
    """Instagram akkauntni profilga yozadi: ID yoki username mos profil → tanlangan profil → yangi profil."""
    existing = db.account_by_ig(ig_id, include_inactive=True)
    if not existing and username:
        existing = next((a for a in db.list_accounts() if (a.get("ig_username") or "").lower() == username.lower()
                         and not a.get("ig_user_id")), None)
    if not existing and prefer_profile:
        cand = db.get_account(prefer_profile)
        existing = cand if cand and not cand.get("ig_user_id") else None
    fields = dict(ig_user_id=ig_id, ig_username=username, ig_token=token, active=1)
    if existing:
        db.update_account(existing["id"], **fields)
        acc_id = existing["id"]
    else:
        acc_id = db.add_account(name=username or "Yangi profil", categories="*", business_text="",
                                settings_yaml=default_settings_yaml(), **fields)
    db.kv_set(f"token_refreshed:{acc_id}", iso())
    db.log("tizim", f"Instagram ulandi: @{username}", acc_id)
    return acc_id


@app.get("/instagram/callback")
async def instagram_callback(code: str = "", state: str = "", error_description: str = "", _: str = Depends(admin)):
    saved = db.kv_get(f"oauth:{state}") if state else ""
    if not saved:
        return back("/profillar", xato="Ulash havolasi eskirgan — qaytadan «Instagram bilan ulash» ni bosing.")
    db.kv_set(f"oauth:{state}", "")
    info = json.loads(saved)
    target = f"/profil/{info['profil']}" if info.get("profil") else "/profillar"
    if not code:
        return back(target, xato=f"Instagram ruxsat bermadi: {error_description or 'bekor qilindi'}")
    if info.get("unified"):
        try:
            tok = await iglib.exchange_code_unified(env.ig_app_id, env.ig_app_secret.split(",")[0].strip(), _redirect_uri(),
                                                    code, env.ig_api_version)
            probe = Instagram(tok["access_token"], "me", env.ig_api_version)
            accounts = await probe.assigned_accounts()
        except Exception as e:  # noqa: BLE001
            return back(target, xato=f"Facebook orqali ulanmadi: {e}")
        if not accounts:
            return back(target, xato="Hech qanday Instagram akkaunt tanlanmadi — oynada Instagram akkauntlarni belgilang.")
        ids = [await _connect_ig_account(a["id"], a.get("username") or "", tok["access_token"],
                                         info.get("profil") if len(accounts) == 1 else None) for a in accounts]
        agent.reload()
        notes = []
        for acc_id in ids:
            prof = agent.profiles.get(acc_id)
            if prof:
                res = await prof.ig.subscribe_webhooks()
                if isinstance(res, dict) and res.get("success") is False:
                    notes.append(f"@{prof.username}: obuna Meta panelida")
                await agent.refresh_posts(prof)
        names = ", ".join("@" + (a.get("username") or a["id"]) for a in accounts)
        return back(f"/profil/{ids[0]}" if len(ids) == 1 else "/profillar",
                    ok=f"Ulandi: {names}. Har bir profilning kategoriyalari va biznes matnini tekshiring.")
    try:
        code = code.split("#")[0]  # Instagram kod oxiriga "#_" qo'shadi
        token = await iglib.exchange_code(env.ig_app_id, env.ig_app_secret.split(",")[0].strip(), _redirect_uri(), code)
        probe = Instagram(token["access_token"], "me", env.ig_api_version)
        me = await probe.me()
        await probe.aclose()
        ig_id = str(me.get("user_id") or token.get("user_id"))
        username = me.get("username") or ""
    except Exception as e:  # noqa: BLE001
        return back(target, xato=f"Instagram bilan ulanmadi: {e}")

    # Shu Instagram allaqachon boshqa profilga ulangan bo'lsa — o'shani yangilaymiz, aks holda tanlangan profilni
    existing = db.account_by_ig(ig_id, include_inactive=True) or (db.get_account(info["profil"]) if info.get("profil") else None)
    fields = dict(ig_user_id=ig_id, ig_username=username, ig_token=token["access_token"], active=1)
    if existing:
        db.update_account(existing["id"], **fields)
        acc_id = existing["id"]
    else:
        acc_id = db.add_account(name=username or "Yangi profil", categories="*", business_text="",
                                settings_yaml=default_settings_yaml(), **fields)
    db.kv_set(f"token_refreshed:{acc_id}", iso())
    agent.reload()
    profile = agent.profiles.get(acc_id)
    note = ""
    if profile:
        try:
            await profile.ig.subscribe_webhooks()
        except Exception as e:  # noqa: BLE001
            note = f" Webhook obunasi xato: {e}"
        await agent.refresh_posts(profile)
    db.log("tizim", f"Instagram ulandi: @{username}", acc_id)
    return back(f"/profil/{acc_id}", ok=f"@{username} ulandi.{note} Endi kategoriyalar va biznes matnini tekshiring.")


SIMPLE_SETTINGS = [
    # (yo'l, turi, nomi, guruh)
    ("agent.ism", "text", "Agent o'zini qanday tanishtiradi", "🤖 Agent"),
    ("agent.uslub", "area", "Yozish uslubi", "🤖 Agent"),
    ("direct.yoqilgan", "bool", "Direct xabarlarga javob beradi", "💬 Direct va sotuv"),
    ("sotuv.narx_aytish", "bool", "Narxni aytadi", "💬 Direct va sotuv"),
    ("sotuv.operatorga_uzatish", "bool", "Murakkab savolda operatorni chaqiradi", "💬 Direct va sotuv"),
    ("agent.max_javoblar", "int", "Bitta dialogda ko'pi bilan nechta javob", "💬 Direct va sotuv"),
    ("sotuv.lid_savollari", "lines", "Raqamdan keyin so'raladigan savollar (har qatorda bittadan)", "💬 Direct va sotuv"),
    ("sotuv.raqam_tasdiq_matni", "area", "Raqam olinganda yuboriladigan matn", "💬 Direct va sotuv"),
    ("komment.yoqilgan", "bool", "Kommentlarga javob beradi", "🗨️ Kommentlar"),
    ("komment.directga_yozish", "bool", "Savol yozganga Direct'da yozadi", "🗨️ Kommentlar"),
    ("komment.neytral_kommentga_javob", "bool", "«Mashallah», emojiga rahmat yozadi", "🗨️ Kommentlar"),
    ("moderatsiya.yoqilgan", "bool", "Haqorat va spamni tozalaydi", "🗨️ Kommentlar"),
    ("komment.cta_matni", "text", "Komment ostidagi yozuv", "🗨️ Kommentlar"),
    ("eslatma.yoqilgan", "bool", "Jim qolgan mijozga eslatma yuboradi", "⏰ Eslatma va xotira"),
    ("eslatma.kechikish_daqiqa", "int", "Eslatma necha daqiqadan keyin", "⏰ Eslatma va xotira"),
    ("xotira.mijozga_darhol_yuborish", "bool", "Telegram'dagi javobingizni mijozga darhol yetkazadi", "⏰ Eslatma va xotira"),
    ("operator_yozsa_ai_toxtash_soat", "int", "Siz qo'lda yozsangiz AI necha soat jim turadi", "⏰ Eslatma va xotira"),
    ("test_rejimi.yoqilgan", "bool", "Test rejimi — faqat test akkauntlarga javob beradi", "🧪 Test rejimi"),
    ("test_rejimi.usernamelar", "lines", "Test akkauntlar (har qatorda bitta username)", "🧪 Test rejimi"),
]


def _set_path(data: dict, path: str, value) -> None:
    node = data
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


@app.post("/profil/{acc_id}/sozlamalar")
async def profile_settings_save(acc_id: int, request: Request, _: str = Depends(admin)):
    acc = db.get_account(acc_id)
    if not acc:
        raise HTTPException(404)
    form = await request.form()
    data = validate_yaml_dict(acc.get("settings_yaml") or default_settings_yaml())
    for path, kind, _label, _group in SIMPLE_SETTINGS:
        raw = form.get(path)
        if kind == "bool":
            value = raw == "1"
        elif kind == "int":
            try:
                value = int(str(raw or "0").strip())
            except ValueError:
                continue
        elif kind == "lines":
            value = [x.strip().lstrip("@") if "username" in path else x.strip()
                     for x in str(raw or "").splitlines() if x.strip()]
        else:
            value = str(raw or "").replace("\r\n", "\n").strip()
        _set_path(data, path, value)
    db.update_account(acc_id, settings_yaml=yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000))
    agent.reload()
    return back(f"/profil/{acc_id}#sozlamalar", ok="Sozlamalar saqlandi")


@app.post("/profil/{acc_id}/tarix")
async def profile_history(acc_id: int, _: str = Depends(admin)):
    prof = agent.profiles.get(acc_id)
    if not prof or not (prof.account.get("ig_token") and prof.ig_id):
        return back(f"/profil/{acc_id}", xato="Avval Instagram'ni ulang — tarix Instagram'dan o'qiladi.")
    db.kv_set(f"tarix:{acc_id}", json.dumps({"holat": "ishlayapti", "vaqt": iso()}))

    async def run():
        try:
            await agent.import_history(prof)
        except Exception as e:  # noqa: BLE001
            db.kv_set(f"tarix:{acc_id}", json.dumps({"holat": "xato", "xato": str(e)[:300], "vaqt": iso()}, ensure_ascii=False))
    agent.spawn(run())
    msg = "Eski yozishmalar o'qilmoqda… Bir-ikki daqiqadan keyin sahifani yangilang."
    if agent.ai is None:
        msg += " (Claude kaliti yo'q — yozishmalar faqat saqlanadi, tahlil qilinmaydi.)"
    return back(f"/profil/{acc_id}#tarix", ok=msg)


@app.post("/profil/{acc_id}/tekshirish")
async def profile_check(acc_id: int, _: str = Depends(admin)):
    acc = db.get_account(acc_id)
    if not acc or not acc.get("ig_token"):
        return back(f"/profil/{acc_id}", xato="Avval Instagram tokenni kiriting")
    ig = make_ig({**acc, "ig_user_id": "me"})
    try:
        me = await ig.me()
        ig_id = str(me.get("user_id") or me.get("id"))
        db.update_account(acc_id, ig_user_id=ig_id, ig_username=me.get("username") or acc.get("ig_username"))
        await make_ig({**acc, "ig_user_id": ig_id}).subscribe_webhooks()
    except Exception as e:  # noqa: BLE001
        return back(f"/profil/{acc_id}", xato=f"Instagram javobi: {e}")
    agent.reload()
    await agent.refresh_posts(agent.profiles.get(acc_id))
    return back(f"/profil/{acc_id}", ok=f"Ulandi: @{me.get('username')} (ID {ig_id}), webhook obunasi yoqildi")


# --- Mahsulotlar ---
@app.get("/mahsulotlar")
async def products(request: Request, _: str = Depends(admin), kategoriya: str = "", q: str = "", xato: str = "", ok: str = ""):
    from collections import Counter
    everything = db.list_products()
    items = [p_ for p_ in everything if not kategoriya or p_["category"] == kategoriya]
    if q.strip():
        needle = q.strip().lower()
        items = [p_ for p_ in items if needle in (p_["name"] or "").lower() or needle in (p_.get("keywords") or "").lower()]
    cat_counts = sorted(Counter(p_["category"] for p_ in everything).items(), key=lambda x: -x[1])
    return render(request, "products.html", products=items, total=len(everything), categories=db.product_categories(),
                  cat_counts=cat_counts, kategoriya=kategoriya, q=q,
                  catalog_url=db.kv_get("catalog_url", env.catalog_csv_url), xato=xato, ok=ok, sites=_sites(),
                  import_status=json.loads(db.kv_get("import_status", "{}") or "{}"), site_sync_hours=env.site_sync_hours)


@app.get("/mahsulot/{pid}")
async def product_page(pid: int, request: Request, _: str = Depends(admin)):
    product = db.get_product(pid) if pid else {"id": 0, "post_ids": [], "in_stock": 1}
    if product is None:
        raise HTTPException(404)
    posts = [p for prof in agent.profiles.values() for p in prof.posts]
    return render(request, "product.html", p=product, categories=db.product_categories(), posts=posts,
                  memory=[m for m in db.list_memory() if m["product_id"] == pid] if pid else [])


@app.post("/mahsulot/{pid}")
async def product_save(pid: int, name: str = Form(...), category: str = Form(...), price: str = Form(""),
                       price_note: str = Form(""), description: str = Form(""), variants: str = Form(""),
                       in_stock: str = Form(""), keywords: str = Form(""), template: str = Form(""),
                       post_ids: str = Form(""), old_price: str = Form(""), amal: str = Form("saqlash"), _: str = Depends(admin)):
    if amal == "ochirish" and pid:
        db.delete_product(pid)
        return back("/mahsulotlar", ok="Mahsulot o'chirildi")
    fields = dict(name=name.strip(), category=category.strip() or "Umumiy", price=catalog.parse_price(price),
                  price_note=price_note.strip(), description=description.strip(), variants=variants.strip(),
                  in_stock=1 if in_stock else 0, keywords=keywords.strip(), template=template.strip(),
                  post_ids=[x.strip() for x in post_ids.replace(",", " ").split() if x.strip()],
                  old_price=catalog.parse_price(old_price))
    if pid:
        db.update_product(pid, **fields)
    else:
        pid = db.upsert_product(**fields, source="qolda")
    return back("/mahsulotlar", ok=f"«{fields['name']}» saqlandi")


@app.post("/mahsulotlar/import")
async def products_import(fayl: UploadFile = File(...), _: str = Depends(admin)):
    raw = await fayl.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    count, errors = catalog.import_csv(db, text)
    return back("/mahsulotlar", xato="; ".join(errors[:5]) if errors else None, ok=f"{count} ta mahsulot yuklandi")


@app.post("/mahsulotlar/sheets")
async def products_sheets(url: str = Form(""), _: str = Depends(admin)):
    db.kv_set("catalog_url", url.strip())
    if not url.strip():
        return back("/mahsulotlar", ok="Google Sheets ulanishi o'chirildi")
    try:
        count, errors = await catalog.sync_from_url(db, url.strip())
    except Exception as e:  # noqa: BLE001
        return back("/mahsulotlar", xato=f"Jadvalni o'qib bo'lmadi: {e}. Jadval «Havolaga ega hamma ko'ra oladi» bo'lishi kerak.")
    return back("/mahsulotlar", ok=f"Google Sheets ulandi: {count} ta mahsulot. Har {env.catalog_sync_minutes} daqiqada yangilanadi.")


# --- Saytdan import (shoxmarket.uz va boshqalar) ---
def _sites() -> list[dict]:
    try:
        return json.loads(db.kv_get("sites", "[]"))
    except json.JSONDecodeError:
        return []


def _save_site(entry: dict) -> None:
    sites = [s for s in _sites() if s["url"] != entry["url"]] + [entry]
    db.kv_set("sites", json.dumps(sites, ensure_ascii=False))


async def _run_site_import(url: str, category: str, max_products: int, mapping: str = "") -> None:
    db.kv_set("import_status", json.dumps({"url": url, "holat": "ishlayapti", "boshlandi": iso()}, ensure_ascii=False))
    report = await scraper.import_site(db, url, ai=agent.ai, category=category, max_products=max_products,
                                       category_map=scraper.parse_category_map(mapping))
    _save_site({"url": url, "category": category, "max": max_products, "mapping": mapping, "oxirgi": iso(),
                "natija": report.summary()})
    db.kv_set("import_status", json.dumps({"url": url, "holat": "tugadi", "natija": report.summary(),
                                           "xatolar": report.errors[:5], "tugadi": iso()}, ensure_ascii=False))


@app.post("/mahsulotlar/sayt")
async def products_site(url: str = Form(...), kategoriya: str = Form(""), soni: str = Form("300"), moslash: str = Form(""),
                        _: str = Depends(admin)):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    status = json.loads(db.kv_get("import_status", "{}") or "{}")
    if status.get("holat") == "ishlayapti":
        return back("/mahsulotlar", xato="Boshqa import hali tugamadi — biroz kuting.")
    max_products = max(1, min(int(soni) if soni.strip().isdigit() else 300, 1000))
    if not moslash.strip():
        moslash = next((s.get("mapping", "") for s in _sites() if s["url"] == url), "")
    agent.spawn(_run_site_import(url, kategoriya.strip(), max_products, moslash.replace("\r\n", "\n").strip()))
    return back("/mahsulotlar", ok=f"{urlparse(url).netloc} dan import boshlandi. Bir-ikki daqiqadan keyin sahifani yangilang.")


@app.post("/mahsulotlar/sayt/ochirish")
async def products_site_remove(url: str = Form(...), _: str = Depends(admin)):
    db.kv_set("sites", json.dumps([s for s in _sites() if s["url"] != url], ensure_ascii=False))
    return back("/mahsulotlar", ok="Sayt avtomatik yangilanishdan olib tashlandi (mahsulotlar katalogda qoladi).")


@app.get("/mahsulotlar/namuna.csv")
async def products_template(_: str = Depends(admin)):
    return Response(catalog.CSV_TEMPLATE, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=katalog_namuna.csv"})


# --- Xotira ---
@app.get("/xotira")
async def memory_page(request: Request, _: str = Depends(admin), ok: str = ""):
    return render(request, "memory.html", memory=db.list_memory(), products=db.list_products(), ok=ok)


@app.post("/xotira/{mid}")
async def memory_save(mid: int, question: str = Form(""), answer: str = Form(""), account_id: str = Form(""),
                      product_id: str = Form(""), amal: str = Form("saqlash"), _: str = Depends(admin)):
    if amal == "ochirish":
        db.delete_memory(mid)
        return back("/xotira", ok="O'chirildi")
    if not question.strip() or not answer.strip():
        return back("/xotira")
    if mid:
        db.update_memory(mid, question, answer, _opt_int(account_id), _opt_int(product_id))
    else:
        db.add_memory(question, answer, _opt_int(account_id), _opt_int(product_id), "panel")
    return back("/xotira", ok="Saqlandi")


# --- Javobsiz savollar ---
@app.get("/savollar")
async def gaps(request: Request, _: str = Depends(admin), ok: str = ""):
    return render(request, "gaps.html", gaps=db.list_gaps(), ok=ok)


@app.post("/savollar/guruhga")
async def gaps_to_telegram(_: str = Depends(admin)):
    """Javob kutayotgan savollarni Telegram guruhga yuborish — Reply qilib javob berish uchun."""
    if not (env.telegram_bot_token and env.telegram_chat_id):
        return back("/savollar", xato="Avval Telegram bot va guruhni ulang (Ulanishlar).")
    sent = 0
    for gap in db.list_gaps(status="kutilmoqda")[:15]:
        prof = agent.profiles.get(gap["account_id"])
        if prof:
            await agent._send_gap(prof, gap, None, db.get_product(gap.get("product_id")))
            sent += 1
    return back("/savollar", ok=f"{sent} ta savol guruhga yuborildi — Telegram'da Reply qilib javob yozing.")


@app.post("/savollar/{gap_id}")
async def gap_answer(gap_id: int, javob: str = Form(""), qamrov: str = Form("profil"), amal: str = Form("saqlash"),
                     _: str = Depends(admin)):
    if not db.get_gap(gap_id):
        raise HTTPException(404)
    if amal == "etiborsiz":
        db.set_gap(gap_id, None, "e'tiborsiz")
        return back("/savollar")
    sent = await agent.answer_gap(gap_id, javob, source="panel", scope=qamrov)
    return back("/savollar", ok=f"Xotiraga saqlandi{f', {sent} ta mijozga yuborildi' if sent else ''}")


# --- Dialoglar va kommentlar ---
@app.get("/dialoglar")
async def dialogs(request: Request, _: str = Depends(admin), profil: int = 0):
    return render(request, "dialogs.html", convs=db.list_convs(profil or None), now=iso(), profil=profil)


@app.get("/dialog/{conv_id}")
async def dialog(conv_id: str, request: Request, _: str = Depends(admin)):
    conv = db.get_conv(conv_id)
    if not conv:
        raise HTTPException(404)
    return render(request, "dialog.html", conv=conv, messages=db.history(conv_id, 200), active=agent.ai_active(conv),
                  account=db.get_account(conv["account_id"]))


@app.post("/dialog/{conv_id}/ai")
async def dialog_toggle(conv_id: str, holat: str = Form(...), _: str = Depends(admin)):
    if holat == "yoq":
        db.update_conv(conv_id, ai_enabled=1, paused_until=None, handoff_sent=0, ai_replies=0)
    else:
        db.update_conv(conv_id, ai_enabled=0)
    return RedirectResponse(f"/dialog/{quote(conv_id)}", 303)


@app.post("/dialog/{conv_id}/yuborish")
async def dialog_send(conv_id: str, matn: str = Form(...), _: str = Depends(admin)):
    await agent.send_operator_message(conv_id, matn)
    return RedirectResponse(f"/dialog/{quote(conv_id)}", 303)


@app.get("/kommentlar")
async def comments(request: Request, _: str = Depends(admin), profil: int = 0):
    return render(request, "comments.html", comments=db.list_comments(profil or None), profil=profil)


@app.post("/telegram/test")
async def telegram_test(_: str = Depends(admin)):
    chats = {env.telegram_chat_id} | {a["tg_chat_id"] for a in db.list_accounts() if a.get("tg_chat_id")}
    results = []
    for chat in filter(None, chats):
        results.append(bool(await tg.send("✅ Sotuv agenti: Telegram ulangan, lidlar va savollar shu yerga keladi.", chat)))
    if env.public_url and env.telegram_webhook_secret:
        await tg.set_webhook(f"{env.public_url}/telegram/webhook", env.telegram_webhook_secret)
    db.log("telegram", f"test xabar: {sum(results)}/{len(results)} guruhga yuborildi")
    return RedirectResponse("/", 303)
