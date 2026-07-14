"""
HTTP 路由 —— 用 FastAPI 的 APIRouter 组织(异步),方便以后加更多端点/版本。

端点一览:
  GET  /health   存活检查
  GET  /config   前端拉配置(问候语、4 个按钮、FAQ)
  POST /event    快捷按钮点击(topic 话题 / faq 常见问题 / link 跳转)
  POST /chat     一条打字消息          -> AI 回复
  POST /voice    一段录音(multipart)  -> 转写 -> AI 回复

【FastAPI 白拿的好处】用 Pydantic 模型声明请求体,字段缺失/类型不对 FastAPI 会自动返回 422,
  不用再手写 `if not sid` 这种校验(回应你 review 里关心的参数校验)。
"""
import os

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel

from core import sessions
from core.widget_config import CONFIG, ACTIONS
from ai import stt, llm
from integrations import slack

router = APIRouter()
STORE = sessions.STORE
# 录音大小上限(字节)。粗略防超大文件;精确的按时长限制在前端做。
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(8_000_000)))


# ======================== 请求体模型(Pydantic 自动校验) ========================
class EventReq(BaseModel):
    # 例:{"session_id":"sess_ab12","action":"topic","id":"odm"}
    #     {"session_id":"sess_ab12","action":"faq","index":1}
    session_id: str
    action: str                    # "topic" | "faq" | "link"
    id: str | None = None          # topic/link 用:按钮 id
    index: int | None = None       # faq 用:第几条问题
    page_url: str | None = None
    lang: str | None = None


class ChatReq(BaseModel):
    # 例:{"session_id":"sess_ab12","text":"你们支持防水吗","page_url":"/products/"}
    session_id: str
    text: str
    page_url: str | None = None
    lang: str | None = None


# ======================== 小工具 ========================
async def _run_llm(sid):
    """
    跑一轮大模型,并把结果落进内存(异步)。步骤:
      1) 取会话快照 + 最近 N 轮(滑动窗口)
      2) await llm.respond() 拿到 (回复, 线索更新)
      3) 有线索就 merge 进 lead;有回复就作为 assistant 追加进 turns
    例:用户刚说完"邮箱 a@x.com" → 模型回"好的已记录…" + lead={email:"a@x.com"} →
        这里把 email 合并进 lead(会先过格式校验),把回复追加进 turns,返回给上层发 Slack/前端。
    """
    snap = STORE.snapshot(sid)
    window = STORE.window(sid)
    reply, lead = await llm.respond(snap, CONFIG.get("faq", []), window)
    if lead:
        STORE.update_lead(sid, lead)
    if reply:
        STORE.append_turn(sid, "assistant", reply)
    return reply, lead


# ======================== 路由 ========================
@router.get("/health")
async def health():
    # 返回存活状态 + 当前活跃会话数
    return {"status": "ok", **STORE.stats()}


@router.get("/config")
async def config():
    # 前端从这里加载 问候语 + 按钮 + FAQ(团队改 config/widget.json 即可,不用动代码)
    return CONFIG


@router.post("/event")
async def event(req: EventReq):
    """
    处理快捷按钮点击。action 三种:
      - topic:话题按钮(如 ODM)→ 种意图 + 建 Slack 卡 + 返回开场白(不调大模型)
      - faq  :常见问题       → 返回写死答案(不调大模型),并把这条 Q&A 记进 turns
      - link :跳转按钮       → 只记一下点击(实际跳转在前端做)
    """
    STORE.get_or_create(req.session_id, {"page_url": req.page_url, "lang": req.lang})

    if req.action == "topic":
        # 例:点了 [🏭 ODM] → id="odm" → 查到按钮 → 种 intent="odm" → 返回它的开场白
        act = ACTIONS.get(req.id, {})
        STORE.set_intent(req.session_id, act.get("intent"))
        opener = act.get("opener", "")
        if opener:
            STORE.append_turn(req.session_id, "assistant", opener)  # 开场白也算一轮,后面能接上下文
        await slack.ensure_card(STORE, req.session_id)              # 高意向按钮,建卡
        await slack.update_card(STORE, req.session_id)
        return {"reply": opener}

    if req.action == "faq":
        # 例:点了第 2 条常见问题 → index=1 → 返回它的写死答案 + 链接
        faq = CONFIG.get("faq", [])
        if req.index is None or not (0 <= req.index < len(faq)):
            raise HTTPException(status_code=400, detail="bad faq index")
        item = faq[req.index]
        STORE.append_turn(req.session_id, "user", item["q"])        # 记进对话,后续追问能接上
        STORE.append_turn(req.session_id, "assistant", item["a"])
        return {"reply": item["a"], "link": item.get("link")}

    if req.action == "link":
        STORE.touch(req.session_id)                                  # 只刷新活跃时间;跳转在前端
        return {"ok": True}

    raise HTTPException(status_code=400, detail="unknown action")


@router.post("/chat")
async def chat(req: ChatReq):
    """一条打字消息的完整处理:落库 → 发 Slack 明细 → 调 AI → 回复 + 更新卡。"""
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")

    STORE.get_or_create(req.session_id, {"page_url": req.page_url, "lang": req.lang})
    STORE.append_turn(req.session_id, "user", text)           # 1) 记进对话
    await slack.ensure_card(STORE, req.session_id)            # 2) 确保有卡
    await slack.post_detail(STORE, req.session_id, f"👤 {text}")   # 3) 用户这句发进 thread

    reply, _ = await _run_llm(req.session_id)                 # 4) 调 AI
    await slack.post_detail(STORE, req.session_id, f"🤖 {reply}")  # 5) AI 回复发进 thread
    await slack.update_card(STORE, req.session_id)            # 6) 刷新卡(可能填了新线索)
    return {"reply": reply}


@router.post("/voice")
async def voice(
    session_id: str = Form(...),
    audio: UploadFile = File(...),
    page_url: str | None = Form(None),
    lang: str | None = Form(None),
):
    """
    一段录音的完整处理(multipart 上传)。跟 /chat 几乎一样,只是最前面多了"转写",
    且明细里带原始音频。
    """
    audio_bytes = await audio.read()
    if len(audio_bytes) > MAX_AUDIO_BYTES:      # 超大文件直接拒(防滥用)
        raise HTTPException(status_code=413, detail="audio too large")

    STORE.get_or_create(session_id, {"page_url": page_url, "lang": lang})

    # 1) 语音 -> 文字(lang=None 时自动识别语种,多语言核心)
    transcript = await stt.transcribe(audio_bytes, filename=audio.filename or "voice.webm",
                                      language=lang or None)
    if not transcript:                          # 静音/听不清:友好提示(默认英文),别硬塞给模型
        return {"reply": "Sorry, I didn't catch that — could you say it again or type it?",
                "transcript": ""}

    STORE.append_turn(session_id, "user", transcript)   # 2) 转写记进对话
    await slack.ensure_card(STORE, session_id)          # 3) 确保有卡
    # 4) 原始音频 + 转写 一起发进 thread(满足"原语音和转文字都进 Slack"的需求)
    await slack.post_detail(STORE, session_id, f"🎤 {transcript}", audio_bytes=audio_bytes,
                            filename=audio.filename or "voice.webm")

    reply, _ = await _run_llm(session_id)               # 5) 调 AI
    await slack.post_detail(STORE, session_id, f"🤖 {reply}")   # 6) 回复发进 thread
    await slack.update_card(STORE, session_id)          # 7) 刷新卡
    # audio_bytes 只是本次请求的局部变量,函数返回后自动释放,绝不长期驻留内存/磁盘
    return {"reply": reply, "transcript": transcript}
