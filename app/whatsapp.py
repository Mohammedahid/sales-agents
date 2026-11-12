"""WhatsApp Cloud API helpers."""
import base64
import hashlib
import hmac
import logging

import httpx

from .config import Brand, settings

log = logging.getLogger("whatsapp")
_http = httpx.AsyncClient(timeout=30)


def _url(path: str) -> str:
    return f"https://graph.facebook.com/{settings.graph_version}/{path}"


def verify_signature(raw_body: bytes, header: str) -> bool:
    """Check X-Hub-Signature-256 when WA_APP_SECRET is set."""
    if not settings.wa_app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.wa_app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


async def send_text(brand: Brand, to: str, text: str):
    r = await _http.post(
        _url(f"{brand.phone_number_id}/messages"),
        headers={"Authorization": f"Bearer {brand.access_token}"},
        json={"messaging_product": "whatsapp", "to": to, "type": "text",
              "text": {"body": text, "preview_url": False}},
    )
    if r.status_code >= 300:
        log.error("send_text failed %s: %s", r.status_code, r.text)
    return r


async def mark_read_typing(brand: Brand, message_id: str):
    """Blue ticks + 'typing…' indicator while the agent thinks."""
    try:
        await _http.post(
            _url(f"{brand.phone_number_id}/messages"),
            headers={"Authorization": f"Bearer {brand.access_token}"},
            json={"messaging_product": "whatsapp", "status": "read",
                  "message_id": message_id, "typing_indicator": {"type": "text"}},
        )
    except Exception as e:  # never block a reply on this
        log.warning("typing indicator failed: %s", e)


async def download_image_b64(brand: Brand, media_id: str):
    """Return (media_type, base64) for an incoming image, or None."""
    try:
        h = {"Authorization": f"Bearer {brand.access_token}"}
        meta = (await _http.get(_url(media_id), headers=h)).json()
        data = (await _http.get(meta["url"], headers=h)).content
        mime = meta.get("mime_type", "image/jpeg").split(";")[0]
        if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            return None
        return mime, base64.b64encode(data).decode()
    except Exception as e:
        log.warning("image download failed: %s", e)
        return None


async def upload_media(brand: Brand, path, mime="application/pdf"):
    """Upload a file (e.g. profile PDF) to WhatsApp; returns media id or None."""
    try:
        data = open(path, "rb").read()
        r = await _http.post(
            _url(f"{brand.phone_number_id}/media"),
            headers={"Authorization": f"Bearer {brand.access_token}"},
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (path.name, data, mime)},
            timeout=120,
        )
        if r.status_code >= 300:
            log.error("upload_media failed %s: %s", r.status_code, r.text)
            return None
        return r.json().get("id")
    except Exception as e:
        log.error("upload_media error: %s", e)
        return None


async def send_template(brand: Brand, to: str, tpl, name: str, media_id=None):
    components = []
    if tpl.header and media_id:
        media = {"id": media_id}
        if tpl.header == "document":
            media["filename"] = f"{brand.name}.pdf"
        components.append({"type": "header", "parameters": [{"type": tpl.header, tpl.header: media}]})
    if tpl.has_name:
        components.append({"type": "body", "parameters": [{"type": "text", "text": name}]})
    payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
               "template": {"name": tpl.name, "language": {"code": brand.template_lang},
                            "components": components}}
    r = await _http.post(_url(f"{brand.phone_number_id}/messages"),
                         headers={"Authorization": f"Bearer {brand.access_token}"}, json=payload)
    if r.status_code >= 300:
        log.error("send_template failed %s: %s", r.status_code, r.text)
    return r
