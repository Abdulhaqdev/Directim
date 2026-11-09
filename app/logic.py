"""Asosiy mantiq: bir nechta Instagram profil, javoblar, lidlar, xotira."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from . import telegram as tgfmt
from .ai import AI, CommentResult, DMResult, build_knowledge, comment_rules, dm_rules, followup_rules
from .db import DB, conv_key, iso, now, parse_iso, split_categories
from .instagram import IGError, split_text
from .settings import Config

log = logging.getLogger("logic")

PHONE_RE = re.compile(r"(?<!\d)(?:\+?\s*998[\s\-()]*)?(\d{2})[\s\-()]*(\d{3})[\s\-]*(\d{2})[\s\-]*(\d{2})(?!\d)")


def extract_phone(text: str | None) -> str | None:
    """Matndan O'zbekiston raqamini topib +998XXXXXXXXX ko'rinishga keltiradi."""
    if not text:
        return None
    for m in PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if digits.startswith("998"):
            digits = digits[3:]
        if len(digits) == 9:
            return "+998" + digits
    return None


DM_WINDOW = timedelta(hours=23)       # Instagram: mijozning oxirgi xabaridan 24 soat ichida yozish mumkin
SESSION_GAP = timedelta(hours=24)     # shundan ko'p jim tursa — yangi suhbat (hisoblagichlar nolga)
ALERT_COOLDOWN = timedelta(hours=1)


def is_cyrillic(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("Ѐ" <= c <= "ӿ" for c in letters) > len(letters) / 2


@dataclass
class Profile:
    """Bitta Instagram profil va uning sozlamalari."""

    account: dict
    cfg: Config
    ig: object
    posts: list[dict] = field(default_factory=list)

    @property
    def id(self) -> int:
        return self.account["id"]

    @property
    def ig_id(self) -> str:
        return str(self.account.get("ig_user_id") or "")

    @property
    def username(self) -> str:
        return self.account.get("ig_username") or ""

    @property
    def categories(self) -> list[str]:
        return split_categories(self.account.get("categories")) or ["*"]

    @property
    def tg_chat(self) -> str | None:
        return self.account.get("tg_chat_id") or None



ECHO_TEXT_TTL = 600  # soniya: shu vaqt ichida kelgan echo matni agentnikimi — tekshiriladi


def _norm_echo(text: str) -> str:
    return " ".join((text or "").split())



SYSTEM_CARD_TYPES = ("template", "fallback")


def _card_text(att: dict) -> str:
    """Karta (template/fallback) ichidagi ko'rinadigan matn: sarlavha, izoh, havola."""
    payload = att.get("payload") or {}
    parts = [payload.get("title"), payload.get("text"), payload.get("url")]
    generic = payload.get("generic") or {}
    for el in (generic.get("elements") or payload.get("elements") or []):
        if isinstance(el, dict):
            parts += [el.get("title"), el.get("subtitle")]
    return " · ".join(str(x).strip() for x in parts if x and str(x).strip())[:300]


def _is_system_card(message: dict) -> bool:
    """Matnsiz, faqat ichi bo'sh karta(lar)dan iborat xabar."""
    atts = message.get("attachments") or []
    if (message.get("text") or "").strip() or not atts:
        return False
    return all(a.get("type") in SYSTEM_CARD_TYPES and not _card_text(a) for a in atts)


