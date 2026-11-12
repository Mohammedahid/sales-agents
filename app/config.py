"""Settings loaded from environment variables (.env)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # load .env when running without Docker (e.g. on Windows)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass
class Brand:
    key: str                 # "seen" / "nvolve"
    name: str                # brand display name
    agent_name: str          # persona name shown to customers
    human_name: str          # real employee who takes over
    phone_number_id: str     # WhatsApp Cloud API phone number id
    access_token: str        # WhatsApp token for this number
    description: str         # one-line description of the product
    urls: list = field(default_factory=list)  # URLs to monitor
    template_name: str = ""          # approved WhatsApp template for intro campaigns
    template_lang: str = "ar"
    template_header_document: bool = True  # template header = Document (profile PDF)
    template_has_name: bool = True          # template body has {{1}} = contact name

    @property
    def knowledge(self) -> str:
        parts = []
        for name in (f"{self.key}.md", f"{self.key}-website.md"):
            path = KNOWLEDGE_DIR / name
            if path.exists():
                parts.append(path.read_text(encoding="utf-8"))
        return "\n\n".join(parts)


def _brand(key: str, default_name: str, default_desc: str) -> Brand:
    p = key.upper()
    urls = [u.strip() for u in _env(f"{p}_MONITOR_URLS").split(",") if u.strip()]
    return Brand(
        key=key,
        name=_env(f"{p}_NAME", default_name),
        agent_name=_env(f"{p}_AGENT_NAME", "فهد"),
        human_name=_env(f"{p}_HUMAN_NAME", "زميلي"),
        phone_number_id=_env(f"{p}_PHONE_NUMBER_ID"),
        access_token=_env(f"{p}_WA_TOKEN") or _env("WA_ACCESS_TOKEN"),
        description=_env(f"{p}_DESCRIPTION", default_desc),
        urls=urls,
        template_name=_env(f"{p}_TEMPLATE_NAME", f"{key}_intro"),
        template_lang=_env(f"{p}_TEMPLATE_LANG", "ar"),
        template_header_document=_env(f"{p}_TEMPLATE_HEADER", "document").lower() == "document",
        template_has_name=_env(f"{p}_TEMPLATE_HAS_NAME", "1") == "1",
    )


class Settings:
    def __init__(self):
        self.llm_provider = _env("LLM_PROVIDER", "anthropic").lower()  # anthropic | gemini
        self.gemini_api_key = _env("GEMINI_API_KEY")
        self.gemini_model = _env("GEMINI_MODEL", "gemini-flash-latest")
        self.gemini_reasoning = _env("GEMINI_REASONING", "low")
        self.gemini_fallback_model = _env("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest")
        self.anthropic_api_key = _env("ANTHROPIC_API_KEY")
        self.model = _env("MODEL", "claude-haiku-5-5")
        self.admin_model = _env("ADMIN_MODEL", self.model)

        self.wa_verify_token = _env("WA_VERIFY_TOKEN", "change-me")
        self.wa_app_secret = _env("WA_APP_SECRET")
        self.graph_version = _env("GRAPH_VERSION", "v23.0")

        self.telegram_token = _env("TELEGRAM_BOT_TOKEN")
        self.telegram_admins = [int(x) for x in _env("TELEGRAM_ADMIN_IDS").split(",") if x.strip()]

        self.human_pause_hours = float(_env("HUMAN_PAUSE_HOURS", "12"))
        self.debounce_seconds = float(_env("DEBOUNCE_SECONDS", "5"))
        self.daily_report_hour = int(_env("DAILY_REPORT_HOUR", "9"))
        self.health_interval_min = int(_env("HEALTH_INTERVAL_MIN", "5"))
        self.timezone = _env("TZ", "Asia/Riyadh")
        self.campaign_delay = float(_env("CAMPAIGN_DELAY_SECONDS", "3"))
        self.default_contact_name = _env("DEFAULT_CONTACT_NAME", "أستاذنا")

        self.brands = {
            "seen": _brand("seen", "سين", "حلول الشاشات الرقمية"),
            "nvolve": _brand("nvolve", "Nvolve", "منصة الفعاليات والماركت بليس"),
        }

    def brand_by_phone_id(self, phone_number_id: str):
        for b in self.brands.values():
            if b.phone_number_id and b.phone_number_id == phone_number_id:
                return b
        return None


settings = Settings()
