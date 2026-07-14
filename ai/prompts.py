"""
提示词(prompt)相关:AI 的人设,以及把上下文(意图/线索/FAQ)拼进系统提示的辅助函数。

注意:注释是中文(方便维护),但真正发给模型的 prompt 文本一律英文(默认语言英文)。
多语言策略:让模型"跟随用户最后一条消息的语言"回复,识别不出时兜底用英文。
"""

# AI 的人设与目标。这是每次对话都放在最前面的"系统提示"。文本用英文;默认语言英文。
PERSONA = """You are the website assistant for GMIC AI — an AI-voice-hardware ODM/OEM \
company that designs and manufactures custom AI voice devices (AI voice recorders, \
wearable microphones, smart badges, AI scribe devices, Bluetooth/Wi-Fi audio hardware, \
and fully custom voice-capture devices).

Your goals, in order:
1. Help the visitor concisely, warmly, and professionally.
2. Before the conversation wraps up, make sure you have the visitor's NAME and at least \
ONE contact method (email OR phone/WhatsApp). If it's missing, ask for it naturally in \
context (e.g. "Leave your email and I'll have our ODM team send you a detailed proposal"); \
never interrogate.
3. LANGUAGE: reply in the SAME language as the visitor's latest message. If the language \
is unclear or the message is empty, default to English.

Style: short (2-4 sentences), friendly, no markdown headers, no long bullet lists."""


def intent_line(intent):
    """
    根据按钮种下的意图,给 AI 一句英文上下文提示,让它顺着话题聊。

    例:用户点了 [🏭 ODM] → intent="odm" → 返回一句提示告诉模型"他想做定制,主动问产品/数量/交期"。
        这样 AI 一上来就问对问题,不会再泛泛问 "what can I help you with"。
    """
    labels = {
        "odm": "The visitor came in via the ODM/OEM custom-manufacturing button. Assume they "
               "want custom hardware; proactively ask about product, quantity, and timeline.",
        "products": "The visitor is browsing products.",
        "demo": "The visitor wants to book a demo/call.",
    }
    return labels.get(intent, "")


def faq_reference(faq):
    """
    把 FAQ 的标准答案(英文)喂给模型,让它"自由回答"时口径和按钮里的写死答案保持一致。
    只喂已填好的,TODO 占位的跳过。
    """
    lines = []
    for item in faq or []:
        a = item.get("a", "")
        if a and not a.startswith("TODO"):
            lines.append(f"- Q: {item['q']}\n  A: {a}")
    return ("Reference facts — keep your answers consistent with these:\n" + "\n".join(lines)) if lines else ""


def lead_line(lead):
    """
    把"目前已知访客哪些信息、还缺什么"用英文告诉模型,方便它决定要不要追问联系方式。

    例:lead={email:"a@x.com", missing:["name"]} →
        返回 "Known about the visitor: {'email': 'a@x.com'}. Still missing (try to obtain): ['name']"
    """
    if not lead:
        return "Known about the visitor: nothing yet."
    known = {k: v for k, v in lead.items() if k != "missing" and v}
    missing = lead.get("missing", [])
    parts = [f"Known about the visitor: {known or 'nothing yet'}."]
    if missing:
        parts.append(f"Still missing (try to obtain): {missing}.")
    return " ".join(parts)
