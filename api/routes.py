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
import logging

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel

from core import sessions
from core.widget_config import CONFIG, ACTIONS
from ai import stt, llm
from integrations import slack

log = logging.getLogger(__name__)

router = APIRouter()
STORE = sessions.STORE
# 录音大小上限(字节)。粗略防超大文件;精确的按时长限制在前端做。
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(8_000_000)))

# 大模型挂了(quota 耗尽/key 失效/超时)时给用户的兜底回复。默认英文(见多语言策略)。
# 为什么要兜底:见记忆 openai-key-pool-emergency——6-29 全线 key 耗尽过。没兜底的话
# LLM 一抛异常整个 /chat、/voice 就 500,用户"发了没反应",线索也断在半路。
LLM_FALLBACK_REPLY = ("Thanks for reaching out! Our team will follow up shortly — "
                      "please leave your email or phone so we can get back to you.")
# 语音听不清/转写失败时的友好提示(默认英文)。
STT_FALLBACK_REPLY = "Sorry, I didn't catch that — could you say it again or type it?"


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

    【兜底】模型调用失败(quota/key/超时)不让整个请求 500:吞掉异常、记 log、回一句兜底话术,
      线索置空。这样用户仍收到回复、Slack 卡照常刷,线索不断在半路。
    """
    snap = STORE.snapshot(sid)
    window = STORE.window(sid)
    try:
        reply, lead = await llm.respond(snap, CONFIG.get("faq", []), window)
    except Exception:
        log.exception("llm.respond failed for session %s — using fallback reply", sid)
        reply, lead = LLM_FALLBACK_REPLY, {}
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
    处理 widget 里【快捷按钮】的点击 —— 注意:不是打字、也不是语音(那两条分别走 /chat 和 /voice)。
    这里只管问候语下面那几个 shortcut(话题按钮 / FAQ / 跳转链接),多数分支不调大模型,秒回、省钱。
    action 有三种:topic(话题)/ faq(常见问题)/ link(跳转);具体每种干啥见下面各分支的注释。
    """
    # 不管哪种 action,先确保这个用户的会话存在(用户第一次点按钮,就在这一步建会话)。
    STORE.get_or_create(req.session_id, {"page_url": req.page_url, "lang": req.lang})

    # ── 话题按钮(如 🏭 ODM):种入口意图 + 建 Slack 卡 + 回一句写死的开场白,不调大模型 ──
    if req.action == "topic":
        # act = 这个按钮在 widget.json 里的整条配置(core.widget_config 已按 id 建好索引 ACTIONS)。
        #   例:req.id="odm" → act={"id":"odm","label":"🏭 Custom / ODM","type":"topic",
        #                          "entry_intent":"odm","opener":"We do full ODM/OEM..."}
        act = ACTIONS.get(req.id)
        if not act:   # id 查不到按钮(前端传错 / 配置漏)→ 400,别静默返回空 reply + 白建空卡
            raise HTTPException(status_code=400, detail="unknown action id")

        # 种"入口意图":记下用户从哪个话题按钮进来(如 "odm"),首次锁定不覆盖。
        STORE.set_entry_intent(req.session_id, act.get("entry_intent"))

        # opener = 开场白:点这个话题按钮后 bot 先说的那句【写死】的话(不调大模型,直接秒回)。
        #   例(odm):"We do full ODM/OEM custom hardware ... product, quantity, target launch date?"
        opener = act.get("opener", "")
        if opener:
            STORE.append_turn(req.session_id, "assistant", opener)  # 开场白也算一轮,后续对话能接上它

        # 话题按钮 = 高意向:立刻在 Slack 建线索卡,并把刚种的 entry_intent 刷进卡。
        await slack.ensure_card(STORE, req.session_id)
        await slack.update_card(STORE, req.session_id)
        return {"reply": opener}   # 产出:{"reply": 开场白} → 前端显示成 bot 的第一句

    # ── 常见问题(FAQ):按 index 取那条,回写死的标准答案(+可选链接),不调大模型 ──
    if req.action == "faq":
        faq = CONFIG.get("faq", [])
        if req.index is None or not (0 <= req.index < len(faq)):   # index 越界/缺失 → 400
            raise HTTPException(status_code=400, detail="bad faq index")
        item = faq[req.index]                                       # item = {"q":问题, "a":答案, "link":可选}
        STORE.append_turn(req.session_id, "user", item["q"])        # 把"问题"当用户说的记一轮(追问能接上)
        STORE.append_turn(req.session_id, "assistant", item["a"])   # 把"标准答案"当 bot 回的记一轮
        return {"reply": item["a"], "link": item.get("link")}       # 产出:{写死答案 + 可选的了解更多链接}

    # ── 跳转按钮(看产品 / 预约演示):真正的跳转在前端做,后端保活 + 记下点的是哪个链接 ──
    if req.action == "link":
        # 用 id 查出点的是哪个按钮(products / demo …),这样才能区分/统计,而不是"任何链接一视同仁"。
        act = ACTIONS.get(req.id)
        if not act:   # id 查不到(前端传错 / 配置漏)→ 400,和 topic 分支一致
            raise HTTPException(status_code=400, detail="unknown action id")
        STORE.touch(req.session_id)   # 只刷新活跃时间,防会话被 TTL 清掉(跳转本身在前端做)
        # 记一笔"点了哪个链接去哪":先落 log 便于观察;以后要正经统计可在此镜像进 Slack/DB。
        log.info("link click: session=%s id=%s url=%s", req.session_id, req.id, act.get("url"))
        return {"ok": True}                                         # 产出:{"ok": True}(没有 reply)

    raise HTTPException(status_code=400, detail="unknown action")   # 三种都不是 → 未知 action


