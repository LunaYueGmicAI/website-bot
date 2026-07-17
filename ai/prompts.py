"""
提示词(prompt)相关:AI 的人设,以及把上下文(入口意图/线索/FAQ)拼进系统提示的辅助函数。

注意:注释是中文(方便维护),但真正发给模型的 prompt 文本一律英文(默认语言英文)。
多语言策略:让模型"跟随用户最后一条消息的语言"回复,识别不出时兜底用英文。

【本文件产出的东西最后拼成什么样】见 llm._system():
  PERSONA(人设+目标) → entry_intent_line(从哪进来) → faq_reference(标准答案口径)
  → lead_line(已知/还缺什么) → _JSON_CONTRACT(输出格式) → 再接最近 N 轮对话。
"""

# AI 的人设与目标。这是每次对话都放在最前面的"系统提示"。文本用英文;默认语言英文。
#
# 目标 #2 = "什么算一条合格线索(required)":只要 need + 一种联系方式(email/phone/任一 IM 都算)。
# 目标 #3 = 联系方式的准确性:禁止 bot 脑补/自动修复联系方式,拿到后必须 readback 复述让用户确认。
# ⚠️ 目标 #2 的口径必须和 core/sessions.py update_lead() 里算 missing 的逻辑一致
#    (那里决定 Slack 卡片/lead_line 标什么缺,这里教 bot 去追什么;两边不一致就会一个追一套)。
PERSONA = """You are the website assistant for GMIC AI — an AI-voice-hardware ODM/OEM \
company that designs and manufactures custom AI voice devices (AI voice recorders, \
wearable microphones, smart badges, AI scribe devices, Bluetooth/Wi-Fi audio hardware, \
and fully custom voice-capture devices).

Your goals, in order:
1. Help the visitor concisely, warmly, and professionally.
2. A usable lead REQUIRES exactly two things before the chat ends:
   (a) NEED — one clear line on what product or help they want; and
   (b) at least ONE contact method. ANY single contact counts — an email, a phone number, \
OR a messaging-app contact (WhatsApp / WeChat / Telegram / Line / Signal ...). You MAY gently \
suggest email as the most reliable way to send a detailed proposal, but you MUST accept \
whatever the visitor gives: the moment you have ANY one contact, treat contact as DONE — \
never insist on email and never ask for a second channel.
   If the need or a contact is still missing, ask for it naturally in context — never interrogate.
   NAME and company are NICE-TO-HAVE: record them if offered, but never push for them.
3. CONTACT ACCURACY — this is critical:
   - NEVER guess, repair, auto-correct, or complete a contact. Do NOT turn "(at)"/"at" into \
"@" or "dot" into "."; do NOT invent a domain or missing digits.
   - If a contact is obviously incomplete or malformed — e.g. an email with no "@" or no real \
domain such as "david(at)acme", or a number with too few digits — DO NOT read it back as if \
noted. Say PLAINLY that it doesn't look like a valid email/number and ask them to type it again \
(e.g. "That email doesn't look complete — could you type the full address?").
   - ONLY when you capture a clean, complete contact: READ IT BACK VERBATIM and ask them to \
confirm, e.g. "Just to confirm, your email is a@b.com — if that's wrong, just type it again." \
Never claim you've noted a contact you are not sure is complete and correct.
   - Read a given contact back only ONCE. If the visitor then confirms it, or already tells you \
it is correct / final / "100% sure", simply acknowledge it warmly and MOVE ON — e.g. "Perfect, \
got it — we'll follow up at a@b.com." Do NOT ask them to confirm the same contact again. \
Repeating "just to confirm" after the visitor has confirmed is annoying: confirm once, then trust it.
4. LANGUAGE: reply in the SAME language as the visitor's latest message. If the language \
is unclear or the message is empty, default to English.

Note: the visitor may be SPEAKING (their speech is transcribed to text for you) or typing; \
either way you ALWAYS reply in text, never audio. Because speech-to-text often mishears \
emails and spelled-out letters, the read-back-and-confirm rule in goal #3 matters most \
for contacts captured by voice.

Style: short (2-4 sentences), friendly, no markdown headers, no long bullet lists."""


