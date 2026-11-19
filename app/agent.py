"""The sales agent: Claude + tools (save_lead, request_human)."""
import json
import logging
import re

from anthropic import AsyncAnthropic

from . import db
from .config import Brand, settings
from .prompts import sales_prompt

log = logging.getLogger("agent")
client = AsyncAnthropic(api_key=settings.anthropic_api_key or "missing")

TOOLS = [
    {
        "name": "save_lead",
        "description": "احفظ/حدّث بيانات العميل المحتمل لما تعرف اسمه واحتياجه.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "company": {"type": "string"},
                "city": {"type": "string"},
                "need": {"type": "string", "description": "وش يبغى العميل بالضبط"},
                "budget": {"type": "string"},
                "timeline": {"type": "string"},
                "call_time": {"type": "string", "description": "الوقت اللي يناسب العميل نتصل فيه، زي ما قاله"},
                "notes": {"type": "string"},
            },
            "required": ["need"],
        },
    },
    {
        "name": "request_human",
        "description": "حوّل المحادثة لموظف حقيقي. البوت يوقف الرد على هذا العميل بعدها.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "سبب التحويل باختصار"},
                "summary": {"type": "string", "description": "ملخص المحادثة واحتياج العميل للموظف"},
            },
            "required": ["reason", "summary"],
        },
    },
]


def _normalize(history):
    """Merge consecutive same-role turns; ensure the list starts with a user turn."""
    out = []
    for m in history:
        content = m["content"]
        blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": m["role"], "content": list(blocks)})
    if out and out[0]["role"] == "assistant":
        # We started the conversation (intro campaign): keep our message as context.
        out.insert(0, {"role": "user", "content": [{"type": "text", "text":
                       "(ملاحظة للنظام: إحنا اللي بدأنا المحادثة بهالرسالة التعريفية، والعميل يرد عليها الحين)"}]})
    return out


