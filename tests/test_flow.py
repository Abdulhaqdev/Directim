"""Instagram, Telegram va Claude'ni soxta obyektlar bilan almashtirib, butun oqimni tekshiradi."""
import asyncio
import hashlib
import hmac
import json
from datetime import timedelta

import pytest

from app import catalog
from app.ai import AI, CommentResult, DMResult, build_knowledge
from app.db import DB, conv_key, iso, now
from app.logic import Agent, extract_phone
from app.settings import default_settings_yaml

BOOKS_IG = "17840000000000001"
KIDS_IG = "17840000000000002"


class FakeIG:
    def __init__(self, name):
        self.name = name
        self.dms, self.private, self.replies, self.hidden, self.deleted = [], [], [], [], []

    async def send_dm(self, rid, text, on_sent=None):
        self.dms.append((rid, text))
        mid = f"{self.name}-mid-{len(self.dms)}"
        if on_sent:
            on_sent(mid)
        return [mid]

    async def private_reply(self, cid, text, on_sent=None):
        self.private.append((cid, text))
        mid = f"{self.name}-pmid-{len(self.private)}"
        if on_sent:
            on_sent(mid)
        return [mid]

    async def reply_comment(self, cid, text):
        self.replies.append((cid, text))

    async def hide_comment(self, cid):
        self.hidden.append(cid)

    async def delete_comment(self, cid):
        self.deleted.append(cid)

    async def get_user(self, uid):
        return {"username": f"user_{uid}", "name": "Test Mijoz"}

    async def get_media(self, mid):
        return {"caption": "Post matni", "permalink": "https://instagram.com/p/x"}

    async def recent_media(self, limit=25):
        return []

    async def download_image(self, url):
        return ("image/jpeg", "AAAA")


class FakeTG:
    default_chat_id = "-100main"

    def __init__(self):
        self.sent = []  # (chat, text, reply_to)

    async def send(self, text, chat_id=None, reply_to=None, buttons=None):
        self.sent.append((chat_id or self.default_chat_id, text, reply_to))
        self.buttons = getattr(self, "buttons", [])
        self.buttons.append(buttons)
        return 1000 + len(self.sent)

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edits = getattr(self, "edits", [])
        self.edits.append((chat_id, message_id, text))
        return True

    async def answer_callback(self, callback_id, text=""):
        self.toasts = getattr(self, "toasts", [])
        self.toasts.append(text)

    def texts(self):
        return [t for _, t, _ in self.sent]


class FakeAI:
    def __init__(self):
        self.dm_queue: list[DMResult] = []
        self.comment_queue: list[CommentResult] = []
        self.calls = []

    async def dm_reply(self, knowledge, rules, messages):
        self.calls.append({"knowledge": knowledge, "messages": messages})
        return self.dm_queue.pop(0)

    async def classify_comment(self, knowledge, rules, block):
        self.calls.append({"knowledge": knowledge, "block": block})
        return self.comment_queue.pop(0)


def dm(reply="", **kw):
    base = dict(reply=reply, phone=None, name=None, interest=None, product_id=None, for_whom=None, region=None,
                needs_human=False, handoff_reason=None, unknown_question=None, summary="xulosa")
    base.update(kw)
    return DMResult(**base)


def cm(category, public="", dm_message="", **kw):
    return CommentResult(category=category, public_reply=public, dm_message=dm_message,
                         unknown_question=kw.get("unknown_question"), product_id=kw.get("product_id"), reason="-")


def msg_event(ig_id, sender, text, mid, echo=False, recipient=None, attachments=None):
    message = {"mid": mid, "text": text}
    if echo:
        message["is_echo"] = True
    if attachments:
        message["attachments"] = attachments
    return {"object": "instagram", "entry": [{"id": ig_id, "messaging": [
        {"sender": {"id": sender}, "recipient": {"id": recipient or ig_id}, "message": message}]}]}


def comment_event(ig_id, cid, uid, username, text, media="m1"):
    return {"object": "instagram", "entry": [{"id": ig_id, "changes": [{"field": "comments", "value": {
        "id": cid, "text": text, "from": {"id": uid, "username": username}, "media": {"id": media}}}]}]}


@pytest.fixture
def setup():
    db = DB(":memory:")
    books = db.add_account(name="Kitoblar", ig_user_id=BOOKS_IG, ig_username="ilmbooks.uz", ig_token="t1",
                           tg_chat_id="", categories="Kitoblar, Dekor", business_text="Kitob do'koni",
                           settings_yaml=default_settings_yaml(), active=1)
    kids = db.add_account(name="Bolalar", ig_user_id=KIDS_IG, ig_username="bolalar_nabor", ig_token="t2",
                          tg_chat_id="-100kids", categories="Bolalar", business_text="Bolalar do'koni",
                          settings_yaml="", active=1)
    saodat = db.upsert_product(name="Saodat asri", category="Kitoblar", price=269000, price_note="to'plam",
                               template="Saodat asri 4 kitob — 269 000 so'm", post_ids=["post-saodat"])
    grusha = db.upsert_product(name="Boks grusha", category="Bolalar", price=129000)
    igs = {}

    def factory(acc):
        igs.setdefault(acc["id"], FakeIG(acc["name"]))
        return igs[acc["id"]]

    tg, ai = FakeTG(), FakeAI()
    agent = Agent(db, tg, ai, factory, debounce=0.01)
    return dict(agent=agent, db=db, tg=tg, ai=ai, books=books, kids=kids, ig_books=igs[books], ig_kids=igs[kids],
                saodat=saodat, grusha=grusha)


def run(coro):
    return asyncio.run(coro)


def test_extract_phone():
    assert extract_phone("+998 88 111 07 37") == "+998881110737"
    assert extract_phone("raqamim 90 123 45 67") == "+998901234567"
    assert extract_phone("998931234567") == "+998931234567"
    assert extract_phone("narxi 269 000 so'm") is None


def test_each_profile_sees_only_its_catalog(setup):
    a, ai = setup["agent"], setup["ai"]

    async def scenario():
        ai.dm_queue += [dm("Saodat asri 269 000 so'm"), dm("Grusha 129 000 so'm")]
        await a.handle_webhook(msg_event(BOOKS_IG, "c1", "saodat narxi?", "m1"))
        await a.flush()
        await a.handle_webhook(msg_event(KIDS_IG, "c2", "grusha qancha?", "m2"))
        await a.flush()

    run(scenario())
    books_k = next(c["knowledge"] for c in ai.calls if "Kitob do'koni" in c["knowledge"])
    kids_k = next(c["knowledge"] for c in ai.calls if "Bolalar do'koni" in c["knowledge"])
    assert "Saodat asri" in books_k and "Boks grusha" not in books_k
    assert "Boks grusha" in kids_k and "Saodat asri" not in kids_k
    assert "JAVOB SHABLONI" in books_k
    assert setup["ig_books"].dms[0][0] == "c1" and setup["ig_kids"].dms[0][0] == "c2"


def test_unknown_account_ignored(setup):
    run(setup["agent"].handle_webhook(msg_event("999", "c1", "salom", "m1")))
    assert setup["ai"].calls == []


def test_lead_goes_to_profile_group(setup):
    a, ai, tg = setup["agent"], setup["ai"], setup["tg"]

    async def scenario():
        ai.dm_queue.append(dm("Rahmat!", interest="Boks grusha", region="Samarqand"))
        await a.handle_webhook(msg_event(KIDS_IG, "c3", "90 123 45 67", "m1"))
        await a.flush()

    run(scenario())
    chat, text, _ = tg.sent[0]
    assert chat == "-100kids" and "LEAD" in text and "Bolalar" in text and "+998901234567" in text
    assert "Raqamingiz qabul qilindi" in setup["ig_kids"].dms[-1][1]
    assert len(setup["db"].list_leads()) == 1


