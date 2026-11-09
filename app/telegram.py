"""Telegram: lidlar, savollar, ogohlantirishlar — tugmalar bilan (Sotildi/Bo'lmadi, Kerak emas/Keyinroq, Qayta ochish)."""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger("telegram")

PANEL_URL = ""  # main.py PUBLIC_URL'ni shu yerga yozadi — xabarlarda "panelda ochish" havolalari uchun


def esc(value) -> str:
    return html.escape(str(value)) if value not in (None, "") else "—"


Buttons = list[list[tuple[str, str]]]  # [[(matn, "callback:data" yoki "https://url")]]


def _markup(buttons: Buttons | None) -> dict | None:
    if not buttons:
        return None
    rows = []
    for row in buttons:
        cells = []
        for text, data in row:
            cells.append({"text": text, "url": data} if data.startswith(("http://", "https://")) else
                         {"text": text, "callback_data": data[:64]})
        rows.append(cells)
    return {"inline_keyboard": rows}


class Telegram:
    def __init__(self, bot_token: str, default_chat_id: str):
        self.bot_token = bot_token
        self.default_chat_id = default_chat_id
        self.http = httpx.AsyncClient(timeout=20)
        self.migrations: dict[str, str] = {}   # guruh supergroup'ga aylanganda: eski ID → yangi ID
        self.on_migrate = None                 # main.py saqlab qo'yishi uchun

    @property
    def base(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    async def _call(self, method: str, body: dict, timeout: float | None = None) -> dict:
        try:
            r = await self.http.post(f"{self.base}/{method}", json=body, timeout=timeout or 20)
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.error("Telegram %s ulanish xatosi: %s", method, e)
            return {"ok": False, "description": str(e)}

    async def send(self, text: str, chat_id: str | None = None, reply_to: int | None = None,
                   buttons: Buttons | None = None) -> int | None:
        """Xabar yuboradi, Telegram message_id qaytaradi (xato bo'lsa None)."""
        chat = str(chat_id or self.default_chat_id or "")
        chat = self.migrations.get(chat, chat)
        if not self.bot_token or not chat:
            log.warning("Telegram sozlanmagan, xabar yuborilmadi: %s", text[:200])
            return None
        body = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if len(text) > 4000:
            # HTML tegini yarmidan kesib qo'ymaslik uchun oddiy matnga o'tamiz
            body["text"] = html.unescape(re.sub(r"<[^>]+>", "", text))[:4000]
            body.pop("parse_mode")
        if reply_to:
            body["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        markup = _markup(buttons)
        if markup:
            body["reply_markup"] = markup
        for _ in range(2):
            data = await self._call("sendMessage", body)
            if data.get("ok"):
                return data["result"]["message_id"]
            new_id = (data.get("parameters") or {}).get("migrate_to_chat_id")
            if new_id:
                log.warning("Telegram guruh %s → %s ga ko'chdi", chat, new_id)
                self.migrations[chat] = str(new_id)
                if self.on_migrate:
                    self.on_migrate(chat, str(new_id))
                body["chat_id"] = chat = str(new_id)
                continue
            log.error("Telegram xato: %s", str(data)[:300])
            return None
        return None

    async def edit(self, chat_id: str, message_id: int, text: str, buttons: Buttons | None = None) -> bool:
        body = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000], "parse_mode": "HTML",
                "disable_web_page_preview": True, "reply_markup": _markup(buttons) or {"inline_keyboard": []}}
        return bool((await self._call("editMessageText", body)).get("ok"))

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})

    async def get_me(self) -> dict:
        return (await self._call("getMe", {})).get("result") or {}

    async def get_updates(self, offset: int, timeout: int = 25) -> list[dict]:
        data = await self._call("getUpdates", {"offset": offset, "timeout": timeout,
                                               "allowed_updates": ["message", "my_chat_member", "callback_query"]},
                                timeout=timeout + 10)
        return data.get("result") or [] if data.get("ok") else []

    async def delete_webhook(self) -> dict:
        return await self._call("deleteWebhook", {})

    async def set_webhook(self, url: str, secret: str) -> dict:
        return await self._call("setWebhook", {
            "url": url, "secret_token": secret, "allowed_updates": ["message", "my_chat_member", "callback_query"],
        })


