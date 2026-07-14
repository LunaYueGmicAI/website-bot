"""
会话存储 + 对话内存管控。

【两层记忆模型】
  - 工作记忆(本模块,放进程内存/RAM):只保存"当前正在进行"的对话,尽量小。
  - 归档(Slack):每一轮对话一产生就实时转发到 Slack,永久保存。
  因为 Slack 里什么都有,所以内存这层可以随时裁剪/清理,丢了也不丢数据。

【四道闸门控制内存】① 单会话轮数上限 ② TTL 超时清理 ③ 全局数量上限+LRU ④ 单条大小限制(在 api 层)

【主键 session_id】
  每个用户对应一个 session_id,它同时是:①本字典的 key ②Slack thread 的归属 ③前端浏览器身份。
  一个进程、一个字典、很多用户,靠 key 隔开,永远不会串。

【并发说明】后端是 FastAPI 异步(单进程事件循环)。本模块的方法都是"纯同步、内部不 await",
  所以在事件循环里天然是原子的(不会执行到一半被别的协程插进来)。那把 threading.Lock 因此
  基本用不到,但留着无害(万一以后引入线程池还能兜底)。
"""
import os
import re
import time
import asyncio
import threading
from collections import OrderedDict

# ---- 可调参数(从 .env 读,给了默认值)----
TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))     # 闲置多久算过期(默认 30 分钟)
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "500"))           # 全局最多同时保留几个会话
HISTORY_TURNS = int(os.getenv("LLM_HISTORY_TURNS", "8"))        # 每轮喂给大模型的最近几轮
# 内存里最多留几轮 = 默认等于喂给 LLM 的轮数。
#   为什么不多留:多出来的既不发给模型、前端也不靠服务器回滚(前端自己存显示副本),
#   而老对话又已经在 Slack 归档,所以"多留"没有意义。约束:必须 >= HISTORY_TURNS。
MAX_TURNS = int(os.getenv("MAX_TURNS_IN_MEMORY", str(HISTORY_TURNS)))

# lead(线索)里我们会存的字段。missing 单独算,不在这里。
_LEAD_FIELDS = ("name", "email", "phone", "company", "need")

# 【改动③】写入前的格式校验:光靠 prompt 说"别编造"不够稳,代码这里再兜一层。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now():
    # 当前时间戳(秒)。单独包一层方便测试。
    return time.time()


def _valid_email(v):
    # 例:"a@x.com" -> True;"我的邮箱" / "a@x" -> False。不合格就不写进 lead。
    return bool(_EMAIL_RE.match(v.strip()))


def _valid_phone(v):
    # 抽出所有数字判断位数(容忍 +、空格、横杠)。例:"+1 (669) 900-0008" -> 11 位数字 -> True。
    digits = re.sub(r"\D", "", v)
    return 7 <= len(digits) <= 15


