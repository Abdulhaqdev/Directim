"""SQLite ombori: profillar, katalog, xotira, dialoglar, lidlar, kommentlar, javobsiz savollar."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,                        -- panelda ko'rinadigan nom: "Kitoblar"
    ig_user_id TEXT UNIQUE,           -- Instagram professional akkaunt ID (webhook entry.id)
    ig_username TEXT,
    ig_token TEXT,
    tg_chat_id TEXT,                  -- lidlar va savollar shu guruhga (bo'sh = umumiy guruh)
    categories TEXT DEFAULT '*',      -- qaysi katalog kategoriyalarini sotadi: "Kitoblar, Dekor" yoki "*"
    business_text TEXT DEFAULT '',
    settings_yaml TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    category TEXT,
    price INTEGER,                    -- so'm
    price_note TEXT,                  -- "dona", "to'plam", "chegirmada" ...
    description TEXT,
    variants TEXT,                    -- ranglar, o'lchamlar
    in_stock INTEGER DEFAULT 1,
    keywords TEXT,                    -- mijozlar qanday atashi: "saodat, 4 kitob"
    template TEXT,                    -- shu mahsulot so'ralganda ishlatiladigan javob shabloni
    post_ids TEXT DEFAULT '[]',       -- bog'langan Instagram post ID'lari
    source TEXT DEFAULT 'qolda',      -- qolda | csv | sheets | sayt:<domen>
    old_price INTEGER,                -- chegirmadan oldingi narx
    image_url TEXT,
    source_url TEXT,                  -- saytdan olingan bo'lsa — mahsulot sahifasi
    updated_at TEXT,
    UNIQUE(name, category)
);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,               -- NULL = barcha profillar uchun
    product_id INTEGER,               -- NULL = umumiy savol
    question TEXT,
    answer TEXT,
    source TEXT,                      -- telegram | panel | morechat
    hits INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,              -- "<account_id>:<igsid>"
    account_id INTEGER,
    igsid TEXT,
    username TEXT,
    name TEXT,
    ai_enabled INTEGER DEFAULT 1,
    paused_until TEXT,
    phone TEXT,
    lead_sent INTEGER DEFAULT 0,
    lead_data TEXT DEFAULT '{}',
    ai_replies INTEGER DEFAULT 0,
    handoff_sent INTEGER DEFAULT 0,
    followups_sent INTEGER DEFAULT 0,
    last_user_at TEXT,
    last_bot_at TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT,
    role TEXT,                        -- user | assistant | operator
    text TEXT,
    source TEXT,                      -- dm | comment | story | followup | xotira
    mid TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_mid ON messages(mid);
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    account_id INTEGER,
    media_id TEXT,
    user_id TEXT,
    username TEXT,
    text TEXT,
    category TEXT,
    action TEXT,
    public_reply TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    conv_id TEXT,
    phone TEXT,
    data TEXT,
    source TEXT,
    tg_sent INTEGER DEFAULT 0,        -- Telegram'ga yetib bordimi (bormasa fon vazifasi qayta yuboradi)
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    product_id INTEGER,
    question TEXT,
    hits INTEGER DEFAULT 1,
    answer TEXT,
    status TEXT DEFAULT 'kutilmoqda',  -- kutilmoqda | javob_berildi | e'tiborsiz
    tg_chat_id TEXT,
    tg_message_id INTEGER,
    created_at TEXT,
    UNIQUE(account_id, question)
);
CREATE TABLE IF NOT EXISTS gap_waiters (gap_id INTEGER, conv_id TEXT, PRIMARY KEY(gap_id, conv_id));
CREATE TABLE IF NOT EXISTS sent_mids (mid TEXT PRIMARY KEY, created_at TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    kind TEXT,
    text TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ads (           -- Meta Ad Library'dan yig'ilgan reklamalar
    ad_id TEXT PRIMARY KEY,                 -- Ad Library ID
    page_id TEXT,
    page_name TEXT,
    instagram TEXT,
    started_at TEXT,                        -- reklama boshlangan sana
    variants INTEGER DEFAULT 1,             -- shu kreativ nechta reklamada ishlatilgan
    landing_url TEXT,                       -- reklama olib boradigan sahifa
    product_id INTEGER,                     -- products jadvalidagi mahsulot
    ad_text TEXT,
    ad_price INTEGER,                       -- reklama matnidagi narx
    ad_old_price INTEGER,
    active INTEGER DEFAULT 1,
    collected_at TEXT
);
CREATE TABLE IF NOT EXISTS control_messages (   -- «Boshqaruv agenti» suhbati (egasi ↔ agent)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT,                                  -- user | assistant | note
    text TEXT,
    action_id INTEGER,                          -- tasdiq kartasi (bo'lsa)
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS control_actions (    -- tasdiq kutayotgan / bajarilgan amallar
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT,
    args TEXT,
    title TEXT,                                 -- kartadagi sarlavha
    details TEXT,                               -- kartadagi tafsilot (yuboriladigan matn va h.k.)
    status TEXT DEFAULT 'kutilmoqda',           -- kutilmoqda | bajarildi | rad etildi | xato
    result TEXT,
    created_at TEXT,
    decided_at TEXT
);
"""

