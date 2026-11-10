"""Boshqaruv agenti: egasi panelda yozadi yoki gapiradi — agent hisobot beradi, muammolarni topadi va
o'zgarishlarni (mijozga xabar, narx, ko'rsatma, sozlama, xotira) faqat egasi tasdiqlagandan keyin bajaradi.

O'qish vositalari darhol ishlaydi. O'zgartiruvchi vositalar «tasdiq kartasi» yaratadi; egasi kartadagi
«Tasdiqlash» tugmasini bosmaguncha hech narsa o'zgarmaydi va hech kimga hech narsa yuborilmaydi.
"""
from __future__ import annotations

import html
import json
import logging
from datetime import timedelta, timezone

import anthropic
import yaml

from .db import DB, iso, now, parse_iso
from .settings import default_settings_yaml, validate_yaml_dict

log = logging.getLogger("control")

TASHKENT = timezone(timedelta(hours=5))
MAX_TOOL_ROUNDS = 8
MAX_RECIPIENTS = 20
HISTORY_LIMIT = 40
RESULT_LIMIT = 12000


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


INT = {"type": "integer"}
STR = {"type": "string"}

READ_TOOLS = [
    {"name": "hisobot", "description": "Davr bo'yicha hisobot: har bir profil uchun yangi dialoglar, mijoz xabarlari, AI javoblari, "
     "lidlar (holati bilan), kommentlar (turlari bilan), operator kerak bo'lgan va AI to'xtatilgan dialoglar, javobsiz savollar. "
     "Hisobot, statistika, «bugun nima bo'ldi» kabi savollarda ishlat.",
     "input_schema": _obj({"kunlar": {"type": "number", "description": "Necha kunlik (1 = oxirgi 24 soat). Standart 1."},
                           "profil_id": {**INT, "description": "Faqat shu profil (ixtiyoriy)"}})},
    {"name": "lidlar", "description": "Oxirgi lidlar (telefon qoldirgan mijozlar): ism, raqam, mahsulot, viloyat, holat (yangi/sotildi/bolmadi).",
     "input_schema": _obj({"kunlar": {"type": "number", "description": "Standart 7"},
                           "holat": {"type": "string", "enum": ["yangi", "sotildi", "bolmadi"]},
                           "limit": {**INT, "description": "Standart 20"}})},
    {"name": "dialoglar", "description": "Mijozlar bilan Direct dialoglari ro'yxati (oxirgi xabar bilan). conv_id shu yerdan olinadi.",
     "input_schema": _obj({"qidiruv": {**STR, "description": "username, ism, telefon yoki xabar matnidan bo'lak"},
                           "profil_id": INT,
                           "filtr": {"type": "string", "enum": ["hammasi", "operator_kerak", "raqamsiz", "raqamli", "ai_toxtagan", "javob_kutmoqda"],
                                     "description": "javob_kutmoqda — oxirgi xabar mijozniki"},
                           "kunlar": {"type": "number", "description": "Faqat oxirgi N kunda yozganlar"},
                           "limit": {**INT, "description": "Standart 15"}})},
    {"name": "dialogni_oqish", "description": "Bitta dialogning to'liq yozishmasi va holati.",
     "input_schema": _obj({"conv_id": STR, "limit": {**INT, "description": "Standart 30"}}, ["conv_id"])},
    {"name": "mahsulotlar", "description": "Katalogdan mahsulot qidirish (nom, kalit so'z, tavsif bo'yicha). id, narx, eski narx, mavjudlik, manba.",
     "input_schema": _obj({"qidiruv": STR, "limit": {**INT, "description": "Standart 15"}})},
    {"name": "profillar", "description": "Instagram profillari: id, nom, username, ulanganmi, kategoriyalar, asosiy sozlamalar.",
     "input_schema": _obj({})},
    {"name": "profil_korsatmasi", "description": "Profilning to'liq ko'rsatmasi (biznes matni — agent qanday gapirishi, yetkazish, taqiqlar) va YAML sozlamalari.",
     "input_schema": _obj({"profil_id": INT}, ["profil_id"])},
    {"name": "javobsiz_savollar", "description": "Agent javob bera olmagan, egasi javobini kutayotgan savollar (gap_id bilan).",
     "input_schema": _obj({"holat": {"type": "string", "enum": ["kutilmoqda", "taklif", "javob berildi", "e'tiborsiz"]},
                           "limit": {**INT, "description": "Standart 20"}})},
    {"name": "xotira", "description": "Xotira (bilim): egasi tasdiqlagan savol-javoblar. memory_id shu yerdan.",
     "input_schema": _obj({"qidiruv": STR, "limit": {**INT, "description": "Standart 20"}})},
    {"name": "hodisalar", "description": "Tizim jurnali: xatolar, Telegram/Instagram muammolari, katalog yangilanishi va h.k. «Muammo bormi?» savolida ishlat.",
     "input_schema": _obj({"limit": {**INT, "description": "Standart 25"}})},
    {"name": "reklamalar", "description": "Meta reklamalari bazasi: qaysi mahsulot nechta faol reklamada, qaysi sahifadan.",
     "input_schema": _obj({})},
]

