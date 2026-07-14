"""
HTTP 路由 —— 用 Flask Blueprint 组织,方便以后加更多端点/版本。

端点一览:
  GET  /health   存活检查
  GET  /config   前端拉取配置(问候语、4 个按钮、FAQ)
  POST /event    快捷按钮点击(topic 话题 / faq 常见问题 / link 跳转)
  POST /chat     一条打字消息          -> AI 回复
  POST /voice    一段录音(multipart)  -> 转写 -> AI 回复
"""
import os

from flask import Blueprint, request, jsonify

from core import sessions
from core.widget_config import CONFIG, ACTIONS
from ai import stt, llm
from integrations import slack

bp = Blueprint("api", __name__)

STORE = sessions.STORE
# 录音大小上限(字节)。粗略防超大文件;精确按时长限制在前端做。
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(8_000_000)))


# ============================ 小工具 ============================
def _meta_from_json():
    """从 JSON 请求体里取来源信息(哪个页面、什么语言),存进会话备用。"""
    body = request.get_json(silent=True) or {}
    return {"page_url": body.get("page_url"), "lang": body.get("lang")}


def _run_llm(sid):
    """
    跑一轮大模型,并把结果落进内存。步骤:
      1) 取会话快照 + 最近 N 轮(滑动窗口)
      2) 调 llm.respond() 拿到 (回复, 线索更新)
      3) 有线索就 merge 进 lead;有回复就作为 assistant 追加进 turns
    例:用户刚说完"邮箱 a@x.com" → 模型回"好的已记录…" + lead={email:"a@x.com"} →
        这里把 email 合并进 lead,把回复追加进 turns,返回给上层发 Slack/前端。
    """
    snap = STORE.snapshot(sid)
    window = STORE.window(sid)
    reply, lead = llm.respond(snap, CONFIG.get("faq", []), window)
    if lead:
        STORE.update_lead(sid, lead)
    if reply:
        STORE.append_turn(sid, "assistant", reply)
    return reply, lead


# ============================ 路由 ============================
@bp.get("/health")
def health():
    # 返回存活状态 + 当前活跃会话数
    return jsonify(status="ok", **STORE.stats())


@bp.get("/config")
def config():
    # 前端从这里加载 问候语 + 按钮 + FAQ(团队改 config/widget.json 即可,不用动代码)
    return jsonify(CONFIG)


@bp.post("/event")
def event():
    """
    处理快捷按钮点击。action 三种:
      - topic:话题按钮(如 ODM)→ 种意图 + 建 Slack 卡 + 返回开场白(不调大模型)
      - faq  :常见问题       → 返回写死答案(不调大模型),并把这条 Q&A 记进 turns
      - link :跳转按钮       → 只记一下点击(实际跳转在前端做)
    """
    body = request.get_json(silent=True) or {}
    sid = body.get("session_id")
    action = body.get("action")
    if not sid or not action:
        return jsonify(error="session_id and action required"), 400

    STORE.get_or_create(sid, _meta_from_json())

    if action == "topic":
        # 例:点了 [🏭 ODM] → id="odm" → 查到该按钮 → 种 intent="odm" → 返回它的开场白
        act = ACTIONS.get(body.get("id"), {})
        STORE.set_intent(sid, act.get("intent"))
        opener = act.get("opener", "")
        if opener:
            STORE.append_turn(sid, "assistant", opener)   # 开场白也算一轮 assistant,后面能接上下文
        slack.ensure_card(STORE, sid)                      # 高意向按钮,建卡
        slack.update_card(STORE, sid)
        return jsonify(reply=opener)

    if action == "faq":
        # 例:点了第 2 条常见问题 → index=1 → 返回它的写死答案 + 链接
        faq = CONFIG.get("faq", [])
        idx = body.get("index")
        if idx is None or not (0 <= idx < len(faq)):
            return jsonify(error="bad faq index"), 400
        item = faq[idx]
        STORE.append_turn(sid, "user", item["q"])          # 把问答记进对话,后续追问能接上
        STORE.append_turn(sid, "assistant", item["a"])
        return jsonify(reply=item["a"], link=item.get("link"))

    if action == "link":
        STORE.touch(sid)                                    # 只刷新活跃时间;人还在,跳转在前端
        return jsonify(ok=True)

    return jsonify(error="unknown action"), 400


@bp.post("/chat")
def chat():
    """一条打字消息的完整处理:落库 → 发 Slack 明细 → 调 AI → 回复 + 更新卡。"""
    body = request.get_json(silent=True) or {}
    sid = body.get("session_id")
    text = (body.get("text") or "").strip()
    if not sid or not text:
        return jsonify(error="session_id and text required"), 400

    STORE.get_or_create(sid, _meta_from_json())
    STORE.append_turn(sid, "user", text)     # 1) 记进对话
    slack.ensure_card(STORE, sid)            # 2) 确保有卡
    slack.post_detail(STORE, sid, f"👤 {text}")   # 3) 用户这句发进 thread

    reply, _ = _run_llm(sid)                 # 4) 调 AI
    slack.post_detail(STORE, sid, f"🤖 {reply}")  # 5) AI 回复发进 thread
    slack.update_card(STORE, sid)            # 6) 刷新卡(可能填了新线索)
    return jsonify(reply=reply)


@bp.post("/voice")
def voice():
    """
    一段录音的完整处理。跟 /chat 几乎一样,只是最前面多了"转写",且明细里带原始音频。
    注意:是 multipart 上传(不是 JSON),字段从 request.form / request.files 取。
    """
    sid = request.form.get("session_id")
    if not sid:
        return jsonify(error="session_id required"), 400
    f = request.files.get("audio")
    if not f:
        return jsonify(error="audio file required"), 400

    audio = f.read()
    if len(audio) > MAX_AUDIO_BYTES:          # 超大文件直接拒(防滥用)
        return jsonify(error="audio too large"), 413

    meta = {"page_url": request.form.get("page_url"), "lang": request.form.get("lang")}
    STORE.get_or_create(sid, meta)

    # 1) 语音 -> 文字
    transcript = stt.transcribe(audio, filename=f.filename or "voice.webm",
                                language=request.form.get("lang") or None)
    if not transcript:                        # 静音/听不清:友好提示(默认英文),别硬塞给模型
        return jsonify(reply="Sorry, I didn't catch that — could you say it again or type it?", transcript="")

    STORE.append_turn(sid, "user", transcript)   # 2) 转写记进对话
    slack.ensure_card(STORE, sid)                # 3) 确保有卡
    # 4) 原始音频 + 转写 一起发进 thread(满足"原语音和转文字都进 Slack"的需求)
    slack.post_detail(STORE, sid, f"🎤 {transcript}", audio_bytes=audio,
                      filename=f.filename or "voice.webm")

    reply, _ = _run_llm(sid)                     # 5) 调 AI
    slack.post_detail(STORE, sid, f"🤖 {reply}") # 6) 回复发进 thread
    slack.update_card(STORE, sid)                # 7) 刷新卡
    # audio 只是这次请求的局部变量,函数返回后自动释放,绝不长期驻留内存/磁盘
    return jsonify(reply=reply, transcript=transcript)
