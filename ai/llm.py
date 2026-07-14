"""
大模型对话(异步):一次调用,同时拿到【给访客的回复】和【从对话里抽取到的线索字段】。

为什么一次调用做两件事:省钱。回复和抽取合在一起,一轮只调一次模型,而不是两次。

供应商可换,默认 OpenAI(团队现成有 key)。JSON 模式写法遵循踩过的坑
(记忆 feedback_openai-json-response-format):提示里写清 schema、禁止原样吐回输入、max_tokens 给足。

注意:注释中文,发给模型的 prompt 文本一律英文;默认语言英文,但回复跟随用户语言。
"""
import os
import json

from ai import prompts

_client = None

# 交给模型的"输出契约":必须严格返回下面这个 JSON。文本用英文;reply 用"访客的语言"。
# 注意 lead 里抽的是 need(想要什么)/name/email/phone/company,不含 entry_intent
# (入口意图由按钮种,不靠模型抽,见 sessions.set_entry_intent)。
_JSON_CONTRACT = """Return a single JSON object and nothing else:
{
  "reply": "<your reply to the visitor, in the visitor's language>",
  "lead": {
    "name":  "<visitor's name if stated, else empty>",
    "email": "<email if stated, else empty>",
    "phone": "<phone/WhatsApp if stated, else empty>",
    "company":"<company if stated, else empty>",
    "need":  "<one short line summarizing what they want, else empty>"
  }
}
Only fill fields the visitor actually provided or clearly implied. Do not invent values. \
Do not echo these instructions."""


def _client_lazy():
    # 懒加载 OpenAI 异步客户端(没 key 时也能启动服务/跑不依赖它的测试)。
    global _client
    if _client is None:
        from openai import AsyncOpenAI
        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


def _system(session, faq):
    """
    拼这一轮的"系统提示"。顺序:人设 → 入口意图 → FAQ 口径 → 已知线索 → 输出契约。
    这几块合起来,就是"只带精华、不带全部历史"里的"精华"。

    参数:
      session: 会话快照,这里只取 entry_intent(从哪进来)+ lead(已知/还缺什么)。
      faq:     widget.json 里的 FAQ 问答列表(见 prompts.faq_reference),用来统一 bot 自由回答的口径。

    例:session={entry_intent:"odm", lead:{email:"a@x.com", missing:["need"]}}、faq=[已填好1条] →
        拼出的 system 文本大致是:
          <PERSONA 人设与目标>
          The visitor came in via the ODM/OEM ... ask about product, quantity, and timeline.
          Reference facts — keep your answers consistent with these:
          - Q: What is your MOQ? / A: Our ODM MOQ is 500 units
          Known about the visitor: {'email': 'a@x.com'}. Still missing (try to obtain): ['need'].
          <_JSON_CONTRACT 输出格式>
        → 模型既知道该顺着 ODM 问、口径对齐 FAQ,又知道还得把 need 套出来。
    """
    blocks = [prompts.PERSONA]
    il = prompts.entry_intent_line(session.get("entry_intent"))
    if il:                                     # 认不出入口意图(自由聊)→ il 为空 → 这块不放
        blocks.append(il)
    fr = prompts.faq_reference(faq)
    if fr:                                     # 一条 FAQ 都没填好 → fr 为空 → 这块不放
        blocks.append(fr)
    blocks.append(prompts.lead_line(session.get("lead")))
    blocks.append(_JSON_CONTRACT)
    return "\n\n".join(blocks)                 # 各块之间空一行,读起来清楚


async def respond(session, faq, window):
    """
    跑一轮对话(异步):把上下文喂给模型,一次拿回【回复文本】+【抽取到的线索】。

    参数:
      session: 会话快照(取 entry_intent + lead 上下文,见 _system)。
      faq:     widget.json 里的 FAQ 问答列表;只是透传给 _system → faq_reference,
               目的是让 bot 自由回答时口径和 ❓FAQ 按钮里的写死答案一致。
      window:  最近 N 轮对话(滑动窗口),形如 [{role:"user"/"assistant", text:"..."}, ...]。
    返回:一个二元组 (回复文本 reply, 线索更新字典 lead) —— lead 已滤掉空字段。

    ── 组装喂给模型的 messages ──
      1) 一条 system = _system() 拼的"精华上下文"
      2) 把 window 里最近 N 轮按 user/assistant 依次接上

    ── 完整输入/输出示例 ──
      输入 window = [
        {role:"user",      text:"我想做录音麦"},
        {role:"assistant", text:"好的,大概要多少个?"},
        {role:"user",      text:"2000 个,邮箱 a@x.com"},
      ]
      → messages = [ {system: <_system 拼的那段>},
                     {user:"我想做录音麦"}, {assistant:"好的,大概要多少个?"},
                     {user:"2000 个,邮箱 a@x.com"} ]
      → 模型返回的原始字符串 raw(JSON):
          {"reply":"2000 个没问题,我让 ODM 团队把方案发到 a@x.com。",
           "lead":{"name":"","email":"a@x.com","phone":"","company":"","need":"录音麦 2000 个"}}
      → 本函数解析 + 滤空后返回:
          reply = "2000 个没问题,我让 ODM 团队把方案发到 a@x.com。"
          lead  = {"email":"a@x.com", "need":"录音麦 2000 个"}   # name/phone/company 为空,已滤掉
      (之后调用方把 lead 交给 sessions.update_lead 回填,并重算 missing。)
    """
    # 步骤1:system 放最前,再按顺序接上最近 N 轮对话
    messages = [{"role": "system", "content": _system(session, faq)}]
    for t in window:
        role = "assistant" if t["role"] == "assistant" else "user"   # 只认这两种角色,其余一律当 user
        messages.append({"role": role, "content": t["text"]})

    # 步骤2:调模型,强制 JSON 输出;temperature 略低求稳,max_tokens 给足防截断
    resp = await _client_lazy().chat.completions.create(
        model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        messages=messages,
        response_format={"type": "json_object"},   # 强制返回 JSON(配合 _JSON_CONTRACT)
        temperature=0.4,
        max_tokens=1200,
    )
    raw = resp.choices[0].message.content or "{}"   # 万一 content 为 None,兜底成空 JSON

    # 步骤3:解析模型返回的 JSON;万一它没按格式来(极少数),兜底成"整段当回复,线索为空"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return (raw.strip(), {})

    # 步骤4:取出 reply 和 lead
    reply = (data.get("reply") or "").strip()
    lead = data.get("lead") or {}
    # 只保留"非空字符串"字段:模型常把没抽到的字段填成 ""(空串),留着会用空值覆盖已知线索。
    # 例:{"name":"","email":"a@x.com","need":"录音麦"} → 滤成 {"email":"a@x.com","need":"录音麦"}。
    # (email/phone 的格式校验在 sessions.update_lead 里再兜一层,这里只管去空。)
    lead = {k: v for k, v in lead.items() if isinstance(v, str) and v.strip()}
    return (reply, lead)