WRITE_TOOLS = [
    {"name": "mijozga_yozish", "description": f"Bir yoki bir nechta mijozga Instagram Direct orqali xabar yuborish (ko'pi bilan {MAX_RECIPIENTS} ta). "
     "Tasdiq kartasi yaratadi. Instagram faqat mijoz oxirgi 24 soatda yozgan bo'lsa yuborishga ruxsat beradi. "
     "Yuborilgan dialogda AI vaqtincha jim turadi (operator yozgandek).",
     "input_schema": _obj({"conv_ids": {"type": "array", "items": STR, "description": "dialoglar vositasidan olingan conv_id lar"},
                           "matn": STR}, ["conv_ids", "matn"])},
    {"name": "telegramga_yozish", "description": "Asosiy Telegram guruhga xabar yuborish (masalan operatorlarga topshiriq yoki hisobot).",
     "input_schema": _obj({"matn": STR}, ["matn"])},
    {"name": "mahsulotni_ozgartirish", "description": "Mahsulot narxi, eski narxi, nomi, tavsifi, kalit so'zlari yoki mavjudligini o'zgartirish.",
     "input_schema": _obj({"product_id": INT, "nom": STR, "narx": INT, "eski_narx": INT, "tavsif": STR,
                           "kalit_sozlar": STR, "mavjud": {"type": "boolean"}}, ["product_id"])},
    {"name": "xotiraga_qoshish", "description": "Xotiraga yangi savol-javob qo'shish — agent mijozlarga shu javobni beradi.",
     "input_schema": _obj({"savol": STR, "javob": STR, "profil_id": {**INT, "description": "bo'sh — hamma profil uchun"},
                           "product_id": INT}, ["savol", "javob"])},
    {"name": "xotirani_ochirish", "description": "Xotiradagi noto'g'ri yoki eskirgan yozuvni o'chirish.",
     "input_schema": _obj({"memory_id": INT}, ["memory_id"])},
    {"name": "savolga_javob", "description": "Javobsiz savolga javob berish: xotiraga saqlanadi va shu savolni kutayotgan mijozlarga yuboriladi.",
     "input_schema": _obj({"gap_id": INT, "javob": STR}, ["gap_id", "javob"])},
    {"name": "korsatmani_ozgartirish", "description": "Profil ko'rsatmasini (biznes matni) o'zgartirish: oxiriga qo'shish yoki butunlay almashtirish.",
     "input_schema": _obj({"profil_id": INT, "rejim": {"type": "string", "enum": ["qoshish", "almashtirish"]}, "matn": STR},
                          ["profil_id", "rejim", "matn"])},
    {"name": "sozlamani_ozgartirish", "description": "Profil sozlamasini o'zgartirish. kalit — YAML yo'li, masalan 'komment.yoqilgan', "
     "'agent.uslub', 'eslatma.kechikish_daqiqa', 'sotuv.lid_savollari'. Faqat mavjud kalitlar.",
     "input_schema": _obj({"profil_id": INT, "kalit": STR,
                           "qiymat": {"description": "true/false, son, matn yoki matnlar ro'yxati"}}, ["profil_id", "kalit", "qiymat"])},
    {"name": "ai_holati", "description": "Bitta dialogda AI'ni yoqish yoki o'chirish.",
     "input_schema": _obj({"conv_id": STR, "holat": {"type": "string", "enum": ["yoq", "ochir"]}}, ["conv_id", "holat"])},
    {"name": "lid_holati", "description": "Lid holatini belgilash: sotildi, bolmadi yoki yangi.",
     "input_schema": _obj({"lead_id": INT, "holat": {"type": "string", "enum": ["sotildi", "bolmadi", "yangi"]}}, ["lead_id", "holat"])},
]

WRITE_NAMES = {t["name"] for t in WRITE_TOOLS}
TOOLS = READ_TOOLS + WRITE_TOOLS

