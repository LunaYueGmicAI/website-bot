"""
Slack forwarding: one channel, one thread per conversation.

- Lead card = thread root message, updated in real time via chat.update(ts). We store the
  root ts on the session (slack_thread_ts).
- Detail = thread replies: voice -> original audio file + transcript; text -> the message.

If SLACK_BOT_TOKEN is unset (before P0), all functions no-op so the rest of the pipeline
still runs in dev.
"""
import os

_client = None


def _client_lazy():
    global _client
    if _client is None:
        token = os.getenv("SLACK_BOT_TOKEN")
        if not token:
            return None
        from slack_sdk import WebClient
        _client = WebClient(token=token)
    return _client


def _channel():
    return os.getenv("SLACK_CHANNEL", "#gmic-web-voice-leads")


def _card_text(session):
    lead = session.get("lead") or {}
    intent = session.get("intent") or "—"
    contact = lead.get("email") or lead.get("phone")
    parts = [
        "*🆕 GMIC 网站咨询*",
        f"• 意图: {intent}",
        f"• 联系: {'✅ ' + contact if contact else '❌ 未留'}",
        f"• 姓名: {lead.get('name') or '—'}",
        f"• 需求: {lead.get('need') or '—'}",
        f"• 来源: {session.get('meta', {}).get('page_url', '—')}",
    ]
    if lead.get("missing"):
        parts.append(f"• 待补: {', '.join(lead['missing'])}")
    return "\n".join(parts)


def ensure_card(store, sid):
    """Create the thread-root lead card if this session doesn't have one yet. Returns ts."""
    session = store.snapshot(sid)
    if not session:
        return None
    if session.get("slack_thread_ts"):
        return session["slack_thread_ts"]
    c = _client_lazy()
    if not c:
        return None
    resp = c.chat_postMessage(channel=_channel(), text=_card_text(session))
    ts = resp["ts"]
    store.set_slack_ts(sid, ts)
    return ts


def update_card(store, sid):
    """Re-render the lead card in place (chat.update on the stored root ts)."""
    session = store.snapshot(sid)
    c = _client_lazy()
    if not c or not session or not session.get("slack_thread_ts"):
        return
    c.chat_update(channel=_channel(), ts=session["slack_thread_ts"], text=_card_text(session))


def post_detail(store, sid, text, audio_bytes=None, filename="voice.webm"):
    """Post a threaded reply. If audio_bytes given, upload the original clip too."""
    session = store.snapshot(sid)
    c = _client_lazy()
    if not c or not session:
        return
    ts = session.get("slack_thread_ts")
    if audio_bytes:
        c.files_upload_v2(
            channel=_channel(), thread_ts=ts,
            filename=filename, content=audio_bytes,
            initial_comment=text,
        )
    else:
        c.chat_postMessage(channel=_channel(), thread_ts=ts, text=text)
