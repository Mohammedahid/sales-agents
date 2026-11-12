"""FastAPI app: WhatsApp webhook + background Telegram bot / monitors."""
import asyncio
import logging
import random
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from . import campaign, db, telegram_bot as tg, whatsapp
from .agent import run_sales_agent
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")
logging.getLogger("httpx").setLevel(logging.WARNING)  # don't print URLs that contain the bot token

_pending: dict = {}  # (brand, customer) -> asyncio.Task (debounce timers)


@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(c()) for c in (tg.polling_loop, tg.daily_report_loop, tg.health_loop)]
    log.info("started. brands: %s", {k: bool(b.phone_number_id) for k, b in settings.brands.items()})
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return (Path(__file__).parent / "privacy.html").read_text(encoding="utf-8")


@app.get("/health")
async def healthcheck():
    return {"ok": True}


@app.get("/webhook/whatsapp")
async def verify(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == settings.wa_verify_token:
        return Response(content=p.get("hub.challenge", ""), media_type="text/plain")
    return Response(status_code=403)


@app.post("/webhook/whatsapp")
async def receive(request: Request):
    raw = await request.body()
    if not whatsapp.verify_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        return Response(status_code=401)
    body = await request.json()
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            try:
                await _handle_change(change.get("field"), change.get("value", {}))
            except Exception:
                log.exception("failed to handle change")
    return {"ok": True}  # always 200 fast, so Meta doesn't retry


async def _handle_change(field, value):
    phone_id = value.get("metadata", {}).get("phone_number_id", "")
    brand = settings.brand_by_phone_id(phone_id)
    if not brand:
        log.warning("unknown phone_number_id %s", phone_id)
        return

    # A human replied from the WhatsApp Business app (Coexistence) → bot steps back.
    if field == "smb_message_echoes":
        for echo in value.get("message_echoes", []):
            if db.already_processed(echo.get("id", "")):
                continue
            customer = echo.get("to", "")
            text = (echo.get("text") or {}).get("body", "")
            if text:
                db.add_message(brand.key, customer, "assistant", text)
            db.pause(brand.key, customer, settings.human_pause_hours, "رد موظف من التطبيق")
        return

    if field != "messages":
        return
    for st in value.get("statuses", []):
        err = (st.get("errors") or [{}])[0].get("title", "") if st.get("errors") else ""
        db.update_campaign_status(st.get("id", ""), st.get("status", ""), err)
    names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
    for m in value.get("messages", []):
        if db.already_processed(m["id"]):
            continue
        customer = m["from"]
        db.set_contact_name(brand.key, customer, names.get(customer, ""))
        blocks, preview = await _to_blocks(brand, m)
        if not blocks:
            continue
        db.add_message(brand.key, customer, "user", blocks)
        was_campaign = db.mark_campaign_reply(brand.key, customer)

        if campaign.is_stop(preview):
            db.opt_out(brand.key, customer)
            await whatsapp.send_text(brand, customer, campaign.OPTOUT_REPLY)
            db.add_message(brand.key, customer, "assistant", campaign.OPTOUT_REPLY)
            continue
        if was_campaign and campaign.is_interest(preview):
            await tg.broadcast(f"🔥 عميل مهتم من حملة {brand.name}: {names.get(customer, '')} +{customer}\n"
                               f"البوت بيكمل معه الحين. https://wa.me/{customer}")

        if db.is_paused(brand.key, customer):
            await tg.notify_paused_message(brand, customer, preview, names.get(customer, ""))
            continue
        _schedule_reply(brand, customer, m["id"])


async def _to_blocks(brand, m):
    t = m.get("type")
    if t == "text":
        body = m["text"]["body"]
        return [{"type": "text", "text": body}], body
    if t == "image":
        caption = m["image"].get("caption", "")
        img = await whatsapp.download_image_b64(brand, m["image"]["id"])
        blocks = []
        if img:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": img[0], "data": img[1]}})
        blocks.append({"type": "text", "text": caption or "(العميل أرسل صورة)"})
        return blocks, f"[صورة] {caption}"
    if t == "button":
        return [{"type": "text", "text": m["button"].get("text", "")}], m["button"].get("text", "")
    if t == "interactive":
        i = m["interactive"]
        title = (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
        return [{"type": "text", "text": title}], title
    if t in ("audio", "video", "document", "sticker", "location", "contacts"):
        note = {
            "audio": "(العميل أرسل رسالة صوتية — ما تقدر تسمعها. اطلب منه بلطف يكتب طلبه كتابة، أو اعرض تحويله لموظف)",
            "location": "(العميل أرسل موقعه)",
        }.get(t, f"(العميل أرسل {t})")
        return [{"type": "text", "text": note}], f"[{t}]"
    return None, ""


def _schedule_reply(brand, customer, last_msg_id):
    """Wait a few seconds so several quick messages get one combined answer."""
    key = (brand.key, customer)
    old = _pending.get(key)
    if old and not old.done():
        old.cancel()
    _pending[key] = asyncio.create_task(_reply_after_delay(brand, customer, last_msg_id))


async def _reply_after_delay(brand, customer, last_msg_id):
    key = (brand.key, customer)
    try:
        await asyncio.sleep(settings.debounce_seconds)
    except asyncio.CancelledError:
        return
    _pending.pop(key, None)  # past this point we don't get cancelled
    await whatsapp.mark_read_typing(brand, last_msg_id)
    try:
        reply, handoff = await run_sales_agent(brand, customer)
    except Exception:
        log.exception("agent failed")
        reply, handoff = None, {"reason": "خطأ تقني بالبوت", "summary": "البوت ما قدر يرد، تابع العميل يدوياً"}

    if reply:
        await asyncio.sleep(random.uniform(1.0, 3.0))  # feel natural, not instant
        await whatsapp.send_text(brand, customer, reply)
        db.add_message(brand.key, customer, "assistant", reply)

    if handoff:
        db.pause(brand.key, customer, settings.human_pause_hours, handoff.get("reason", ""))
        db.add_handoff(brand.key, customer, handoff.get("reason", ""), handoff.get("summary", ""))
        await tg.notify_handoff(brand, customer, handoff.get("reason", ""), handoff.get("summary", ""),
                                db.contact_name(brand.key, customer))