# ===================== Xabar matnlari =====================
def _who(conv: dict) -> str:
    username = conv.get("username")
    name = esc(conv.get("name")) if conv.get("name") else ""
    if username:
        return f'{name} (<a href="https://instagram.com/{esc(username)}">@{esc(username)}</a>)'.strip()
    return name or esc(conv.get("igsid") or conv.get("id"))


def _channel(account: dict, kind: str) -> str:
    name = f" ({esc(account.get('name'))})" if account.get("name") else ""
    return f"Instagram · {kind} · @{esc(account.get('ig_username'))}{name}"


def _links(conv: dict) -> list[str]:
    lines = []
    if conv.get("username"):
        lines.append(f'🔗 <b>Suhbat:</b> <a href="https://ig.me/m/{esc(conv["username"])}">Instagram\'da ochish</a>')
    if PANEL_URL and conv.get("id"):
        from urllib.parse import quote
        lines.append(f'🗂 <b>Panel:</b> <a href="{PANEL_URL}/dialog/{quote(str(conv["id"]))}">ochish</a>')
    return lines


def _ago(iso_time: str | None) -> str:
    if not iso_time:
        return ""
    try:
        minutes = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso_time)).total_seconds() // 60)
    except ValueError:
        return ""
    if minutes < 1:
        return "hozirgina"
    if minutes < 60:
        return f"{minutes} daqiqa oldin"
    return f"{minutes // 60} soat oldin" if minutes < 1440 else f"{minutes // 1440} kun oldin"


TEMPERATURE = {"issiq": "🔥 TEZKOR", "iliq": "🌤 Iliq", "sovuq": "❄️ Sovuq"}


def lead_message(account: dict, conv: dict, data: dict, source: str, update: bool = False) -> str:
    kind = {"dm": "DM", "comment": "komment", "followup": "DM", "xotira": "DM"}.get(source, source)
    temp = TEMPERATURE.get(data.get("temperature") or "", "")
    head = "ℹ️ <b>LID YANGILANDI</b>" if update else "🟢 <b>LEAD</b>" + (f" · {temp}" if temp else "") + " · 📞 Yangi mijoz raqami!"
    lines = [head, "",
             f"👤 <b>Mijoz:</b> {_who(conv)}",
             f"📞 <b>Telefon:</b> {esc(data.get('phone') or conv.get('phone'))}",
             f"🌐 <b>Kanal:</b> {_channel(account, kind)}"]
    if data.get("interest"):
        lines.append(f"🛍 <b>Qiziqish:</b> {esc(data['interest'])}")
    if data.get("for_whom"):
        lines.append(f"🎯 <b>Kim uchun:</b> {esc(data['for_whom'])}")
    if data.get("region"):
        lines.append(f"📍 <b>Viloyat:</b> {esc(data['region'])}")
    if data.get("summary"):
        lines.append(f"📝 <b>Xulosa:</b> <i>{esc(data['summary'][:400])}</i>")
    if data.get("lead_reason"):
        lines.append(f"⚡ <b>Sabab:</b> <i>{esc(data['lead_reason'][:200])}</i>")
    lines += _links(conv)
    if not update:
        lines += ["", "<i>Mijozga qo'ng'iroq qiling va natijani tugma bilan belgilang.</i>"]
    return "\n".join(lines)


def lead_buttons(lead_id: int) -> Buttons:
    return [[("💰 Sotildi", f"lead:sold:{lead_id}"), ("🚫 Bo'lmadi", f"lead:lost:{lead_id}")]]


def handoff_message(account: dict, conv: dict, reason: str, last_text: str) -> str:
    lines = ["🙋 <b>OPERATOR KERAK</b> · AI shu dialogda to'xtadi", "",
             f"👤 <b>Mijoz:</b> {_who(conv)}",
             f"🌐 <b>Kanal:</b> {_channel(account, 'DM')}",
             f"❗ <b>Sabab:</b> {esc(reason)}",
             f"💬 <b>Oxirgi xabar:</b> <i>{esc(last_text[:500])}</i>"]
    lines += _links(conv)
    return "\n".join(lines)


