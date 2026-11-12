"""Telegram admin bot (long polling — no webhook/domain needed).

Commands:
  /status    حالة المواقع + إحصائيات اليوم
  /report    التقرير اليومي الآن
  /leads     آخر العملاء المحتملين
  /handoffs  المحادثات المحوّلة للموظفين
  /paused    المحادثات اللي البوت موقف فيها
  /test_seen    جرّب كأنك زبون على رقم سين
  /test_nvolve  جرّب كأنك زبون على رقم Nvolve
  /endtest      اطلع من وضع التجربة

الحملات (رسالة تعريفية + ملف أو فيديو):
  /templates   الرسائل الجاهزة (seen_intro، seen_restaurants، nvolve_intro…)
  /intro seen_restaurants 0551234567 محمد   رسالة لرقم واحد
  /campaign seen_restaurants   + الأرقام تحت (كل سطر: الاسم، الرقم)
  أو ابعت ملف CSV/TXT وبالتعليق: /campaign seen_restaurants
  (seen لحالها = seen_intro)
  /confirm   ابدأ الإرسال  —  /cancel  إلغاء
  /campaign_report   نتائج آخر الحملات
Any other text → AI answer about the products & numbers.
Reply (Telegram "Reply") to a handoff alert → your text is sent to the customer on WhatsApp.
"""
import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from . import db, health, whatsapp
from .config import settings

log = logging.getLogger("telegram")
API = f"https://api.telegram.org/bot{settings.telegram_token}"
_http = httpx.AsyncClient(timeout=60)
_pending_campaign: dict = {}  # admin chat_id -> (brand_key, contacts)
_test_mode: dict = {}  # admin chat_id -> brand key (simulate a WhatsApp customer)


async def send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = await _http.post(f"{API}/sendMessage", json=payload)
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        log.error("telegram send failed: %s", e)


async def broadcast(text, reply_markup=None):
    ids = []
    for admin in settings.telegram_admins:
        mid = await send(admin, text, reply_markup)
        if mid:
            ids.append(mid)
    return ids


# ---- alerts called from the WhatsApp side -----------------------------------
async def notify_handoff(brand, customer, reason, summary, wa_name=""):
    text = (
        f"🔔 تحويل لموظف — {brand.name}\n"
        f"العميل: {wa_name or '-'} (+{customer})\n"
        f"السبب: {reason}\n\nالملخص: {summary}\n\n"
        f"افتح المحادثة: https://wa.me/{customer}\n"
        f"↩️ أو اعمل Reply على هالرسالة وبنبعت ردك للعميل من رقم {brand.name}."
    )
    kb = {"inline_keyboard": [[{"text": "▶️ رجّع البوت يرد", "callback_data": f"resume|{brand.key}|{customer}"}]]}
    for mid in await broadcast(text, kb):
        db.link_tg(mid, brand.key, customer)


async def notify_paused_message(brand, customer, preview, wa_name=""):
    text = f"💬 رسالة جديدة (البوت موقف) — {brand.name}\n{wa_name or ''} +{customer}:\n{preview[:500]}"
    for mid in await broadcast(text):
        db.link_tg(mid, brand.key, customer)


# ---- report -----------------------------------------------------------------
async def build_live_context(hours=24):
    results = await health.check_all()
    st = db.stats(time.time() - hours * 3600)
    lines = [health.format_results(results), "", f"آخر {hours} ساعة:"]
    for key, s in st.items():
        b = settings.brands[key]
        lines.append(
            f"• {b.name}: {s['chats']} عميل، {s['messages']} رسالة، "
            f"{s['leads']} عميل محتمل، {s['handoffs']} تحويل"
        )
    paused = db.paused_list()
    lines.append(f"• محادثات مع موظف حالياً: {len(paused)}")
    return "\n".join(lines)


async def daily_report_text():
    return "📊 التقرير اليومي\n\n" + await build_live_context(24)


