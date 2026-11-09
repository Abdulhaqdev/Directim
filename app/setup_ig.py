"""Barcha profillarning Instagram tokenlarini tekshirish va webhook obunasini yoqish.

Ishlatish:  python -m app.setup_ig
(Panelda profil sahifasidagi «Tekshirish va ulash» tugmasi ham shu ishni qiladi.)
"""
import asyncio

from .db import DB
from .instagram import Instagram
from .settings import env


async def main() -> None:
    db = DB(env.db_path)
    accounts = [a for a in db.list_accounts() if a.get("ig_token")]
    if not accounts:
        print("❌ Tokeni kiritilgan profil yo'q. Panel → Profillar → token kiriting.")
        return
    for acc in accounts:
        ig = Instagram(acc["ig_token"], "me", env.ig_api_version)
        try:
            me = await ig.me()
            ig_id = str(me.get("user_id") or me.get("id"))
            db.update_account(acc["id"], ig_user_id=ig_id, ig_username=me.get("username") or acc.get("ig_username"))
            await Instagram(acc["ig_token"], ig_id, env.ig_api_version).subscribe_webhooks()
            print(f"✅ {acc['name']}: @{me.get('username')} (ID {ig_id}) — webhook yoqildi")
        except Exception as e:  # noqa: BLE001
            print(f"❌ {acc['name']}: {e}")
        finally:
            await ig.aclose()


if __name__ == "__main__":
    asyncio.run(main())
