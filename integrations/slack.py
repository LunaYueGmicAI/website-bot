"""
Slack 转发(异步):一个频道,每通对话一个 thread。

- 线索卡 = thread 的根消息,用 chat.update(ts) 实时回填(第一次发时把根消息 ID 存进会话)。
- 明细    = thread 里的回复:语音发"原始音频 + 转写";文字发文字。

若没配 SLACK_BOT_TOKEN(P0 之前),所有函数直接空转,好让其余流程在本地照常跑。
卡片文案给内部团队看,统一英文(与默认语言一致)。

【容错原则】Slack 只是"旁路归档",绝不能拖垮给用户的主响应。所以:
  1) 每个真正调 Slack API 的地方都在 worker 里 try/except 吞掉异常(记 log 就好);
  2) ⭐ 发送走【队列 + 单后台 worker】(见下):请求处理函数只把活儿丢进队列就立刻返回,
     不再原地等 Slack。就算 token 有效但 API 报错(限流/没被邀进频道/网络抖动),/chat、
     /voice 也照样把响应返回给用户。

【为什么要队列(2026-07-20 加)】Slack 单频道限速 ≈ 1 条消息/秒。以前每次调用直接发、
  且 429 被 try/except 静默吞掉 → 一旦瞬时并发(如投广告涌入)撞上限流,那条线索就【悄悄丢了】,
  而 Slack 是目前唯一的持久存储。改成:所有发送进一个 FIFO 队列,单个后台 worker 逐条发、
  两条之间至少隔 SLACK_MIN_INTERVAL 秒(遵守限速),撞 429 则按 Retry-After 退避重试
  SLACK_MAX_RETRIES 次。低流量下几乎无感;真遇突发也只是慢一点、不丢线索。
  单 worker FIFO 还顺带保证顺序:同一会话"先建卡(拿到 thread ts)→ 再往 thread 发明细"不会乱序。
"""
import os
import time
import asyncio
import logging

log = logging.getLogger(__name__)

_client = None

# ── 队列 & 限速参数 ───────────────────────────────────────────────
# 两次真正发出之间的最小间隔(秒)。Slack 单频道 ~1 条/秒,取略高的 1.1s 留余量。
_MIN_INTERVAL = float(os.getenv("SLACK_MIN_INTERVAL", "1.1"))
# 撞 429 时最多重试几次(每次按响应里的 Retry-After 退避)。
_MAX_RETRIES = int(os.getenv("SLACK_MAX_RETRIES", "5"))

_queue = None          # asyncio.Queue,元素 = (job 协程工厂, desc 字符串);None=worker 未启动
_worker_task = None    # 后台 worker 任务
_last_send_mono = 0.0  # 上次真正发出的时刻(time.monotonic()),用于两条之间的节流


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


# ======================== 发送队列 ========================
async def start_worker():
    """应用启动时拉起:建队列 + 起单个后台 worker。幂等(重复调用不会起第二个)。"""
    global _queue, _worker_task
    if _worker_task is not None:
        return
    _queue = asyncio.Queue()
    _worker_task = asyncio.create_task(_worker_loop())
    log.info("Slack send worker started (min_interval=%.2fs, max_retries=%d)", _MIN_INTERVAL, _MAX_RETRIES)