# ---- command handling -------------------------------------------------------
async def handle_update(u):
    if "callback_query" in u:
        cq = u["callback_query"]
        if cq["from"]["id"] not in settings.telegram_admins:
            return
        action, brand_key, customer = cq["data"].split("|")
        if action == "resume":
            db.resume(brand_key, customer)
            await _http.post(f"{API}/answerCallbackQuery",
                             json={"callback_query_id": cq["id"], "text": "رجع البوت يرد ✅"})
            await send(cq["message"]["chat"]["id"], f"✅ البوت رجع يرد على +{customer}")
        return

    msg = u.get("message")
    if not msg or ("text" not in msg and "document" not in msg):
        return
    chat_id = msg["chat"]["id"]
    if msg["from"]["id"] not in settings.telegram_admins:
        await send(chat_id, f"غير مصرح. رقمك: {msg['from']['id']}")
        return
    if "document" in msg:
        await _handle_campaign_file(chat_id, msg)
        return
    text = msg["text"].strip()

    # Reply to an alert → relay to WhatsApp customer
    reply_to = msg.get("reply_to_message")
    if reply_to:
        target = db.tg_target(reply_to["message_id"])
        if target:
            brand = settings.brands[target[0]]
            r = await whatsapp.send_text(brand, target[1], text)
            if r.status_code < 300:
                db.add_message(brand.key, target[1], "assistant", text)
                db.pause(brand.key, target[1], settings.human_pause_hours, "رد من تلجرام")
                await send(chat_id, "✅ انبعت للعميل")
            else:
                await send(chat_id, f"❌ ما انبعت ({r.status_code}). ممكن نافذة الـ24 ساعة سكرت.")
            return

    cmd = text.split()[0].split("@")[0].lower()
    if cmd in ("/campaign", "/intro", "/confirm", "/cancel", "/campaign_report", "/templates"):
        await _handle_campaign_cmd(chat_id, cmd, text)
        return
    if cmd in ("/test_seen", "/test_nvolve"):
        key = cmd.split("_")[1]
        _test_mode[chat_id] = key
        db.resume(key, f"test{chat_id}")
        await send(chat_id, f"🧪 وضع التجربة: إنت هلأ زبون على رقم {settings.brands[key].name}. اكتب زي ما بيكتب الزبون. للخروج: /endtest")
        return
    if cmd == "/endtest":
        _test_mode.pop(chat_id, None)
        await send(chat_id, "✅ طلعت من وضع التجربة.")
        return
    if chat_id in _test_mode and not text.startswith("/"):
        from .agent import run_sales_agent
        brand = settings.brands[_test_mode[chat_id]]
        cust = f"test{chat_id}"
        db.add_message(brand.key, cust, "user", [{"type": "text", "text": text}])
        await _http.post(f"{API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
        try:
            reply, handoff = await run_sales_agent(brand, cust)
        except Exception as e:
            log.exception("test agent failed")
            await send(chat_id, f"صار خطأ: {type(e).__name__} {e}"[:1500])
            return
        if reply:
            db.add_message(brand.key, cust, "assistant", reply)
            await send(chat_id, f"{brand.agent_name}: {reply}")
        if handoff:
            await send(chat_id, f"🔔 (تجربة) هون كان البوت رح يحوّل لموظف.\nالسبب: {handoff.get('reason')}\nالملخص: {handoff.get('summary')}")
        return
    if cmd in ("/start", "/help"):
        await send(chat_id, __doc__)
    elif cmd == "/status":
        await send(chat_id, await build_live_context(24))
    elif cmd == "/report":
        await send(chat_id, await daily_report_text())
    elif cmd == "/leads":
        rows = db.recent_leads(10)
        if not rows:
            await send(chat_id, "ما في عملاء محتملين لسا.")
        else:
            out = []
            for r in rows:
                d = r["data"]
                when = datetime.fromtimestamp(r["ts"], ZoneInfo(settings.timezone)).strftime("%m-%d %H:%M")
                out.append(f"• [{settings.brands[r['brand']].name}] {d.get('name') or d.get('whatsapp_name') or '-'} "
                           f"| {d.get('company', '')} | {d.get('need', '')} | +{r['customer']} | {when}")
            await send(chat_id, "\n".join(out))
    elif cmd == "/handoffs":
        rows = db.open_handoffs()
        await send(chat_id, "\n\n".join(
            f"• [{settings.brands[r['brand']].name}] +{r['customer']}\n{r['reason']}: {r['summary']}" for r in rows
        ) or "ما في تحويلات مفتوحة.")
    elif cmd == "/paused":
        rows = db.paused_list()
        await send(chat_id, "\n".join(
            f"• [{settings.brands[r['brand']].name}] +{r['customer']} — {r['reason']}" for r in rows
        ) or "البوت شغال على كل المحادثات.")
    else:
        from .agent import run_admin_agent
        await _http.post(f"{API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
        try:
            answer = await run_admin_agent(text, await build_live_context(24))
        except Exception as e:
            log.exception("admin agent failed")
            answer = f"صار خطأ: {type(e).__name__} {e}"[:1500]
        await send(chat_id, answer)


async def polling_loop():
    if not settings.telegram_token:
        log.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled")
        return
    offset = 0
    while True:
        try:
            r = await _http.get(f"{API}/getUpdates", params={"timeout": 50, "offset": offset})
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                try:
                    await handle_update(u)
                except Exception:
                    log.exception("telegram update failed")
        except Exception as e:
            log.warning("telegram polling error: %s", e)
            await asyncio.sleep(5)


async def daily_report_loop():
    tz = ZoneInfo(settings.timezone)
    sent_on = None
    while True:
        now = datetime.now(tz)
        if now.hour == settings.daily_report_hour and sent_on != now.date():
            sent_on = now.date()
            try:
                await broadcast(await daily_report_text())
            except Exception:
                log.exception("daily report failed")
        await asyncio.sleep(60)


async def health_loop():
    while True:
        try:
            results = await health.check_all()
            for r in health.changes(results):
                icon = "✅ رجع شغال" if r["ok"] else "🔴 واقف"
                await broadcast(f"{icon}: {r['brand']} — {r['url']} ({r['status']})")
        except Exception:
            log.exception("health loop failed")
        await asyncio.sleep(settings.health_interval_min * 60)


# ---- campaigns ----------------------------------------------------------------
def _tpl_arg(text):
    from . import campaign
    parts = text.split()
    return campaign.get_template(parts[1]) if len(parts) > 1 else None


_MEDIA_LABEL = {"document": "📎 مع ملف PDF", "video": "🎬 مع فيديو", "image": "🖼️ مع صورة", None: "نص فقط"}


async def _preview_campaign(chat_id, tpl, contacts):
    brand = tpl.brand
    if not contacts:
        await send(chat_id, "ما لقيت أرقام صالحة. كل سطر لازم يكون فيه رقم، مثلاً: محمد, 0551234567")
        return
    if not brand.phone_number_id:
        await send(chat_id, f"رقم واتساب {brand.name} مش مربوط (PHONE_NUMBER_ID فاضي بملف .env).")
        return
    opted = sum(1 for p, _ in contacts if db.is_opted_out(brand.key, p))
    _pending_campaign[chat_id] = (tpl.name, contacts)
    sample = "\n".join(f"• {n or '-'} +{p}" for p, n in contacts[:5])
    await send(chat_id,
               f"📋 حملة {brand.name} — قالب: {tpl.name} ({_MEDIA_LABEL.get(tpl.header)})\n"
               f"عدد الأرقام: {len(contacts)}  (منهم {opted} طلبوا إيقاف وما رح ينبعتلهم)\n{sample}\n"
               f"{'…' if len(contacts) > 5 else ''}\n\n"
               f"معاينة الرسالة:\n{tpl.text_for(contacts[0][1])}\n\n"
               f"⚠️ رسائل التسويق مدفوعة من Meta. اكتب /confirm للإرسال أو /cancel للإلغاء.")


async def _handle_campaign_file(chat_id, msg):
    from . import campaign
    caption = (msg.get("caption") or "").strip()
    tpl = _tpl_arg(caption) if caption.lower().startswith("/campaign") else None
    if not tpl:
        await send(chat_id, "ابعت الملف وبالتعليق (Caption) اكتب مثلاً: /campaign seen_restaurants\nشوف الأسماء بـ /templates")
        return
    try:
        info = (await _http.get(f"{API}/getFile", params={"file_id": msg["document"]["file_id"]})).json()
        path = info["result"]["file_path"]
        raw = (await _http.get(f"https://api.telegram.org/file/bot{settings.telegram_token}/{path}")).content
    except Exception as e:
        await send(chat_id, f"ما قدرت أنزّل الملف: {e}")
        return
    text = ""
    for enc in ("utf-8-sig", "cp1256", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    await _preview_campaign(chat_id, tpl, campaign.parse_contacts(text))


async def _handle_campaign_cmd(chat_id, cmd, text):
    from . import campaign
    if cmd == "/templates":
        rows = campaign.list_templates()
        await send(chat_id, "\n".join(
            f"• {t.name} — {t.brand.name} ({_MEDIA_LABEL.get(t.header)})" for t in rows
        ) or "ما في رسائل جاهزة بمجلد knowledge/templates")
    elif cmd == "/campaign":
        tpl = _tpl_arg(text)
        if not tpl:
            await send(chat_id, "اكتب مثلاً: /campaign seen_restaurants  وتحتها الأرقام، كل سطر: الاسم، الرقم\n"
                                "الأسماء المتاحة: /templates")
            return
        body = "\n".join(text.splitlines()[1:])
        await _preview_campaign(chat_id, tpl, campaign.parse_contacts(body))
    elif cmd == "/confirm":
        pending = _pending_campaign.pop(chat_id, None)
        tpl = campaign.get_template(pending[0]) if pending else None
        if not tpl:
            await send(chat_id, "ما في حملة جاهزة. ابدأ بـ /campaign seen_restaurants")
            return
        await send(chat_id, f"🚀 بدأ إرسال {tpl.name} لـ {len(pending[1])} رقم. ببعتلك تقرير لما يخلص.")
        asyncio.create_task(campaign.run_campaign(tpl, pending[1], lambda t: send(chat_id, t)))
    elif cmd == "/cancel":
        _pending_campaign.pop(chat_id, None)
        await send(chat_id, "تم الإلغاء.")
    elif cmd == "/intro":
        tpl = _tpl_arg(text)
        parts = text.split(maxsplit=3)
        phone = campaign.normalize_phone(parts[2]) if len(parts) > 2 else ""
        if not tpl or not phone:
            await send(chat_id, "اكتب مثلاً: /intro seen_restaurants 0551234567 محمد\nالأسماء المتاحة: /templates")
            return
        if not tpl.brand.phone_number_id:
            await send(chat_id, f"رقم واتساب {tpl.brand.name} مش مربوط.")
            return
        name = parts[3] if len(parts) > 3 else ""
        media_id = await campaign.prepare_media(tpl)
        ok, err = await campaign.send_intro(tpl, phone, name, media_id)
        await send(chat_id, f"✅ انبعتت ({tpl.name}) لـ +{phone}" if ok else f"❌ ما انبعتت: {err}")
    elif cmd == "/campaign_report":
        rows = db.campaign_report()
        if not rows:
            await send(chat_id, "ما في حملات لسا.")
            return
        out = []
        for r in rows:
            out.append(f"📣 #{r['id']} {r.get('template') or settings.brands[r['brand']].name} — {r['total']} رقم\n"
                       f"انقبل: {r['accepted']} | وصل: {r['delivered'] + r['read']} | قرأ: {r['read']} | "
                       f"فشل: {r['failed']}\nرد: {r['replied']} | طلب إيقاف: {r['optouts']}")
        await send(chat_id, "\n\n".join(out))