def test_unknown_question_telegram_reply_saves_memory_and_answers_customer(setup):
    a, ai, tg, db = setup["agent"], setup["ai"], setup["tg"], setup["db"]

    async def scenario():
        ai.dm_queue.append(dm("Bu savolni egamizdan aniqlab beraman.", unknown_question="«Saodat asri» sahifa soni",
                              product_id=setup["saodat"]))
        await a.handle_webhook(msg_event(BOOKS_IG, "c4", "saodat asri necha betli?", "m1"))
        await a.flush()
        # Ikkinchi mijoz ham shu savolni berdi — yangi Telegram xabar bo'lmaydi, kutuvchilar ro'yxatiga qo'shiladi
        ai.dm_queue.append(dm("Aniqlab beraman.", unknown_question="«Saodat asri» sahifa soni", product_id=setup["saodat"]))
        await a.handle_webhook(msg_event(BOOKS_IG, "c5", "nechta sahifa?", "m2"))
        await a.flush()

        gap_msgs = [t for t in tg.texts() if "javob topa olmadi" in t]
        assert len(gap_msgs) == 1
        gap = db.list_gaps()[0]
        assert gap["tg_message_id"] and gap["product_id"] == setup["saodat"]

        # Egasi Telegram'da Reply qiladi
        ai.dm_queue += [dm("Har bir kitob 300 betdan iborat."), dm("Har bir kitob 300 bet.")]
        await a.handle_telegram_update({"message": {
            "message_id": 77, "text": "Har bir kitob taxminan 300 bet", "chat": {"id": gap["tg_chat_id"]}, "from": {"id": 5},
            "reply_to_message": {"message_id": gap["tg_message_id"]}}}, admin_ids=set())

    run(scenario())
    mem = db.memory_for(setup["books"])
    assert any(m["answer"] == "Har bir kitob taxminan 300 bet" and m["product_id"] == setup["saodat"] for m in mem)
    assert db.list_gaps()[0]["status"] == "javob_berildi"
    assert [d[0] for d in setup["ig_books"].dms[-2:]] == ["c4", "c5"]
    assert any("Javob berildi" in t and "2 ta mijozga" in t for t in tg.texts())
    note = ai.calls[-1]["messages"][-1]["content"]
    assert "Do'kon egasi" in note and "300 bet" in note
    k = a.knowledge(a.profiles[setup["books"]])
    assert "300 bet" in k
    assert "300 bet" not in a.knowledge(a.profiles[setup["kids"]])


def test_telegram_reply_from_non_admin_ignored(setup):
    a, db = setup["agent"], setup["db"]
    gap, _ = db.add_gap(setup["books"], "Savol?")
    db.set_gap_tg(gap["id"], "-100main", 555)
    run(a.handle_telegram_update({"message": {"message_id": 1, "text": "javob", "chat": {"id": "-100main"}, "from": {"id": 9},
                                              "reply_to_message": {"message_id": 555}}}, admin_ids={"5"}))
    assert db.get_gap(gap["id"])["status"] == "kutilmoqda"


def test_global_memory_visible_to_all(setup):
    a, db = setup["agent"], setup["db"]
    gap, _ = db.add_gap(setup["kids"], "Ish vaqtingiz?")
    run(a.answer_gap(gap["id"], "Har kuni 9:00–21:00", scope="hammasi"))
    assert "9:00–21:00" in a.knowledge(a.profiles[setup["books"]])
    assert "9:00–21:00" in a.knowledge(a.profiles[setup["kids"]])


def test_comment_on_linked_post(setup):
    a, ai, ig = setup["agent"], setup["ai"], setup["ig_books"]
    ai.comment_queue.append(cm("SAVOL", "Saodat asri — 4 kitob. ✅ Direct'ga yozdik", "Salom! Saodat asri 269 000 so'm."))
    run(a.handle_webhook(comment_event(BOOKS_IG, "k1", "u1", "kitobxon", "narxi?", media="post-saodat")))
    assert f"[#{setup['saodat']}]" in ai.calls[0]["block"]
    assert ig.private and ig.replies
    conv = setup["db"].get_conv(conv_key(setup["books"], "u1"))
    assert conv["lead_data"]["interest"] == "Saodat asri"


def test_moderation(setup):
    a, ai, ig, tg = setup["agent"], setup["ai"], setup["ig_books"], setup["tg"]
    ai.comment_queue += [cm("HAQORAT"), cm("SPAM")]
    run(a.handle_webhook(comment_event(BOOKS_IG, "k2", "u2", "bad", "yomon so'z")))
    run(a.handle_webhook(comment_event(BOOKS_IG, "k3", "u3", "spam", "obuna bo'ling")))
    assert ig.hidden == ["k2", "k3"]  # yashiriladi — Telegram'dan qayta ochish mumkin
    assert sum("Salbiy izoh" in t for t in tg.texts()) == 2
    assert tg.buttons[-1][0][0][1] == f"mod:show:{setup['books']}:k3"


def test_operator_echo_pauses_ai(setup):
    a, ai, ig, db = setup["agent"], setup["ai"], setup["ig_books"], setup["db"]

    async def scenario():
        ai.dm_queue.append(dm("AI javob"))
        await a.handle_webhook(msg_event(BOOKS_IG, "c6", "salom", "m1"))
        await a.flush()
        await a.handle_webhook(msg_event(BOOKS_IG, BOOKS_IG, "AI javob", "Kitoblar-mid-1", echo=True, recipient="c6"))
        assert a.ai_active(db.get_conv(conv_key(setup["books"], "c6")))
        await a.handle_webhook(msg_event(BOOKS_IG, BOOKS_IG, "Men operatorman", "op-1", echo=True, recipient="c6"))
        await a.handle_webhook(msg_event(BOOKS_IG, "c6", "yaxshi", "m2"))
        await a.flush()

    run(scenario())
    assert len(ig.dms) == 1
    assert not a.ai_active(db.get_conv(conv_key(setup["books"], "c6")))


def test_followup(setup):
    a, ai, db = setup["agent"], setup["ai"], setup["db"]

    async def scenario():
        ai.dm_queue.append(dm("Saodat asri 269 000 so'm. Raqam qoldirasizmi?"))
        await a.handle_webhook(msg_event(BOOKS_IG, "c7", "narxi?", "m1"))
        await a.flush()
        key = conv_key(setup["books"], "c7")
        db.update_conv(key, last_bot_at=iso(now() - timedelta(minutes=200)), last_user_at=iso(now() - timedelta(minutes=205)))
        ai.dm_queue.append(dm("Eslatma"))
        assert await a.run_followups() == 1
        assert await a.run_followups() == 0

    run(scenario())


def test_csv_import_and_sheets_url():
    db = DB(":memory:")
    count, errors = catalog.import_csv(db, catalog.CSV_TEMPLATE)
    assert count == 1 and not errors
    p = db.list_products()[0]
    assert p["name"] == "Saodat asri" and p["price"] == 269000 and p["category"] == "Kitoblar" and p["template"]
    catalog.import_csv(db, catalog.CSV_TEMPLATE.replace("269000", "279000"))
    assert len(db.list_products()) == 1 and db.list_products()[0]["price"] == 279000
    catalog.import_csv(db, "nomi;kategoriya;narx\nAtir;Naborlar;99 000\n")
    assert any(x["name"] == "Atir" and x["price"] == 99000 for x in db.list_products())
    assert catalog.sheets_csv_url("https://docs.google.com/spreadsheets/d/ABC_1/edit#gid=42") == \
        "https://docs.google.com/spreadsheets/d/ABC_1/export?format=csv&gid=42"


def test_knowledge_marks_out_of_stock():
    k = build_knowledge("x", [{"id": 1, "name": "A", "category": "K", "price": None, "in_stock": 0}], [], [])
    assert "MAVJUD EMAS" in k and "narxi ko'rsatilmagan" in k