class Agent:
    def __init__(self, db: DB, tg, ai: AI | None, ig_factory: Callable[[dict], object], debounce: float = 4.0):
        self.db = db
        self.tg = tg
        self.ai = ai
        self.ig_factory = ig_factory
        self.debounce = debounce
        self.profiles: dict[int, Profile] = {}
        self._pending: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending_images: dict[str, list[str]] = {}
        self._inflight_comments: set[str] = set()
        self._alerts: dict[str, object] = {}
        self._running: set[str] = set()       # javob hozir yozilayotgan dialoglar
        self._rerun: set[str] = set()         # javob paytida yangi xabar kelgan dialoglar
        self._sending: dict[str, int] = {}    # Instagram'ga xabar ketayotgan mijozlar (echo uchun)
        # Yuborilgan matnlar (echo'ni mid bo'yicha topib bo'lmasa: Facebook login'da yuborish va echo mid'lari har xil)
        self._sent_texts: dict[str, list[tuple[float, str]]] = {}
        self._delivered: set[tuple[int, str]] = set()
        self._tasks: set[asyncio.Task] = set()
        self.on_main_chat = None  # main.py: asosiy guruh /start bilan o'zgarganda
        self.reload()

    # --- profillar ---
    def reload(self) -> None:
        old_posts = {pid: p.posts for pid, p in self.profiles.items()}
        self.profiles = {}
        for acc in self.db.list_accounts(active_only=True):
            self.profiles[acc["id"]] = Profile(
                account=acc, cfg=Config.from_yaml(acc.get("settings_yaml")), ig=self.ig_factory(acc),
                posts=old_posts.get(acc["id"], []),
            )

    def profile_for_ig(self, ig_id: str | None) -> Profile | None:
        for p in self.profiles.values():
            if ig_id and p.ig_id == str(ig_id):
                return p
        return None

    def products_for(self, profile: Profile) -> list[dict]:
        return self.db.list_products(profile.categories)

    def knowledge(self, profile: Profile) -> str:
        products = self.products_for(profile)
        memory = self.db.memory_for(profile.id, {p["id"] for p in products})
        return build_knowledge(profile.account.get("business_text") or "", products, memory, profile.posts)

    async def refresh_posts(self, profile: Profile | None = None) -> None:
        for p in [profile] if profile else list(self.profiles.values()):
            if not p.account.get("ig_token") or not p.ig_id:
                continue  # Instagram hali ulanmagan
            try:
                p.posts = await p.ig.recent_media(limit=25)
                log.info("%s: postlar yangilandi (%d)", p.username, len(p.posts))
            except Exception as e:  # noqa: BLE001
                log.warning("%s: postlarni olishda xato: %s", p.username, e)

    def _allowed(self, profile: Profile, username: str | None) -> bool:
        if not profile.cfg.get("test_rejimi.yoqilgan", False):
            return True
        allowed = {u.lower().lstrip("@") for u in profile.cfg.get("test_rejimi.usernamelar", []) or []}
        return bool(username) and username.lower() in allowed

    def ai_active(self, conv: dict) -> bool:
        if not conv.get("ai_enabled"):
            return False
        paused = parse_iso(conv.get("paused_until"))
        return not (paused and paused > now())

    def _pause(self, profile: Profile, conv_id: str) -> float:
        hours = float(profile.cfg.get("operator_yozsa_ai_toxtash_soat", 12))
        self.db.update_conv(conv_id, paused_until=iso(now() + timedelta(hours=hours)))
        return hours

    async def alert(self, profile: Profile | None, key: str, text: str) -> None:
        """Muammo haqida Telegram'ga xabar (bir xil muammo — soatiga ko'pi bilan bir marta)."""
        full_key = f"{profile.id if profile else 0}:{key}"
        last = self._alerts.get(full_key)
        if last and now() - last < ALERT_COOLDOWN:  # type: ignore[operator]
            return
        self._alerts[full_key] = now()
        name = f"{profile.account.get('name')} (@{profile.username})" if profile else "Tizim"
        await self.tg.send(f"⚠️ <b>{tgfmt.esc(name)}</b>\n{tgfmt.esc(text)}", profile.tg_chat if profile else None)
        self.db.log("xato", text, profile.id if profile else None)

    async def _ig_failed(self, profile: Profile, e: Exception, action: str) -> None:
        if isinstance(e, IGError) and e.token_problem:
            await self.alert(profile, "token", "Instagram token ishlamayapti — agent javob yubora olmayapti. "
                                               "Panel → Profillar → yangi token kiriting va «Tekshirish va ulash».")
        else:
            await self.alert(profile, f"ig:{action}", f"Instagram xatosi ({action}): {str(e)[:300]}")

    def spawn(self, coro) -> asyncio.Task:
        """Fon vazifasi (havolasi saqlanadi — garbage collector o'chirib yubormaydi)."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _note_sent(self, igsid: str, text: str) -> None:
        """Agent yuborayotgan matn bo'laklarini eslab qoladi (echo'ni matn bo'yicha tanish uchun)."""
        cutoff = time.monotonic() - ECHO_TEXT_TTL
        items = [x for x in self._sent_texts.get(igsid, []) if x[0] >= cutoff]
        items += [(time.monotonic(), _norm_echo(part)) for part in split_text(text)]
        self._sent_texts[igsid] = items[-20:]

    def _take_sent_text(self, igsid: str, text: str | None) -> bool:
        """Echo matni agent yaqinda yuborgan matnga mos kelsa — True (moslik bir marta ishlatiladi)."""
        if not text:
            return False
        key, cutoff = _norm_echo(text), time.monotonic() - ECHO_TEXT_TTL
        items = self._sent_texts.get(igsid, [])
        for i, (t, sent) in enumerate(items):
            if t >= cutoff and sent == key:
                del items[i]
                return True
        return False

    async def send_dm(self, profile: Profile, igsid: str, text: str) -> list[str]:
        """Direct'ga yuborish: har bir mid darhol eslab qolinadi, yuborish paytida echo kutiladi."""
        self._note_sent(igsid, text)
        self._sending[igsid] = self._sending.get(igsid, 0) + 1
        try:
            return await profile.ig.send_dm(igsid, text, on_sent=self.db.remember_sent)
        finally:
            self._sending[igsid] -= 1
            if self._sending[igsid] <= 0:
                self._sending.pop(igsid, None)

    async def send_operator_message(self, conv_id: str, text: str) -> bool:
        """Paneldan operator xabari: yuboriladi, bir marta saqlanadi, AI vaqtincha jim turadi."""
        conv = self.db.get_conv(conv_id)
        profile = self.profiles.get(conv["account_id"]) if conv else None
        if not conv or not profile or not text.strip():
            return False
        try:
            mids = await self.send_dm(profile, conv["igsid"], text.strip())
        except IGError as e:
            await self._ig_failed(profile, e, "operator xabari")
            return False
        self.db.add_message(conv_id, "operator", text.strip(), "dm", mids[0] if mids else None)
        self._pause(profile, conv_id)
        return True

    async def _is_own_echo(self, mid: str | None, igsid: str | None, text: str | None = None) -> bool:
        def known() -> bool:
            return (self.db.is_our_mid(mid) or bool(mid and self.db.has_mid(mid))
                    or bool(igsid and self._take_sent_text(igsid, text)))
        if known():
            return True
        # Echo yuborish tugashidan oldin kelishi mumkin — biroz kutib qayta tekshiramiz
        for _ in range(20):
            if igsid not in self._sending:
                break
            await asyncio.sleep(0.25)
        if known():
            return True
        await asyncio.sleep(0.5)
        return known()

    def dm_window_open(self, conv_id: str) -> bool:
        """Direct'da mijozga yozish mumkinmi (mijoz Direct'da oxirgi 24 soatda yozganmi)."""
        last = parse_iso(self.db.last_dm_user_at(conv_id))
        return bool(last) and now() - last <= DM_WINDOW

    # --- webhook ---
    async def handle_webhook(self, payload: dict) -> None:
        if payload.get("object") not in ("instagram", "page"):
            return
        for entry in payload.get("entry", []):
            profile = self.profile_for_ig(entry.get("id"))
            if profile is None:
                log.warning("Noma'lum Instagram akkaunt: %s (panelda profil qo'shing)", entry.get("id"))
                continue
            for event in entry.get("messaging", []) or []:
                try:
                    await self.handle_messaging(profile, event)
                except Exception:  # noqa: BLE001
                    log.exception("Direct hodisasida xato")
            for change in entry.get("changes", []) or []:
                if change.get("field") == "comments":
                    try:
                        await self.handle_comment(profile, change.get("value", {}))
                    except Exception:  # noqa: BLE001
                        log.exception("Komment hodisasida xato")

    # --- Direct ---
    async def handle_messaging(self, profile: Profile, event: dict) -> None:
        message = event.get("message")
        if not message or message.get("is_deleted") or message.get("is_unsupported"):
            return
        sender = (event.get("sender") or {}).get("id")
        recipient = (event.get("recipient") or {}).get("id")
        mid = message.get("mid")

        if message.get("is_echo") or sender == profile.ig_id:
            if not recipient or await self._is_own_echo(mid, recipient, message.get("text")):
                return
            # Operator Instagram ilovasidan qo'lda yozdi — AI shu dialogda vaqtincha to'xtaydi
            conv = self.db.ensure_conv(profile.id, recipient)
            self.db.add_message(conv["id"], "operator", message.get("text") or "[media]", "dm", mid)
            hours = self._pause(profile, conv["id"])
            self.db.log("operator", f"{conv.get('username') or recipient}: qo'lda yozildi, AI {hours:g} soat jim", profile.id)
            return

        if not sender or (mid and self.db.has_mid(mid)):
            return
        if _is_system_card(message):
            return  # Instagram'ning o'zi qo'shgan bo'sh karta (masalan raqam yozilganda) — mijoz xabari emas
        conv = self.db.get_conv(conv_key(profile.id, sender))
        if conv is None or not conv.get("username"):
            info = await profile.ig.get_user(sender)
            conv = self.db.ensure_conv(profile.id, sender, info.get("username"), info.get("name"))

        last_dm = parse_iso(self.db.last_dm_user_at(conv["id"]))
        if last_dm and now() - last_dm > SESSION_GAP and (conv.get("ai_replies") or conv.get("handoff_sent") or conv.get("lead_sent")):
            # Qaytgan mijoz — yangi suhbat: yangi buyurtma = yangi lid. Eski raqam eslatma sifatida qoladi.
            lead = dict(conv.get("lead_data") or {})
            old_phone = conv.get("phone") or lead.get("oldingi_telefon")
            lead = {"oldingi_telefon": old_phone} if old_phone else {}
            self.db.update_conv(conv["id"], ai_replies=0, handoff_sent=0, followups_sent=0, lead_sent=0,
                                phone=None, lead_data=lead)
            conv = self.db.get_conv(conv["id"])

        text, source = self._describe_message(message)
        self.db.add_message(conv["id"], "user", text, source, mid)
        images = [(a.get("payload") or {}).get("url") for a in message.get("attachments", []) or [] if a.get("type") == "image"]
        if images:
            self._pending_images.setdefault(conv["id"], []).extend(u for u in images if u)

        phone = extract_phone(message.get("text"))
        if phone and phone != conv.get("phone"):
            # Yangi yoki tuzatilgan raqam (lid yuborilgan bo'lsa — keyingi javobda "Lid yangilandi")
            self.db.update_conv(conv["id"], phone=phone)

        if not profile.cfg.get("direct.yoqilgan", True) or not self._allowed(profile, conv.get("username")):
            return
        if self.ai_active(conv):
            self._schedule_reply(conv["id"])

    @staticmethod
    def _describe_message(message: dict) -> tuple[str, str]:
        text = (message.get("text") or "").strip()
        source, notes = "dm", []
        if (message.get("reply_to") or {}).get("story"):
            source = "story"
            notes.append("[Mijoz story'ga javob yozdi]")
        labels = {"image": "rasm", "audio": "ovozli xabar", "video": "video", "share": "post", "ig_reel": "reels",
                  "story_mention": "story belgisi"}
        for att in message.get("attachments", []) or []:
            if att.get("type") in SYSTEM_CARD_TYPES:
                card = _card_text(att)
                if card:
                    notes.append(f"[Mijoz karta yubordi: {card}]")
                continue
            notes.append(f"[Mijoz {labels.get(att.get('type'), att.get('type'))} yubordi]")
        return " ".join(notes + ([text] if text else [])).strip() or "[bo'sh xabar]", source

    def _schedule_reply(self, conv_id: str) -> None:
        """Mijoz ketma-ket bir nechta xabar yozsa, hammasiga bitta javob.
        Javob allaqachon yozilayotgan bo'lsa — uni uzmaymiz, tugagach qayta ko'rib chiqamiz."""
        if conv_id in self._running:
            self._rerun.add(conv_id)
            return
        old = self._pending.get(conv_id)
        if old and not old.done():
            old.cancel()  # hali kutish (sleep) yoki navbatda — hech narsa yuborilmagan
        self._pending[conv_id] = self.spawn(self._delayed_reply(conv_id))

    async def _delayed_reply(self, conv_id: str, tail: str | None = None) -> None:
        try:
            await asyncio.sleep(self.debounce)
            lock = self._locks.setdefault(conv_id, asyncio.Lock())
            await lock.acquire()
        except asyncio.CancelledError:
            return
        self._running.add(conv_id)
        late: list[str] = []
        try:
            start_id = self.db.max_message_id(conv_id)
            await asyncio.shield(self.reply(conv_id, tail_user=tail))
            if conv_id in self._rerun:
                late = self.db.user_texts_after(conv_id, start_id)
        except Exception:  # noqa: BLE001
            log.exception("Javob berishda xato")
        finally:
            self._running.discard(conv_id)
            self._rerun.discard(conv_id)
            lock.release()
        if late:
            # Javob yozilayotganda mijoz yana yozdi — endi o'shalarga javob beramiz
            conv = self.db.get_conv(conv_id)
            if conv and self.ai_active(conv):
                self._pending[conv_id] = self.spawn(self._delayed_reply(conv_id, tail="\n".join(late)))

    async def flush(self) -> None:
        """Testlar uchun: barcha kutilayotgan javoblar va fon vazifalari tugaguncha kutish."""
        for _ in range(50):
            tasks = [t for t in self._pending.values() if not t.done()] + [t for t in self._tasks if not t.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    def _build_messages(self, conv: dict, images: list[tuple[str, str]], extra_note: str | None,
                        tail_user: str | None = None) -> list[dict]:
        msgs: list[dict] = []
        for r in self.db.history(conv["id"], limit=40):
            role = "user" if r["role"] == "user" else "assistant"
            text = f"[Operator yozdi] {r['text']}" if r["role"] == "operator" else r["text"]
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"][0]["text"] += "\n" + text
            else:
                msgs.append({"role": role, "content": [{"type": "text", "text": text}]})
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        if not msgs:
            return []
        if msgs[-1]["role"] != "user":
            text = (f"[Oldingi javobing yozilayotganda mijoz yana yozgan edi]\n{tail_user}" if tail_user
                    else "[Mijoz hali yangi xabar yozmadi]")
            msgs.append({"role": "user", "content": [{"type": "text", "text": text}]})
        for media_type, data in images[:3]:
            msgs[-1]["content"].insert(0, {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
        state = self._state_note(conv) + (f"\n{extra_note}" if extra_note else "")
        msgs.append({"role": "system", "content": state})  # o'zgaruvchan holat — kesh prefiksini buzmaydi
        return msgs

    @staticmethod
    def _state_note(conv: dict) -> str:
        lead = conv.get("lead_data") or {}
        lines = ["Dialog holati:",
                 f"- Telefon raqam: {'olingan (' + conv['phone'] + ')' if conv.get('phone') else 'hali olinmagan'}"]
        if conv.get("name"):
            lines.append(f"- Mijozning Instagram'dagi ismi: {conv['name']}")
        if not conv.get("phone") and lead.get("oldingi_telefon"):
            lines.append(f"- Mijoz avvalgi buyurtmada {lead['oldingi_telefon']} raqamini qoldirgan. Buyurtma qilmoqchi bo'lsa, "
                         "shu raqamga bog'lanishni so'rab tasdiqlat; tasdiqlasa `phone` ga shu raqamni yoz.")
        for key, label in (("interest", "Qiziqish"), ("for_whom", "Kim uchun"), ("region", "Viloyat")):
            if lead.get(key):
                lines.append(f"- {label}: {lead[key]}")
        if not conv.get("ai_replies"):
            lines.append("- Bu dialogdagi birinchi javobing.")
        return "\n".join(lines)

    async def reply(self, conv_id: str, extra_note: str | None = None, source: str = "dm", force: bool = False,
                    tail_user: str | None = None) -> DMResult | None:
        """force=True — egasining javobini yetkazish: pauza va javoblar chegarasini chetlab o'tadi
        (lekin panelda AI o'chirilgan bo'lsa — yo'q)."""
        conv = self.db.get_conv(conv_id)
        profile = self.profiles.get(conv["account_id"]) if conv else None
        if not conv or not profile or self.ai is None or not conv.get("ai_enabled"):
            return None
        if not force and not self.ai_active(conv):
            return None

        max_replies = int(profile.cfg.get("agent.max_javoblar", 10))
        if conv.get("ai_replies", 0) >= max_replies and not force:
            if source == "followup":
                return None  # eslatma uchun "operator kerak" deb yolg'on signal bermaymiz
            if not conv.get("handoff_sent"):
                last = self.db.history(conv_id, 1)
                await self.tg.send(tgfmt.handoff_message(profile.account, conv, f"AI {max_replies} ta javob chegarasiga yetdi",
                                                         last[-1]["text"] if last else ""), profile.tg_chat,
                                   buttons=tgfmt.handoff_buttons(conv_id))
                self.db.update_conv(conv_id, handoff_sent=1)
            return None

        images = []
        if profile.cfg.get("direct.rasm_tushunish", True):
            for url in self._pending_images.pop(conv_id, []):
                img = await profile.ig.download_image(url)
                if img:
                    images.append(img)

        messages = self._build_messages(conv, images, extra_note, tail_user)
        if not messages:
            return None
        result = await self.ai.dm_reply(self.knowledge(profile), dm_rules(profile.cfg, profile.username), messages)
        if result is None:
            await self.alert(profile, "ai", f"AI javob bermadi ({getattr(self.ai, 'last_error', '') or 'nomaʼlum xato'}). "
                                            f"Mijoz: @{conv.get('username') or conv['igsid']} javobsiz qoldi.")
            return None
        await self._apply_dm_result(profile, conv, result, source)
        return result

    async def _apply_dm_result(self, profile: Profile, conv: dict, result: DMResult, source: str) -> None:
        conv_id = conv["id"]
        last_user = next((m["text"] for m in reversed(self.db.history(conv_id, 10)) if m["role"] == "user"), "")
        phone = conv.get("phone") or extract_phone(last_user) or extract_phone(result.phone)
        new_phone = bool(phone) and not conv.get("lead_sent")
        phone_changed = bool(phone) and bool(conv.get("lead_sent")) and phone != (conv.get("lead_data") or {}).get("phone")

        lead = dict(conv.get("lead_data") or {})
        newly_filled = False
        for key in ("name", "interest", "for_whom", "region", "product_id"):
            value = getattr(result, key)
            if value and value != lead.get(key):
                if not lead.get(key) and key in ("for_whom", "region"):
                    newly_filled = True
                lead[key] = value
        lead["summary"] = result.summary

        reply_text = (result.reply or "").strip()
        if new_phone:
            confirm = profile.cfg.get("sotuv.raqam_tasdiq_matni", "✅ Raqamingiz qabul qilindi\n📞 Tez orada siz bilan bog'lanamiz")
            if is_cyrillic(last_user):
                confirm = profile.cfg.get("sotuv.raqam_tasdiq_matni_kirill", confirm)
            reply_text = f"{reply_text}\n\n{confirm}".strip()

        updates: dict = {"lead_data": lead}
        if phone:
            updates["phone"] = phone
        fresh = {**conv, **updates}
        # Lid — Direct javobidan OLDIN: Instagram xato bersa ham raqam yo'qolmaydi
        if new_phone:
            lead["phone"] = phone
            lead["temperature"] = result.temperature
            lead["lead_reason"] = result.lead_reason
            msg_id = await self.send_lead(profile, fresh, lead, source)
            self.db.log("lid", f"{conv.get('username') or conv['igsid']}: {phone}" + ("" if msg_id else " (Telegram'ga ketmadi, qayta urinadi)"),
                        profile.id)
            updates["lead_sent"] = 1
        elif phone_changed or (conv.get("lead_sent") and newly_filled):
            lead["phone"] = phone
            await self.tg.send(tgfmt.lead_message(profile.account, fresh, lead, source, update=True), profile.tg_chat)
        self.db.update_conv(conv_id, **updates)

        if reply_text:
            try:
                mids = await self.send_dm(profile, conv["igsid"], reply_text)
            except IGError as e:
                await self._ig_failed(profile, e, "Direct javob")
                mids = None
            if mids is not None:
                self.db.add_message(conv_id, "assistant", reply_text, source, mids[0] if mids else None)
                self.db.update_conv(conv_id, ai_replies=conv.get("ai_replies", 0) + 1)

        if result.unknown_question:
            await self.ask_owner(profile, result.unknown_question, result.product_id, fresh)

        if result.needs_human and profile.cfg.get("sotuv.operatorga_uzatish", True):
            await self.tg.send(tgfmt.handoff_message(profile.account, fresh, result.handoff_reason or "Mijoz operator so'radi", last_user),
                               profile.tg_chat, buttons=tgfmt.handoff_buttons(conv_id))
            self._pause(profile, conv_id)
            self.db.update_conv(conv_id, handoff_sent=1)
            self.db.log("operator", f"{conv.get('username') or conv['igsid']}: operatorga uzatildi", profile.id)

    # --- Javobsiz savol → egasiga → xotira ---
    async def ask_owner(self, profile: Profile, question: str, product_id: int | None, conv: dict | None) -> None:
        product = self.db.get_product(product_id) if product_id else None
        gap, is_new = self.db.add_gap(profile.id, question, product["id"] if product else None, conv["id"] if conv else None)
        if not gap:
            return
        if gap["status"] == "javob_berildi" and conv and gap.get("answer"):
            # Javob allaqachon bor — mijozga darhol yetkazamiz (joriy javob tugagach)
            key = (gap["id"], conv["id"])
            if key not in self._delivered:
                self._delivered.add(key)
                self.spawn(self._deliver_answer(conv["id"], gap["question"], gap["answer"]))
            return
        if gap["status"] == "e'tiborsiz":
            self.db.set_gap(gap["id"], None, "kutilmoqda")  # yana so'rashdi — qayta ochamiz
            is_new = True
        if is_new or not gap.get("tg_message_id"):
            await self._send_gap(profile, gap, conv, product)

    async def _send_gap(self, profile: Profile, gap: dict, conv: dict | None, product: dict | None) -> None:
        chat = profile.tg_chat or self.tg.default_chat_id
        msg_id = await self.tg.send(tgfmt.gap_message(profile.account, conv, gap["question"], product, gap["id"],
                                                      gap.get("hits") or 1, iso()), chat,
                                    buttons=tgfmt.gap_buttons(gap["id"]))
        if msg_id:
            self.db.set_gap_tg(gap["id"], chat, msg_id)

    async def send_lead(self, profile: Profile, conv: dict, lead: dict, source: str) -> int | None:
        """Lidni bazaga yozadi va Telegram'ga «Sotildi / Bo'lmadi» tugmalari bilan yuboradi."""
        lead_id = self.db.add_lead(profile.id, conv["id"], lead.get("phone") or conv.get("phone"), lead, source, tg_sent=False)
        chat = profile.tg_chat or self.tg.default_chat_id
        msg_id = await self.tg.send(tgfmt.lead_message(profile.account, conv, lead, source), chat,
                                    buttons=tgfmt.lead_buttons(lead_id))
        if msg_id:
            self.db.mark_lead_sent(lead_id, chat, msg_id)
        return msg_id

    async def send_due_reminders(self) -> int:
        """«Keyinroq» bosilgan savollarni vaqti kelganda qayta yuboradi."""
        n = 0
        for gap in self.db.due_reminders():
            profile = self.profiles.get(gap["account_id"])
            if profile:
                await self._send_gap(profile, gap, None, self.db.get_product(gap.get("product_id")))
                n += 1
        return n

    @staticmethod
    def _answer_note(question: str, answer: str) -> str:
        return (f"Do'kon egasi mijozning savoliga javob bergan.\nSavol: {question}\nJavob: {answer}\n"
                "Shu javobni mijozga tabiiy, qisqa qilib yetkaz (qayta salomlashma), so'ng suhbatni davom ettir. "
                "Bu savolni `unknown_question` ga qayta yozma.")

    async def _deliver_answer(self, conv_id: str, question: str, answer: str) -> bool:
        """Egasining javobini mijozga yetkazadi. AI panelda o'chirilgan bo'lsa — javob so'zma-so'z yuboriladi."""
        conv = self.db.get_conv(conv_id)
        profile = self.profiles.get(conv["account_id"]) if conv else None
        if not conv or not profile or not self.dm_window_open(conv_id):
            return False  # Instagram 24 soatdan keyin (yoki faqat komment yozgan bo'lsa) yozishga ruxsat bermaydi
        async with self._locks.setdefault(conv_id, asyncio.Lock()):
            if not conv.get("ai_enabled") or self.ai is None:
                try:
                    mids = await self.send_dm(profile, conv["igsid"], answer)
                except IGError as e:
                    await self._ig_failed(profile, e, "egasining javobi")
                    return False
                self.db.add_message(conv_id, "operator", answer, "xotira", mids[0] if mids else None)
                return True
            return bool(await self.reply(conv_id, extra_note=self._answer_note(question, answer), source="xotira", force=True))

    async def answer_gap(self, gap_id: int, answer: str, source: str = "panel", scope: str = "profil") -> int:
        """Egasining javobini xotiraga saqlaydi va kutayotgan mijozlarga yuboradi. Yuborilganlar sonini qaytaradi."""
        gap = self.db.get_gap(gap_id)
        answer = answer.strip()
        if not gap or not answer:
            return 0
        account_id = None if scope == "hammasi" else gap["account_id"]
        self.db.add_memory(gap["question"], answer, account_id, gap.get("product_id"), source)
        self.db.set_gap(gap_id, answer, "javob_berildi")
        self.db.log("xotira", f"Saqlandi: {gap['question']} → {answer[:100]}", gap["account_id"])

        sent = 0
        profile = self.profiles.get(gap["account_id"])
        if profile is None or not profile.cfg.get("xotira.mijozga_darhol_yuborish", True):
            return 0
        for conv_id in self.db.gap_waiters(gap_id):
            if (gap_id, conv_id) in self._delivered:
                continue
            self._delivered.add((gap_id, conv_id))
            if await self._deliver_answer(conv_id, gap["question"], answer):
                sent += 1
        return sent

    async def handle_telegram_update(self, update: dict, admin_ids: set[str]) -> None:
        """Telegram: Reply bilan javob, tugmalar (callback), /start bilan guruhni ulash, /holat, /savollar."""
        if update.get("callback_query"):
            await self._handle_callback(update["callback_query"], admin_ids)
            return
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        user_id = str((msg.get("from") or {}).get("id", ""))
        if not text or not chat_id:
            return
        if text.startswith("/"):
            await self._handle_command(text, chat_id, user_id, admin_ids, msg)
            return
        reply_to = msg.get("reply_to_message") or {}
        if not reply_to or (admin_ids and user_id not in admin_ids):
            return
        gap = self.db.gap_by_tg(chat_id, reply_to.get("message_id"))
        if not gap:
            return
        if gap["status"] == "javob_berildi":
            await self.tg.send("Bu savolga allaqachon javob berilgan. O'zgartirish uchun panel → Xotira.", chat_id, msg.get("message_id"))
            return
        if gap["status"] != "kutilmoqda":
            self.db.set_gap(gap["id"], None, "kutilmoqda")
        sent = await self.answer_gap(gap["id"], text, source="telegram")
        prof = self.profiles.get(gap["account_id"])
        account = prof.account if prof else {}
        await self.tg.send(tgfmt.gap_saved_message(account, gap["question"], text, sent), chat_id, msg.get("message_id"))
        await self.tg.edit(chat_id, reply_to["message_id"],
                           tgfmt.stamp(tgfmt.gap_message(account, None, gap["question"], self.db.get_product(gap.get("product_id")), gap["id"]),
                                       "✅ Javob berildi"))

    async def _handle_command(self, text: str, chat_id: str, user_id: str, admin_ids: set[str], msg: dict) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/start" and arg.startswith("bind_"):
            target = self.db.kv_get(f"tg_bind:{arg}")
            if not target:
                await self.tg.send("Ulash havolasi eskirgan — paneldan yangisini oling.", chat_id)
                return
            title = (msg.get("chat") or {}).get("title") or chat_id
            if target == "main":
                self.db.kv_set("cfg:telegram_chat_id", chat_id)
                self.tg.default_chat_id = chat_id
                if self.on_main_chat:
                    self.on_main_chat(chat_id)
                where = "Asosiy guruh"
            else:
                self.db.update_account(int(target), tg_chat_id=chat_id)
                self.reload()
                acc = self.db.get_account(int(target)) or {}
                where = f"«{acc.get('name')}» profili"
            self.db.log("telegram", f"{title} guruhi ulandi → {where}")
            await self.tg.send(f"✅ <b>Telegram-guruh ulandi</b>\n{tgfmt.esc(where)} lidlari, savollari va ogohlantirishlari shu yerga keladi.\n\n"
                               "Buyruqlar: /holat — bugungi natijalar, /savollar — javob kutayotgan savollar", chat_id)
            return
        if admin_ids and user_id not in admin_ids:
            return
        if cmd == "/holat":
            st = self.db.lead_stats(1)
            pending = len(self.db.list_gaps(status="kutilmoqda"))
            await self.tg.send(f"📊 <b>Oxirgi 24 soat</b>\n🟢 Lidlar: {st['jami']}\n💰 Sotildi: {st['sotildi']}\n"
                               f"🚫 Bo'lmadi: {st['bolmadi']}\n❓ Javob kutayotgan savollar: {pending}", chat_id)
        elif cmd == "/savollar":
            gaps = self.db.list_gaps(status="kutilmoqda")[:10]
            if not gaps:
                await self.tg.send("🎉 Javob kutayotgan savol yo'q", chat_id)
            for gap in gaps:
                profile = self.profiles.get(gap["account_id"])
                if profile:
                    await self._send_gap(profile, gap, None, self.db.get_product(gap.get("product_id")))

    async def _handle_callback(self, cq: dict, admin_ids: set[str]) -> None:
        data = cq.get("data") or ""
        user = cq.get("from") or {}
        who = user.get("first_name") or user.get("username") or "admin"
        msg = cq.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        message_id = msg.get("message_id")
        if admin_ids and str(user.get("id")) not in admin_ids:
            await self.tg.answer_callback(cq["id"], "Sizda ruxsat yo'q")
            return
        parts = data.split(":")
        kind = parts[0]
        note, toast = "", ""
        log.info("Telegram tugma: %s (%s)", data, who)
        try:
            if kind == "demo":
                note, toast = f"✓ Tugma ishlayapti — {who} (bu namuna xabar)", "Namuna: tugma ishlayapti ✓"
            elif kind == "lead" and len(parts) == 3:
                lead = self.db.get_lead(int(parts[2]))
                if lead:
                    status = "sotildi" if parts[1] == "sold" else "bolmadi"
                    self.db.set_lead_status(lead["id"], status, who)
                    note = f"💰 Sotildi — {who}" if status == "sotildi" else f"🚫 Bo'lmadi — {who}"
                    toast = "Belgilandi ✓"
                    profile = self.profiles.get(lead["account_id"])
                    conv = self.db.get_conv(lead["conv_id"]) or {}
                    if profile:
                        await self.tg.edit(chat_id, message_id, tgfmt.stamp(
                            tgfmt.lead_message(profile.account, conv, lead["data"], lead["source"]), note))
                        note = ""
                    self.db.log("lid", f"{conv.get('username') or lead['phone']}: {status} ({who})", lead["account_id"])
            elif kind == "gap" and len(parts) == 3:
                gap = self.db.get_gap(int(parts[2]))
                if gap:
                    profile = self.profiles.get(gap["account_id"])
                    account = profile.account if profile else {}
                    base = tgfmt.gap_message(account, None, gap["question"], self.db.get_product(gap.get("product_id")), gap["id"])
                    if parts[1] == "no":
                        self.db.set_gap(gap["id"], None, "e'tiborsiz")
                        await self.tg.edit(chat_id, message_id, tgfmt.stamp(base, f"❌ Kerak emas — {who}"))
                        toast = "E'tiborsiz qoldirildi"
                    else:
                        self.db.snooze_gap(gap["id"], 3)
                        await self.tg.edit(chat_id, message_id, tgfmt.stamp(base, f"⏰ 3 soatdan keyin yana eslatiladi — {who}"))
                        toast = "3 soatdan keyin eslataman"
            elif kind == "ho" and len(parts) >= 3:
                conv_id = ":".join(parts[2:])
                if parts[1] == "ai":
                    self.db.update_conv(conv_id, ai_enabled=1, paused_until=None, handoff_sent=0)
                    note, toast = f"🤖 AI yana javob beryapti — {who}", "AI yoqildi"
                else:
                    note, toast = f"✅ {who} mijoz bilan gaplashdi", "Belgilandi"
            elif kind == "mod" and len(parts) >= 4:
                profile = self.profiles.get(int(parts[2]))
                comment_id = ":".join(parts[3:])
                if profile:
                    if parts[1] == "show":
                        await profile.ig.unhide_comment(comment_id)
                        note, toast = f"↩️ Izoh qayta ochildi — {who}", "Izoh ochildi"
                    else:
                        await profile.ig.delete_comment(comment_id)
                        note, toast = f"🗑 Izoh o'chirildi — {who}", "O'chirildi"
                    row = self.db.get_comment(comment_id)
                    if row:
                        self.db.add_comment(**{**row, "action": f"{row.get('action')}, {note}"})
        except IGError as e:
            toast = f"Instagram xatosi: {str(e)[:120]}"
        if note and message_id:
            plain = tgfmt.esc(msg.get("text") or "")
            await self.tg.edit(chat_id, message_id, f"{plain}\n\n<b>{tgfmt.esc(note)}</b>")
        await self.tg.answer_callback(cq["id"], toast)

    # --- Kommentlar ---
    async def handle_comment(self, profile: Profile, value: dict) -> None:
        comment_id = value.get("id")
        text = (value.get("text") or "").strip()
        if not comment_id or not text or self.db.comment_seen(comment_id) or comment_id in self._inflight_comments:
            return
        self._inflight_comments.add(comment_id)
        try:
            await self._process_comment(profile, value, comment_id, text)
        finally:
            self._inflight_comments.discard(comment_id)

    async def _process_comment(self, profile: Profile, value: dict, comment_id: str, text: str) -> None:
        sender = value.get("from") or {}
        user_id, username = sender.get("id"), sender.get("username")
        media_id = (value.get("media") or {}).get("id")
        if user_id == profile.ig_id or (username and username.lower() == profile.username.lower()):
            return
        base_row = dict(id=comment_id, account_id=profile.id, media_id=media_id, user_id=user_id, username=username, text=text)
        if not profile.cfg.get("komment.yoqilgan", True) or not self._allowed(profile, username):
            self.db.add_comment(**base_row, category="-", action="o'tkazildi")
            return

        media = await profile.ig.get_media(media_id) if media_id else {}
        linked = self.db.products_for_post(media_id) if media_id else []
        block = (
            f"Post matni: {(media.get('caption') or '(nomaʼlum)')[:1500]}\n"
            + (f"Bu postga bog'langan mahsulot(lar): {', '.join(f'[#{p['id']}] «{p['name']}»' for p in linked)}\n" if linked else "")
            + f"\nKomment muallifi: @{username}\nKomment: {text}"
            + ("\n(Bu boshqa kommentga javob)" if value.get("parent_id") else "")
        )
        result: CommentResult | None = None
        if self.ai is not None:
            result = await self.ai.classify_comment(self.knowledge(profile), comment_rules(profile.cfg, profile.username), block)
        if result is None:
            self.db.add_comment(**base_row, category="?", action="AI javob bermadi")
            return

        category = result.category
        keywords = [k.lower() for k in profile.cfg.get("komment.direct_kalit_sozlar", []) or []]
        if category in ("NEYTRAL", "BOSHQA") and (any(k in text.lower() for k in keywords) or text.strip() in ("+", "++", "+++")):
            category = "SAVOL"

        actions: list[str] = []
        public = ""
        already = self.db.recent_comment_reply(profile.id, user_id, media_id) if user_id else None
        if already and category in ("SAVOL", "XARID", "NEYTRAL"):
            # Shu mijozga shu post ostida yaqinda javob berganmiz — takrorlamaymiz (Direct'da davom etadi)
            self.db.add_comment(**base_row, category=category, action="takror — javob yozilmadi", public_reply="")
            return
        if category in ("HAQORAT", "SALBIY", "SPAM") and profile.cfg.get("moderatsiya.yoqilgan", True):
            mode = profile.cfg.get(f"moderatsiya.{category.lower()}", "yashirish")
            try:
                if mode == "ochirish":
                    await profile.ig.delete_comment(comment_id)
                    actions.append("o'chirildi")
                elif mode == "yashirish":
                    await profile.ig.hide_comment(comment_id)
                    actions.append("yashirildi")
                else:
                    actions.append("tegilmadi")
            except IGError as e:
                await self._ig_failed(profile, e, "moderatsiya")
                actions.append("xato")
            if profile.cfg.get("moderatsiya.telegramga_yuborish", True):
                await self.tg.send(tgfmt.moderation_message(profile.account, username, text, category, actions[-1],
                                                            media.get("permalink")), profile.tg_chat,
                                   buttons=tgfmt.moderation_buttons(profile.id, comment_id, actions[-1]))
        else:
            want_dm = category in ("SAVOL", "XARID", "SHIKOYAT") and profile.cfg.get("komment.directga_yozish", True)
            dm_ok = False
            if want_dm and result.dm_message and user_id:
                conv = self.db.ensure_conv(profile.id, user_id, username)
                self.db.add_message(conv["id"], "user", f"[Kommentda yozdi] {text}", "comment")
                try:
                    self._note_sent(user_id, split_text(result.dm_message)[0])
                    self._sending[user_id] = self._sending.get(user_id, 0) + 1
                    try:
                        mids = await profile.ig.private_reply(comment_id, result.dm_message, on_sent=self.db.remember_sent)
                    finally:
                        self._sending.pop(user_id, None)
                    self.db.add_message(conv["id"], "assistant", result.dm_message, "comment", mids[0] if mids else None)
                    self.db.update_conv(conv["id"], ai_replies=conv.get("ai_replies", 0) + 1)
                    actions.append("Direct'ga yozildi")
                    dm_ok = True
                except IGError as e:
                    log.warning("Private reply xato: %s", e)
                    actions.append("Direct xato")
                    await self._ig_failed(profile, e, "kommentga Direct")
                if result.product_id or linked:
                    lead = dict(conv.get("lead_data") or {})
                    product = self.db.get_product(result.product_id) or (linked[0] if linked else None)
                    if product:
                        lead.setdefault("interest", product["name"])
                        lead.setdefault("product_id", product["id"])
                        self.db.update_conv(conv["id"], lead_data=lead)
                if category == "SHIKOYAT":
                    await self.tg.send(tgfmt.handoff_message(profile.account, conv, "Kommentda shikoyat", text), profile.tg_chat,
                                       buttons=tgfmt.handoff_buttons(conv["id"]))
                if result.unknown_question:
                    await self.ask_owner(profile, result.unknown_question,
                                         result.product_id or (linked[0]["id"] if linked else None), self.db.get_conv(conv["id"]))

            # Kommentda raqam — toifasidan qat'i nazar lid (masalan faqat "+998 90 123 45 67" yozilgan)
            phone = extract_phone(text)
            if phone and user_id:
                conv = self.db.ensure_conv(profile.id, user_id, username)
                if not conv.get("lead_sent"):
                    lead = {**(conv.get("lead_data") or {}), "phone": phone, "summary": f"Kommentda raqam qoldirdi: {text[:200]}"}
                    lead.setdefault("temperature", "issiq")
                    lead.setdefault("lead_reason", "kommentda raqam qoldirdi")
                    await self.send_lead(profile, {**conv, "phone": phone}, lead, "comment")
                    self.db.update_conv(conv["id"], phone=phone, lead_sent=1, lead_data=lead)
                    actions.append("lid")

            reply_allowed = profile.cfg.get("komment.ochiq_javob", True)
            if category == "NEYTRAL" and not profile.cfg.get("komment.neytral_kommentga_javob", True):
                reply_allowed = False
            reply_text_public = result.public_reply
            if want_dm and not dm_ok:
                # Direct'ga yozib bo'lmadi — "Direct'ga yozdik" deb yolg'on yozmaymiz, javobni komment ostiga beramiz
                reply_text_public = result.public_fallback if category in ("SAVOL", "XARID") else ""
            if reply_allowed and reply_text_public and category != "BOSHQA":
                public = reply_text_public.strip()
                try:
                    await profile.ig.reply_comment(comment_id, public)
                    actions.append("ochiq javob")
                except IGError as e:
                    log.warning("Kommentga javob xato: %s", e)
                    await self._ig_failed(profile, e, "komment javobi")
                    public = ""

        self.db.add_comment(**base_row, category=category, action=", ".join(actions) or "javobsiz", public_reply=public)

    # --- Instagram tarixini o'qish va o'rganish ---
    async def import_history(self, profile: Profile, max_conversations: int = 100, analyze: bool = True) -> dict:
        """Eski Direct yozishmalarni Instagram'dan oladi, bazaga yozadi va (Claude bo'lsa) tahlil qilib takliflar beradi.
        Hech kimga xabar yuborilmaydi."""
        report = {"dialoglar": 0, "xabarlar": 0, "takliflar": 0, "savollar": 0, "maslahatlar": []}
        convs = await profile.ig.conversations(limit=max_conversations)
        transcripts: list[str] = []
        for c in convs:
            others = [p for p in ((c.get("participants") or {}).get("data") or []) if str(p.get("id")) != profile.ig_id]
            if not others:
                continue
            customer = others[0]
            conv = self.db.ensure_conv(profile.id, str(customer["id"]), customer.get("username"))
            msgs = sorted(await profile.ig.conversation_messages(c["id"]), key=lambda m: m.get("created_time", ""))
            lines = []
            for m in msgs:
                text = (m.get("message") or "").strip()
                if not text:
                    continue
                ours = str((m.get("from") or {}).get("id")) == profile.ig_id
                if m.get("id") and not self.db.has_mid(m["id"]):
                    created = (m.get("created_time") or "").replace("+0000", "+00:00") or None
                    self.db.add_message(conv["id"], "operator" if ours else "user", text, "tarix", m["id"], created_at=created)
                    report["xabarlar"] += 1
                masked = PHONE_RE.sub("<TEL>", text)[:500]
                lines.append(("D: " if ours else "M: ") + masked)
            if lines:
                transcripts.append("\n".join(lines))
                report["dialoglar"] += 1
        if analyze and self.ai is not None and transcripts:
            knowledge = self.knowledge(profile)
            chunk, chunks = [], []
            for t in transcripts:
                chunk.append(t)
                if sum(len(x) for x in chunk) > 40000:
                    chunks.append(chunk)
                    chunk = []
            if chunk:
                chunks.append(chunk)
            products = {p["name"].lower(): p["id"] for p in self.products_for(profile)}
            for part in chunks:
                result = await self.ai.analyze_history(knowledge, "\n\n---\n\n".join(part))
                if result is None:
                    continue
                for faq in result.faqs:
                    pid = next((v for k, v in products.items() if faq.product and faq.product.lower() in k), None)
                    if self.db.add_suggestion(profile.id, faq.question, faq.answer, pid):
                        report["takliflar"] += 1
                for q in result.open_questions:
                    _, is_new = self.db.add_gap(profile.id, q)
                    report["savollar"] += int(is_new)
                report["maslahatlar"] += result.tips
        self.db.kv_set(f"tarix:{profile.id}", json.dumps({**report, "vaqt": iso()}, ensure_ascii=False))
        self.db.log("tarix", f"{report['dialoglar']} ta eski dialog o'qildi: {report['takliflar']} ta javob taklifi, "
                             f"{report['savollar']} ta yangi savol", profile.id)
        if report["takliflar"] or report["savollar"]:
            await self.tg.send(f"📚 <b>{tgfmt.esc(profile.account.get('name'))}</b>: eski yozishmalar o'rganildi — "
                               f"{report['takliflar']} ta javob taklifi va {report['savollar']} ta savol panelda tasdiqlashingizni kutmoqda.",
                               profile.tg_chat)
        return report

    # --- Sinov (panelda): Instagram'ga hech narsa yuborilmaydi, faqat nima bo'lishini ko'rsatadi ---
    async def simulate_dm(self, profile: Profile, chat: list[dict], state: dict) -> dict:
        msgs: list[dict] = []
        for m in chat:
            role = "user" if m["role"] == "user" else "assistant"
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"][0]["text"] += "\n" + m["text"]
            else:
                msgs.append({"role": role, "content": [{"type": "text", "text": m["text"]}]})
        conv = {"id": "sinov", "igsid": "sinov", "username": "sinov_mijoz", "name": "Sinov mijoz",
                "phone": state.get("phone"), "lead_data": state.get("lead", {}), "ai_replies": state.get("n", 0),
                "lead_sent": state.get("lead_sent", 0)}
        msgs.append({"role": "system", "content": self._state_note(conv)})
        result = await self.ai.dm_reply(self.knowledge(profile), dm_rules(profile.cfg, profile.username), msgs)
        if result is None:
            return {"error": getattr(self.ai, "last_error", "") or "nomaʼlum xato"}
        last_user = chat[-1]["text"] if chat else ""
        phone = conv["phone"] or extract_phone(last_user) or extract_phone(result.phone)
        lead = dict(conv["lead_data"])
        for key in ("name", "interest", "for_whom", "region", "product_id"):
            if getattr(result, key):
                lead[key] = getattr(result, key)
        lead["summary"] = result.summary
        reply = (result.reply or "").strip()
        events: list[dict] = []
        if phone and not conv["lead_sent"]:
            confirm = profile.cfg.get("sotuv.raqam_tasdiq_matni", "")
            if is_cyrillic(last_user):
                confirm = profile.cfg.get("sotuv.raqam_tasdiq_matni_kirill", confirm)
            reply = f"{reply}\n\n{confirm}".strip()
            lead["phone"] = phone
            lead["temperature"] = result.temperature
            lead["lead_reason"] = result.lead_reason
            events.append({"title": "🆕 Telegram guruhga lid ketadi",
                           "html": tgfmt.lead_message(profile.account, {**conv, "phone": phone}, lead, "dm")})
            state["lead_sent"] = 1
        if result.unknown_question:
            events.append({"title": "❓ Sizga savol keladi (Reply qilib javob berasiz)",
                           "html": tgfmt.gap_message(profile.account, conv, result.unknown_question,
                                                     self.db.get_product(result.product_id), 0)})
        if result.needs_human:
            events.append({"title": "🙋 Operator chaqiriladi, AI shu dialogda to'xtaydi",
                           "html": tgfmt.handoff_message(profile.account, conv, result.handoff_reason or "", last_user)})
        state.update(phone=phone, lead=lead, n=state.get("n", 0) + 1)
        product = self.db.get_product(result.product_id)
        info = {"mahsulot": product["name"] if product else (result.interest or ""), "telefon": phone or "",
                "viloyat": lead.get("region") or "", "kim_uchun": lead.get("for_whom") or "", "xulosa": result.summary}
        return {"reply": reply, "events": events, "info": info}

    async def simulate_comment(self, profile: Profile, text: str) -> dict:
        block = f"Post matni: (sinov)\n\nKomment muallifi: @sinov_mijoz\nKomment: {text}"
        result = await self.ai.classify_comment(self.knowledge(profile), comment_rules(profile.cfg, profile.username), block)
        if result is None:
            return {"error": getattr(self.ai, "last_error", "") or "nomaʼlum xato"}
        category = result.category
        keywords = [k.lower() for k in profile.cfg.get("komment.direct_kalit_sozlar", []) or []]
        if category in ("NEYTRAL", "BOSHQA") and any(k in text.lower() for k in keywords):
            category = "SAVOL"
        action = {
            "HAQORAT": f"Komment {'o‘chiriladi' if profile.cfg.get('moderatsiya.haqorat') == 'ochirish' else 'yashiriladi'}",
            "SALBIY": f"Komment {'o‘chiriladi' if profile.cfg.get('moderatsiya.salbiy') == 'ochirish' else 'yashiriladi'}",
            "SPAM": f"Komment {'o‘chiriladi' if profile.cfg.get('moderatsiya.spam') == 'ochirish' else 'yashiriladi'}",
            "SAVOL": "Direct'ga javob yoziladi + komment ostida qisqa javob",
            "XARID": "Direct'ga javob yoziladi + komment ostida qisqa javob",
            "SHIKOYAT": "Uzr so'raladi, Direct'ga yoziladi, Telegram'ga «Operator kerak»",
            "NEYTRAL": "Qisqa rahmat yoziladi",
            "BOSHQA": "Javob yozilmaydi",
        }[category]
        return {"category": category, "action": action, "public": result.public_reply, "dm": result.dm_message,
                "reason": result.reason}

    # --- Instagram tokenlarini yangilash (60 kunlik token — har 7 kunda yangilaymiz) ---
    async def refresh_tokens(self, every: timedelta = timedelta(days=7)) -> int:
        done = 0
        for profile in list(self.profiles.values()):
            if not profile.account.get("ig_token"):
                continue
            key = f"token_refreshed:{profile.id}"
            last = parse_iso(self.db.kv_get(key))
            if last and now() - last < every:
                continue
            try:
                data = await profile.ig.refresh_token()
            except Exception as e:  # noqa: BLE001
                await self._ig_failed(profile, e, "token yangilash")
                continue
            if data.get("access_token"):
                self.db.update_account(profile.id, ig_token=data["access_token"])
                profile.account["ig_token"] = data["access_token"]
            self.db.kv_set(key, iso())
            done += 1
        return done

    async def retry_unsent_leads(self) -> int:
        """Telegram'ga yetib bormagan lidlarni qayta yuboradi."""
        sent = 0
        for lead in self.db.unsent_leads():
            profile = self.profiles.get(lead["account_id"])
            conv = self.db.get_conv(lead["conv_id"])
            if not profile or not conv:
                continue
            chat = profile.tg_chat or self.tg.default_chat_id
            msg_id = await self.tg.send(tgfmt.lead_message(profile.account, conv, lead["data"], lead["source"]), chat,
                                        buttons=tgfmt.lead_buttons(lead["id"]))
            if msg_id:
                self.db.mark_lead_sent(lead["id"], chat, msg_id)
                sent += 1
        return sent

    # --- Eslatmalar ---
    async def run_followups(self) -> int:
        if self.ai is None:
            return 0
        since = iso(now() - timedelta(days=1))
        sent = 0
        for conv in self.db.followup_candidates():
            profile = self.profiles.get(conv["account_id"])
            if not profile or not profile.cfg.get("eslatma.yoqilgan", True):
                continue
            if self.db.count_events("eslatma", since, profile.id) >= int(profile.cfg.get("eslatma.kunlik_limit", 20)):
                continue
            if conv.get("followups_sent", 0) >= int(profile.cfg.get("eslatma.dialog_uchun_max", 1)):
                continue
            if not self.ai_active(conv) or not self._allowed(profile, conv.get("username")):
                continue
            if self.db.last_message_role(conv["id"]) != "assistant":
                continue  # oxirgi xabar operatorniki yoki mijozniki — eslatma kerak emas
            last_bot = parse_iso(conv["last_bot_at"])
            delay = timedelta(minutes=float(profile.cfg.get("eslatma.kechikish_daqiqa", 180)))
            if not last_bot or now() - last_bot < delay or not self.dm_window_open(conv["id"]):
                continue
            async with self._locks.setdefault(conv["id"], asyncio.Lock()):
                if self.db.last_message_role(conv["id"]) != "assistant" or conv["id"] in self._running:
                    continue  # shu paytda mijoz yozdi
                result = await self.reply(conv["id"], extra_note=followup_rules(), source="followup")
            self.db.update_conv(conv["id"], followups_sent=conv.get("followups_sent", 0) + 1)
            if result:
                sent += 1
                self.db.log("eslatma", conv.get("username") or conv["igsid"], profile.id)
        return sent
