"""
Slack 转发(异步):一个频道,每通对话一个 thread。

- 线索卡 = thread 的根消息,用 chat.update(ts) 实时回填(第一次发时把根消息 ID 存进会话)。
- 明细    = thread 里的回复:语音发"原始音频 + 转写";文字发文字。

若没配 SLACK_BOT_TOKEN(P0 之前),所有函数直接空转,好让其余流程在本地照常跑。
卡片文案给内部团队看,统一英文(与默认语言一致)。

【容错原则】Slack 只是"旁路归档",绝不能拖垮给用户的主响应。所以下面每个函数在真正调
  Slack API 的地方都 try/except 吞掉异常(记 log 就好):就算 token 有效但 API 报错
  (限流、bot 没被邀进频道、网络抖动),/chat、/voice 也照样把 AI 回复返回给用户。
"""
import os
import logging

log = logging.getLogger(__name__)

_client = None


def _client_lazy():
    # 懒加载 Slack 异步客户端;没 token 就返回 None(调用方因此空转)。
    global _client
    if _client is None:
        token = os.getenv("SLACK_BOT_TOKEN")
        if not token:
            return None
        from slack_sdk.web.async_client import AsyncWebClient
        _client = AsyncWebClient(token=token)
    return _client


def _channel():
    return os.getenv("SLACK_CHANNEL", "#gmic-web-voice-leads")


def _card_text(session):
    """
    把会话渲染成"线索卡"文本。每次 lead/entry_intent 变了,就用这个重新生成、覆盖那张卡。
    例:lead={email:"a@x.com", need:"recorder", missing:["name"]}, entry_intent="odm" →
        一张列着 Entry/Contact/Name/Need/Source/Missing 的卡片文本。

    注意 Entry 和 Need 的区别:Entry=从哪个按钮进来(入口,不变);Need=对话里演变出的真实诉求。
    用户可能从 ODM 进来但聊着聊着变成"就想买现货"——那时 Entry 仍是 odm,Need 会更新成后者。
    """
    lead = session.get("lead") or {}
    entry_intent = session.get("entry_intent") or "—"
    contact = lead.get("email") or lead.get("phone")
    parts = [
        "*🆕 New GMIC website inquiry*",
        f"• Entry: {entry_intent}",
        f"• Contact: {'✅ ' + contact if contact else '❌ not left'}",
        f"• Name: {lead.get('name') or '—'}",
        f"• Need: {lead.get('need') or '—'}",
        f"• Source: {session.get('meta', {}).get('page_url', '—')}",
    ]
    if lead.get("missing"):
        parts.append(f"• Missing: {', '.join(lead['missing'])}")
    return "\n".join(parts)


async def ensure_card(store, sid):
    """
    确保这通对话在 Slack 有一张"线索卡"(thread 根消息);没有就新建(异步)。

    关键:第一次发消息时 Slack 返回该消息的 ID(ts)。把 ts 存进会话,
    之后所有更新(chat.update)和明细回复(thread_ts)都靠它定位到这张卡/这个 thread。
    例:用户点 ODM 首次触发 → 发一张卡 → Slack 返回 ts="169...01" → 存进 session;
        下次不会重复建卡。
    """
    session = store.snapshot(sid)
    if not session:
        return None
    if session.get("slack_thread_ts"):        # 已有卡,不重复建
        return session["slack_thread_ts"]
    c = _client_lazy()
    if not c:
        return None
    try:
        resp = await c.chat_postMessage(channel=_channel(), text=_card_text(session))
    except Exception:
        log.exception("Slack ensure_card failed for %s", sid)
        return None                           # 建卡失败:不设 ts,后续 update_card/post_detail 自动空转
    ts = resp["ts"]
    store.set_slack_ts(sid, ts)               # 记住根消息 ID
    return ts


async def update_card(store, sid):
    """就地刷新线索卡:用存好的根消息 ts 调 chat.update,把卡改成最新状态(实时回填,异步)。"""
    session = store.snapshot(sid)
    c = _client_lazy()
    if not c or not session or not session.get("slack_thread_ts"):
        return
    try:
        await c.chat_update(channel=_channel(), ts=session["slack_thread_ts"], text=_card_text(session))
    except Exception:
        log.exception("Slack update_card failed for %s", sid)


async def post_detail(store, sid, text, audio_bytes=None, filename="voice.webm"):
    """
    往 thread 里发一条明细回复(异步)。若带 audio_bytes,就把原始录音也传上去。
    例:
      文字轮 → post_detail(text="👤 你们支持防水吗")             → 一条文字回复
      语音轮 → post_detail(text="🎤 我想做2000个", audio_bytes=…) → 原始音频 + 转写一起挂到 thread
    """
    session = store.snapshot(sid)
    c = _client_lazy()
    if not c or not session:
        return
    ts = session.get("slack_thread_ts")
    if not ts:
        # 没有 thread 根(建卡失败过)→ 别发成频道顶层的游离消息,直接跳过这条明细。
        log.warning("post_detail skipped for %s: no slack_thread_ts", sid)
        return
    try:
        if audio_bytes:
            # 语音:把原始音频 + 转写文字一起发进 thread。
            # ⚠️ 分清两件事:Slack【托管/播放】API 上传的音频文件 → 支持(就是这里 files_upload_v2 干的,
            #    团队能在 thread 里点开听);但 Slack【自动转写】API 上传的音频 → 不支持(只转 Slack
            #    客户端里现录的 clip/huddle)。所以转写文字得我们自己用 Groq Whisper 出(见 ai/stt.py),
            #    再作为 initial_comment(=text,形如 "🎤 转写内容")一并发出去。
            await c.files_upload_v2(
                channel=_channel(), thread_ts=ts,
                filename=filename, content=audio_bytes,
                initial_comment=text,
            )
        else:
            # 文字:直接发一条 thread 回复
            await c.chat_postMessage(channel=_channel(), thread_ts=ts, text=text)
    except Exception:
        log.exception("Slack post_detail failed for %s", sid)
