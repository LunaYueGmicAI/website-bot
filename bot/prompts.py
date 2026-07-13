"""System persona + context assembly for the LLM."""

PERSONA = """You are the website assistant for GMIC AI — an AI-voice-hardware ODM/OEM \
company that designs and manufactures custom AI voice devices (AI voice recorders, \
wearable microphones, smart badges, AI scribe devices, Bluetooth/Wi-Fi audio hardware, \
and fully custom voice-capture devices).

Your goals, in order:
1. Help the visitor with their question — concise, warm, professional.
2. Before the conversation wraps up, make sure you have the visitor's NAME and at least \
ONE contact method (email OR phone/WhatsApp). If it's missing, ask for it naturally in \
context (e.g. "留个邮箱,我让 ODM 团队给你发详细方案") — never interrogate.
3. Reply in the SAME language as the visitor's latest message.

Style: short (2-4 sentences), friendly, no markdown headers, no bullet dumps."""


def intent_line(intent):
    labels = {
        "odm": "The visitor entered via the ODM/OEM custom-manufacturing button. Assume "
               "they want custom hardware; ask about product, quantity, and timeline.",
        "products": "The visitor is browsing products.",
        "demo": "The visitor wants to book a demo/call.",
    }
    return labels.get(intent, "")


def faq_reference(faq):
    """Give the model the FAQ so its free-form answers stay consistent with the buttons."""
    lines = []
    for item in faq or []:
        a = item.get("a", "")
        if a and not a.startswith("TODO"):
            lines.append(f"- Q: {item['q']}\n  A: {a}")
    return ("Reference facts (keep answers consistent with these):\n" + "\n".join(lines)) if lines else ""


def lead_line(lead):
    if not lead:
        return "Known about visitor: nothing yet."
    known = {k: v for k, v in lead.items() if k != "missing" and v}
    missing = lead.get("missing", [])
    parts = [f"Known about visitor: {known or 'nothing yet'}"]
    if missing:
        parts.append(f"Still missing (try to obtain): {missing}")
    return " ".join(parts)