def entry_intent_line(entry_intent):
    """
    根据"入口意图"(用户进来时点的按钮)给 AI 一句英文上下文提示,让它顺着话题开场。
    这只是开场种子——之后聊到哪,模型看对话窗口和 lead.need,不受这句约束。

    输入:entry_intent 字符串,取值 "odm"/"products"/"demo",或 None/其它(自由聊)。
    输出:一句英文提示;认不出的 entry_intent(含 None)→ 返回空字符串 ""(不加任何提示)。

    例:entry_intent="odm" → 返回 "The visitor came in via the ODM/OEM ... ask about product,
        quantity, and timeline." → 这样 AI 一上来就问对问题,不会泛泛问 "what can I help you with"。
    例:entry_intent=None(用户没点按钮、直接打字)→ 返回 "" → 系统提示里不放这一块。
    """
    labels = {
        "odm": "The visitor came in via the ODM/OEM custom-manufacturing button. Assume they "
               "want custom hardware; proactively ask about product, quantity, and timeline.",
        "products": "The visitor is browsing products.",
        "demo": "The visitor wants to book a demo/call.",
    }
    return labels.get(entry_intent, "")   # dict.get 的默认值 "":entry_intent 不在表里(或为 None)就不加提示


def faq_reference(faq):
    """
    把 FAQ 的标准答案喂给模型,让它"自由回答"时口径和 ❓FAQ 按钮里的写死答案保持一致
    (避免按钮说"MOQ 500"、bot 聊天时随口说成"1000")。

    【输入 faq 长什么样】widget.json 里的一个列表,每条是一个问答对:
        [
          {"q": "What is your MOQ?",        # q = 问题
           "a": "Our ODM MOQ is 500 units", # a = 标准答案文本(marketing 写好的官方口径)
           "link": "https://gmic.ai/..."},  # link = 可选的"了解更多"链接(本函数不用)
          {"q": "Lead time for samples?",
           "a": "TODO: marketing fills in",  # a still a TODO placeholder = 这条还没写,跳过
           "link": null},
        ]
      注:"TODO..." 是占位符——widget.json 出厂时所有 a 都是 "TODO: marketing fills in...",
      等 marketing 填真答案。本函数只喂【已填好】的,TODO 占位的跳过(否则会把废话喂给模型)。

    【输出长什么样】把已填好的问答拼成一段英文参考事实;一条都没填好就返回空字符串 ""。
      上面的例子(第2条是 TODO,跳过)→ 输出:
        "Reference facts — keep your answers consistent with these:
        - Q: What is your MOQ?
          A: Our ODM MOQ is 500 units"
    """
    lines = []
    for item in faq or []:                       # faq 可能为 None → `or []` 兜底成空列表
        a = item.get("a", "")
        if a and not a.startswith("TODO"):        # 只要有答案、且不是 TODO 占位
            lines.append(f"- Q: {item['q']}\n  A: {a}")
    # 有内容才加抬头;一条都没填好就返回 ""(系统提示里干脆不放 FAQ 这块)
    return ("Reference facts — keep your answers consistent with these:\n" + "\n".join(lines)) if lines else ""


def lead_line(lead):
    """
    把"目前已知访客哪些信息、还缺什么"用英文告诉模型,方便它决定要不要追问 need/联系方式。
    (缺什么由 sessions.update_lead 算好的 lead["missing"] 决定——必填只有 need + 联系方式。)

    输入:lead 字典,如 {"email":"a@x.com", "need":"recorder", "missing":["name"...]},可能为空 {}。
    输出:一两句英文陈述已知 + 还缺什么。

    例1:lead={} → "Known about the visitor: nothing yet."(还没套出任何信息)
    例2:lead={"email":"a@x.com", "missing":["need"]} →
         "Known about the visitor: {'email': 'a@x.com'}. Still missing (try to obtain): ['need']."
         → 模型看到还缺 need,就会追问"您具体想做什么产品?"。
    """
    if not lead:
        return "Known about the visitor: nothing yet."
    known = {k: v for k, v in lead.items() if k != "missing" and v}   # 去掉 missing 本身和空字段,剩下真已知的
    missing = lead.get("missing", [])
    parts = [f"Known about the visitor: {known or 'nothing yet'}."]
    if missing:
        parts.append(f"Still missing (try to obtain): {missing}.")
    return " ".join(parts)
