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
    拼这一轮的"系统提示"。顺序:人设 → 意图 → FAQ 口径 → 已知线索 → 输出契约。
    这几块合起来,就是"只带精华、不带全部历史"里的"精华"。
    例:intent=odm、lead={email:...} → 系统提示里会带上"访客走 ODM 进来、已知邮箱、还缺姓名"等,
        让模型问对问题、并知道还要追问姓名。
    """
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


async def respond(session, faq, window):
    """
    跑一轮对话(异步)。

    参数:
      session: 会话快照(取 intent + lead 上下文)
      faq:     widget.json 里的 FAQ 列表(统一口径用)
      window:  最近 N 轮对话(滑动窗口),形如 [{role,text}, ...]
    返回:(回复文本, 线索更新字典)

    组装喂给模型的消息:
      1) 一条 system = 上面 _system() 拼的"精华"
      2) 把最近 N 轮按 user/assistant 依次接上
    例:window=[{role:user,text:"想做录音麦"},{role:assistant,text:"多少个?"},{role:user,text:"2000个"}]
        → messages=[system, user:"想做录音麦", assistant:"多少个?", user:"2000个"]
        → 模型返回 {"reply":"2000 个没问题…","lead":{"need":"录音麦","...":"..."}}
    """
    messages = [{"role": "system", "content": _system(session, faq)}]
    for t in window:
        role = "assistant" if t["role"] == "assistant" else "user"
        messages.append({"role": role, "content": t["text"]})

    resp = await _client_lazy().chat.completions.create(
        model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        messages=messages,
        response_format={"type": "json_object"},   # 强制返回 JSON
        temperature=0.4,
        max_tokens=1200,
    )
    raw = resp.choices[0].message.content or "{}"

    # 解析模型返回的 JSON;万一它没按格式来,兜底成"整段当回复,线索为空"。
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return (raw.strip(), {})

    reply = (data.get("reply") or "").strip()
    lead = data.get("lead") or {}
    # 只保留非空字符串字段(过滤掉模型填的空字段,避免用空值覆盖已知线索;
    # 邮箱/电话的格式校验在 sessions.update_lead 里再兜一层)。
    lead = {k: v for k, v in lead.items() if isinstance(v, str) and v.strip()}
    return (reply, lead)