async def stop_worker():
    """应用关闭时:尽量把队列排空(有上限,别为死活发不出的消息卡住关机),再取消 worker。"""
    global _worker_task, _queue
    if _queue is not None:
        try:
            await asyncio.wait_for(_queue.join(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("Slack queue not drained within 10s on shutdown")
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
    _queue = None


async def _worker_loop():
    """逐条取活儿发:两条之间节流到 >= _MIN_INTERVAL 秒;单条撞 429 会在 _send_with_retry 里退避重试。"""
    global _last_send_mono
    while True:
        job, desc = await _queue.get()
        try:
            # 节流:距上次真正发出不足 _MIN_INTERVAL 就先睡一会,遵守 Slack ~1 条/秒/频道。
            gap = _MIN_INTERVAL - (time.monotonic() - _last_send_mono)
            if gap > 0:
                await asyncio.sleep(gap)
            await _send_with_retry(job, desc)
            _last_send_mono = time.monotonic()
        except Exception:  # noqa: BLE001 —— worker 绝不能因单条崩掉整条流水线
            log.exception("Slack worker crashed on %s", desc)
        finally:
            _queue.task_done()


async def _send_with_retry(job, desc):
    """跑一个发送 job;撞 429(限流)按 Retry-After 退避重试,其余异常记 log 后放弃(旁路归档,不拖主流程)。"""
    from slack_sdk.errors import SlackApiError
    for attempt in range(_MAX_RETRIES + 1):
        try:
            await job()
            return
        except SlackApiError as e:
            resp = getattr(e, "response", None)
            status = getattr(resp, "status_code", None)
            if status == 429 and attempt < _MAX_RETRIES:
                try:
                    retry_after = float(resp.headers.get("Retry-After", "1"))
                except (TypeError, ValueError, AttributeError):
                    retry_after = 1.0
                log.warning("Slack 429 on %s → retry in %.1fs (attempt %d/%d)",
                            desc, retry_after, attempt + 1, _MAX_RETRIES)
                await asyncio.sleep(retry_after + 0.1)
                continue
            # 非限流错误(或重试用尽)→ 记 log 放弃这条(不再抛,避免拖垮 worker)。
            log.exception("Slack API error on %s (status=%s)", desc, status)
            return
        except Exception:
            log.exception("Slack call failed on %s", desc)
            return


def _enqueue(job, desc):
    """把发送 job 丢进队列;worker 没启动的极少数场景(如没走 lifespan)退化成后台任务,尽力发。"""
    if _queue is not None:
        _queue.put_nowait((job, desc))
        return
    try:
        asyncio.get_running_loop().create_task(_send_with_retry(job, desc))
    except RuntimeError:
        # 没有事件循环(几乎不会发生)→ 放弃这条旁路归档,主流程不受影响。
        log.warning("no event loop to enqueue Slack job %s; dropped", desc)


# ======================== 卡片渲染 ========================
def _card_text(session):
    """
    把会话渲染成"线索卡"文本。每次 lead/entry_intents 变了,就用这个重新生成、覆盖那张卡。
    例:lead={email:"a@x.com", need:"recorder", missing:["name"]}, entry_intents=["odm","help-me-choose"] →
        一张列着 Entry/Contact/Name/Need/Source/Missing 的卡片文本("入口"行列出走过的所有入口)。

    注意 Entry 和 Need 的区别:Entry=走过哪些入口按钮/Tab(累积、去重,首个为主归因);
    Need=对话里演变出的真实诉求。用户可能从 ODM 进来、又点了选型,聊着聊着变成"就想买现货"——
    那时 Entry 仍是 ["odm","help-me-choose"](记录走过的路),Need 会更新成后者。
    """
    lead = session.get("lead") or {}
    # entry_intents 是【列表】(累积、去重):用户走过的入口按钮/问卷 Tab / 语音留言。
    entry_intents = session.get("entry_intents") or []
    # ⭐ 来源区分:语音留言(entry_intents 含 "voice-message",由 /voice/message 种;语音走一次性会话,
    #   故这条会话就只有它)走"语音直达"卡头,其余都是聊天询盘。团队在频道里一眼就能分辨这条线索
    #   是打字聊出来的还是直接发的语音留言。
    # ⭐ 卡片标签统一【简体中文】(团队看得快):只有【标签】是中文,【值】一律原样——
    #   need/留言用用户说的语言总结(中文就中文、英文就英文),称呼、邮箱、电话、URL 都不翻译。
    is_voice = "voice-message" in entry_intents
    header = "*🎙️ 新语音留言*" if is_voice else "*💬 新聊天询盘*"
    # 语音留言的"入口"行显示"🎙️ 语音留言"更直观;聊天则把走过的入口用 " → " 串起来(空则 —)。
    entry_display = "🎙️ 语音留言" if is_voice else (" → ".join(entry_intents) if entry_intents else "—")
    # 联系方式拆成 邮箱 / 电话 / 即时通讯 各一行:给了哪个显示哪个(以前压成一个,email 优先 →
    # 用户报了 phone/IM 反而看不到,现在都单独列)。
    email = lead.get("email")
    phone = lead.get("phone")
    messengers = lead.get("messengers") or []

    # ⭐ 语音留言卡【精简】:它没有对话/意图分析(纯留言),团队要的就是"怎么联系 + 说了啥 + 原音频"。
    #   所以只留联系方式(有哪个显示哪个)+ 留言(=转写译中文),原音频在 thread 里。不塞 称呼/来源/缺失。
    if is_voice:
        lines = [header]
        if email:
            lines.append(f"• 邮箱: {email}")
        if phone:
            lines.append(f"• 电话: {phone}")
        if messengers:
            lines.append(f"• 即时通讯: {', '.join(messengers)}")
        lines.append(f"• 留言: {lead.get('need') or '(见 thread 中的录音)'}")   # need 存的是转写(已译中文)
        return "\n".join(lines)

    # ↓↓↓ 聊天线索卡(完整版):入口/邮箱/电话/即时通讯/称呼/需求/来源/缺失
    parts = [
        header,
        f"• 入口: {entry_display}",
        f"• 邮箱: {'✅ ' + email if email else '—'}",
        f"• 电话: {'✅ ' + phone if phone else '—'}",
        f"• 即时通讯: {'✅ ' + ', '.join(messengers) if messengers else '—'}",
        f"• 称呼: {lead.get('name') or '—'}",
        f"• 需求: {lead.get('need') or '—'}",
        f"• 来源: {session.get('meta', {}).get('page_url') or '—'}",
    ]
    if lead.get("missing"):
        # missing 是内部键(contact/need)→ 映射成中文展示;未知键原样。
        _MISS_ZH = {"contact": "联系方式", "need": "需求"}
        parts.append(f"• 缺失: {', '.join(_MISS_ZH.get(m, m) for m in lead['missing'])}")
    return "\n".join(parts)


# ======================== 对外接口(均为"入队即返回",真正发送由 worker 完成)========================
async def ensure_card(store, sid):
    """
    确保这通对话在 Slack 有一张"线索卡"(thread 根消息);没有就新建。
    入队一个建卡 job(worker 里发),第一次发消息时 Slack 返回该消息 ID(ts)→ 存进会话,
    之后所有更新(chat.update)和明细回复(thread_ts)都靠它定位。
    幂等:已有 ts 就直接返回,不入队;入队后 job 执行时会再判一次(防同会话并发重复建卡)。
    """
    session = store.snapshot(sid)
    if not session or session.get("slack_thread_ts"):   # 没会话 / 已有卡 → 不用建
        return
    if not _client_lazy():                               # 没配 token → 空转
        return

    async def job():
        s = store.snapshot(sid)
        if not s or s.get("slack_thread_ts"):            # 执行时再判幂等(前面可能已有别的 job 建好了)
            return
        c = _client_lazy()
        resp = await c.chat_postMessage(channel=_channel(), text=_card_text(s))
        store.set_slack_ts(sid, resp["ts"])              # 记住根消息 ID

    _enqueue(job, f"ensure_card:{sid}")


async def update_card(store, sid):
    """就地刷新线索卡:入队一个 chat.update job,把卡改成最新状态(实时回填)。"""
    if not _client_lazy():
        return

    async def job():
        s = store.snapshot(sid)
        if not s or not s.get("slack_thread_ts"):        # 还没建出卡 → 没什么可更新
            return
        c = _client_lazy()
        await c.chat_update(channel=_channel(), ts=s["slack_thread_ts"], text=_card_text(s))

    _enqueue(job, f"update_card:{sid}")


async def post_detail(store, sid, text, audio_bytes=None, filename="voice.webm"):
    """
    往 thread 里发一条明细回复。若带 audio_bytes,就把原始录音也传上去。
    入队一个 job(worker 里发);job 执行时读会话里的 thread ts 定位 thread。
    例:
      文字轮 → post_detail(text="👤 你们支持防水吗")             → 一条文字回复
      语音轮 → post_detail(text="🎤 我想做2000个", audio_bytes=…) → 原始音频 + 转写一起挂到 thread
    """
    if not _client_lazy():
        return

    async def job():
        s = store.snapshot(sid)
        if not s:
            return
        ts = s.get("slack_thread_ts")
        if not ts:
            # 没有 thread 根(建卡失败过)→ 别发成频道顶层的游离消息,直接跳过这条明细。
            log.warning("post_detail skipped for %s: no slack_thread_ts", sid)
            return
        c = _client_lazy()
        if audio_bytes:
            # 语音:把原始音频 + 转写文字一起发进 thread。
            # ⚠️ 分清两件事:Slack【托管/播放】API 上传的音频文件 → 支持(就是这里 files_upload_v2 干的,
            #    团队能在 thread 里点开听);但 Slack【自动转写】API 上传的音频 → 不支持(只转 Slack
            #    客户端里现录的 clip/huddle)。所以转写文字得我们自己用 Whisper 出(见 ai/stt.py),
            #    再作为 initial_comment(=text,形如 "🎤 转写内容")一并发出去。
            await c.files_upload_v2(
                channel=_channel(), thread_ts=ts,
                filename=filename, content=audio_bytes,
                initial_comment=text,
            )
        else:
            # 文字:直接发一条 thread 回复
            await c.chat_postMessage(channel=_channel(), thread_ts=ts, text=text)

    _enqueue(job, f"post_detail:{sid}")
