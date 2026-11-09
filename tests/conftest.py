import os
import tempfile

# Testlarda fon vazifalari (Instagram/Telegram'ga haqiqiy so'rovlar) ishlamasin
os.environ.setdefault("AGENT_NO_BACKGROUND", "1")

# Testlar haqiqiy bazaga (data/agent.db) va haqiqiy kalitlarga tegmasin:
# alohida vaqtinchalik baza, .env'dagi qiymatlar o'rniga bo'sh qiymatlar (.env mavjud o'zgaruvchini bosib yozmaydi)
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="agent-test-"), "agent.db")
for _key in ("ANTHROPIC_API_KEY", "IG_APP_ID", "IG_APP_SECRET", "IG_CONFIG_ID", "IG_VERIFY_TOKEN", "IG_ACCESS_TOKEN",
             "IG_USER_ID", "IG_USERNAME", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_WEBHOOK_SECRET",
             "TELEGRAM_ADMIN_IDS", "PUBLIC_URL", "CATALOG_CSV_URL", "ADMIN_PASSWORD"):
    os.environ[_key] = ""