@router.post("/chat")
async def chat(req: ChatReq):
    """
    用户【打字】发一条消息的入口(和 /event 按钮、/voice 语音并列)。会真正调大模型。
    具体每步干啥、产出什么见下面各 step 的注释。
    """
    # 空消息直接 400,别浪费一次大模型调用。
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")

    # 1) 确保会话存在(首次打字就在这建会话,并记下来源页 page_url、语言 lang 进 meta)
    STORE.get_or_create(req.session_id, {"page_url": req.page_url, "lang": req.lang})
    # 2) 把用户这句记进对话(turns),后续滑动窗口喂给模型时能带上
    STORE.append_turn(req.session_id, "user", text)
    # 3) 确保 Slack 有这通对话的线索卡(没有就建,根消息 ts 存进会话)
    await slack.ensure_card(STORE, req.session_id)
    # 4) 用户原话发进 Slack thread 做明细归档(👤 前缀标明是访客说的)
    await slack.post_detail(STORE, req.session_id, f"👤 {text}")

    # 5) 调大模型:产出 reply(给用户的回复)+ 顺带抽到的线索;_run_llm 内部已回填 lead/turns 并带兜底
    reply, _ = await _run_llm(req.session_id)
    # 6) AI 回复也发进 thread 归档(🤖 前缀标明是 bot 说的)
    await slack.post_detail(STORE, req.session_id, f"🤖 {reply}")
    # 7) 刷新线索卡:这轮可能抽到了新 lead(邮箱/need 等),把卡更新成最新状态
    await slack.update_card(STORE, req.session_id)
    return {"reply": reply}   # 产出:{"reply": AI 回复} → 前端(P3)取 reply 显示成 bot 气泡


@router.post("/voice")
async def voice(
    session_id: str = Form(...),
    audio: UploadFile = File(...),
    page_url: str | None = Form(None),
    lang: str | None = Form(None),
):
    """
    用户【发语音】的入口(和 /event 按钮、/chat 打字并列)。走 multipart 上传原始录音。
    跟 /chat 几乎一样,只是最前面多了一步"转写(STT)",且 Slack 明细里额外带上原始音频。
    具体每步干啥、产出什么见下面各 step 的注释。
    """
    # 0) 读出录音字节;超大文件直接 413 拒掉(防滥用/爆内存),精确的按时长限制在前端做
    audio_bytes = await audio.read()
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio too large")

    # 1) 确保会话存在(首次发语音就在这建会话,并记下来源页 page_url、语言 lang 进 meta)
    STORE.get_or_create(session_id, {"page_url": page_url, "lang": lang})

    # 2) 语音 → 文字:lang=None 时让 Whisper 自动识别语种(多语言核心)。
    #    STT 失败(Groq 挂/超时)不 500:吞掉异常记 log,当作"没转出文字"处理。
    try:
        transcript = await stt.transcribe(audio_bytes, filename=audio.filename or "voice.webm",
                                          language=lang or None)
    except Exception:
        log.exception("stt.transcribe failed for session %s", session_id)
        transcript = ""
    # 3) 静音/听不清/转写失败 → 直接回友好提示(默认英文),别把空字符串硬塞给大模型
    if not transcript:
        return {"reply": STT_FALLBACK_REPLY, "transcript": ""}   # 产出:{兜底话术, transcript:""}

    # 4) 转写文字当"用户这轮说的话"记进对话(turns),后续滑动窗口能带上
    STORE.append_turn(session_id, "user", transcript)
    # 5) 确保 Slack 有这通对话的线索卡(没有就建)
    await slack.ensure_card(STORE, session_id)
    # 6) 原始音频 + 转写文字一起发进 thread 归档(🎤 前缀 + 满足"原语音和转文字都进 Slack"的需求)
    await slack.post_detail(STORE, session_id, f"🎤 {transcript}", audio_bytes=audio_bytes,
                            filename=audio.filename or "voice.webm")

    # 7) 调大模型:产出 reply + 顺带抽到的线索;_run_llm 内部已回填 lead/turns 并带兜底
    reply, _ = await _run_llm(session_id)
    # 8) AI 回复也发进 thread 归档(🤖 前缀标明是 bot 说的)
    await slack.post_detail(STORE, session_id, f"🤖 {reply}")
    # 9) 刷新线索卡:这轮可能抽到了新 lead,把卡更新成最新状态
    await slack.update_card(STORE, session_id)
    # audio_bytes 只是本次请求的局部变量,函数返回后自动释放,绝不长期驻留内存/磁盘
    return {"reply": reply, "transcript": transcript}   # 产出:{AI 回复 + 这轮转写文字} → 前端显示
