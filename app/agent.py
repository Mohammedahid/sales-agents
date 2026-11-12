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
    if name:
        system.append({"type": "text", "text":
                       f"اسم ملف العميل على واتساب: {name} (ممكن ما يكون اسمه الحقيقي، لا تناديه فيه إلا إذا عرّف بنفسه بهالاسم). رقمه: {customer}"})

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
                handoff = dict(b.input)
                out = "تم إبلاغ الفريق. طمّن العميل إن الموظف بيكمل معه هنا."
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
[[LEAD: الاسم | الجهة | المدينة | الاحتياج]]
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


async def _run_gemini(brand, customer, name, system_text, messages):
    msgs = [{"role": "system", "content": system_text + GEMINI_TAGS_NOTE}] + _to_openai(messages)
    msg = await _gemini_with_fallback(msgs)
    text = msg.get("content") or ""
    handoff = None
    for kind, body in _TAG_RE.findall(text):
        parts = [x.strip() for x in body.split("|")]
        if kind == "LEAD":
            keys = ["name", "company", "city", "need"]
            db.save_lead(brand.key, customer,
                         {**{k: v for k, v in zip(keys, parts) if v}, "whatsapp_name": name})
        else:
            handoff = {"reason": parts[0] if parts else "", "summary": " | ".join(parts[1:])}
    reply = _TAG_RE.sub("", text).strip()
    return reply, handoff
