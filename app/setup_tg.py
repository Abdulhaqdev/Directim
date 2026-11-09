"""Telegram botni tekshirish va guruh ID'sini topish.

1) Botni lidlar guruhiga qo'shing va guruhda istalgan xabar yozing.
2) python -m app.setup_tg
"""
import asyncio

import httpx

from .settings import env


async def main() -> None:
    if not env.telegram_bot_token:
        print("❌ .env faylida TELEGRAM_BOT_TOKEN yo'q")
        return
    async with httpx.AsyncClient(timeout=20) as http:
        base = f"https://api.telegram.org/bot{env.telegram_bot_token}"
        me = (await http.get(f"{base}/getMe")).json()
        print("Bot:", me.get("result", {}).get("username"))
        updates = (await http.get(f"{base}/getUpdates")).json().get("result", [])
        chats = {}
        for u in updates:
            msg = u.get("message") or u.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if chat.get("type") in ("group", "supergroup"):
                chats[chat["id"]] = chat.get("title")
        for cid, title in chats.items():
            print(f"Guruh: {title}  →  TELEGRAM_CHAT_ID={cid}")
        if not chats:
            print("Guruh topilmadi: botni guruhga qo'shib, guruhda bitta xabar yozing va qayta ishga tushiring.")
        if env.telegram_chat_id:
            r = (await http.post(f"{base}/sendMessage", json={"chat_id": env.telegram_chat_id, "text": "✅ Test: lidlar shu guruhga keladi"})).json()
            print("Test xabar:", "yuborildi" if r.get("ok") else r)


if __name__ == "__main__":
    asyncio.run(main())