class SessionStore:
    def __init__(self):
        # 用 OrderedDict 而非普通 dict:它记住顺序,能几乎零成本实现 LRU——
        # 刚用过的挪到末尾,要淘汰时从头部(最久没动的)删。
        self._data = OrderedDict()
        self._lock = threading.Lock()   # 见文件顶部"并发说明":基本用不到,留着兜底

    # ======================== 生命周期 ========================
    def get_or_create(self, sid, meta=None):
        """
        拿到某用户的会话;没有就新建。

        例:用户第一次点开 widget,前端带 session_id="sess_ab12" 进来 → 这里没有它 → 新建空会话。
            第二次说话,同一个 sid 再进来 → 直接返回上次那条,并刷新"最近活跃时间"。
        """
        with self._lock:
            s = self._data.get(sid)
            if s is None:
                s = {
                    "id": sid,
                    "created_at": _now(),
                    "last_seen": _now(),     # 相当于 updated_at:每次活动刷新,TTL/LRU 都看它
                    "entry_intent": None,    # 入口意图:进来时点的话题按钮(如 "odm");单值、首次锁定不变;None = 自由聊/其它
                                             # 注意:这只是"从哪进来"的入口标签;对话中演变的真实诉求看 lead.need
                    "lead": {},              # 线索:边聊边回填的"一条"记录(不是每轮一条)
                    "turns": [],             # 逐句对话:每说一句 append 一条,有上限
                    "slack_thread_ts": None, # 这通对话在 Slack 那条 thread 的根消息 ID(不是时间!)
                    "meta": meta or {},      # 附加信息:来源页面、语言等
                }
                self._data[sid] = s
                self._evict_over_cap_locked()   # 新增后可能超全局上限,顺手淘汰最久没用的
            else:
                s["last_seen"] = _now()
                self._data.move_to_end(sid)     # 标记为"最近使用"(挪到末尾,LRU 用)
            return s

    def touch(self, sid):
        """只刷新活跃时间(比如用户点了跳转按钮,没产生对话,但人还在)。"""
        with self._lock:
            s = self._data.get(sid)
            if s:
                s["last_seen"] = _now()
                self._data.move_to_end(sid)

    # ======================== 写入 ========================
    def set_entry_intent(self, sid, entry_intent):
        """
        种下"入口意图"(用户进来时点的话题按钮)。规则:第一个话题按钮说了算,后面不覆盖。

        为什么首次锁定:这个字段记录的是"从哪个入口进来"(投放/转化归因用),是一个确定性的
        入口信号,不该被后续点击冲掉。而"用户聊着聊着诉求变了"由 lead.need 承接(每轮 LLM 重抽、
        非空即覆盖),两者分工:entry_intent=从哪进来(不变),need=现在想要什么(随对话演变)。

        例:用户先点 [🏭 ODM] → entry_intent="odm";之后即使又触发别的话题,也不把 "odm" 冲掉。
        """
        with self._lock:
            s = self._data.get(sid)
            if s and not s["entry_intent"]:
                s["entry_intent"] = entry_intent

    def append_turn(self, sid, role, text):
        """
        追加一轮对话(role = "user" 或 "assistant")。
        【改动②】每条 turn 只存 {role, text},不再存没用到的时间戳 ts。

        【闸门①:单会话轮数上限】只留最新 MAX_TURNS 轮,更早的已在 Slack 归档,内存里可丢。
        例:MAX_TURNS=4,已有 [m0,m1,m2,m3],又来 m4 →
            append 后 [m0,m1,m2,m3,m4](5 条)→ 超了 → 裁成 [m1,m2,m3,m4]。
        """
        with self._lock:
            s = self._data.get(sid)
            if not s:
                return
            s["turns"].append({"role": role, "text": text})
            if len(s["turns"]) > MAX_TURNS:
                s["turns"] = s["turns"][-MAX_TURNS:]   # 只留末尾(最新)MAX_TURNS 条
            s["last_seen"] = _now()
            self._data.move_to_end(sid)

    def update_lead(self, sid, fields):
        """
        把新识别到的线索字段合并进"那一条" lead 记录,并重算 missing(还缺什么)。

        【关键1:lead 是一条记录、不断回填,不是每轮一条】
          初始    lead={} → 第1句 update_lead({need:"录音麦"}) → {need:"录音麦"}
          第2句 update_lead({email:"a@x.com"}) → {need:"录音麦", email:"a@x.com"}
        【关键2:非空才覆盖,空值不冲掉已有】所以支持"纠错"——
          用户先说 a@x.com,后说"写错了是 b@y.com" → LLM 提取新邮箱(非空)→ 覆盖成 b@y.com;
          某轮没提邮箱(提取为空)→ 跳过,不会把已存的 b@y.com 抹掉。
        【关键3:email/phone 写入前先校验格式】不合格就不写(代码兜底,不只信 prompt)。
          例:LLM 误把 "gmac" 当邮箱 → _valid_email 判 False → 不写 → missing 里仍标 contact → bot 继续追问。
        【关键4:什么算"必填(required)"→ 只有 need + 一种联系方式】missing 只追这两样;
          name/company 是"有就记、没有不追"的加分项,不进 missing。这套口径必须和 prompts.py
          PERSONA 目标 #2 一致(那里教 bot 追什么,这里决定卡片/lead_line 标什么缺),否则两边打架。
          例:lead={need:"录音麦"} 但没留邮箱/电话 → missing=["contact"] → bot 追"留个邮箱吧";
              lead={email:"a@x.com"} 但没说想要啥 → missing=["need"] → bot 追"您具体想做什么产品?"。
        """
        with self._lock:
            s = self._data.get(sid)
            if not s or not fields:
                return
            for k in _LEAD_FIELDS:
                v = fields.get(k)
                if not v:
                    continue                       # 空值跳过(保护已有值,支持纠错)
                if k == "email" and not _valid_email(v):
                    continue                       # 邮箱格式不对,不写
                if k == "phone" and not _valid_phone(v):
                    continue                       # 电话位数不对,不写
                s["lead"][k] = v                   # 非空且合格 → 覆盖写入(取最新一次)
            # 重算"还缺哪些必填信息"。必填 = 【need(想要什么)】+【一种联系方式(email 优先, phone 也算)】。
            # name/company 是加分项,给了就记、没有不追,故不进 missing。改这里务必同步 prompts.py PERSONA 目标 #2。
            has_contact = bool(s["lead"].get("email") or s["lead"].get("phone"))
            missing = []
            if not s["lead"].get("need"):
                missing.append("need")
            if not has_contact:
                missing.append("contact")
            s["lead"]["missing"] = missing

    def set_slack_ts(self, sid, ts):
        """记住这通对话在 Slack 的 thread 根消息 ID(之后 chat.update 靠它精准改那张卡)。"""
        with self._lock:
            s = self._data.get(sid)
            if s:
                s["slack_thread_ts"] = ts

    # ======================== 读取 ========================
    def window(self, sid, n=None):
        """
        取"最近 n 轮"喂给大模型(滑动窗口)。

        为什么不喂全部:每轮都带整段历史会越聊越贵越慢。只带最近 n 轮;
        丢掉的老对话 Slack 里有,关键事实(entry_intent/lead)另存,不会丢。
        例:HISTORY_TURNS=8,对话 30 轮 → 只取最后 8 轮发给模型。
        """
        n = n or HISTORY_TURNS
        with self._lock:
            s = self._data.get(sid)
            return list(s["turns"][-n:]) if s else []

    def snapshot(self, sid):
        """返回会话的一份拷贝(只读用,避免外部直接改内部结构)。"""
        with self._lock:
            s = self._data.get(sid)
            return dict(s) if s else None

    def stats(self):
        """当前有多少个活跃会话(给 /health 用)。"""
        with self._lock:
            return {"sessions": len(self._data)}

    # ======================== 淘汰 ========================
    def _evict_over_cap_locked(self):
        """
        【闸门③:全局数量上限 + LRU】(调用方已持锁)
        活跃会话超过 MAX_SESSIONS 时,从头部(最久没动的)一直删到不超标。
        例:上限=500,第 501 个进来 → 删掉最久没人说话的那 1 个(它只是数据,不是线程,删了不影响别人)。
        """
        while len(self._data) > MAX_SESSIONS:
            self._data.popitem(last=False)     # last=False 删最旧的

    def sweep_expired(self):
        """
        【闸门②:TTL 超时清理】把闲置超过 TTL_SECONDS 的会话删掉。由后台协程定时调用。
        删了不丢数据(Slack 有归档)。返回这次清理掉的数量。
        例:TTL=1800(30分钟),某会话 last_seen 是 40 分钟前 → 判过期 → 删除。
        """
        cutoff = _now() - TTL_SECONDS
        with self._lock:
            dead = [sid for sid, s in self._data.items() if s["last_seen"] < cutoff]
            for sid in dead:
                self._data.pop(sid, None)
            return len(dead)


# 模块级单例:整个进程共用这一个存储(所有用户的会话都在这里)
STORE = SessionStore()


async def run_sweeper(interval=300):
    """
    后台 TTL 清理协程(async 版):每 interval 秒扫一次过期会话。
    由 app 启动时用 asyncio.create_task 拉起。

    用协程而不是线程的好处:跑在同一个事件循环里,不会被 debug/重载器杀掉
    (回避了之前 Flask 线程被杀的坑,见记忆 feedback_flask-debug-threads)。
    例:每 5 分钟醒一次 → 把 30 分钟没人理的会话从内存清掉。
    """
    while True:
        await asyncio.sleep(interval)
        try:
            STORE.sweep_expired()
        except Exception:
            pass   # 清理协程绝不能因一次异常把自己搞挂