async def run_sales_agent(brand: Brand, customer: str):
    """Returns (reply_text, handoff_dict_or_None)."""
    messages = _normalize(db.history(brand.key, customer))
    if not messages:
        return None, None
    name = db.contact_name(brand.key, customer)
    system = [{
        "type": "text",
        "text": sales_prompt(brand),
        "cache_control": {"type": "ephemeral"},
    }]
    # The WhatsApp profile name is NOT given to the model: it must ask the customer for his name.
    system.append({"type": "text", "text":
                   f"رقم العميل: {customer}. ما تعرف اسمه إلا إذا قاله لك بالمحادثة."})

    if settings.llm_provider == "gemini":
        sys_text = "\n\n".join(x["text"] for x in system)
        return await _run_gemini(brand, customer, name, sys_text, messages)

    handoff = None
    for _ in range(4):
        resp = await client.messages.create(
            model=settings.model, max_tokens=600, system=system,
            tools=TOOLS, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return text, handoff

        assistant_blocks = []
        for b in resp.content:
            if b.type == "text" and b.text:
                assistant_blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                assistant_blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        messages.append({"role": "assistant", "content": assistant_blocks})
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            if b.name == "save_lead":
                db.save_lead(brand.key, customer, {**b.input, "whatsapp_name": name})
                out = "تم الحفظ"
            elif b.name == "request_human":
                if _handoff_allowed(brand, customer, messages):
                    handoff = dict(b.input)
                    out = "تم إبلاغ الفريق. طمّن العميل إن الموظف بيكمل معه هنا."
                else:
                    out = "مرفوض: " + _collect_note(brand, customer)
            else:
                out = "أداة غير معروفة"
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        messages.append({"role": "user", "content": results})

    return "أبشر، لحظات وأرجع لك 🙏", handoff


async def run_admin_agent(question: str, live_context: str) -> str:
    from .prompts import ADMIN_PROMPT
    b = settings.brands
    system = ADMIN_PROMPT.format(
        seen_desc=b["seen"].description, nvolve_desc=b["nvolve"].description,
        seen_kb=b["seen"].knowledge[:20000] or "-", nvolve_kb=b["nvolve"].knowledge[:20000] or "-",
        live=live_context,
    )
    if settings.llm_provider == "gemini":
        msg = await _gemini_with_fallback([{"role": "system", "content": system},
                                           {"role": "user", "content": question}])
        return (msg.get("content") or "").strip()
    resp = await client.messages.create(
        model=settings.admin_model, max_tokens=1200, system=system,
        messages=[{"role": "user", "content": question}],
    )
    return "".join(x.text for x in resp.content if x.type == "text").strip()


# ---------------------------------------------------------------------------
# Google Gemini (free tier) via its OpenAI-compatible endpoint
# ---------------------------------------------------------------------------
import httpx  # noqa: E402

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
_gemini_http = httpx.AsyncClient(timeout=httpx.Timeout(45, connect=15))
OPENAI_TOOLS = [{"type": "function", "function": {
    "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in TOOLS]


async def _gemini_call(messages, tools=None, model=None):
    payload = {"model": model or settings.gemini_model, "messages": messages}
    if settings.gemini_reasoning:
        payload["reasoning_effort"] = settings.gemini_reasoning  # less "thinking" = faster replies
    if tools:
        payload["tools"] = tools
    r = await _gemini_http.post(GEMINI_URL, json=payload,
                                headers={"Authorization": f"Bearer {settings.gemini_api_key}"})
    if r.status_code >= 300:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:500]}")
    return r.json()["choices"][0]["message"]


def _to_openai(messages):
    out = []
    for m in messages:
        parts = []
        for b in m["content"]:
            if b.get("type") == "text":
                parts.append({"type": "text", "text": b["text"]})
            elif b.get("type") == "image":
                src = b["source"]
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
        if m["role"] == "assistant":
            out.append({"role": "assistant", "content": " ".join(p.get("text", "") for p in parts)})
        elif parts:
            out.append({"role": "user", "content": parts})
    return out


GEMINI_TAGS_NOTE = """
## مهم: طريقة الأدوات
ما عندك أدوات برمجية. بدل save_lead و request_human اكتب بآخر ردك سطر منفصل بهالشكل (العميل ما بيشوفه):
[[LEAD: الاسم | الجهة | المدينة | الاحتياج | وقت الاتصال]]
(اترك الخانة اللي ما تعرفها فاضية، واكتب وقت الاتصال زي ما قاله العميل)
[[HUMAN: سبب التحويل | ملخص المحادثة واحتياج العميل]]
اكتب السطر بس لما يلزم، وكل سطر مرة وحدة.
"""
_TAG_RE = re.compile(r"\[\[(LEAD|HUMAN):(.*?)\]\]", re.S)


async def _gemini_with_fallback(msgs):
    try:
        return await _gemini_call(msgs)
    except Exception as e:
        if not settings.gemini_fallback_model or settings.gemini_fallback_model == settings.gemini_model:
            raise
        log.warning("gemini main model failed (%s), trying fallback", type(e).__name__)
        return await _gemini_call(msgs, model=settings.gemini_fallback_model)


EXPLICIT_HUMAN_WORDS = ("موظف", "مسؤول", "شخص", "انسان", "إنسان", "اتصل", "اتصال", "كلمني", "مكالمة",
                        "عرض سعر", "سعر رسمي", "عقد", "فاتورة", "شكوى", "مدير", "أحد يكلمني", "احد يكلمني")
REQUIRED_LEAD = {"name": "اسمه", "company": "اسم نشاطه ونوعه", "city": "مدينته",
                 "call_time": "الوقت المناسب للاتصال"}


def _last_user_text(messages):
    for m in reversed(messages):
        if m["role"] == "user":
            return " ".join(b.get("text", "") for b in m["content"] if isinstance(b, dict))
    return ""


def _missing_lead(brand, customer):
    lead = db.latest_lead(brand.key, customer)
    return [label for key, label in REQUIRED_LEAD.items() if not lead.get(key)]


def _collect_note(brand, customer):
    """Instruction telling the model which interest-flow question to ask next."""
    missing = _missing_lead(brand, customer)
    if "اسمه" in missing:
        q = 'اسأله عن اسمه بهالصيغة: "هلا ومرحبا.. ممكن الأسم لا هنت؟"'
    elif "اسم نشاطه ونوعه" in missing or "مدينته" in missing:
        first = (db.latest_lead(brand.key, customer).get("name") or "").split()[:1]
        first = first[0] if first else ""
        q = (f'اسأله عن الناقص من ({"، ".join(m for m in missing if m != "الوقت المناسب للاتصال")}) بهالصيغة تقريباً: '
             f'"والنعم أخ {first}.. وش اسم نشاطكم وبأي مجال وأي مدينة؟" '
             '(إذا بنت قل "أخت"، وإذا ناقص المدينة بس اسأله "وبأي مدينة؟")')
    else:
        q = ('اكتب له بالضبط: "واضح.. اسمح لي بتواصل معك هاتفيا لإعداد الحساب المجاني. '
             'هل الوقت مناسب الان او تفضل وقت معين؟"')
    return ("لا تحوّل العميل الحين ولا تقول إن أحد بيتواصل معه. " + q)


def _handoff_allowed(brand, customer, messages):
    text = _last_user_text(messages)
    if any(w in text for w in EXPLICIT_HUMAN_WORDS):
        return True
    return not _missing_lead(brand, customer)


def _parse_tags(brand, customer, name, text):
    handoff = None
    for kind, body in _TAG_RE.findall(text):
        parts = [x.strip() for x in body.split("|")]
        if kind == "LEAD":
            keys = ["name", "company", "city", "need", "call_time"]
            db.save_lead(brand.key, customer,
                         {**{k: v for k, v in zip(keys, parts) if v and v not in ("-", "—", "؟")},
                          "whatsapp_name": name})
        else:
            handoff = {"reason": parts[0] if parts else "", "summary": " | ".join(parts[1:])}
    return _TAG_RE.sub("", text).strip(), handoff


async def _run_gemini(brand, customer, name, system_text, messages):
    msgs = [{"role": "system", "content": system_text + GEMINI_TAGS_NOTE}] + _to_openai(messages)
    msg = await _gemini_with_fallback(msgs)
    reply, handoff = _parse_tags(brand, customer, name, msg.get("content") or "")
    if handoff and not _handoff_allowed(brand, customer, messages):
        # Too early: ask again, this time collecting the missing info instead of handing off.
        msgs.append({"role": "user", "content":
                     f"(ملاحظة من النظام وليست من العميل: {_collect_note(brand, customer)} اكتب ردك للعميل فقط.)"})
        msg = await _gemini_with_fallback(msgs)
        reply, _ = _parse_tags(brand, customer, name, msg.get("content") or "")
        handoff = None
    return reply, handoff