SYSTEM = """# ROL
Sen — «Sotuv agenti» platformasining boshqaruv agentisan. Platforma egasi bilan gaplashasan: u yozadi yoki ovoz bilan gapiradi
(ovozdan matnga o'girilgan, shuning uchun so'zlarda xato bo'lishi mumkin — ma'nosini tushun, noaniq bo'lsa qisqa so'ra).
Platforma Instagram Direct va kommentlarga AI-sotuvchi orqali javob beradi, telefon raqamlarni lid qilib Telegram guruhga yuboradi.

# QOIDALAR
- Raqam, ism, narx, holat — faqat vositalardan olingan ma'lumot. Hech narsani o'zingdan to'qima. Kerak bo'lsa avval vosita bilan tekshir.
- O'zgartiruvchi vositalar (mijozga yozish, Telegram, narx, ko'rsatma, sozlama, xotira, AI holati, lid holati) darhol bajarilmaydi:
  ular tasdiq kartasi yaratadi. Karta yaratilgach «bajarildi» dema — «kartani tasdiqlang» de.
- Egasi aniq so'ramagan o'zgarishni taklif qilishing mumkin, lekin kartani faqat u rozi bo'lsa yoki aniq buyursa yarat.
- Mijozga yoziladigan matn: mijoz tilida, qisqa, xushmuomala, do'kon nomidan. Narx va shartlar — faqat katalog/ko'rsatmadan.
- Instagram qoidasi: mijoz oxirgi 24 soatda yozmagan bo'lsa unga Direct yuborib bo'lmaydi — buni ogohlantir.
- Bir vaqtda ko'pi bilan {max_rcpt} ta mijozga yoz. Ko'proq bo'lsa, eng muhimlarini tanla va buni ayt.
- Javoblar o'zbek tilida (egasi rus tilida yozsa — rus tilida), qisqa va aniq: avval xulosa, keyin kerak bo'lsa 3–7 qatorli ro'yxat.
  Markdown jadval ishlatma; oddiy qatorlar va «•» belgisi yetarli.
- Hisobotda muammolarni alohida ko'rsat: operator kutayotgan dialoglar, javobsiz savollar, Telegram'ga yetmagan lidlar, xatolar.

# HOZIRGI HOLAT
Bugun: {today} (Toshkent vaqti).
Profillar:
{profiles}
"""


def _fmt_time(value: str | None) -> str:
    dt = parse_iso(value)
    return dt.astimezone(TASHKENT).strftime("%d.%m %H:%M") if dt else ""


def _money(n) -> str:
    return f"{n:,}".replace(",", " ") + " so'm" if isinstance(n, int) else "—"


