"""Sozlamalar: .env (maxfiy kalitlar) + har bir profilning YAML sozlamalari."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def _load_dotenv(path: Path) -> None:
    """Oddiy .env o'quvchi. Mavjud muhit o'zgaruvchilarini bosib yozmaydi."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(ROOT / ".env")


@dataclass
class Env:
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
    claude_effort: str = os.getenv("CLAUDE_EFFORT", "low")
    # Meta ilovasi — barcha Instagram profillar uchun bitta
    ig_app_id: str = os.getenv("IG_APP_ID", "")                       # «Instagram bilan ulash» tugmasi uchun
    ig_app_secret: str = os.getenv("IG_APP_SECRET", "")               # vergul bilan bir nechta bo'lishi mumkin
    ig_config_id: str = os.getenv("IG_CONFIG_ID", "")                 # Unified login: Facebook Login for Business konfiguratsiyasi
    ig_verify_token: str = os.getenv("IG_VERIFY_TOKEN", "")
    ig_api_version: str = os.getenv("IG_API_VERSION", "v26.0")
    # Birinchi ishga tushishda shu qiymatlardan birinchi profil yaratiladi (keyin panelda boshqariladi)
    ig_access_token: str = os.getenv("IG_ACCESS_TOKEN", "")
    ig_user_id: str = os.getenv("IG_USER_ID", "")
    ig_username: str = os.getenv("IG_USERNAME", "").lstrip("@")
    # Telegram
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")           # umumiy guruh
    telegram_webhook_secret: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    telegram_admin_ids: str = os.getenv("TELEGRAM_ADMIN_IDS", "")       # javob bera oladiganlar (bo'sh = guruhdagi hamma)
    public_url: str = os.getenv("PUBLIC_URL", "").rstrip("/")
    # Katalog
    catalog_csv_url: str = os.getenv("CATALOG_CSV_URL", "")             # Google Sheets → CSV havola
    catalog_sync_minutes: int = int(os.getenv("CATALOG_SYNC_MINUTES", "30"))
    site_sync_hours: int = int(os.getenv("SITE_SYNC_HOURS", "24"))           # saytlardan narxlarni yangilash
    # Panel
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    # Maxfiylik siyosati sahifasidagi aloqa (Meta App Review uchun ochiq sahifa)
    contact_email: str = os.getenv("CONTACT_EMAIL", "")
    business_name: str = os.getenv("BUSINESS_NAME", "Sotuv agenti")
    db_path: str = os.getenv("DB_PATH", str(ROOT / "data" / "agent.db"))
    reply_debounce_seconds: float = float(os.getenv("REPLY_DEBOUNCE_SECONDS", "4"))

    def admin_ids(self) -> set[str]:
        return {x.strip() for x in self.telegram_admin_ids.split(",") if x.strip()}


env = Env()


@dataclass
class Config:
    """Profil sozlamalari (o'zbekcha kalitlar)."""

    raw: dict = field(default_factory=dict)

    def get(self, path: str, default=None):
        node = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @classmethod
    def from_yaml(cls, text: str | None) -> "Config":
        base = yaml.safe_load(default_settings_yaml()) or {}
        own = yaml.safe_load(text or "") or {}
        return cls(raw=_deep_merge(base, own if isinstance(own, dict) else {}))


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def default_settings_yaml() -> str:
    path = CONFIG_DIR / "sozlamalar.yaml"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def validate_yaml_dict(text: str) -> dict:
    parsed = yaml.safe_load(text) or {}
    if not isinstance(parsed, dict):
        raise ValueError("Sozlamalar 'kalit: qiymat' ko'rinishida bo'lishi kerak")
    return parsed
