"""Outbound intro campaigns: send an approved WhatsApp template (+ profile PDF) to a list of numbers."""
import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import db, whatsapp
from .config import KNOWLEDGE_DIR, Brand, settings

log = logging.getLogger("campaign")

STOP_WORDS = {
    "ايقاف", "إيقاف", "وقف", "stop", "الغاء", "إلغاء", "لا شكرا", "لا شكراً", "لا شكرًا",
    "مو مهتم", "غير مهتم", "لا تراسلني", "unsubscribe",
}
INTEREST_WORDS = {"مهتم", "مهتمة", "interested", "ايه مهتم", "نعم مهتم", "أبي أجرب", "ابي اجرب", "ابغى اجرب", "أبغى أجرب"}
OPTOUT_REPLY = "ولا يهمك، ما راح نرسل لك رسائل ثانية 🙏 وإذا احتجت شي مستقبلاً حنا بالخدمة."


def _clean(text: str) -> str:
    return re.sub(r"[^\w\s؀-ۿ]", "", (text or "")).strip().lower()


def is_stop(text: str) -> bool:
    return _clean(text) in {_clean(w) for w in STOP_WORDS}


def is_interest(text: str) -> bool:
    return _clean(text) in {_clean(w) for w in INTEREST_WORDS}


def normalize_phone(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("05") and len(d) == 10:      # 05XXXXXXXX → 9665XXXXXXXX
        d = "966" + d[1:]
    elif d.startswith("5") and len(d) == 9:      # 5XXXXXXXX
        d = "966" + d
    return d if 10 <= len(d) <= 15 else ""


def parse_contacts(text: str):
    """Lines like 'محمد, 0551234567' or '966551234567 محمد' → [(phone, name)] (deduped)."""
    seen, out = set(), []
    for line in text.splitlines():
        parts = [p.strip() for p in re.split(r"[,;\t|]", line) if p.strip()]
        if len(parts) == 1:  # "0551234567 محمد" or just a number
            m = re.search(r"\+?[\d\s-]{9,}", parts[0])
            if not m:
                continue
            phone_raw, name = m.group(), parts[0].replace(m.group(), "").strip()
        else:
            phone_raw = next((p for p in parts if len(re.sub(r"\D", "", p)) >= 9), "")
            name = next((p for p in parts if p != phone_raw and not re.fullmatch(r"[\d\s+-]+", p)), "")
        phone = normalize_phone(phone_raw)
        if phone and phone not in seen:
            seen.add(phone)
            out.append((phone, name))
    return out


TEMPLATES_DIR = KNOWLEDGE_DIR / "templates"
MEDIA_TYPES = {".pdf": ("document", "application/pdf"), ".mp4": ("video", "video/mp4"),
               ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"), ".png": ("image", "image/png")}


@dataclass
class Template:
    name: str
    brand: Brand
    body: str
    media: Optional[Path] = None
    header: Optional[str] = None   # document / video / image
    mime: str = ""

    @property
    def has_name(self):
        return "{{1}}" in self.body

    def text_for(self, name):
        return self.body.replace("{{1}}", name or settings.default_contact_name)


def get_template(arg: str) -> Optional[Template]:
    """'seen' → the brand's default template; 'seen_restaurants' → that template."""
    arg = (arg or "").strip().lower()
    name = settings.brands[arg].template_name if arg in settings.brands else arg
    brand = settings.brands.get(name.split("_")[0])
    body_path = TEMPLATES_DIR / f"{name}.txt"
    if not brand or not body_path.exists():
        return None
    media = next((TEMPLATES_DIR / f"{name}{ext}" for ext in MEDIA_TYPES if (TEMPLATES_DIR / f"{name}{ext}").exists()), None)
    header, mime = MEDIA_TYPES[media.suffix.lower()] if media else (None, "")
    return Template(name, brand, body_path.read_text(encoding="utf-8").strip(), media, header, mime)


def list_templates():
    if not TEMPLATES_DIR.exists():
        return []
    return [t for t in (get_template(p.stem) for p in sorted(TEMPLATES_DIR.glob("*.txt"))) if t]


async def prepare_media(tpl: Template):
    if not tpl.media:
        return None
    return await whatsapp.upload_media(tpl.brand, tpl.media, tpl.mime)


async def send_intro(tpl: Template, phone: str, name: str, media_id, campaign_id=None):
    """Returns (ok, error_text)."""
    brand = tpl.brand
    if db.is_opted_out(brand.key, phone):
        return False, "طلب إيقاف سابقاً"
    if tpl.media and not media_id:
        return False, "ما قدرت أرفع الملف/الفيديو لواتساب"
    r = await whatsapp.send_template(brand, phone, tpl, name or settings.default_contact_name, media_id)
    try:
        data = r.json()
    except Exception:
        data = {}
    if r.status_code >= 300:
        return False, data.get("error", {}).get("message", r.text[:200])
    wamid = data.get("messages", [{}])[0].get("id", "")
    db.add_campaign_msg(wamid, campaign_id, brand.key, phone)
    db.set_contact_name(brand.key, phone, name)
    db.add_message(brand.key, phone, "assistant", tpl.text_for(name))
    return True, ""


async def run_campaign(tpl: Template, contacts, notify):
    brand = tpl.brand
    media_id = await prepare_media(tpl)
    cid = db.new_campaign(brand.key, len(contacts), tpl.name)
    sent, skipped, failed, errors = 0, 0, 0, {}
    for phone, name in contacts:
        if db.is_opted_out(brand.key, phone):
            skipped += 1
            continue
        try:
            ok, err = await send_intro(tpl, phone, name, media_id, cid)
        except Exception as e:
            ok, err = False, type(e).__name__
        if ok:
            sent += 1
        else:
            failed += 1
            errors[err] = errors.get(err, 0) + 1
        await asyncio.sleep(settings.campaign_delay)
    lines = [f"📣 خلصت حملة {tpl.name} (#{cid})",
             f"✅ انبعت: {sent}", f"⏭️ مستبعد (طلب إيقاف): {skipped}", f"❌ فشل: {failed}"]
    for e, n in list(errors.items())[:5]:
        lines.append(f"  • {n}× {e}")
    lines.append("تابع النتائج بـ /campaign_report")
    await notify("\n".join(lines))