def _clip(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _set_path(data: dict, path: str, value) -> None:
    node = data
    keys = path.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value


def _get_path(data: dict, path: str):
    node = data
    for k in path.split("."):
        if not isinstance(node, dict) or k not in node:
            raise KeyError(path)
        node = node[k]
    return node


class ControlAgent:
    def __init__(self, db: DB, agent, tg, telegram_chat=lambda: ""):
        self.db = db
        self.agent = agent          # logic.Agent — ai, profiles, reload(), send_operator_message(), answer_gap()
        self.tg = tg
        self.telegram_chat = telegram_chat

    # ------------------------------------------------------------------ suhbat
    def _system(self) -> str:
        lines = []
        for a in self.db.list_accounts():
            ig = "ulangan" if a.get("ig_user_id") and a.get("ig_token") else "Instagram ulanmagan"
            lines.append(f"- [{a['id']}] {a['name']} — @{a.get('ig_username') or '—'} ({ig}{', o‘chirilgan' if not a.get('active') else ''})")
        return SYSTEM.format(max_rcpt=MAX_RECIPIENTS, today=now().astimezone(TASHKENT).strftime("%d.%m.%Y %H:%M"),
                             profiles="\n".join(lines) or "(profil yo'q)")

    def _history(self) -> list[dict]:
        """Saqlangan suhbatni Claude formatiga o'tkazadi (karta va tizim izohlari foydalanuvchi tomonida)."""
        msgs: list[dict] = []
        for m in self.db.control_messages(HISTORY_LIMIT):
            if m["role"] == "assistant":
                role, text = "assistant", m["text"]
            elif m["role"] == "action":
                role = "user"
                text = (f"[Tizim] Tasdiq kartasi #{m['action_id']}: {m.get('title')} — holati: {m.get('action_status')}"
                        + (f" ({m['action_result']})" if m.get("action_result") else ""))
            elif m["role"] == "note":
                role, text = "user", f"[Tizim] {m['text']}"
            else:
                role, text = "user", m["text"]
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n\n" + text
            else:
                msgs.append({"role": role, "content": text})
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    async def chat(self, text: str) -> list[dict]:
        """Egasining xabari → agent javobi (va kerak bo'lsa tasdiq kartalari). Yangi xabarlarni qaytaradi."""
        text = (text or "").strip()
        if not text:
            return []
        first_id = self.db.add_control_message("user", text[:4000])
        ai = self.agent.ai
        if ai is None:
            self.db.add_control_message("assistant", "⚠️ Claude kaliti kiritilmagan — Ulanishlar → Claude bo'limida kiriting.")
            return self.db.control_messages(after_id=first_id - 1)
        messages = self._history()
        system = self._system()
        reply = ""
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = await ai.client.messages.create(
                    model=ai.model, max_tokens=4000, system=system, tools=TOOLS, messages=messages,
                    output_config={"effort": "medium"},
                )
                if response.stop_reason == "refusal":
                    reply = "Bu so'rovni bajara olmayman."
                    break
                tool_calls = [b for b in response.content if b.type == "tool_use"]
                texts = [b.text for b in response.content if b.type == "text" and b.text.strip()]
                if not tool_calls:
                    reply = "\n\n".join(texts).strip()
                    if response.stop_reason == "max_tokens":
                        reply += "\n\n(javob uzun bo'lib, kesildi)"
                    break
                messages.append({"role": "assistant", "content": response.content})
                results = []
                for call in tool_calls:
                    try:
                        content = await self._run_tool(call.name, dict(call.input or {}))
                        results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})
                    except Exception as e:  # noqa: BLE001 — xatoni Claude'ga qaytaramiz, u egasiga tushuntiradi
                        log.warning("Boshqaruv vositasi %s xato: %s", call.name, e)
                        results.append({"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                                        "content": f"Xato: {e}"[:1000]})
                messages.append({"role": "user", "content": results})
            else:
                reply = reply or "Vazifa juda ko'p qadam talab qildi — savolni aniqroq yozing."
        except anthropic.AuthenticationError:
            reply = "⚠️ Claude kaliti noto'g'ri (Ulanishlar → Claude)."
        except anthropic.RateLimitError:
            reply = "⚠️ Claude so'rovlar limiti — bir daqiqadan keyin qayta urinib ko'ring."
        except anthropic.APIConnectionError:
            reply = "⚠️ Claude bilan aloqa yo'q (internet)."
        except anthropic.APIStatusError as e:
            reply = f"⚠️ Claude xatosi {e.status_code}: {_clip(e.message, 200)}"
        if reply:
            self.db.add_control_message("assistant", reply)
        return self.db.control_messages(after_id=first_id - 1)

    async def _run_tool(self, name: str, args: dict) -> str:
        if name in WRITE_NAMES:
            title, details, clean = self._prepare_action(name, args)
            action_id = self.db.add_control_action(name, clean, title, details)
            self.db.add_control_message("action", title, action_id)
            return (f"Tasdiq kartasi #{action_id} yaratildi: «{title}». Egasi «Tasdiqlash»ni bosmaguncha hech narsa "
                    "bajarilmaydi. Egasiga kartani ko'rib tasdiqlashini ayt.")
        fn = getattr(self, f"_t_{name}", None)
        if fn is None:
            raise ValueError(f"noma'lum vosita {name}")
        out = fn(**args)
        text = json.dumps(out, ensure_ascii=False, default=str)
        return text if len(text) <= RESULT_LIMIT else text[:RESULT_LIMIT] + "… (qisqartirildi)"

    # ------------------------------------------------------------------ o'qish vositalari
    def _t_hisobot(self, kunlar: float = 1, profil_id: int | None = None) -> dict:
        kunlar = max(0.05, min(float(kunlar or 1), 365))
        since = iso(now() - timedelta(days=kunlar))
        unsent = self.db._one("SELECT COUNT(*) AS n FROM leads WHERE tg_sent=0", ())["n"]
        problems = [e for e in self.db.events(60) if (e.get("created_at") or "") >= since
                    and any(w in (e.get("text") or "").lower() for w in ("xato", "error", "muammo", "yuborilmadi", "rad", "token"))]
        return {"davr_kun": kunlar, "profillar": self.db.period_report(kunlar, profil_id),
                "telegramga_yetmagan_lidlar": unsent,
                "muammoli_hodisalar": [{"vaqt": _fmt_time(e["created_at"]), "tur": e["kind"], "matn": _clip(e["text"], 200)}
                                       for e in problems[:10]]}

    def _t_lidlar(self, kunlar: float = 7, holat: str | None = None, limit: int = 20) -> list[dict]:
        since = iso(now() - timedelta(days=float(kunlar or 7)))
        sql = ("SELECT l.*, a.name AS profil, c.username FROM leads l LEFT JOIN accounts a ON a.id=l.account_id "
               "LEFT JOIN conversations c ON c.id=l.conv_id WHERE l.created_at>=?")
        params: list = [since]
        if holat:
            sql += " AND COALESCE(l.status,'yangi')=?"
            params.append(holat)
        rows = self.db._all(sql + " ORDER BY l.id DESC LIMIT ?", (*params, min(int(limit or 20), 100)))
        out = []
        for r in rows:
            d = json.loads(r.get("data") or "{}")
            out.append({"lead_id": r["id"], "vaqt": _fmt_time(r["created_at"]), "profil": r.get("profil"),
                        "username": r.get("username"), "ism": d.get("name"), "telefon": r.get("phone"),
                        "mahsulot": d.get("interest"), "viloyat": d.get("region"), "holat": r.get("status") or "yangi",
                        "telegramga_yuborilgan": bool(r.get("tg_sent")), "conv_id": r.get("conv_id"), "manba": r.get("source")})
        return out

    def _t_dialoglar(self, qidiruv: str | None = None, profil_id: int | None = None, filtr: str = "hammasi",
                     kunlar: float | None = None, limit: int = 15) -> list[dict]:
        rows = self.db.list_convs(profil_id or None, 500)
        q = (qidiruv or "").lower().strip()
        since = iso(now() - timedelta(days=float(kunlar))) if kunlar else ""
        out = []
        for c in rows:
            if since and (c.get("last_user_at") or "") < since:
                continue
            if q and q not in " ".join(str(c.get(k) or "") for k in ("username", "name", "phone", "last_text")).lower():
                continue
            last_role = self.db.last_message_role(c["id"])
            paused = c.get("paused_until") and c["paused_until"] > iso()
            if filtr == "operator_kerak" and not c.get("handoff_sent"):
                continue
            if filtr == "raqamsiz" and c.get("phone"):
                continue
            if filtr == "raqamli" and not c.get("phone"):
                continue
            if filtr == "ai_toxtagan" and c.get("ai_enabled") and not paused:
                continue
            if filtr == "javob_kutmoqda" and last_role != "user":
                continue
            out.append({"conv_id": c["id"], "profil": c.get("account_name"), "username": c.get("username"), "ism": c.get("name"),
                        "telefon": c.get("phone"), "oxirgi_mijoz_xabari": _fmt_time(c.get("last_user_at")),
                        "oxirgi_xabar": _clip(c.get("last_text"), 160), "oxirgi_yozgan": last_role,
                        "ai": "o'chirilgan" if not c.get("ai_enabled") else ("vaqtincha jim" if paused else "yoqilgan"),
                        "operator_kerak": bool(c.get("handoff_sent")),
                        "direct_ochiq_24soat": self.agent.dm_window_open(c["id"])})
            if len(out) >= min(int(limit or 15), 50):
                break
        return out

    def _t_dialogni_oqish(self, conv_id: str, limit: int = 30) -> dict:
        conv = self.db.get_conv(conv_id)
        if not conv:
            raise ValueError("Bunday dialog yo'q")
        msgs = self.db.history(conv_id, min(int(limit or 30), 100))
        return {"conv_id": conv_id, "username": conv.get("username"), "ism": conv.get("name"), "telefon": conv.get("phone"),
                "ai_yoqilgan": bool(conv.get("ai_enabled")), "jim_gacha": _fmt_time(conv.get("paused_until")),
                "lid": json.loads(conv.get("lead_data") or "{}"), "direct_ochiq_24soat": self.agent.dm_window_open(conv_id),
                "xabarlar": [{"kim": {"user": "mijoz", "assistant": "AI", "operator": "operator"}.get(m["role"], m["role"]),
                              "vaqt": _fmt_time(m.get("created_at")), "matn": _clip(m.get("text"), 600)} for m in msgs]}

    def _t_mahsulotlar(self, qidiruv: str | None = None, limit: int = 15) -> list[dict]:
        q = f"%{(qidiruv or '').strip().lower()}%"
        rows = self.db._all("SELECT * FROM products WHERE lower(name) LIKE ? OR lower(COALESCE(keywords,'')) LIKE ? "
                            "OR lower(COALESCE(description,'')) LIKE ? ORDER BY in_stock DESC, id LIMIT ?",
                            (q, q, q, min(int(limit or 15), 60)))
        return [{"product_id": p["id"], "nom": p["name"], "kategoriya": p.get("category"), "narx": p.get("price"),
                 "eski_narx": p.get("old_price"), "mavjud": bool(p.get("in_stock", 1)), "manba": p.get("source"),
                 "kalit_sozlar": p.get("keywords"), "tavsif": _clip(p.get("description"), 300)} for p in rows]

    def _t_profillar(self) -> list[dict]:
        out = []
        for a in self.db.list_accounts():
            try:
                cfg = validate_yaml_dict(a.get("settings_yaml") or default_settings_yaml())
            except Exception:  # noqa: BLE001
                cfg = {}
            out.append({"profil_id": a["id"], "nom": a["name"], "instagram": a.get("ig_username"), "faol": bool(a.get("active")),
                        "instagram_ulangan": bool(a.get("ig_user_id") and a.get("ig_token")), "kategoriyalar": a.get("categories"),
                        "direct": (cfg.get("direct") or {}).get("yoqilgan"), "komment": (cfg.get("komment") or {}).get("yoqilgan"),
                        "test_rejimi": (cfg.get("test_rejimi") or {}).get("yoqilgan")})
        return out

    def _t_profil_korsatmasi(self, profil_id: int) -> dict:
        a = self.db.get_account(int(profil_id))
        if not a:
            raise ValueError("Bunday profil yo'q")
        return {"profil_id": a["id"], "nom": a["name"], "korsatma": a.get("business_text") or "",
                "sozlamalar_yaml": a.get("settings_yaml") or default_settings_yaml()}

    def _t_javobsiz_savollar(self, holat: str = "kutilmoqda", limit: int = 20) -> list[dict]:
        rows = self.db.list_gaps(status=holat or None)[: min(int(limit or 20), 60)]
        return [{"gap_id": g["id"], "profil": g.get("account_name"), "savol": g["question"], "necha_marta": g.get("hits"),
                 "mahsulot": g.get("product_name"), "holat": g.get("status"), "javob": g.get("answer"),
                 "kutayotgan_mijozlar": len(self.db.gap_waiters(g["id"]))} for g in rows]

    def _t_xotira(self, qidiruv: str | None = None, limit: int = 20) -> list[dict]:
        q = (qidiruv or "").lower()
        rows = [m for m in self.db.list_memory() if not q or q in f"{m['question']} {m['answer']}".lower()]
        return [{"memory_id": m["id"], "savol": m["question"], "javob": _clip(m["answer"], 400), "profil": m.get("account_name") or "hammasi",
                 "mahsulot": m.get("product_name"), "manba": m.get("source"), "ishlatilgan": m.get("hits")}
                for m in rows[: min(int(limit or 20), 80)]]

    def _t_hodisalar(self, limit: int = 25) -> list[dict]:
        return [{"vaqt": _fmt_time(e["created_at"]), "tur": e["kind"], "profil": e.get("account_name"), "matn": _clip(e["text"], 300)}
                for e in self.db.events(min(int(limit or 25), 100))]

    def _t_reklamalar(self) -> list[dict]:
        rows = self.db._all("SELECT p.id, p.name, p.price, COUNT(a.ad_id) AS n, GROUP_CONCAT(DISTINCT a.page_name) AS pages, "
                            "MIN(a.started_at) AS first FROM ads a LEFT JOIN products p ON p.id=a.product_id WHERE a.active=1 "
                            "GROUP BY a.product_id ORDER BY n DESC", ())
        return [{"product_id": r["id"], "mahsulot": r["name"], "narx": r["price"], "faol_reklamalar": r["n"],
                 "sahifalar": r["pages"], "birinchi_reklama": r["first"]} for r in rows]

    # ------------------------------------------------------------------ tasdiq kartalari
    def _prepare_action(self, name: str, args: dict) -> tuple[str, str, dict]:
        """Tekshiradi va karta sarlavhasi + tafsilotini tayyorlaydi. Xato bo'lsa ValueError (Claude'ga qaytadi)."""
        if name == "mijozga_yozish":
            ids = list(dict.fromkeys(str(x) for x in args.get("conv_ids") or []))
            matn = (args.get("matn") or "").strip()
            if not ids or not matn:
                raise ValueError("conv_ids va matn kerak")
            if len(ids) > MAX_RECIPIENTS:
                raise ValueError(f"Bir kartada ko'pi bilan {MAX_RECIPIENTS} ta mijoz")
            who, closed = [], 0
            for cid in ids:
                conv = self.db.get_conv(cid)
                if not conv:
                    raise ValueError(f"Dialog topilmadi: {cid}")
                is_open = self.agent.dm_window_open(cid)
                closed += 0 if is_open else 1
                who.append(f"@{conv.get('username') or conv['igsid']}" + (f" ({conv['name']})" if conv.get("name") else "")
                           + ("" if is_open else " — 24 soat o'tgan, yuborilmaydi"))
            title = f"{len(ids)} ta mijozga Direct xabar" if len(ids) > 1 else f"{who[0].split(' ')[0]} ga Direct xabar"
            details = "Kimga: " + ", ".join(who) + f"\n\nMatn:\n{matn}"
            if closed:
                details += f"\n\n⚠️ {closed} ta mijozga Instagram qoidasi bo'yicha yuborib bo'lmaydi."
            return title, details, {"conv_ids": ids, "matn": matn}
        if name == "telegramga_yozish":
            matn = (args.get("matn") or "").strip()
            if not matn:
                raise ValueError("matn kerak")
            return "Telegram guruhga xabar", matn, {"matn": matn}
        if name == "mahsulotni_ozgartirish":
            p = self.db.get_product(int(args["product_id"]))
            if not p:
                raise ValueError("Bunday mahsulot yo'q")
            labels = {"nom": ("name", "Nomi"), "narx": ("price", "Narxi"), "eski_narx": ("old_price", "Eski narxi"),
                      "tavsif": ("description", "Tavsif"), "kalit_sozlar": ("keywords", "Kalit so'zlar"), "mavjud": ("in_stock", "Mavjud")}
            changes, lines = {}, []
            for key, (col, label) in labels.items():
                if key in args and args[key] is not None:
                    new = (1 if args[key] else 0) if key == "mavjud" else args[key]
                    old = p.get(col)
                    show = (lambda v: _money(v)) if key in ("narx", "eski_narx") else (lambda v: "ha" if v else "yo'q") if key == "mavjud" else (lambda v: _clip(str(v or "—"), 300))
                    lines.append(f"{label}: {show(old)} → {show(new)}")
                    changes[col] = new
            if not changes:
                raise ValueError("O'zgartiriladigan maydon berilmadi")
            return f"Mahsulot #{p['id']} «{_clip(p['name'], 60)}» o'zgaradi", "\n".join(lines), {"product_id": p["id"], "changes": changes}
        if name == "xotiraga_qoshish":
            savol, javob = (args.get("savol") or "").strip(), (args.get("javob") or "").strip()
            if not savol or not javob:
                raise ValueError("savol va javob kerak")
            acc = self.db.get_account(int(args["profil_id"])) if args.get("profil_id") else None
            prod = self.db.get_product(int(args["product_id"])) if args.get("product_id") else None
            scope = (f"Profil: {acc['name']}" if acc else "Hamma profillar") + (f" · Mahsulot: {prod['name']}" if prod else "")
            return "Xotiraga yangi javob", f"{scope}\n\nSavol: {savol}\nJavob: {javob}", {
                "savol": savol, "javob": javob, "profil_id": acc["id"] if acc else None, "product_id": prod["id"] if prod else None}
        if name == "xotirani_ochirish":
            m = next((x for x in self.db.list_memory() if x["id"] == int(args["memory_id"])), None)
            if not m:
                raise ValueError("Bunday xotira yozuvi yo'q")
            return f"Xotiradan #{m['id']} o'chiriladi", f"Savol: {m['question']}\nJavob: {_clip(m['answer'], 400)}", {"memory_id": m["id"]}
        if name == "savolga_javob":
            g = self.db.get_gap(int(args["gap_id"]))
            javob = (args.get("javob") or "").strip()
            if not g or not javob:
                raise ValueError("Savol topilmadi yoki javob bo'sh")
            waiting = len(self.db.gap_waiters(g["id"]))
            return "Javobsiz savolga javob", (f"Savol: {g['question']}\nJavob: {javob}\n\nXotiraga saqlanadi"
                                              + (f", kutayotgan {waiting} ta mijozga yuboriladi." if waiting else ".")), {
                "gap_id": g["id"], "javob": javob}
        if name == "korsatmani_ozgartirish":
            a = self.db.get_account(int(args["profil_id"]))
            matn = (args.get("matn") or "").strip()
            if not a or not matn:
                raise ValueError("Profil topilmadi yoki matn bo'sh")
            rejim = args.get("rejim") or "qoshish"
            title = f"«{a['name']}» ko'rsatmasi " + ("to'ldiriladi" if rejim == "qoshish" else "almashtiriladi")
            return title, ("Oxiriga qo'shiladi:\n" if rejim == "qoshish" else "Yangi ko'rsatma:\n") + matn, {
                "profil_id": a["id"], "rejim": rejim, "matn": matn}
        if name == "sozlamani_ozgartirish":
            a = self.db.get_account(int(args["profil_id"]))
            if not a:
                raise ValueError("Bunday profil yo'q")
            key = str(args.get("kalit") or "").strip()
            data = validate_yaml_dict(a.get("settings_yaml") or default_settings_yaml())
            try:
                old = _get_path(data, key)
            except KeyError:
                raise ValueError(f"Bunday sozlama yo'q: {key}") from None
            new = args.get("qiymat")
            if isinstance(old, bool) and not isinstance(new, bool):
                new = str(new).strip().lower() in ("true", "1", "ha", "yoq", "yoqilgan", "on")
            elif isinstance(old, int) and not isinstance(old, bool):
                new = int(new)
            elif isinstance(old, list) and isinstance(new, str):
                new = [x.strip() for x in new.splitlines() if x.strip()]
            return f"«{a['name']}» sozlamasi: {key}", f"{old!r} → {new!r}", {"profil_id": a["id"], "kalit": key, "qiymat": new}
        if name == "ai_holati":
            conv = self.db.get_conv(str(args["conv_id"]))
            if not conv:
                raise ValueError("Bunday dialog yo'q")
            on = args.get("holat") == "yoq"
            return (f"@{conv.get('username') or conv['igsid']} dialogida AI " + ("yoqiladi" if on else "o'chiriladi")), "", {
                "conv_id": conv["id"], "holat": "yoq" if on else "ochir"}
        if name == "lid_holati":
            lead = self.db.get_lead(int(args["lead_id"]))
            if not lead:
                raise ValueError("Bunday lid yo'q")
            return f"Lid #{lead['id']} ({lead.get('phone')}) → {args['holat']}", "", {"lead_id": lead["id"], "holat": args["holat"]}
        raise ValueError(f"noma'lum amal {name}")

    async def decide(self, action_id: int, approve: bool) -> dict:
        """Egasi kartani tasdiqladi yoki rad etdi."""
        action = self.db.get_control_action(action_id)
        if not action:
            raise ValueError("Karta topilmadi")
        if action["status"] != "kutilmoqda":
            return action
        if not approve:
            self.db.set_control_action(action_id, "rad etildi", "")
            self.db.add_control_message("note", f"❌ Rad etildi: {action['title']}")
            return self.db.get_control_action(action_id)
        try:
            result = await self._execute(action["tool"], action["args"])
            self.db.set_control_action(action_id, "bajarildi", result)
            self.db.add_control_message("note", f"✅ Bajarildi: {action['title']}" + (f" — {result}" if result else ""))
            self.db.log("boshqaruv", f"{action['title']}: {result}")
        except Exception as e:  # noqa: BLE001
            log.exception("Boshqaruv amali bajarilmadi")
            self.db.set_control_action(action_id, "xato", str(e)[:300])
            self.db.add_control_message("note", f"⚠️ Bajarilmadi: {action['title']} — {e}"[:500])
        return self.db.get_control_action(action_id)

    async def _execute(self, tool: str, a: dict) -> str:
        if tool == "mijozga_yozish":
            sent, closed, failed = 0, 0, 0
            for cid in a["conv_ids"]:
                if not self.agent.dm_window_open(cid):
                    closed += 1
                    continue
                if await self.agent.send_operator_message(cid, a["matn"]):
                    sent += 1
                else:
                    failed += 1
            parts = [f"{sent} ta yuborildi"]
            if closed:
                parts.append(f"{closed} ta — 24 soat o'tgan")
            if failed:
                parts.append(f"{failed} ta — xato")
            if not sent:
                raise RuntimeError(", ".join(parts))
            return ", ".join(parts)
        if tool == "telegramga_yozish":
            mid = await self.tg.send(html.escape(a["matn"]), self.telegram_chat() or None)
            if not mid:
                raise RuntimeError("Telegram yubormadi (bot yoki guruh ulanmagan)")
            return "guruhga yuborildi"
        if tool == "mahsulotni_ozgartirish":
            if not self.db.get_product(a["product_id"]):
                raise RuntimeError("mahsulot o'chirilgan")
            self.db.update_product(a["product_id"], **a["changes"])
            return ""
        if tool == "xotiraga_qoshish":
            mid = self.db.add_memory(a["savol"], a["javob"], a.get("profil_id"), a.get("product_id"), "boshqaruv")
            return f"xotira #{mid}"
        if tool == "xotirani_ochirish":
            self.db.delete_memory(a["memory_id"])
            return ""
        if tool == "savolga_javob":
            sent = await self.agent.answer_gap(a["gap_id"], a["javob"], source="boshqaruv", scope="profil")
            return f"{sent} ta mijozga yuborildi" if sent else "xotiraga saqlandi"
        if tool == "korsatmani_ozgartirish":
            acc = self.db.get_account(a["profil_id"])
            text = a["matn"] if a["rejim"] == "almashtirish" else ((acc.get("business_text") or "").rstrip() + "\n\n" + a["matn"]).strip()
            self.db.update_account(a["profil_id"], business_text=text)
            self.agent.reload()
            return ""
        if tool == "sozlamani_ozgartirish":
            acc = self.db.get_account(a["profil_id"])
            data = validate_yaml_dict(acc.get("settings_yaml") or default_settings_yaml())
            _set_path(data, a["kalit"], a["qiymat"])
            self.db.update_account(a["profil_id"], settings_yaml=yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000))
            self.agent.reload()
            return ""
        if tool == "ai_holati":
            if a["holat"] == "yoq":
                self.db.update_conv(a["conv_id"], ai_enabled=1, paused_until=None, handoff_sent=0, ai_replies=0)
            else:
                self.db.update_conv(a["conv_id"], ai_enabled=0)
            return ""
        if tool == "lid_holati":
            self.db.set_lead_status(a["lead_id"], a["holat"], "boshqaruv agenti")
            return ""
        raise RuntimeError(f"noma'lum amal {tool}")
