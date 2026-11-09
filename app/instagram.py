"""Instagram API (Instagram Login) bilan ishlash: Direct, kommentlar, postlar."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging

import httpx

log = logging.getLogger("instagram")

DM_TEXT_LIMIT = 1000  # Instagram bitta xabar uchun chegara — UTF-8 baytlarda (kirill harfi 2 bayt)


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Meta webhook imzosini tekshirish (X-Hub-Signature-256). Bir nechta secret vergul bilan berilishi mumkin
    (Instagram app secret va Meta app secret)."""
    secrets_ = [s.strip() for s in (app_secret or "").split(",") if s.strip()]
    if not secrets_:
        # Secret'siz webhook'ni qabul qilish xavfli (soxta komment → o'chirish). Faqat sinov uchun ruxsat.
        import os
        return os.getenv("ALLOW_UNSIGNED_WEBHOOKS") == "1"
    if not header or not header.startswith("sha256="):
        return False
    given = header.split("=", 1)[1]
    return any(hmac.compare_digest(hmac.new(s.encode(), body, hashlib.sha256).hexdigest(), given) for s in secrets_)


# --- «Instagram bilan ulash» (Instagram Business Login, OAuth) ---
OAUTH_SCOPES = "instagram_business_basic,instagram_business_manage_messages,instagram_business_manage_comments"


def oauth_url(app_id: str, redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode
    return "https://www.instagram.com/oauth/authorize?" + urlencode({
        "client_id": app_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": OAUTH_SCOPES, "state": state, "enable_fb_login": "0", "force_reauth": "true",
    })


def oauth_url_unified(app_id: str, config_id: str, redirect_uri: str, state: str, api_version: str = "v26.0") -> str:
    """Unified login (Facebook Login for Business): Facebook oynasi ochiladi, Instagram akkauntlar tanlanadi."""
    from urllib.parse import urlencode
    return f"https://www.facebook.com/{api_version}/dialog/oauth?" + urlencode({
        "client_id": app_id, "config_id": config_id, "redirect_uri": redirect_uri, "state": state,
        "response_type": "code", "override_default_response_type": "true",
    })


async def exchange_code_unified(app_id: str, app_secret: str, redirect_uri: str, code: str, api_version: str = "v26.0") -> dict:
    """code → tizim foydalanuvchisi tokeni (muddatsiz), graph.facebook.com uchun."""
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(f"https://graph.facebook.com/{api_version}/oauth/access_token", params={
            "client_id": app_id, "client_secret": app_secret, "redirect_uri": redirect_uri, "code": code.split("#")[0],
        })
    if r.status_code >= 400:
        raise IGError(r.status_code, r.text, "oauth/access_token")
    return r.json()


async def exchange_code(app_id: str, app_secret: str, redirect_uri: str, code: str) -> dict:
    """code → uzoq muddatli (60 kunlik) token. {"access_token", "user_id", "expires_in"} qaytaradi."""
    code = code.split("#")[0]
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post("https://api.instagram.com/oauth/access_token", data={
            "client_id": app_id, "client_secret": app_secret, "grant_type": "authorization_code",
            "redirect_uri": redirect_uri, "code": code,
        })
        if r.status_code >= 400:
            raise IGError(r.status_code, r.text, "oauth/access_token")
        short = r.json()
        if isinstance(short.get("data"), list) and short["data"]:
            short = short["data"][0]
        r = await http.get("https://graph.instagram.com/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": app_secret, "access_token": short["access_token"],
        })
        if r.status_code >= 400:
            raise IGError(r.status_code, r.text, "access_token")
        long = r.json()
    return {"access_token": long["access_token"], "user_id": str(short.get("user_id", "")), "expires_in": long.get("expires_in")}


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def split_text(text: str, limit: int = DM_TEXT_LIMIT) -> list[str]:
    """Matnni Instagram chegarasiga (baytlarda) sig'adigan bo'laklarga ajratadi, iloji bo'lsa qator/so'z chegarasida."""
    text = text.strip()
    parts: list[str] = []
    while _bytes(text) > limit:
        # limit baytga sig'adigan eng uzun prefiks
        cut = len(text)
        while _bytes(text[:cut]) > limit:
            cut = max(1, int(cut * limit / _bytes(text[:cut])) - 1)
        window = text[:cut]
        nl = window.rfind("\n")
        sp = window.rfind(" ")
        if nl >= cut // 2:
            cut = nl
        elif sp >= cut // 2:
            cut = sp
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


class IGError(Exception):
    """Instagram API xatosi (token eskirgan, ruxsat yo'q, 24 soat oynasi yopilgan va h.k.)."""

    def __init__(self, status: int, body: str, path: str):
        self.status, self.body, self.path = status, body, path
        super().__init__(f"Instagram {path}: {status} {body[:300]}")

    @property
    def token_problem(self) -> bool:
        return self.status in (401, 403) or '"code":190' in self.body.replace(" ", "")