def test_claude_request_shape():
    import anthropic
    import httpx2

    captured = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        out = {"reply": "Salom!", "phone": None, "name": None, "interest": None, "product_id": None, "for_whom": None,
               "region": None, "needs_human": False, "handoff_reason": None, "unknown_question": None, "summary": "s"}
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(out)}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10}})

    ai = AI("test-key", "claude-opus-5-5", "low")
    ai.client = anthropic.AsyncAnthropic(api_key="test-key", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    msgs = [{"role": "user", "content": [{"type": "text", "text": "salom"}]}, {"role": "system", "content": "holat"}]
    result = asyncio.run(ai.dm_reply("BILIM", "QOIDA", msgs))
    body = captured["body"]
    assert result.reply == "Salom!"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in captured["headers"].get("anthropic-beta", "")
    assert body["output_config"]["effort"] == "low" and body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_server_and_panel():
    from fastapi.testclient import TestClient
    import app.main as main

    main.env.ig_verify_token = "secret-verify"
    main.env.ig_app_secret = "appsecret"
    main.env.admin_password = "parol"
    main.env.telegram_webhook_secret = "tgsecret"
    client = TestClient(main.app)
    auth = ("admin", "parol")

    r = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "secret-verify", "hub.challenge": "42"})
    assert r.status_code == 200 and r.text == "42"
    body = json.dumps({"object": "instagram", "entry": []}).encode()
    assert client.post("/webhook", content=body, headers={"x-hub-signature-256": "sha256=00"}).status_code == 401
    sig = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
    assert client.post("/webhook", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
    assert client.post("/telegram/webhook", json={}).status_code == 401
    assert client.post("/telegram/webhook", json={}, headers={"x-telegram-bot-api-secret-token": "tgsecret"}).status_code == 200
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/kirish"

    # boshlang'ich ma'lumotlar (Morechat'dan) yuklangan
    assert main.db.list_accounts()[0]["name"] == "Kitoblar"
    assert len(main.db.list_products()) >= 10
    assert len(main.db.list_memory()) >= 5
    assert len(main.db.list_gaps()) == 7
    books_k = main.agent.knowledge(next(iter(main.agent.profiles.values())))
    assert "Saodat asri" in books_k and "grusha" not in books_k.lower()

    r = client.post("/profillar/yangi", data={"name": "Bolalar naborlari", "ig_username": "@bolalar", "categories": "Bolalar, Naborlar"},
                    auth=auth, follow_redirects=False)
    assert r.status_code == 303
    acc_id = int(r.headers["location"].split("/profil/")[1].split("?")[0])
    kids_k = main.agent.knowledge(main.agent.profiles[acc_id])
    assert "Boks meshok grusha" in kids_k and "Saodat asri" not in kids_k
    r = client.post("/mahsulot/0", data={"name": "Lego nabor", "category": "Bolalar", "price": "350 000", "in_stock": "1",
                                         "template": "Lego — 350 000 so'm"}, auth=auth, follow_redirects=False)
    assert r.status_code == 303
    assert "Lego nabor" in main.agent.knowledge(main.agent.profiles[acc_id])
    bad = client.post(f"/profil/{acc_id}", data={"name": "X", "settings_yaml": "a: [1"}, auth=auth, follow_redirects=False)
    assert "xato" in bad.headers["location"]
    client.post("/xotira/0", data={"question": "Ish vaqti?", "answer": "9–21", "account_id": "", "product_id": ""}, auth=auth)
    assert any(m["question"] == "Ish vaqti?" for m in main.db.list_memory())
    files = {"fayl": ("k.csv", "nomi,kategoriya,narx\nMashinka,Bolalar,219000\n", "text/csv")}
    r = client.post("/mahsulotlar/import", files=files, auth=auth, follow_redirects=False)
    assert "ok=" in r.headers["location"]

    gap_id = main.db.list_gaps()[0]["id"]
    client.post(f"/savollar/{gap_id}", data={"javob": "310 000 so'm", "qamrov": "profil"}, auth=auth)
    assert main.db.get_gap(gap_id)["status"] == "javob_berildi"

    # cookie bilan kirish
    r = client.post("/kirish", data={"parol": "parol"}, follow_redirects=False)
    assert r.status_code == 303 and "sotuv_agent" in r.headers.get("set-cookie", "")
    assert client.get("/", follow_redirects=False).status_code == 200
    client.cookies.clear()
    assert client.post("/kirish", data={"parol": "xato"}, follow_redirects=False).headers["location"] == "/kirish?xato=1"
    # oddiy sozlamalar formasi
    r = client.post(f"/profil/{acc_id}/sozlamalar", data={"direct.yoqilgan": "1", "agent.ism": "Sinov menejer",
                    "sotuv.lid_savollari": "Kim uchun?\nQaysi viloyat?", "eslatma.kechikish_daqiqa": "90"},
                    auth=auth, follow_redirects=False)
    assert r.status_code == 303
    cfg = main.agent.profiles[acc_id].cfg
    assert cfg.get("agent.ism") == "Sinov menejer" and cfg.get("komment.yoqilgan") is False
    assert cfg.get("sotuv.lid_savollari") == ["Kim uchun?", "Qaysi viloyat?"] and cfg.get("eslatma.kechikish_daqiqa") == 90
    for path in ["/", "/profillar", f"/profil/{acc_id}", "/mahsulotlar", "/mahsulotlar?q=saodat", "/mahsulot/1", "/mahsulot/0",
                 "/xotira", "/savollar", "/dialoglar", "/kommentlar", "/mahsulotlar/namuna.csv", "/sinash", "/ulanishlar",
                 f"/sinash?profil={acc_id}"]:
        assert client.get(path, auth=auth).status_code == 200, path


# ===================== Tuzatishlar uchun testlar =====================
from app.instagram import IGError, split_text


def test_split_text_counts_utf8_bytes():
    cyr = "Ассалому алайкум " * 80  # ~1360 belgi, ~2500 bayt
    parts = split_text(cyr)
    assert len(parts) >= 3 and all(len(p.encode()) <= 1000 for p in parts)
    assert "".join(parts).replace(" ", "") == cyr.replace(" ", "")


def test_card_number_not_phone():
    assert extract_phone("karta 8600 1234 5678 9012") is None


def test_returning_customer_gets_new_session(setup):
    a, ai, db = setup["agent"], setup["ai"], setup["db"]
    a.profiles[setup["books"]].cfg.raw["agent"]["max_javoblar"] = 1

    async def scenario():
        ai.dm_queue.append(dm("1-javob"))
        await a.handle_webhook(msg_event(BOOKS_IG, "r1", "salom", "m1"))
        await a.flush()
        key = conv_key(setup["books"], "r1")
        # 2 kun o'tdi
        db._exec("UPDATE messages SET created_at=? WHERE conv_id=?", (iso(now() - timedelta(days=2)), key))
        ai.dm_queue.append(dm("Yana xush kelibsiz"))
        await a.handle_webhook(msg_event(BOOKS_IG, "r1", "yana salom", "m2"))
        await a.flush()

    run(scenario())
    assert len(setup["ig_books"].dms) == 2


def test_ig_send_failure_still_sends_lead_and_alerts(setup):
    a, ai, tg, ig = setup["agent"], setup["ai"], setup["tg"], setup["ig_books"]

    async def broken(rid, text, **kw):
        raise IGError(400, '{"error":{"code":190,"message":"token expired"}}', "messages")

    ig.send_dm = broken

    async def scenario():
        ai.dm_queue.append(dm("Rahmat!"))
        await a.handle_webhook(msg_event(BOOKS_IG, "f1", "raqamim 90 111 22 33", "m1"))
        await a.flush()

    run(scenario())
    texts = tg.texts()
    assert any("LEAD" in t for t in texts)
    assert any("token ishlamayapti" in t for t in texts)


def test_ai_failure_alerts_once(setup):
    a, ai, tg = setup["agent"], setup["ai"], setup["tg"]

    async def none(*args):
        return None

    ai.dm_reply = none
    ai.last_error = "Claude API xato 500"

    async def scenario():
        await a.handle_webhook(msg_event(BOOKS_IG, "x1", "salom", "m1"))
        await a.flush()
        await a.handle_webhook(msg_event(BOOKS_IG, "x2", "salom", "m2"))
        await a.flush()

    run(scenario())
    assert sum("AI javob bermadi" in t for t in tg.texts()) == 1


def test_comment_dm_failure_no_false_public_reply(setup):
    a, ai, ig = setup["agent"], setup["ai"], setup["ig_books"]

    async def broken(cid, text, **kw):
        raise IGError(400, '{"error":{"message":"already replied"}}', "messages")

    ig.private_reply = broken
    ai.comment_queue.append(cm("SAVOL", "✅ Direct'ga yozdik", "Salom!"))
    run(a.handle_webhook(comment_event(BOOKS_IG, "k9", "u9", "x", "narxi?")))
    assert ig.replies == []


def test_duplicate_comment_webhook_processed_once(setup):
    a, ai = setup["agent"], setup["ai"]
    ai.comment_queue.append(cm("NEYTRAL", "Rahmat 🤍"))
    ev = comment_event(BOOKS_IG, "dup1", "u1", "a", "Mashallah")

    async def scenario():
        await asyncio.gather(a.handle_webhook(ev), a.handle_webhook(ev))

    run(scenario())
    assert len(ai.calls) == 1


def test_no_followup_for_comment_only_conversation(setup):
    a, ai, db = setup["agent"], setup["ai"], setup["db"]
    ai.comment_queue.append(cm("SAVOL", "✅ Direct'ga yozdik", "Salom! Narxi 269 000."))
    run(a.handle_webhook(comment_event(BOOKS_IG, "k10", "u10", "x", "narxi?")))
    key = conv_key(setup["books"], "u10")
    db.update_conv(key, last_bot_at=iso(now() - timedelta(hours=4)), last_user_at=iso(now() - timedelta(hours=4, minutes=1)))
    assert run(a.run_followups()) == 0


def test_skip_deleted_message(setup):
    a, ai = setup["agent"], setup["ai"]
    ev = msg_event(BOOKS_IG, "d1", "x", "m1")
    ev["entry"][0]["messaging"][0]["message"]["is_deleted"] = True
    run(a.handle_webhook(ev))
    assert setup["db"].get_conv(conv_key(setup["books"], "d1")) is None


def test_lead_update_only_when_new_info(setup):
    a, ai, tg = setup["agent"], setup["ai"], setup["tg"]

    async def scenario():
        ai.dm_queue += [dm("Rahmat!", interest="Saodat asri"), dm("Qaysi viloyat?", interest="Saodat asri to'plami"),
                        dm("Yaxshi", region="Buxoro"), dm("Rahmat", region="Buxoro")]
        for i, t in enumerate(["90 123 45 67", "ha", "Buxoro", "rahmat"]):
            await a.handle_webhook(msg_event(BOOKS_IG, "L1", t, f"m{i}"))
            await a.flush()

    run(scenario())
    texts = tg.texts()
    assert sum("LEAD" in t for t in texts) == 1
    assert sum("LID YANGILANDI" in t for t in texts) == 1


def test_token_refresh(setup):
    a, db, ig = setup["agent"], setup["db"], setup["ig_books"]

    async def refresh():
        return {"access_token": "NEW-TOKEN", "expires_in": 5184000}

    ig.refresh_token = refresh
    setup["ig_kids"].refresh_token = refresh
    assert run(a.refresh_tokens()) == 2
    assert db.get_account(setup["books"])["ig_token"] == "NEW-TOKEN"
    assert run(a.refresh_tokens()) == 0  # 7 kun o'tmaguncha qayta yangilanmaydi


def test_delete_product_keeps_memory():
    db = DB(":memory:")
    pid = db.upsert_product(name="A", category="K", price=1)
    db.add_memory("savol", "javob", None, pid, "panel")
    db.delete_product(pid)
    assert db.list_memory()[0]["product_id"] is None


def test_price_parsing():
    assert catalog.parse_price("149000.00") == 149000
    assert catalog.parse_price("149,000 so'm") == 149000
    assert catalog.parse_price("1.250.000") == 1250000


def test_instagram_connect_flow(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    from app.instagram import Instagram

    main.env.admin_password = "parol"
    main.env.ig_app_id = "123456"
    main.env.ig_app_secret = "igsecret"
    main.env.public_url = "https://agent.example.com"
    client = TestClient(main.app)
    auth = ("admin", "parol")

    r = client.get("/instagram/ulash?profil=0", auth=auth, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("https://www.instagram.com/oauth/authorize?") and "client_id=123456" in loc
    assert "instagram_business_manage_messages" in loc and "redirect_uri=https%3A%2F%2Fagent.example.com%2Finstagram%2Fcallback" in loc
    state = loc.split("state=")[1].split("&")[0]

    async def fake_exchange(app_id, secret, redirect, code):
        assert code == "CODE" and secret == "igsecret"
        return {"access_token": "LONG", "user_id": "1789999", "expires_in": 5184000}

    async def fake_me(self):
        return {"user_id": "1789999", "username": "bolalar_nabor"}

    async def ok(self, *a, **k):
        return {"success": True} if not a else []

    monkeypatch.setattr(main.iglib, "exchange_code", fake_exchange)
    monkeypatch.setattr(Instagram, "me", fake_me)
    monkeypatch.setattr(Instagram, "subscribe_webhooks", ok)
    monkeypatch.setattr(Instagram, "recent_media", ok)
    r = client.get(f"/instagram/callback?code=CODE%23_&state={state}", auth=auth, follow_redirects=False)
    assert r.status_code == 303 and "ok=" in r.headers["location"], r.headers["location"]
    acc = main.db.account_by_ig("1789999")
    assert acc and acc["ig_username"] == "bolalar_nabor" and acc["ig_token"] == "LONG"
    assert acc["id"] in main.agent.profiles
    # state bir martalik
    r = client.get(f"/instagram/callback?code=CODE&state={state}", auth=auth, follow_redirects=False)
    assert "xato=" in r.headers["location"]


def test_csrf_blocked():
    from fastapi.testclient import TestClient
    import app.main as main

    main.env.admin_password = "parol"
    client = TestClient(main.app)
    r = client.post("/xotira/0", data={"question": "a", "answer": "b"}, auth=("a", "parol"),
                    headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403


# ===================== Mustaqil tekshiruv topilmalari =====================
import httpx


def test_echo_arriving_before_send_finishes_is_not_operator(setup):
    """1: echo Instagram javobi qaytishidan oldin kelsa ham AI to'xtamasligi kerak."""
    a, ai, ig, db = setup["agent"], setup["ai"], setup["ig_books"], setup["db"]
    gate = asyncio.Event()
    orig = ig.send_dm

    async def slow_send(rid, text, on_sent=None):
        # Meta echo'ni POST javobidan oldin yuboradi
        a.spawn(a.handle_webhook(msg_event(BOOKS_IG, BOOKS_IG, text, f"Kitoblar-mid-{len(ig.dms) + 1}", echo=True, recipient=rid)))
        await asyncio.sleep(0.1)
        return await orig(rid, text, on_sent)

    ig.send_dm = slow_send

    async def scenario():
        ai.dm_queue.append(dm("Salom!"))
        await a.handle_webhook(msg_event(BOOKS_IG, "e1", "salom", "m1"))
        await a.flush()

    run(scenario())
    key = conv_key(setup["books"], "e1")
    assert [m["role"] for m in db.history(key)] == ["user", "assistant"]
    assert a.ai_active(db.get_conv(key))


def test_echo_with_different_mid_is_not_operator(setup):
    """Facebook login: echo mid'i yuborishda qaytgan mid'dan farq qiladi — matn bo'yicha tanib olinadi."""
    a, ai, db = setup["agent"], setup["ai"], setup["db"]

    async def scenario():
        ai.dm_queue.append(dm("Korset narxi 59 000 so'm."))
        await a.handle_webhook(msg_event(BOOKS_IG, "u9", "korset narxi?", "aWdf-user-1"))
        await a.flush()
        # Agentning o'z javobi, lekin boshqa formatdagi mid bilan
        await a.handle_webhook(msg_event(BOOKS_IG, BOOKS_IG, "Korset narxi 59 000 so'm.", "aWdf-echo-1", echo=True, recipient="u9"))
        key = conv_key(setup["books"], "u9")
        assert a.ai_active(db.get_conv(key))
        assert [m["role"] for m in db.history(key)] == ["user", "assistant"]
        # Xuddi shu matnning ikkinchi echo'si endi operator deb hisoblanadi (moslik bir marta ishlatiladi)
        await a.handle_webhook(msg_event(BOOKS_IG, BOOKS_IG, "Boshqa matn", "aWdf-echo-2", echo=True, recipient="u9"))
        assert not a.ai_active(db.get_conv(key))

    run(scenario())


def test_empty_template_card_is_ignored(setup):
    """Instagram raqamdan keyin qo'shadigan bo'sh 'template' karta mijoz xabari sifatida saqlanmaydi."""
    a, ai, db = setup["agent"], setup["ai"], setup["db"]

    async def scenario():
        await a.handle_webhook(msg_event(BOOKS_IG, "t1", None, "card-1", attachments=[{"type": "template", "payload": {}}]))
        await a.flush()

    run(scenario())
    assert db.get_conv(conv_key(setup["books"], "t1")) is None
    assert ai.calls == []


def test_new_message_during_reply_is_not_cancelled(setup):
    """2: javob yozilayotganda kelgan xabar uni uzmaydi; tasdiq bir marta, keyin yangi xabarga javob."""
    a, ai, ig, tg = setup["agent"], setup["ai"], setup["ig_books"], setup["tg"]
    a.debounce = 0.05
    orig_send = tg.send

    async def slow_tg(text, chat_id=None, reply_to=None, buttons=None):
        await asyncio.sleep(0.2)
        return await orig_send(text, chat_id, reply_to, buttons)

    tg.send = slow_tg

    async def scenario():
        ai.dm_queue += [dm("Rahmat!"), dm("Kim uchun olasiz?")]
        await a.handle_webhook(msg_event(BOOKS_IG, "d2", "90 123 45 67", "m1"))
        await asyncio.sleep(0.1)  # javob Telegram'ga lid yuborayotgan payt
        await a.handle_webhook(msg_event(BOOKS_IG, "d2", "o'zim uchun", "m2"))
        await a.flush()

    run(scenario())
    texts = [t for _, t in ig.dms]
    assert sum("Raqamingiz qabul qilindi" in t for t in texts) == 1
    assert len(texts) == 2 and sum("LEAD" in t for t in tg.texts()) == 1


def test_network_error_keeps_lead(setup):
    """3: tarmoq xatosida ham lid Telegram'ga boradi."""
    from app.instagram import Instagram
    a, ai, tg = setup["agent"], setup["ai"], setup["tg"]
    real = Instagram("t", BOOKS_IG)

    async def boom(*a_, **k):
        raise httpx.ReadTimeout("timeout")

    real.http.post = boom
    real.http.get = boom
    a.profiles[setup["books"]].ig = real

    async def scenario():
        ai.dm_queue.append(dm("Rahmat!"))
        await a.handle_webhook(msg_event(BOOKS_IG, "n1", "90 123 45 67", "m1"))
        await a.flush()

    run(scenario())
    assert any("LEAD" in t for t in tg.texts())
    assert any("Instagram xatosi" in t for t in tg.texts())


def test_returning_customer_new_lead_and_corrected_phone(setup):
    """4: qaytgan mijozning yangi buyurtmasi — yangi lid; raqam tuzatilsa — lid yangilanadi."""
    a, ai, tg, db = setup["agent"], setup["ai"], setup["tg"], setup["db"]
    key = conv_key(setup["books"], "rc")

    async def scenario():
        ai.dm_queue.append(dm("Rahmat!"))
        await a.handle_webhook(msg_event(BOOKS_IG, "rc", "90 123 45 67", "m1"))
        await a.flush()
        ai.dm_queue.append(dm("Yangiladik"))
        await a.handle_webhook(msg_event(BOOKS_IG, "rc", "kechirasiz, raqamim 91 222 33 44", "m2"))
        await a.flush()
        assert any("LID YANGILANDI" in t and "+998912223344" in t for t in tg.texts())
        # 3 kun o'tdi, yana yozdi
        db._exec("UPDATE messages SET created_at=? WHERE conv_id=?", (iso(now() - timedelta(days=3)), key))
        ai.dm_queue.append(dm("Xush kelibsiz! Shu raqamga bog'lanaylikmi?"))
        await a.handle_webhook(msg_event(BOOKS_IG, "rc", "yana kitob kerak", "m3"))
        await a.flush()
        assert "+998912223344" in ai.calls[-1]["messages"][-1]["content"]  # avvalgi raqam eslatildi
        ai.dm_queue.append(dm("Rahmat!", phone="+998912223344"))
        await a.handle_webhook(msg_event(BOOKS_IG, "rc", "ha, shu raqamga", "m4"))
        await a.flush()

    run(scenario())
    assert sum("LEAD" in t for t in tg.texts()) == 2
    assert len(db.list_leads()) == 2


def test_phone_in_neutral_comment_creates_lead(setup):
    """5: kommentda faqat raqam bo'lsa ham lid."""
    a, ai, tg = setup["agent"], setup["ai"], setup["tg"]
    ai.comment_queue.append(cm("NEYTRAL"))
    run(a.handle_webhook(comment_event(BOOKS_IG, "kp", "up", "x", "+998 90 123 45 67")))
    assert any("LEAD" in t for t in tg.texts())


def test_known_answer_delivered_and_ignored_question_reopened(setup):
    """6: javobi bor savol — mijozga darhol; e'tiborsiz savol yana so'ralsa — qayta ochiladi."""
    a, ai, tg, db, ig = setup["agent"], setup["ai"], setup["tg"], setup["db"], setup["ig_books"]
    gap, _ = db.add_gap(setup["books"], "Ish vaqti?")
    db.set_gap(gap["id"], "9:00–21:00", "javob_berildi")
    ign, _ = db.add_gap(setup["books"], "Ulgurji narx?")
    db.set_gap(ign["id"], None, "e'tiborsiz")

    async def scenario():
        ai.dm_queue += [dm("Aniqlab beraman", unknown_question="Ish vaqti?"), dm("Har kuni 9:00 dan 21:00 gacha ishlaymiz.")]
        await a.handle_webhook(msg_event(BOOKS_IG, "g1", "soat nechagacha ishlaysiz?", "m1"))
        await a.flush()
        ai.dm_queue.append(dm("Aniqlab beraman", unknown_question="Ulgurji narx?"))
        await a.handle_webhook(msg_event(BOOKS_IG, "g2", "optom narx?", "m2"))
        await a.flush()

    run(scenario())
    assert any("21:00" in t for r, t in ig.dms if r == "g1")
    assert db.get_gap(ign["id"])["status"] == "kutilmoqda"
    assert any("Ulgurji narx?" in t for t in tg.texts())


def test_no_followup_after_operator_or_handoff(setup):
    """7: operator yoki handoff dialoglariga eslatma yo'q."""
    a, ai, db = setup["agent"], setup["ai"], setup["db"]

    async def scenario():
        ai.dm_queue += [dm("Javob"), dm("Javob")]
        for cid in ("o1", "o2"):
            await a.handle_webhook(msg_event(BOOKS_IG, cid, "narxi?", f"m-{cid}"))
            await a.flush()
        k1, k2 = conv_key(setup["books"], "o1"), conv_key(setup["books"], "o2")
        db.add_message(k1, "operator", "Men operatorman")
        db.update_conv(k2, handoff_sent=1)
        for k in (k1, k2):
            db.update_conv(k, paused_until=None, last_bot_at=iso(now() - timedelta(hours=4)), last_user_at=iso(now() - timedelta(hours=5)))
        return await a.run_followups()

    assert run(scenario()) == 0


def test_owner_answer_verbatim_when_ai_off(setup):
    """8: AI panelda o'chirilgan bo'lsa — egasining javobi so'zma-so'z, AI yozmaydi."""
    a, ai, db, ig = setup["agent"], setup["ai"], setup["db"], setup["ig_books"]

    async def scenario():
        ai.dm_queue.append(dm("Aniqlayman", unknown_question="Qog'ozi qanaqa?"))
        await a.handle_webhook(msg_event(BOOKS_IG, "v1", "qog'ozi qanaqa?", "m1"))
        await a.flush()
        db.update_conv(conv_key(setup["books"], "v1"), ai_enabled=0)
        calls_before = len(ai.calls)
        gap = db.list_gaps()[0]
        sent = await a.answer_gap(gap["id"], "Oq ofset qog'oz")
        assert sent == 1 and len(ai.calls) == calls_before

    run(scenario())
    assert ig.dms[-1] == ("v1", "Oq ofset qog'oz")


def test_failed_lead_is_retried(setup):
    """9: Telegram ishlamasa lid yo'qolmaydi — keyin qayta yuboriladi."""
    a, ai, tg, db = setup["agent"], setup["ai"], setup["tg"], setup["db"]
    orig = tg.send

    async def down(text, chat_id=None, reply_to=None, buttons=None):
        return None

    tg.send = down

    async def scenario():
        ai.dm_queue.append(dm("Rahmat!"))
        await a.handle_webhook(msg_event(BOOKS_IG, "t1", "90 123 45 67", "m1"))
        await a.flush()
        assert len(db.unsent_leads()) == 1
        tg.send = orig
        assert await a.retry_unsent_leads() == 1
        assert db.unsent_leads() == []

    run(scenario())


def test_telegram_group_migration():
    from app.telegram import Telegram
    import httpx as hx
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["chat_id"])
        if body["chat_id"] == "-100old":
            return hx.Response(400, json={"ok": False, "error_code": 400, "parameters": {"migrate_to_chat_id": -100777}})
        return hx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    t = Telegram("TOKEN", "-100old")
    t.http = hx.AsyncClient(transport=hx.MockTransport(handler))
    seen = []
    t.on_migrate = lambda old, new: seen.append((old, new))
    assert run(t.send("salom")) == 5
    assert run(t.send("yana")) == 5
    assert calls == ["-100old", "-100777", "-100777"] and seen == [("-100old", "-100777")]
    long = run(t.send("<b>" + "x" * 5000 + "</b>"))
    assert long == 5


def test_unsigned_webhook_rejected_without_secret(monkeypatch):
    from app.instagram import verify_signature
    monkeypatch.delenv("ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    assert verify_signature("", b"{}", None) is False
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "1")
    assert verify_signature("", b"{}", None) is True


# ===================== Saytdan import =====================
from app import scraper

PRODUCT_HTML = """<html><head><meta property="og:type" content="product"><meta property="og:title" content="«Saodat asri» to'plami">
<meta property="og:site_name" content="ShopX"></head><body>
<div class="product__media"><a href="/uploads/p1.jpg">rasm</a></div>
<div class="product__details--info"><h2 class="product__details--info__title">"Saodat asri" to'plami</h2>
<div class="product__details--info__price"><span class="current__price">269,000 so'm</span><span class="old__price">359,000 so'm</span></div>
<p class="product__details--info__desc">4 jild, qattiq muqova, 320 bet.</p></div>
<h2>O'XSHASH MAXSULOTLAR</h2>
<div class="product__items"><span class="product__items--content__subtitle">Kitoblar</span><a href="/product/2">B</a>
<span class="current__price">99 000 so'm</span></div>
<div class="product__items"><span class="product__items--content__subtitle">Kitoblar</span><a href="/product/3">C</a></div>
</body></html>"""

JSONLD_HTML = """<html><head><script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebSite"},
{"@type":"Product","name":"Lego Classic","description":"500 detal","image":["https://cdn.x/lego.jpg"],
"offers":{"@type":"Offer","price":"350000","priceCurrency":"UZS"}}]}</script></head><body><h1>Lego Classic</h1></body></html>"""

HOME_HTML = """<html><head><meta property="og:type" content="website"></head><body>
<div class="product__items"><a href="/product/1">A</a><span class="current__price">149 000 so'm</span></div>
<a href="/product/2">B</a><a href="/about">Biz haqimizda</a></body></html>"""


def test_extract_shop_layout():
    p = scraper.extract_product(PRODUCT_HTML, "https://shop.uz/product/1")
    assert p.name == '"Saodat asri" to\'plami'
    assert scraper.extract_product(PRODUCT_HTML.replace('"Saodat asri" to\'plami', '"Payg\'ambarlar qissalari"'),
                                   "https://shop.uz/product/1").name == "Payg'ambarlar qissalari"
    assert p.price == 269000 and p.old_price == 359000
    assert p.category == "Kitoblar" and p.image_url == "https://shop.uz/uploads/p1.jpg"
    assert "320 bet" in p.description


def test_extract_jsonld():
    p = scraper.extract_product(JSONLD_HTML, "https://lego.uz/p/1")
    assert p.name == "Lego Classic" and p.price == 350000 and p.image_url == "https://cdn.x/lego.jpg" and p.method == "jsonld"


def test_home_page_is_not_product():
    assert scraper.extract_product(HOME_HTML, "https://shop.uz/") is None


class FakeFetcher(scraper.Fetcher):
    def __init__(self, pages):
        self.pages, self.cache, self.requested = pages, {}, []

    async def _get(self, url):
        self.requested.append(url)
        return (200, self.pages[url]) if url in self.pages else (404, "")

    async def aclose(self):
        pass


def test_import_site_discovers_numeric_ids_and_resyncs():
    db = DB(":memory:")
    p2 = PRODUCT_HTML.replace("Saodat asri", "Ikkinchi kitob").replace("269,000", "120,000")
    p5 = PRODUCT_HTML.replace("Saodat asri", "Beshinchi").replace("269,000", "50,000")
    pages = {"https://shop.uz": HOME_HTML, "https://shop.uz/product/1": PRODUCT_HTML,
             "https://shop.uz/product/2": p2, "https://shop.uz/product/5": p5}
    rep = run(scraper.import_site(db, "https://shop.uz", fetcher=FakeFetcher(pages)))
    assert rep.imported == 3, rep  # /product/5 bosh sahifada yo'q, lekin raqam bo'yicha topildi
    names = {p["name"] for p in db.list_products()}
    assert any("Beshinchi" in n for n in names)
    # Egasi kategoriya va shablonni o'zgartirdi, keyin saytda narx o'zgardi va 5-mahsulot o'chirildi
    pid = next(p["id"] for p in db.list_products() if "Ikkinchi" in p["name"])
    db.update_product(pid, category="Bolalar", template="Mening shablonim")
    pages["https://shop.uz/product/2"] = p2.replace("120,000", "110,000")
    del pages["https://shop.uz/product/5"]
    rep2 = run(scraper.import_site(db, "https://shop.uz", fetcher=FakeFetcher(pages)))
    assert rep2.imported == 0 and rep2.updated == 2 and rep2.out_of_stock == 1
    p = db.get_product(pid)
    assert p["price"] == 110000 and p["category"] == "Bolalar" and p["template"] == "Mening shablonim"
    assert not next(x for x in db.list_products() if "Beshinchi" in x["name"])["in_stock"]


def test_import_site_ai_fallback_for_unknown_layout():
    db = DB(":memory:")
    from app.ai import ProductExtract

    class AIStub:
        async def extract_product(self, text, url):
            assert "Noyob mahsulot" in text
            return ProductExtract(is_product=True, name="Noyob mahsulot", price=77000, old_price=None,
                                  category="Umumiy", description="Tavsif")

    pages = {"https://odd.uz/item/9": "<html><body><div>Noyob mahsulot — atigi 77 ming</div></body></html>"}
    rep = run(scraper.import_site(db, "https://odd.uz/item/9", ai=AIStub(), fetcher=FakeFetcher(pages)))
    assert rep.imported == 1 and db.list_products()[0]["price"] == 77000


def test_panel_site_import(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    async def fake_import(db, url, ai=None, category="", max_products=300, fetcher=None, category_map=None):
        db.upsert_scraped(source_url=url + "/product/1", name="Sinov", category=category or "Kitoblar", price=1000,
                          old_price=None, description="", image_url="", source="sayt:shop.uz")
        return scraper.ImportReport(site="shop.uz", imported=1)

    monkeypatch.setattr(main.scraper, "import_site", fake_import)
    main.env.admin_password = "parol"
    with TestClient(main.app) as client:
        r = client.post("/mahsulotlar/sayt", data={"url": "shop.uz", "kategoriya": "", "soni": "10"},
                        auth=("a", "parol"), follow_redirects=False)
        assert r.status_code == 303 and "ok=" in r.headers["location"]
        page = ""
        for _ in range(20):
            page = client.get("/mahsulotlar", auth=("a", "parol")).text
            if "1 ta yangi" in page:
                break
        assert "https://shop.uz" in page and "1 ta yangi" in page


def test_simulate_dm_shows_lead_and_question_without_sending(setup):
    a, ai, tg, ig = setup["agent"], setup["ai"], setup["tg"], setup["ig_books"]
    prof = a.profiles[setup["books"]]
    state: dict = {}
    ai.dm_queue.append(dm("Rahmat! Kim uchun olasiz?", interest="Saodat asri", unknown_question="Muqovasi qattiqmi?"))
    out = run(a.simulate_dm(prof, [{"role": "user", "text": "Saodat asri olaman, 90 123 45 67"}], state))
    assert "Raqamingiz qabul qilindi" in out["reply"]
    titles = [e["title"] for e in out["events"]]
    assert any("lid" in t for t in titles) and any("savol" in t for t in titles)
    assert out["info"]["telefon"] == "+998901234567" and state["lead_sent"] == 1
    assert ig.dms == [] and tg.sent == [] and setup["db"].list_leads() == []  # hech narsa yuborilmadi


def test_simulate_comment(setup):
    a, ai = setup["agent"], setup["ai"]
    ai.comment_queue.append(cm("HAQORAT"))
    out = run(a.simulate_comment(a.profiles[setup["books"]], "yomon so'z"))
    assert out["category"] == "HAQORAT" and "yashiriladi" in out["action"]


def test_comment_fallback_when_dm_closed_and_no_repeat(setup):
    a, ai, ig = setup["agent"], setup["ai"], setup["ig_books"]

    async def closed(cid, text, **kw):
        raise IGError(400, '{"error":{"message":"user cannot receive messages"}}', "messages")

    ig.private_reply = closed
    r = cm("SAVOL", "✅ Direct'ga yozdik", "Salom!")
    r.public_fallback = "Saodat asri — 269 000 so'm, oldindan to'lov yo'q."
    ai.comment_queue.append(r)
    run(a.handle_webhook(comment_event(BOOKS_IG, "f1", "u20", "x", "narxi?")))
    assert ig.replies == [("f1", "Saodat asri — 269 000 so'm, oldindan to'lov yo'q.")]
    # shu mijoz shu post ostida yana yozdi — takroriy javob yo'q
    ig.private_reply = FakeIG("x").private_reply
    ai.comment_queue.append(cm("XARID", "✅ Direct'ga yozdik", "Salom!"))
    run(a.handle_webhook(comment_event(BOOKS_IG, "f2", "u20", "x", "zakaz qildim")))
    assert len(ig.replies) == 1


def test_import_history_saves_messages_and_suggestions(setup):
    a, ai, db, ig, tg = setup["agent"], setup["ai"], setup["db"], setup["ig_books"], setup["tg"]
    from app.ai import HistoryAnalysis, HistoryFAQ

    async def conversations(limit=50, pages=4):
        return [{"id": "c1", "participants": {"data": [{"id": BOOKS_IG, "username": "ilmbooks.uz"}, {"id": "cust1", "username": "malika"}]}}]

    async def conversation_messages(cid, limit=50):
        return [
            {"id": "h2", "created_time": "2026-10-01T10:05:00+0000", "from": {"id": BOOKS_IG}, "message": "Narxi 269 000 so'm, raqamingiz?"},
            {"id": "h1", "created_time": "2026-10-01T10:00:00+0000", "from": {"id": "cust1"}, "message": "Saodat asri qancha?"},
            {"id": "h3", "created_time": "2026-10-01T10:06:00+0000", "from": {"id": "cust1"}, "message": "90 123 45 67"},
        ]

    async def analyze(knowledge, transcripts):
        assert "M: Saodat asri qancha?" in transcripts and "<TEL>" in transcripts and "90 123" not in transcripts
        return HistoryAnalysis(faqs=[HistoryFAQ(question="Saodat asri muqovasi qanaqa?", answer="Qattiq muqova", product="Saodat asri", times=3)],
                               open_questions=["Ish vaqtingiz?"], tips=["Narxdan keyin raqam so'rash yaxshi ishlagan"])

    ig.conversations, ig.conversation_messages, ai.analyze_history = conversations, conversation_messages, analyze
    rep = run(a.import_history(a.profiles[setup["books"]]))
    assert rep["dialoglar"] == 1 and rep["xabarlar"] == 3 and rep["takliflar"] == 1 and rep["savollar"] == 1
    key = conv_key(setup["books"], "cust1")
    assert [m["role"] for m in db.history(key)] == ["user", "operator", "user"]
    assert db.get_conv(key)["last_bot_at"] is None  # eslatma ishga tushmaydi
    sug = [g for g in db.list_gaps() if g["status"] == "taklif"][0]
    assert sug["answer"] == "Qattiq muqova" and sug["product_id"] == setup["saodat"]
    run(a.answer_gap(sug["id"], sug["answer"]))
    assert "Qattiq muqova" in a.knowledge(a.profiles[setup["books"]])
    assert ig.dms == []  # hech kimga yozilmadi
    assert any("eski yozishmalar" in t for t in tg.texts())


# ===================== Telegram tugmalari =====================
def _cb(data, msg_id=1001, chat="-100main", user=5):
    return {"callback_query": {"id": "cb1", "data": data, "from": {"id": user, "first_name": "Ali"},
                               "message": {"message_id": msg_id, "chat": {"id": chat}, "text": "eski matn"}}}


def test_lead_buttons_sold_and_stats(setup):
    a, ai, tg, db = setup["agent"], setup["ai"], setup["tg"], setup["db"]

    async def scenario():
        ai.dm_queue.append(dm("Rahmat!", interest="Saodat asri", temperature="issiq", lead_reason="aniq xarid-niyati · raqam berdi"))
        await a.handle_webhook(msg_event(BOOKS_IG, "s1", "olaman, 90 123 45 67", "m1"))
        await a.flush()
        lead = db.list_leads()[0]
        assert tg.buttons[0] == [[("💰 Sotildi", f"lead:sold:{lead['id']}"), ("🚫 Bo'lmadi", f"lead:lost:{lead['id']}")]]
        assert "🔥 TEZKOR" in tg.sent[0][1] and "aniq xarid-niyati" in tg.sent[0][1]
        assert lead["tg_message_id"] == 1001
        await a.handle_telegram_update(_cb(f"lead:sold:{lead['id']}"), admin_ids=set())

    run(scenario())
    assert db.list_leads()[0]["status"] == "sotildi" and db.list_leads()[0]["closed_by"] == "Ali"
    assert "💰 Sotildi — Ali" in tg.edits[-1][2] and tg.toasts[-1] == "Belgilandi ✓"
    st = db.lead_stats()
    assert st["sotildi"] == 1 and st["konversiya"] == 100


def test_gap_buttons_ignore_and_later(setup):
    a, db, tg = setup["agent"], setup["db"], setup["tg"]
    prof = a.profiles[setup["books"]]

    async def scenario():
        await a.ask_owner(prof, "Ish vaqti?", None, None)
        await a.ask_owner(prof, "Ulgurji narx?", None, None)
        g1, g2 = sorted(db.list_gaps(), key=lambda g: g["id"])
        assert tg.buttons[0] == [[("❌ Kerak emas", f"gap:no:{g1['id']}"), ("⏰ Keyinroq", f"gap:later:{g1['id']}")]]
        await a.handle_telegram_update(_cb(f"gap:no:{g1['id']}"), set())
        await a.handle_telegram_update(_cb(f"gap:later:{g2['id']}"), set())
        assert db.get_gap(g1["id"])["status"] == "e'tiborsiz"
        assert db.get_gap(g2["id"])["remind_at"]
        db._exec("UPDATE gaps SET remind_at=? WHERE id=?", (iso(now() - timedelta(minutes=1)), g2["id"]))
        assert await a.send_due_reminders() == 1
        assert db.get_gap(g2["id"])["remind_at"] is None

    run(scenario())
    assert sum("Ulgurji narx?" in t for t in tg.texts()) == 2


def test_handoff_ai_resume_button(setup):
    a, ai, db, tg = setup["agent"], setup["ai"], setup["db"], setup["tg"]

    async def scenario():
        ai.dm_queue.append(dm("Operator bog'lanadi", needs_human=True, handoff_reason="shikoyat"))
        await a.handle_webhook(msg_event(BOOKS_IG, "h1", "kitob yirtiq keldi", "m1"))
        await a.flush()
        key = conv_key(setup["books"], "h1")
        assert not a.ai_active(db.get_conv(key))
        assert tg.buttons[-1][0][1] == ("🤖 AI davom etsin", f"ho:ai:{key}")
        await a.handle_telegram_update(_cb(f"ho:ai:{key}"), set())
        assert a.ai_active(db.get_conv(key))

    run(scenario())


def test_moderation_unhide_button(setup):
    a, ai, ig, tg = setup["agent"], setup["ai"], setup["ig_books"], setup["tg"]
    ig.unhidden = []

    async def unhide(cid):
        ig.unhidden.append(cid)

    ig.unhide_comment = unhide
    ai.comment_queue.append(cm("SALBIY"))
    run(a.handle_webhook(comment_event(BOOKS_IG, "z1", "u1", "x", "aldadilaring")))
    run(a.handle_telegram_update(_cb(f"mod:show:{setup['books']}:z1"), set()))
    assert ig.unhidden == ["z1"] and "qayta ochildi" in tg.edits[-1][2]


def test_group_bind_via_start_link(setup):
    a, db, tg = setup["agent"], setup["db"], setup["tg"]
    db.kv_set("tg_bind:bind_abc", str(setup["books"]))
    db.kv_set("tg_bind:bind_main", "main")
    seen = []
    a.on_main_chat = seen.append
    upd = lambda code, chat: {"message": {"message_id": 1, "text": f"/start@SotuvBot {code}", "chat": {"id": chat, "title": "Lidlar", "type": "supergroup"}, "from": {"id": 5}}}
    run(a.handle_telegram_update(upd("bind_abc", -100777), set()))
    run(a.handle_telegram_update(upd("bind_main", -100888), set()))
    assert db.get_account(setup["books"])["tg_chat_id"] == "-100777"
    assert db.kv_get("cfg:telegram_chat_id") == "-100888" and seen == ["-100888"]
    assert sum("Telegram-guruh ulandi" in t for t in tg.texts()) == 2
    run(a.handle_telegram_update(upd("bind_eski", -100999), set()))
    assert "eskirgan" in tg.texts()[-1]


def test_commands_holat_and_savollar(setup):
    a, db, tg = setup["agent"], setup["db"], setup["tg"]
    db.add_gap(setup["books"], "Ish vaqti?")
    cmd = lambda t: {"message": {"message_id": 2, "text": t, "chat": {"id": "-100main"}, "from": {"id": 5}}}
    run(a.handle_telegram_update(cmd("/holat"), set()))
    assert "Oxirgi 24 soat" in tg.texts()[-1]
    run(a.handle_telegram_update(cmd("/savollar@SotuvBot"), set()))
    assert "Ish vaqti?" in tg.texts()[-1]


def test_reply_answer_edits_question_message(setup):
    a, db, tg = setup["agent"], setup["db"], setup["tg"]
    run(a.ask_owner(a.profiles[setup["books"]], "Ish vaqti?", None, None))
    gap = db.list_gaps()[0]
    run(a.handle_telegram_update({"message": {"message_id": 9, "text": "9:00–21:00", "chat": {"id": gap["tg_chat_id"]}, "from": {"id": 5},
                                              "reply_to_message": {"message_id": gap["tg_message_id"]}}}, set()))
    assert "Javob berildi" in tg.texts()[-1] and "9:00–21:00" in tg.texts()[-1]
    assert "✅ Javob berildi" in tg.edits[-1][2]


def test_demo_button(setup):
    a, tg = setup["agent"], setup["tg"]
    run(a.handle_telegram_update(_cb("demo:1"), set()))
    assert "namuna" in tg.toasts[-1].lower() and "Tugma ishlayapti" in tg.edits[-1][2]


def test_back_keeps_message_before_fragment():
    import app.main as main
    r = main.back("/ulanishlar#telegram", ok="Saqlandi")
    assert r.headers["location"] == "/ulanishlar?ok=Saqlandi#telegram"


def test_instagram_unified_login_mode():
    import httpx as hx
    from app.instagram import Instagram
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "assigned_instagram_accounts" in str(request.url):
            return hx.Response(200, json={"data": [{"id": "17841400000000001", "username": "ilmbooks.uz"}]})
        if request.url.path.endswith("/messages"):
            return hx.Response(200, json={"message_id": "m1"})
        return hx.Response(200, json={})

    ig = Instagram("EAAtoken", "me")
    ig.http = hx.AsyncClient(transport=hx.MockTransport(handler))
    assert ig.unified and "graph.facebook.com/v26.0" in ig.base
    me = run(ig.me())
    assert me["user_id"] == "17841400000000001" and me["username"] == "ilmbooks.uz"
    assert run(ig.refresh_token()) == {}  # muddatsiz token
    ig.user_id = me["user_id"]
    assert run(ig.send_dm("cust", "salom")) == ["m1"]
    assert calls[-1].startswith("https://graph.facebook.com/v26.0/17841400000000001/messages")
    old = Instagram("IGAAtoken", "1")
    assert not old.unified and "graph.instagram.com" in old.base


def test_unified_login_connect_flow(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    from app.instagram import Instagram

    main.env.admin_password = "parol"
    main.env.ig_app_id = "1510073794267170"
    main.env.ig_app_secret = "metasecret"
    main.env.ig_config_id = "CFG123"
    main.env.public_url = "https://agent.example.com"
    client = TestClient(main.app)
    auth = ("admin", "parol")
    r = client.get("/instagram/ulash?profil=0", auth=auth, follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith("https://www.facebook.com/v26.0/dialog/oauth?") and "config_id=CFG123" in loc
    state = loc.split("state=")[1].split("&")[0]

    async def fake_exchange(app_id, secret, redirect, code, api_version="v26.0"):
        assert secret == "metasecret" and code == "C0DE"
        return {"access_token": "EAAsystemuser"}

    async def fake_accounts(self):
        return [{"id": "17841451829407842", "username": "quvvat_rizo"}, {"id": "17841400000000777", "username": "ilmbooks.uz"}]

    async def ok(self, *a, **k):
        return {"success": True} if not a else []

    monkeypatch.setattr(main.iglib, "exchange_code_unified", fake_exchange)
    monkeypatch.setattr(Instagram, "assigned_accounts", fake_accounts)
    monkeypatch.setattr(Instagram, "subscribe_webhooks", ok)
    monkeypatch.setattr(Instagram, "recent_media", ok)
    r = client.get(f"/instagram/callback?code=C0DE&state={state}", auth=auth, follow_redirects=False)
    assert "ok=" in r.headers["location"], r.headers["location"]
    q = main.db.account_by_ig("17841451829407842")
    b = main.db.account_by_ig("17841400000000777")
    assert q and q["ig_token"] == "EAAsystemuser" and q["ig_username"] == "quvvat_rizo"
    assert b and b["name"] == "Kitoblar"  # mavjud @ilmbooks.uz profiliga ulandi, yangisi yaratilmadi
    main.env.ig_config_id = ""


def test_privacy_pages_are_public():
    from fastapi.testclient import TestClient
    import app.main as main

    main.env.contact_email = "shop@example.com"
    client = TestClient(main.app)
    r = client.get("/privacy", follow_redirects=False)
    assert r.status_code == 200 and "Privacy Policy" in r.text and "shop@example.com" in r.text
    r = client.get("/data-deletion", follow_redirects=False)
    assert r.status_code == 200 and "Data Deletion" in r.text
    main.env.contact_email = ""
    assert "Instagram" in client.get("/data-deletion").text


def test_ads_table_upsert_and_deactivate():
    import os
    from app.db import DB

    db = DB(os.environ["DB_PATH"])
    pid = db.upsert_product(name="Reklama kitobi", category="Kitoblar", price=99000, post_ids=[])
    db.upsert_ad(ad_id="A1", page_id="P1", page_name="Sahifa", started_at="2026-11-01", variants=3,
                 landing_url="https://x.uz/a", product_id=pid, ad_text="matn", ad_price=99000, active=1)
    db.upsert_ad(ad_id="A1", page_id="P1", variants=5, active=1)
    db.upsert_ad(ad_id="A2", page_id="P1", product_id=pid, active=1)
    ads = {a["ad_id"]: a for a in db.list_ads("P1")}
    assert ads["A1"]["variants"] == 5 and ads["A1"]["ad_text"] == "matn" and ads["A1"]["site_price"] == 99000
    assert db.deactivate_missing_ads("P1", {"A1"}) == 1
    assert {a["ad_id"]: a["active"] for a in db.list_ads("P1")} == {"A1": 1, "A2": 0}


def test_control_agent_report_and_approval_card():
    import os
    import anthropic
    import httpx2
    from app.control import ControlAgent

    db = DB(os.environ["DB_PATH"])
    db.clear_control_messages()
    acc = db.add_account(name="Nazorat", ig_user_id="ctl1", ig_username="ctl", ig_token="t", tg_chat_id="",
                         categories="*", business_text="", settings_yaml=default_settings_yaml(), active=1)
    conv = db.ensure_conv(acc, "u777", "mijoz777", "Aziz")
    db.add_message(conv["id"], "user", "Narxi qancha?")
    pid = db.upsert_product(name="Nazorat kitobi", category="Kitoblar", price=100000, post_ids=[])

    replies = [
        [{"type": "tool_use", "id": "t1", "name": "hisobot", "input": {"kunlar": 1}}],
        [{"type": "text", "text": "Bugun 1 ta mijoz yozdi."}],
        [{"type": "tool_use", "id": "t2", "name": "mijozga_yozish", "input": {"conv_ids": [conv["id"]], "matn": "Narxi 100 000 so'm"}},
         {"type": "tool_use", "id": "t3", "name": "mahsulotni_ozgartirish", "input": {"product_id": pid, "narx": 90000}}],
        [{"type": "text", "text": "Kartalarni tasdiqlang."}],
    ]
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        content = replies[len(bodies) - 1]
        stop = "tool_use" if content[0]["type"] == "tool_use" else "end_turn"
        return httpx2.Response(200, json={"id": f"m{len(bodies)}", "type": "message", "role": "assistant",
                                          "model": "claude-opus-5-5", "content": content, "stop_reason": stop,
                                          "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 5}})

    sent = []

    class FakeAgent:
        ai = AI("k", "claude-opus-5-5", "low")
        def dm_window_open(self, cid): return True
        async def send_operator_message(self, cid, text):
            sent.append((cid, text)); return True
        def reload(self): pass

    FakeAgent.ai.client = anthropic.AsyncAnthropic(api_key="k", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    ctl = ControlAgent(db, FakeAgent(), tg=None)

    out = asyncio.run(ctl.chat("Bugungi hisobot"))
    assert [m["role"] for m in out] == ["user", "assistant"] and "1 ta mijoz" in out[-1]["text"]
    tool_result = bodies[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and '"yozgan_mijozlar": 1' in tool_result["content"]
    assert {t["name"] for t in bodies[0]["tools"]} >= {"hisobot", "mijozga_yozish", "sozlamani_ozgartirish"}

    out = asyncio.run(ctl.chat("Unga narxni yozib qo'y va narxni 90 mingga tushir"))
    cards = [m for m in out if m["role"] == "action"]
    assert len(cards) == 2 and all(c["action_status"] == "kutilmoqda" for c in cards)
    assert not sent and db.get_product(pid)["price"] == 100000  # tasdiqsiz hech narsa bajarilmaydi
    assert "100 000 so'm → 90 000 so'm" in cards[1]["details"]
    # history: oldingi karta Claude'ga tizim izohi sifatida boradi
    assert "Tasdiq kartasi" in json.dumps(bodies[3]["messages"], ensure_ascii=False)

    asyncio.run(ctl.decide(cards[0]["action_id"], True))
    asyncio.run(ctl.decide(cards[1]["action_id"], False))
    assert sent == [(conv["id"], "Narxi 100 000 so'm")]
    assert db.get_product(pid)["price"] == 100000
    assert db.get_control_action(cards[0]["action_id"])["status"] == "bajarildi"
    assert db.get_control_action(cards[1]["action_id"])["status"] == "rad etildi"
    notes = [m["text"] for m in db.control_messages() if m["role"] == "note"]
    assert any(n.startswith("✅ Bajarildi") for n in notes) and any(n.startswith("❌ Rad etildi") for n in notes)


def test_control_setting_change_validates_key():
    import os
    from app.control import ControlAgent
    import yaml as _yaml

    db = DB(os.environ["DB_PATH"])
    acc = db.add_account(name="Sozlama", ig_user_id="s1", ig_username="s", ig_token="t", tg_chat_id="", categories="*",
                         business_text="", settings_yaml=default_settings_yaml(), active=1)

    class A:
        ai = None
        def reload(self): pass
    ctl = ControlAgent(db, A(), tg=None)
    with pytest.raises(ValueError):
        ctl._prepare_action("sozlamani_ozgartirish", {"profil_id": acc, "kalit": "yoq.narsa", "qiymat": 1})
    title, details, args = ctl._prepare_action("sozlamani_ozgartirish", {"profil_id": acc, "kalit": "komment.yoqilgan", "qiymat": "false"})
    assert args["qiymat"] is False and "True → False" in details
    asyncio.run(ctl._execute("sozlamani_ozgartirish", args))
    assert _yaml.safe_load(db.get_account(acc)["settings_yaml"])["komment"]["yoqilgan"] is False