def handoff_buttons(conv_id: str) -> Buttons:
    return [[("✅ Men gaplashdim", f"ho:done:{conv_id}"), ("🤖 AI davom etsin", f"ho:ai:{conv_id}")]]


def gap_message(account: dict, conv: dict | None, question: str, product: dict | None, gap_id: int,
                hits: int = 1, asked_at: str | None = None) -> str:
    lines = ["🧠 <b>AI bu savolga javob topa olmadi</b>", "", f"❓ <b>{esc(question)}</b>"]
    who = []
    if conv:
        who.append(f"@{esc(conv['username'])}" if conv.get("username") else "mijoz")
    who.append("Instagram")
    if asked_at:
        who.append(_ago(asked_at))
    lines.append("👤 " + " · ".join(who))
    lines.append(f"🤖 {esc(account.get('name'))} (@{esc(account.get('ig_username'))})")
    if product:
        lines.append(f"🛍 {esc(product['name'])}")
    if hits > 1:
        lines.append(f"🔁 {hits} marta so'ralgan")
    lines += ["", "✍️ <b>Javobni shu xabarga REPLY qilib yozing</b> — xotiraga saqlanadi, kutayotgan mijozga "
                  "yuboriladi va keyingi safar AI o'zi aytadi.", f"#S{gap_id}"]
    return "\n".join(lines)


def gap_buttons(gap_id: int) -> Buttons:
    return [[("❌ Kerak emas", f"gap:no:{gap_id}"), ("⏰ Keyinroq", f"gap:later:{gap_id}")]]


def gap_saved_message(account: dict, question: str, answer: str, sent: int) -> str:
    tail = f"\n📤 Kutayotgan {sent} ta mijozga yuborildi." if sent else ""
    return (f"✅ <b>Javob berildi</b> · 🤖 {esc(account.get('name'))} · ❓ Savol: «{esc(question[:120])}»\n"
            f"{esc(answer)}{tail}")


MOD_REASON = {"HAQORAT": "haqoratli so'z ishlatilgan", "SALBIY": "salbiy / ayblov mazmuni", "SPAM": "spam yoki reklama"}


def moderation_message(account: dict, username: str | None, text: str, category: str, action: str, permalink: str | None) -> str:
    head = {"yashirildi": "🚫 <b>Salbiy izoh — yashirildi</b>", "o'chirildi": "🗑 <b>Salbiy izoh — o'chirildi</b>"}.get(
        action, f"🛡 <b>Moderatsiya</b> — {esc(action)}")
    lines = [head, "",
             f'👤 <b>Muallif:</b> <a href="https://instagram.com/{esc(username)}">@{esc(username)}</a>',
             f"🌐 <b>Kanal:</b> {_channel(account, 'izoh')}",
             f"💬 <b>Izoh:</b> <i>{esc(text[:500])}</i>",
             f"📌 <b>Sabab:</b> {MOD_REASON.get(category, esc(category))}"]
    if permalink:
        lines.append(f'📝 <b>Post:</b> <a href="{esc(permalink)}">ochish</a>')
    if action == "yashirildi":
        lines += ["", "<i>Izoh ommadan yashirildi (muallif o'zi ko'radi). Xato bo'lsa — «Qayta ochish».</i>"]
    return "\n".join(lines)


def moderation_buttons(account_id: int, comment_id: str, action: str) -> Buttons | None:
    if action != "yashirildi":
        return None
    return [[("↩️ Qayta ochish", f"mod:show:{account_id}:{comment_id}"), ("🗑 O'chirish", f"mod:del:{account_id}:{comment_id}")]]


def stamp(text: str, note: str) -> str:
    """Tugma bosilgandan keyin xabar oxiriga natija qo'shish."""
    text = re.sub(r"\n\n<i>Mijozga qo'ng'iroq qiling.*?</i>$", "", text, flags=re.S)
    return f"{text}\n\n<b>{note}</b>"