class Instagram:
    """Ikki xil ulanishni qo'llaydi:
    - Instagram Login (eski ilovalar): token `IG…`, manzil graph.instagram.com, token 60 kunda yangilanadi;
    - Unified login / Facebook Login for Business (2026 dan yangi ilovalar): token `EAA…` (tizim foydalanuvchisi,
      muddatsiz), manzil graph.facebook.com. Endpointlar Instagram akkaunt ID'si bilan bir xil yo'lda."""

    def __init__(self, token: str, user_id: str, api_version: str = "v26.0"):
        self.token = token
        self.user_id = user_id or "me"
        self.unified = (token or "").startswith("EAA")
        host = "graph.facebook.com" if self.unified else "graph.instagram.com"
        self.base = f"https://{host}/{api_version}"
        self.http = httpx.AsyncClient(timeout=30)
        self._media_cache: dict[str, dict] = {}

    async def _post(self, path: str, json: dict | None = None, params: dict | None = None) -> dict:
        try:
            r = await self.http.post(
                f"{self.base}/{path}",
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
            )
        except httpx.HTTPError as e:  # tarmoq uzilishi / timeout ham Instagram xatosi sifatida
            raise IGError(0, f"tarmoq xatosi: {e!r}", path) from e
        if r.status_code >= 400:
            log.error("IG POST %s -> %s %s", path, r.status_code, r.text[:500])
            raise IGError(r.status_code, r.text, path)
        return r.json() if r.content else {}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = await self.http.get(
                f"{self.base}/{path}", params=params, headers={"Authorization": f"Bearer {self.token}"}
            )
        except httpx.HTTPError as e:
            raise IGError(0, f"tarmoq xatosi: {e!r}", path) from e
        if r.status_code >= 400:
            log.error("IG GET %s -> %s %s", path, r.status_code, r.text[:500])
            raise IGError(r.status_code, r.text, path)
        return r.json()

    # --- Direct ---
    async def send_dm(self, recipient_id: str, text: str, on_sent=None) -> list[str]:
        """Matnni yuboradi (uzun bo'lsa bo'laklab). Har bir bo'lak ketishi bilan on_sent(mid) chaqiriladi —
        shunda Instagram'dan qaytgan echo "operator yozdi" deb adashtirilmaydi."""
        mids = []
        for part in split_text(text):
            data = await self._post(
                f"{self.user_id}/messages",
                json={"recipient": {"id": recipient_id}, "message": {"text": part}},
            )
            if data.get("message_id"):
                mids.append(data["message_id"])
                if on_sent:
                    on_sent(data["message_id"])
        return mids

    async def private_reply(self, comment_id: str, text: str, on_sent=None) -> list[str]:
        """Komment egasiga Direct'da javob (bitta komment uchun bir marta, 7 kun ichida)."""
        data = await self._post(
            f"{self.user_id}/messages",
            json={"recipient": {"comment_id": comment_id}, "message": {"text": split_text(text)[0]}},
        )
        if data.get("message_id") and on_sent:
            on_sent(data["message_id"])
        return [data["message_id"]] if data.get("message_id") else []

    # --- Kommentlar ---
    async def reply_comment(self, comment_id: str, text: str) -> None:
        await self._post(f"{comment_id}/replies", params={"message": text})

    async def hide_comment(self, comment_id: str) -> None:
        await self._post(comment_id, params={"hide": "true"})

    async def unhide_comment(self, comment_id: str) -> None:
        await self._post(comment_id, params={"hide": "false"})

    async def delete_comment(self, comment_id: str) -> None:
        try:
            r = await self.http.delete(
                f"{self.base}/{comment_id}", headers={"Authorization": f"Bearer {self.token}"}
            )
        except httpx.HTTPError as e:
            raise IGError(0, f"tarmoq xatosi: {e!r}", comment_id) from e
        if r.status_code >= 400:
            log.error("IG DELETE %s -> %s %s", comment_id, r.status_code, r.text[:500])
            raise IGError(r.status_code, r.text, comment_id)

    # --- Profil va postlar ---
    async def get_user(self, user_id: str) -> dict:
        try:
            return await self._get(user_id, params={"fields": "name,username"})
        except (httpx.HTTPError, IGError):
            return {}

    async def get_media(self, media_id: str) -> dict:
        if media_id in self._media_cache:
            return self._media_cache[media_id]
        try:
            data = await self._get(media_id, params={"fields": "caption,media_type,permalink,timestamp"})
        except (httpx.HTTPError, IGError):
            data = {}
        self._media_cache[media_id] = data
        return data

    async def recent_media(self, limit: int = 25) -> list[dict]:
        data = await self._get(
            f"{self.user_id}/media",
            params={"fields": "id,caption,media_type,permalink,timestamp", "limit": limit},
        )
        items = data.get("data", [])
        for item in items:
            self._media_cache[item["id"]] = item
        return items

    async def refresh_token(self) -> dict:
        """Uzoq muddatli tokenni yangilaydi (60 kun amal qiladi; kamida 24 soatlik token yangilanadi).
        Unified login tokeni muddatsiz — yangilash kerak emas."""
        if self.unified:
            return {}
        try:
            r = await self.http.get(
                "https://graph.instagram.com/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": self.token},
            )
        except httpx.HTTPError as e:
            raise IGError(0, f"tarmoq xatosi: {e!r}", "refresh_access_token") from e
        if r.status_code >= 400:
            raise IGError(r.status_code, r.text, "refresh_access_token")
        data = r.json()
        if data.get("access_token"):
            self.token = data["access_token"]
        return data

    async def aclose(self) -> None:
        await self.http.aclose()

    async def conversations(self, limit: int = 50, pages: int = 4) -> list[dict]:
        """Direct dialoglar ro'yxati (eng yangisidan)."""
        out: list[dict] = []
        params = {"platform": "instagram", "fields": "id,updated_time,participants", "limit": min(limit, 50)}
        data = await self._get(f"{self.user_id}/conversations", params=params)
        out += data.get("data", [])
        while len(out) < limit and pages > 1 and (data.get("paging") or {}).get("cursors", {}).get("after"):
            params["after"] = data["paging"]["cursors"]["after"]
            data = await self._get(f"{self.user_id}/conversations", params=params)
            out += data.get("data", [])
            pages -= 1
        return out[:limit]

    async def conversation_messages(self, conversation_id: str, limit: int = 50) -> list[dict]:
        """Bitta dialogdagi xabarlar (from, message, created_time)."""
        data = await self._get(conversation_id, params={
            "fields": f"messages.limit({min(limit, 50)}){{id,created_time,from,to,message}}"})
        return (data.get("messages") or {}).get("data", [])

    async def assigned_accounts(self) -> list[dict]:
        """Unified login tokeni ochgan barcha Instagram akkauntlar: [{"id", "username"}]."""
        try:
            data = await self._get("me/assigned_instagram_accounts", params={"fields": "id,username", "limit": 100})
            items = data.get("data") or []
        except IGError:
            items = []
        if not items:
            data = await self._get("me", params={"fields": "accounts{instagram_business_account{id,username}}"})
            items = [a["instagram_business_account"] for a in (data.get("accounts") or {}).get("data", [])
                     if a.get("instagram_business_account")]
        return [{"id": str(i["id"]), "username": i.get("username")} for i in items]

    async def me(self) -> dict:
        """{"user_id": Instagram akkaunt ID, "username": ...}"""
        if not self.unified:
            return await self._get("me", params={"fields": "user_id,username,name"})
        # Unified login: /me — tizim foydalanuvchisi; Instagram akkaunt alohida so'raladi
        try:
            data = await self._get("me/assigned_instagram_accounts", params={"fields": "id,username"})
            items = data.get("data") or []
        except IGError:
            items = []
        if not items:
            data = await self._get("me", params={"fields": "accounts{instagram_business_account{id,username}}"})
            items = [a["instagram_business_account"] for a in (data.get("accounts") or {}).get("data", [])
                     if a.get("instagram_business_account")]
        if not items:
            raise IGError(400, "Token bilan bog'langan Instagram akkaunt topilmadi (ilovada akkaunt tester qilinganmi?)", "me")
        acc = items[0]
        return {"user_id": str(acc["id"]), "id": str(acc["id"]), "username": acc.get("username")}

    async def subscribe_webhooks(self) -> dict:
        if not self.unified:
            return await self._post("me/subscribed_apps", params={"subscribed_fields": "messages,comments"})
        try:
            return await self._post(f"{self.user_id}/subscribed_apps", params={"subscribed_fields": "messages,comments"})
        except IGError as e:
            # Unified login'da obuna Meta ilovasi panelidagi Webhooks bo'limida qilinadi
            return {"success": False, "eslatma": str(e)[:200]}

    async def download_image(self, url: str) -> tuple[str, str] | None:
        """Rasmni yuklab (media_type, base64) qaytaradi — Claude'ga ko'rsatish uchun."""
        try:
            r = await self.http.get(url, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPError:
            return None
        media_type = r.headers.get("content-type", "image/jpeg").split(";")[0]
        if media_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
            media_type = "image/jpeg"
        if len(r.content) > 4_500_000:
            return None
        return media_type, base64.standard_b64encode(r.content).decode()
