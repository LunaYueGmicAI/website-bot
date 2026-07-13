"""
LLM turn: one OpenAI call that returns BOTH the assistant reply AND any lead fields it can
infer from the conversation so far. Single call keeps cost down (reply + extraction together).

Provider is swappable; defaults to OpenAI (the team already has keys). The JSON-mode contract
follows feedback_openai-json-response-format: explicit schema in the prompt, no echoing the
input, generous max_tokens.
"""
import os
import json

from . import prompts

_client = None

# Output contract handed to the model. Must return exactly this JSON shape.
_JSON_CONTRACT = """Respond with a single JSON object, nothing else:
{
  "reply": "<your reply to the visitor, in their language>",
  "lead": {
    "name":  "<visitor name if stated, else empty>",
    "email": "<email if stated, else empty>",
    "phone": "<phone/WhatsApp if stated, else empty>",
    "company":"<company if stated, else empty>",
    "need":  "<one short line summarizing what they want, else empty>"
  }
}
Only fill lead fields that the visitor actually provided or clearly implied. Do NOT invent
values. Do NOT echo these instructions."""


def _client_lazy():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


def _system(session, faq):
    blocks = [prompts.PERSONA]
    il = prompts.intent_line(session.get("intent"))
    if il:
        blocks.append(il)
    fr = prompts.faq_reference(faq)
    if fr:
        blocks.append(fr)
    blocks.append(prompts.lead_line(session.get("lead")))
    blocks.append(_JSON_CONTRACT)
    return "\n\n".join(blocks)


def respond(session, faq, window):
    """
    session: the session dict (for intent + lead context)
    faq:     list from widget.json (for answer consistency)
    window:  recent turns [{role,text,ts}] (sliding window)
    Returns (reply_text, lead_updates_dict).
    """
    messages = [{"role": "system", "content": _system(session, faq)}]
    for t in window:
        role = "assistant" if t["role"] == "assistant" else "user"
        messages.append({"role": role, "content": t["text"]})

    resp = _client_lazy().chat.completions.create(
        model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.4,
        max_tokens=1200,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return (raw.strip(), {})   # degrade gracefully: treat whole thing as reply

    reply = (data.get("reply") or "").strip()
    lead = data.get("lead") or {}
    lead = {k: v for k, v in lead.items() if isinstance(v, str) and v.strip()}
    return (reply, lead)