PRODUCT_FIELDS = ("name", "category", "price", "price_note", "description", "variants", "in_stock",
                  "keywords", "template", "post_ids", "source", "old_price", "image_url", "source_url")
ACCOUNT_FIELDS = ("name", "ig_user_id", "ig_username", "ig_token", "tg_chat_id", "categories",
                  "business_text", "settings_yaml", "active")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def conv_key(account_id: int, igsid: str) -> str:
    return f"{account_id}:{igsid}"


def split_categories(value: str | None) -> list[str]:
    return [c.strip() for c in (value or "").split(",") if c.strip()]


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            # eski bazalar uchun yangi ustunlar
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(leads)").fetchall()}
            if "tg_sent" not in cols:
                self.conn.execute("ALTER TABLE leads ADD COLUMN tg_sent INTEGER DEFAULT 1")
            for table, col, typ in (("leads", "status", "TEXT DEFAULT 'yangi'"), ("leads", "closed_at", "TEXT"),
                                    ("leads", "closed_by", "TEXT"), ("leads", "tg_chat_id", "TEXT"),
                                    ("leads", "tg_message_id", "INTEGER"), ("gaps", "remind_at", "TEXT")):
                existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            pcols = {r[1] for r in self.conn.execute("PRAGMA table_info(products)").fetchall()}
            for col, typ in (("old_price", "INTEGER"), ("image_url", "TEXT"), ("source_url", "TEXT")):
                if col not in pcols:
                    self.conn.execute(f"ALTER TABLE products ADD COLUMN {col} {typ}")
            self.conn.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # --- kalit-qiymat ---
    def kv_get(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (key, value))

    # --- profillar ---
    def add_account(self, **fields) -> int:
        data = {k: fields.get(k) for k in ACCOUNT_FIELDS if k in fields}
        data["created_at"] = iso()
        cols = ", ".join(data)
        cur = self._exec(f"INSERT INTO accounts({cols}) VALUES ({', '.join('?' for _ in data)})", tuple(data.values()))
        return int(cur.lastrowid)

    def update_account(self, account_id: int, **fields) -> None:
        data = {k: v for k, v in fields.items() if k in ACCOUNT_FIELDS}
        if data:
            cols = ", ".join(f"{k}=?" for k in data)
            self._exec(f"UPDATE accounts SET {cols} WHERE id=?", (*data.values(), account_id))

    def get_account(self, account_id: int) -> dict | None:
        return self._one("SELECT * FROM accounts WHERE id=?", (account_id,))

    def account_by_ig(self, ig_user_id: str, include_inactive: bool = False) -> dict | None:
        sql = "SELECT * FROM accounts WHERE ig_user_id=?" + ("" if include_inactive else " AND active=1")
        return self._one(sql, (str(ig_user_id),))

    def rename_tg_chat(self, old: str, new: str) -> None:
        self._exec("UPDATE accounts SET tg_chat_id=? WHERE tg_chat_id=?", (new, old))
        self._exec("UPDATE gaps SET tg_chat_id=? WHERE tg_chat_id=?", (new, old))

    def list_accounts(self, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM accounts" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        return self._all(sql)

    # --- katalog ---
    def upsert_product(self, **fields) -> int:
        data = {k: fields.get(k) for k in PRODUCT_FIELDS if k in fields}
        if isinstance(data.get("post_ids"), list):
            data["post_ids"] = json.dumps(data["post_ids"])
        data["updated_at"] = iso()
        existing = self._one("SELECT id FROM products WHERE name=? AND category IS ?", (data.get("name"), data.get("category")))
        if existing:
            cols = ", ".join(f"{k}=?" for k in data)
            self._exec(f"UPDATE products SET {cols} WHERE id=?", (*data.values(), existing["id"]))
            return existing["id"]
        cols = ", ".join(data)
        cur = self._exec(f"INSERT INTO products({cols}) VALUES ({', '.join('?' for _ in data)})", tuple(data.values()))
        return int(cur.lastrowid)

    def upsert_scraped(self, source_url: str, name: str, category: str, price: int | None, old_price: int | None,
                       description: str, image_url: str, source: str, keep_category: bool = True) -> bool:
        """Saytdan olingan mahsulot. Yangi bo'lsa True. Mavjud bo'lsa faqat narx/rasm/holat yangilanadi —
        siz qo'lda o'zgartirgan nom, kategoriya, shablon, tavsif va kalit so'zlar saqlanib qoladi."""
        existing = self._one("SELECT * FROM products WHERE source_url=?", (source_url,))
        if existing is None:
            same_name = self._one("SELECT * FROM products WHERE name=? AND category IS ?", (name, category))
            if same_name:
                # Bir xil nomli mahsulot bor (sizniki yoki boshqa saytniki) — ustiga yozmaymiz, alohida saqlaymiz
                if same_name.get("source") == source:
                    name = f"{name} ({source_url.rstrip('/').rsplit('/', 1)[-1]})"
                else:
                    name = f"{name} ({source.removeprefix('sayt:')})"
        if existing:
            fields = dict(price=price, old_price=old_price, image_url=image_url or existing.get("image_url"),
                          in_stock=1, source_url=source_url)
            if not existing.get("description"):
                fields["description"] = description
            if not keep_category:
                fields["category"] = category
            self.update_product(existing["id"], **fields)
            return False
        self.upsert_product(name=name, category=category, price=price, old_price=old_price, description=description,
                            image_url=image_url, source_url=source_url, source=source, in_stock=1, post_ids=[])
        return True

    # --- Boshqaruv agenti ---
    def add_control_message(self, role: str, text: str, action_id: int | None = None) -> int:
        cur = self._exec("INSERT INTO control_messages(role, text, action_id, created_at) VALUES (?,?,?,?)",
                         (role, text, action_id, iso()))
        return int(cur.lastrowid)

    def control_messages(self, limit: int = 200, after_id: int = 0) -> list[dict]:
        rows = self._all("SELECT m.*, a.tool, a.title, a.details, a.status AS action_status, a.result AS action_result "
                         "FROM control_messages m LEFT JOIN control_actions a ON a.id=m.action_id "
                         "WHERE m.id>? ORDER BY m.id DESC LIMIT ?", (after_id, limit))
        return rows[::-1]

    def clear_control_messages(self) -> None:
        self._exec("DELETE FROM control_messages")
        self._exec("UPDATE control_actions SET status='rad etildi', result='suhbat tozalandi', decided_at=? "
                   "WHERE status='kutilmoqda'", (iso(),))

    def add_control_action(self, tool: str, args: dict, title: str, details: str) -> int:
        cur = self._exec("INSERT INTO control_actions(tool, args, title, details, created_at) VALUES (?,?,?,?,?)",
                         (tool, json.dumps(args, ensure_ascii=False), title, details, iso()))
        return int(cur.lastrowid)

    def get_control_action(self, action_id: int) -> dict | None:
        row = self._one("SELECT * FROM control_actions WHERE id=?", (action_id,))
        if row:
            row["args"] = json.loads(row.get("args") or "{}")
        return row

    def set_control_action(self, action_id: int, status: str, result: str) -> None:
        self._exec("UPDATE control_actions SET status=?, result=?, decided_at=? WHERE id=?",
                   (status, result, iso(), action_id))

    def period_report(self, days: float = 1, account_id: int | None = None) -> list[dict]:
        """Boshqaruv agenti hisoboti: har bir profil bo'yicha davr ko'rsatkichlari."""
        since = iso(now() - timedelta(days=days))
        out = []
        for acc in self.list_accounts():
            if account_id and acc["id"] != account_id:
                continue
            a = acc["id"]
            one = lambda sql, *args: self._one(sql, args)["n"]  # noqa: E731
            leads = {r["status"] or "yangi": r["n"] for r in self._all(
                "SELECT status, COUNT(*) AS n FROM leads WHERE account_id=? AND created_at>=? GROUP BY status", (a, since))}
            comments = {r["category"] or "?": r["n"] for r in self._all(
                "SELECT category, COUNT(*) AS n FROM comments WHERE account_id=? AND created_at>=? GROUP BY category",
                (a, since))}
            out.append({
                "profil_id": a, "profil": acc["name"], "instagram": acc.get("ig_username"),
                "instagram_ulangan": bool(acc.get("ig_user_id") and acc.get("ig_token")), "faol": bool(acc.get("active")),
                "yangi_dialoglar": one("SELECT COUNT(*) AS n FROM conversations WHERE account_id=? AND created_at>=?", a, since),
                "yozgan_mijozlar": one("SELECT COUNT(*) AS n FROM conversations WHERE account_id=? AND last_user_at>=?", a, since),
                "mijoz_xabarlari": one("SELECT COUNT(*) AS n FROM messages m JOIN conversations c ON c.id=m.conv_id "
                                       "WHERE c.account_id=? AND m.role='user' AND m.created_at>=?", a, since),
                "ai_javoblari": one("SELECT COUNT(*) AS n FROM messages m JOIN conversations c ON c.id=m.conv_id "
                                    "WHERE c.account_id=? AND m.role='assistant' AND m.created_at>=?", a, since),
                "operator_xabarlari": one("SELECT COUNT(*) AS n FROM messages m JOIN conversations c ON c.id=m.conv_id "
                                          "WHERE c.account_id=? AND m.role='operator' AND m.created_at>=?", a, since),
                "lidlar": sum(leads.values()), "lid_holatlari": leads,
                "kommentlar": sum(comments.values()), "komment_turlari": comments,
                "operator_kerak_dialoglar": one("SELECT COUNT(*) AS n FROM conversations WHERE account_id=? AND handoff_sent=1 "
                                                "AND last_user_at>=?", a, since),
                "ai_toxtatilgan_dialoglar": one("SELECT COUNT(*) AS n FROM conversations WHERE account_id=? AND "
                                                "(ai_enabled=0 OR paused_until>?)", a, iso()),
                "javobsiz_savollar": one("SELECT COUNT(*) AS n FROM gaps WHERE account_id=? AND status='kutilmoqda'", a),
            })
        return out

    # --- Reklamalar (Meta Ad Library) ---
    def upsert_ad(self, **fields) -> None:
        cols = ("ad_id", "page_id", "page_name", "instagram", "started_at", "variants", "landing_url",
                "product_id", "ad_text", "ad_price", "ad_old_price", "active", "collected_at")
        data = {k: fields.get(k) for k in cols if k in fields}
        names = ", ".join(data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "ad_id")
        self._exec(f"INSERT INTO ads({names}) VALUES ({', '.join('?' for _ in data)}) "
                   f"ON CONFLICT(ad_id) DO UPDATE SET {updates}", tuple(data.values()))

    def deactivate_missing_ads(self, page_id: str, seen_ids: set[str]) -> int:
        rows = self._all("SELECT ad_id FROM ads WHERE page_id=? AND active=1", (page_id,))
        gone = [r["ad_id"] for r in rows if r["ad_id"] not in seen_ids]
        for ad_id in gone:
            self._exec("UPDATE ads SET active=0 WHERE ad_id=?", (ad_id,))
        return len(gone)

    def list_ads(self, page_id: str | None = None) -> list[dict]:
        sql = ("SELECT a.*, p.name AS product_name, p.price AS site_price, p.old_price AS site_old_price "
               "FROM ads a LEFT JOIN products p ON p.id=a.product_id")
        if page_id:
            return self._all(sql + " WHERE a.page_id=? ORDER BY a.started_at DESC", (page_id,))
        return self._all(sql + " ORDER BY a.started_at DESC")

    def mark_missing_scraped(self, source: str, seen_urls: set[str]) -> int:
        rows = self._all("SELECT id, source_url FROM products WHERE source=? AND in_stock=1", (source,))
        missing = [r["id"] for r in rows if r["source_url"] and r["source_url"] not in seen_urls]
        for pid in missing:
            self._exec("UPDATE products SET in_stock=0, updated_at=? WHERE id=?", (iso(), pid))
        return len(missing)

    def update_product(self, product_id: int, **fields) -> None:
        data = {k: v for k, v in fields.items() if k in PRODUCT_FIELDS}
        if isinstance(data.get("post_ids"), list):
            data["post_ids"] = json.dumps(data["post_ids"])
        data["updated_at"] = iso()
        cols = ", ".join(f"{k}=?" for k in data)
        self._exec(f"UPDATE products SET {cols} WHERE id=?", (*data.values(), product_id))

    def delete_product(self, product_id: int) -> None:
        # Xotira yo'qolmasin: mahsulotga bog'langan javoblar umumiy javobga aylanadi
        self._exec("UPDATE memory SET product_id=NULL WHERE product_id=?", (product_id,))
        self._exec("UPDATE gaps SET product_id=NULL WHERE product_id=?", (product_id,))
        self._exec("DELETE FROM products WHERE id=?", (product_id,))

    def get_product(self, product_id: int | None) -> dict | None:
        if not product_id:
            return None
        row = self._one("SELECT * FROM products WHERE id=?", (product_id,))
        if row:
            row["post_ids"] = json.loads(row.get("post_ids") or "[]")
        return row

    def list_products(self, categories: list[str] | None = None) -> list[dict]:
        """categories=None yoki ['*'] — hammasi; aks holda faqat shu kategoriyalar."""
        rows = self._all("SELECT * FROM products ORDER BY category, name")
        for r in rows:
            r["post_ids"] = json.loads(r.get("post_ids") or "[]")
        if categories and "*" not in categories:
            wanted = {c.lower() for c in categories}
            rows = [r for r in rows if (r.get("category") or "").lower() in wanted]
        return rows

    def product_categories(self) -> list[str]:
        return [r["category"] for r in self._all("SELECT DISTINCT category FROM products WHERE category IS NOT NULL ORDER BY category")]

    def products_for_post(self, media_id: str) -> list[dict]:
        return [p for p in self.list_products() if media_id in p["post_ids"]]

    # --- xotira ---
    def add_memory(self, question: str, answer: str, account_id: int | None, product_id: int | None, source: str) -> int:
        existing = self._one(
            "SELECT id FROM memory WHERE question=? AND account_id IS ? AND product_id IS ?",
            (question.strip(), account_id, product_id),
        )
        if existing:
            self._exec("UPDATE memory SET answer=?, source=? WHERE id=?", (answer.strip(), source, existing["id"]))
            return existing["id"]
        cur = self._exec(
            "INSERT INTO memory(account_id, product_id, question, answer, source, created_at) VALUES (?,?,?,?,?,?)",
            (account_id, product_id, question.strip(), answer.strip(), source, iso()),
        )
        return int(cur.lastrowid)

    def update_memory(self, memory_id: int, question: str, answer: str, account_id: int | None, product_id: int | None) -> None:
        self._exec("UPDATE memory SET question=?, answer=?, account_id=?, product_id=? WHERE id=?",
                   (question.strip(), answer.strip(), account_id, product_id, memory_id))

    def delete_memory(self, memory_id: int) -> None:
        self._exec("DELETE FROM memory WHERE id=?", (memory_id,))

    def memory_for(self, account_id: int | None, product_ids: set[int] | None = None) -> list[dict]:
        """Profil uchun ko'rinadigan xotira: shu profilniki + umumiy; mahsulotga bog'langanlari faqat
        profil sotadigan mahsulotlar uchun."""
        rows = self._all(
            "SELECT m.*, p.name AS product_name FROM memory m LEFT JOIN products p ON p.id=m.product_id "
            "WHERE m.account_id IS NULL OR m.account_id=? ORDER BY m.id",
            (account_id,),
        )
        if product_ids is not None:
            rows = [r for r in rows if r["product_id"] is None or r["product_id"] in product_ids]
        return rows

    def list_memory(self) -> list[dict]:
        return self._all(
            "SELECT m.*, p.name AS product_name, a.name AS account_name FROM memory m "
            "LEFT JOIN products p ON p.id=m.product_id LEFT JOIN accounts a ON a.id=m.account_id ORDER BY m.id DESC"
        )

    # --- dialoglar ---
    def get_conv(self, conv_id: str) -> dict | None:
        row = self._one("SELECT * FROM conversations WHERE id=?", (conv_id,))
        if row:
            row["lead_data"] = json.loads(row.get("lead_data") or "{}")
        return row

    def ensure_conv(self, account_id: int, igsid: str, username: str | None = None, name: str | None = None) -> dict:
        key = conv_key(account_id, igsid)
        conv = self.get_conv(key)
        if conv is None:
            self._exec(
                "INSERT INTO conversations(id, account_id, igsid, username, name, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (key, account_id, igsid, username, name, iso(), iso()),
            )
        elif (username and not conv.get("username")) or (name and not conv.get("name")):
            self._exec(
                "UPDATE conversations SET username=COALESCE(username, ?), name=COALESCE(name, ?) WHERE id=?",
                (username, name, key),
            )
        return self.get_conv(key)  # type: ignore[return-value]

    def update_conv(self, conv_id: str, **fields) -> None:
        if not fields:
            return
        if "lead_data" in fields and not isinstance(fields["lead_data"], str):
            fields["lead_data"] = json.dumps(fields["lead_data"], ensure_ascii=False)
        fields["updated_at"] = iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE conversations SET {cols} WHERE id=?", (*fields.values(), conv_id))

    def list_convs(self, account_id: int | None = None, limit: int = 200) -> list[dict]:
        where, args = ("WHERE c.account_id=?", (account_id,)) if account_id else ("", ())
        return self._all(
            "SELECT c.*, a.name AS account_name, "
            "(SELECT text FROM messages m WHERE m.conv_id=c.id ORDER BY id DESC LIMIT 1) AS last_text "
            f"FROM conversations c LEFT JOIN accounts a ON a.id=c.account_id {where} ORDER BY c.updated_at DESC LIMIT ?",
            (*args, limit),
        )

    # --- xabarlar ---
    def add_message(self, conv_id: str, role: str, text: str, source: str = "dm", mid: str | None = None,
                    created_at: str | None = None) -> None:
        """created_at berilsa (tarixdan import) — dialog vaqt belgilari o'zgarmaydi, eslatma ishga tushmaydi."""
        self._exec(
            "INSERT INTO messages(conv_id, role, text, source, mid, created_at) VALUES (?,?,?,?,?,?)",
            (conv_id, role, text, source, mid, created_at or iso()),
        )
        if created_at is None:
            stamp = "last_user_at" if role == "user" else "last_bot_at"
            self._exec(f"UPDATE conversations SET {stamp}=?, updated_at=? WHERE id=?", (iso(), iso(), conv_id))

    def history(self, conv_id: str, limit: int = 40) -> list[dict]:
        rows = self._all("SELECT * FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT ?", (conv_id, limit))
        return list(reversed(rows))

    def last_dm_user_at(self, conv_id: str) -> str | None:
        """Mijozning Direct'dagi oxirgi xabari vaqti (kommentlar hisobga olinmaydi)."""
        row = self._one("SELECT MAX(created_at) AS t FROM messages WHERE conv_id=? AND role='user' AND source IN ('dm','story','tarix')",
                        (conv_id,))
        return row["t"] if row else None

    def has_mid(self, mid: str) -> bool:
        return self._one("SELECT 1 AS x FROM messages WHERE mid=?", (mid,)) is not None

    def remember_sent(self, mid: str | None) -> None:
        if mid:
            self._exec("INSERT OR IGNORE INTO sent_mids(mid, created_at) VALUES (?,?)", (mid, iso()))

    def is_our_mid(self, mid: str | None) -> bool:
        return bool(mid) and self._one("SELECT 1 AS x FROM sent_mids WHERE mid=?", (mid,)) is not None

    # --- kommentlar ---
    def get_comment(self, comment_id: str) -> dict | None:
        return self._one("SELECT * FROM comments WHERE id=?", (comment_id,))

    def comment_seen(self, comment_id: str) -> bool:
        return self._one("SELECT 1 AS x FROM comments WHERE id=?", (comment_id,)) is not None

    def add_comment(self, **row) -> None:
        row.setdefault("created_at", iso())
        cols = ", ".join(row)
        self._exec(f"INSERT OR REPLACE INTO comments({cols}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))

    def recent_comment_reply(self, account_id: int, user_id: str, media_id: str | None, hours: int = 24) -> dict | None:
        """Shu mijozga shu post ostida oxirgi soatlarda javob berganmizmi."""
        since = iso(now() - timedelta(hours=hours))
        return self._one(
            "SELECT * FROM comments WHERE account_id=? AND user_id=? AND media_id IS ? AND created_at>=? "
            "AND (action LIKE '%Direct%' OR action LIKE '%ochiq javob%') ORDER BY created_at DESC LIMIT 1",
            (account_id, user_id, media_id, since))

    def list_comments(self, account_id: int | None = None, limit: int = 200) -> list[dict]:
        where, args = ("WHERE c.account_id=?", (account_id,)) if account_id else ("", ())
        return self._all(
            f"SELECT c.*, a.name AS account_name FROM comments c LEFT JOIN accounts a ON a.id=c.account_id {where} "
            "ORDER BY c.created_at DESC LIMIT ?",
            (*args, limit),
        )

    # --- lidlar ---
    def add_lead(self, account_id: int, conv_id: str, phone: str, data: dict, source: str, tg_sent: bool = True) -> int:
        cur = self._exec(
            "INSERT INTO leads(account_id, conv_id, phone, data, source, tg_sent, created_at) VALUES (?,?,?,?,?,?,?)",
            (account_id, conv_id, phone, json.dumps(data, ensure_ascii=False), source, 1 if tg_sent else 0, iso()),
        )
        return int(cur.lastrowid)

    def unsent_leads(self) -> list[dict]:
        rows = self._all("SELECT * FROM leads WHERE tg_sent=0 ORDER BY id LIMIT 20")
        for r in rows:
            r["data"] = json.loads(r.get("data") or "{}")
        return rows

    def mark_lead_sent(self, lead_id: int, chat_id: str | None = None, message_id: int | None = None) -> None:
        self._exec("UPDATE leads SET tg_sent=1, tg_chat_id=COALESCE(?, tg_chat_id), tg_message_id=COALESCE(?, tg_message_id) WHERE id=?",
                   (chat_id, message_id, lead_id))

    def get_lead(self, lead_id: int) -> dict | None:
        row = self._one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if row:
            row["data"] = json.loads(row.get("data") or "{}")
        return row

    def set_lead_status(self, lead_id: int, status: str, by: str | None = None) -> None:
        self._exec("UPDATE leads SET status=?, closed_at=?, closed_by=? WHERE id=?", (status, iso(), by, lead_id))

    def lead_stats(self, days: int = 30) -> dict:
        since = iso(now() - timedelta(days=days))
        rows = self._all("SELECT status, COUNT(*) AS n FROM leads WHERE created_at>=? GROUP BY status", (since,))
        out = {"yangi": 0, "sotildi": 0, "bolmadi": 0}
        for r in rows:
            out[r["status"] or "yangi"] = r["n"]
        out["jami"] = sum(out.values())
        closed = out["sotildi"] + out["bolmadi"]
        out["konversiya"] = round(100 * out["sotildi"] / closed) if closed else None
        return out

    def list_leads(self, limit: int = 100) -> list[dict]:
        rows = self._all(
            "SELECT l.*, a.name AS account_name FROM leads l LEFT JOIN accounts a ON a.id=l.account_id ORDER BY l.id DESC LIMIT ?",
            (limit,),
        )
        for r in rows:
            r["data"] = json.loads(r.get("data") or "{}")
        return rows

    # --- javobsiz savollar ---
    def add_gap(self, account_id: int, question: str, product_id: int | None = None,
                conv_id: str | None = None) -> tuple[dict | None, bool]:
        """(savol, yangimi) qaytaradi. Kutayotgan mijoz ro'yxatga qo'shiladi."""
        question = question.strip()[:500]
        if not question:
            return None, False
        gap = self._one("SELECT * FROM gaps WHERE account_id=? AND question=?", (account_id, question))
        is_new = gap is None
        if is_new:
            self._exec(
                "INSERT INTO gaps(account_id, product_id, question, created_at) VALUES (?,?,?,?)",
                (account_id, product_id, question, iso()),
            )
            gap = self._one("SELECT * FROM gaps WHERE account_id=? AND question=?", (account_id, question))
        elif gap["status"] == "kutilmoqda":
            self._exec("UPDATE gaps SET hits=hits+1 WHERE id=?", (gap["id"],))
        if conv_id and gap:
            self._exec("INSERT OR IGNORE INTO gap_waiters(gap_id, conv_id) VALUES (?,?)", (gap["id"], conv_id))
        return gap, is_new

    def snooze_gap(self, gap_id: int, hours: float = 3) -> None:
        self._exec("UPDATE gaps SET remind_at=? WHERE id=?", (iso(now() + timedelta(hours=hours)), gap_id))

    def due_reminders(self) -> list[dict]:
        return self._all("SELECT * FROM gaps WHERE status='kutilmoqda' AND remind_at IS NOT NULL AND remind_at<=?", (iso(),))

    def set_gap_tg(self, gap_id: int, chat_id: str, message_id: int) -> None:
        self._exec("UPDATE gaps SET tg_chat_id=?, tg_message_id=?, remind_at=NULL WHERE id=?", (str(chat_id), message_id, gap_id))

    def gap_by_tg(self, chat_id: str, message_id: int) -> dict | None:
        return self._one("SELECT * FROM gaps WHERE tg_chat_id=? AND tg_message_id=?", (str(chat_id), message_id))

    def gap_waiters(self, gap_id: int) -> list[str]:
        return [r["conv_id"] for r in self._all("SELECT conv_id FROM gap_waiters WHERE gap_id=?", (gap_id,))]

    def list_gaps(self, account_id: int | None = None, status: str | None = None) -> list[dict]:
        sql = ("SELECT g.*, a.name AS account_name, p.name AS product_name FROM gaps g "
               "LEFT JOIN accounts a ON a.id=g.account_id LEFT JOIN products p ON p.id=g.product_id WHERE 1=1")
        args: list = []
        if account_id:
            sql += " AND g.account_id=?"
            args.append(account_id)
        if status:
            sql += " AND g.status=?"
            args.append(status)
        return self._all(sql + " ORDER BY g.status, g.hits DESC, g.id DESC", tuple(args))

    def get_gap(self, gap_id: int) -> dict | None:
        return self._one("SELECT * FROM gaps WHERE id=?", (gap_id,))

    def max_message_id(self, conv_id: str) -> int:
        row = self._one("SELECT MAX(id) AS m FROM messages WHERE conv_id=?", (conv_id,))
        return int(row["m"] or 0) if row else 0

    def user_texts_after(self, conv_id: str, after_id: int) -> list[str]:
        return [r["text"] for r in self._all(
            "SELECT text FROM messages WHERE conv_id=? AND role='user' AND id>? ORDER BY id", (conv_id, after_id))]

    def last_message_role(self, conv_id: str) -> str | None:
        row = self._one("SELECT role FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT 1", (conv_id,))
        return row["role"] if row else None

    def add_suggestion(self, account_id: int, question: str, answer: str, product_id: int | None) -> bool:
        """Tarixdan topilgan savol-javob taklifi (egasi tasdiqlaydi). Yangi bo'lsa True."""
        question = question.strip()[:500]
        if not question or self._one("SELECT id FROM gaps WHERE account_id=? AND question=?", (account_id, question)):
            return False
        self._exec("INSERT INTO gaps(account_id, product_id, question, answer, status, created_at) VALUES (?,?,?,?,?,?)",
                   (account_id, product_id, question, answer.strip(), "taklif", iso()))
        return True

    def set_gap(self, gap_id: int, answer: str | None, status: str) -> None:
        self._exec("UPDATE gaps SET answer=?, status=? WHERE id=?", (answer, status, gap_id))

    # --- jurnal ---
    def log(self, kind: str, text: str, account_id: int | None = None) -> None:
        self._exec("INSERT INTO events(account_id, kind, text, created_at) VALUES (?,?,?,?)",
                   (account_id, kind, text[:2000], iso()))

    def events(self, limit: int = 50) -> list[dict]:
        return self._all(
            "SELECT e.*, a.name AS account_name FROM events e LEFT JOIN accounts a ON a.id=e.account_id ORDER BY e.id DESC LIMIT ?",
            (limit,),
        )

    def count_events(self, kind: str, since: str, account_id: int | None = None) -> int:
        return self._one("SELECT COUNT(*) AS n FROM events WHERE kind=? AND created_at>=? AND account_id IS ?",
                         (kind, since, account_id))["n"]

    # --- statistika ---
    def stats(self) -> list[dict]:
        since = iso(now() - timedelta(days=1))
        out = []
        for acc in self.list_accounts():
            a = acc["id"]
            out.append({
                "account": acc,
                "dialoglar": self._one("SELECT COUNT(*) AS n FROM conversations WHERE account_id=? AND last_user_at>=?", (a, since))["n"],
                "lidlar": self._one("SELECT COUNT(*) AS n FROM leads WHERE account_id=? AND created_at>=?", (a, since))["n"],
                "kommentlar": self._one("SELECT COUNT(*) AS n FROM comments WHERE account_id=? AND created_at>=?", (a, since))["n"],
                "javobsiz": self._one("SELECT COUNT(*) AS n FROM gaps WHERE account_id=? AND status='kutilmoqda'", (a,))["n"],
            })
        return out

    def followup_candidates(self) -> list[dict]:
        return self._all(
            "SELECT * FROM conversations WHERE ai_enabled=1 AND handoff_sent=0 AND (phone IS NULL OR phone='') "
            "AND last_bot_at IS NOT NULL AND last_user_at IS NOT NULL AND last_bot_at > last_user_at"
        )
