"""
会话存储 + 对话内存管控。

【两层记忆模型】
  - 工作记忆(本模块,放在进程内存/RAM):只保存"当前正在进行"的对话,尽量小。
  - 归档(Slack):每一轮对话一产生就实时转发到 Slack,永久保存。
  因为 Slack 里什么都有,所以内存这一层可以随时裁剪/清理,丢了也不心疼(不丢数据)。

【为什么要管控内存】
  如果每通对话都无限增长、且永不释放,服务器内存会被慢慢吃光。所以我们用四道闸门
  把内存控制在很小的范围(详见下面各方法的注释)。

【主键 session_id】
  每个用户对应一个 session_id,它同时是:①本字典的 key ②Slack thread 的归属
  ③前端浏览器的身份。一个进程、一个字典、很多用户,靠 key 隔开,永远不会串。
"""
import os
import time
import threading
from collections import OrderedDict

# ---- 可调参数(从 .env 读,给了默认值)----
TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))    # 闲置多久算过期(默认 30 分钟)
MAX_TURNS = int(os.getenv("MAX_TURNS_IN_MEMORY", "20"))        # 单会话内存里最多留几轮
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "500"))          # 全局最多同时保留几个会话
HISTORY_TURNS = int(os.getenv("LLM_HISTORY_TURNS", "8"))       # 每轮喂给大模型的最近几轮

# lead(线索)里我们关心的字段。missing 字段单独算,不在这里。
_LEAD_FIELDS = ("name", "email", "phone", "company", "need")


def _now():
    # 当前时间戳(秒)。单独包一层方便测试。
    return time.time()


class SessionStore:
    def __init__(self):
        # 用 OrderedDict 而不是普通 dict:它能记住插入/访问顺序,
        # 这样实现 LRU(最近最少使用)淘汰几乎零成本——把刚用过的挪到末尾,
        # 要淘汰时直接从头部(最久没动的)删。
        self._data = OrderedDict()
        # 多个用户的请求会并发进来(Flask 多线程),字典读写要加锁,防止数据错乱。
        self._lock = threading.Lock()

    # ======================== 生命周期 ========================
    def get_or_create(self, sid, meta=None):
        """
        拿到某用户的会话;没有就新建。

        例:用户第一次点开 widget,前端带着 session_id="sess_ab12" 请求进来 →
            这里没有它 → 新建一条空会话(intent 空、lead 空、turns 空)。
            用户第二次说话,同一个 sid 再进来 → 直接返回上次那条,并更新"最近活跃时间"。
        """
        with self._lock:
            s = self._data.get(sid)
            if s is None:
                # 新建一条会话骨架
                s = {
                    "id": sid,
                    "created_at": _now(),
                    "last_seen": _now(),
                    "intent": None,          # 按钮种下的话题,例如 "odm";只有一个值
                    "lead": {},              # 线索:边聊边回填的"一条"记录(不是每轮一条)
                    "turns": [],             # 逐句对话:每说一句 append 一条,有上限
                    "slack_thread_ts": None, # 这通对话在 Slack 里那条 thread 的根消息 ID
                    "meta": meta or {},      # 附加信息:来源页面、语言等
                }
                self._data[sid] = s
                # 新增后可能超出全局上限,顺手淘汰最久没用的
                self._evict_over_cap_locked()
            else:
                # 已存在:刷新活跃时间,并标记为"最近使用"(挪到末尾,LRU 用)
                s["last_seen"] = _now()
                self._data.move_to_end(sid)
            return s

    def touch(self, sid):
        """只刷新活跃时间(比如用户点了个跳转按钮,没产生对话,但人还在)。"""
        with self._lock:
            s = self._data.get(sid)
            if s:
                s["last_seen"] = _now()
                self._data.move_to_end(sid)

    # ======================== 写入 ========================
    def set_intent(self, sid, intent):
        """
        种下话题意图。规则:第一个话题按钮说了算,后面不覆盖。

        例:用户先点了 [🏭 ODM] → intent="odm"。
            后面就算又触发了别的话题,也不把 "odm" 冲掉——避免聊着聊着主题被改乱。
        """
        with self._lock:
            s = self._data.get(sid)
            if s and not s["intent"]:      # 只有当前为空才写
                s["intent"] = intent

    def append_turn(self, sid, role, text):
        """
        追加一轮对话(role = "user" 或 "assistant")。

        【单会话轮数上限——第 1 道闸门】
        只保留最新的 MAX_TURNS 轮,更早的已经在 Slack 里归档过了,内存里可以丢。
        例:MAX_TURNS=4,已有 [m0,m1,m2,m3],又来一句 m4 →
            append 后变成 [m0,m1,m2,m3,m4](5 条)→ 超了 → 裁掉最旧的,留 [m1,m2,m3,m4]。
        """
        with self._lock:
            s = self._data.get(sid)
            if not s:
                return
            s["turns"].append({"role": role, "text": text, "ts": _now()})
            if len(s["turns"]) > MAX_TURNS:
                s["turns"] = s["turns"][-MAX_TURNS:]   # 只留末尾(最新)MAX_TURNS 条
            s["last_seen"] = _now()
            self._data.move_to_end(sid)

    def update_lead(self, sid, fields):
        """
        把新识别到的线索字段合并进"那一条" lead 记录,并重新计算还缺什么(missing)。

        【关键:lead 是一条记录、不断回填,不是每轮存一条】
        例:
          初始    lead = {}
          第1句后 update_lead(需求)     → lead = {need:"录音麦"}
          第2句后 update_lead(数量+邮箱) → lead = {need:"录音麦", email:"a@x.com"}
          每次都往同一条里 merge,空值不覆盖已有值。
        missing 逻辑:必须有 name;email 和 phone 至少有一个(算"有联系方式")。
        """
        with self._lock:
            s = self._data.get(sid)
            if not s or not fields:
                return
            for k in _LEAD_FIELDS:
                v = fields.get(k)
                if v:                       # 只合并非空字段,不用空值把已有的冲掉
                    s["lead"][k] = v
            # 重新算"还缺哪些关键信息",供 bot 决定要不要追问
            has_contact = bool(s["lead"].get("email") or s["lead"].get("phone"))
            missing = []
            if not s["lead"].get("name"):
                missing.append("name")      # 缺姓名
            if not has_contact:
                missing.append("contact")   # 缺联系方式
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

        【为什么不喂全部历史】
        每轮都把整段对话发给模型,会越聊越贵、越慢。所以只带最近 n 轮;
        被丢掉的老对话 Slack 里都有,而关键事实(intent/lead)另外单独存着,不会丢。
        例:HISTORY_TURNS=8,对话有 30 轮 → 只取最后 8 轮发给模型。
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
        【全局数量上限 + LRU——第 3 道闸门】(注意:调用方已持锁)
        活跃会话超过 MAX_SESSIONS 时,从头部(最久没动的)一直删到不超标。
        例:上限=500,现在第 501 个进来 → 删掉最久没人说话的那 1 个。
        """
        while len(self._data) > MAX_SESSIONS:
            self._data.popitem(last=False)   # last=False 删的是最旧的

    def sweep_expired(self):
        """
        【TTL 超时清理——第 2 道闸门】
        把闲置超过 TTL_SECONDS 的会话删掉。由后台线程定时调用。删了不丢数据(Slack 有归档)。
        例:TTL=1800(30分钟),某会话 last_seen 是 40 分钟前 → 判定过期 → 删除。
        返回这次清理掉的数量。
        """
        cutoff = _now() - TTL_SECONDS       # 早于这个时间点算过期
        with self._lock:
            dead = [sid for sid, s in self._data.items() if s["last_seen"] < cutoff]
            for sid in dead:
                self._data.pop(sid, None)
            return len(dead)


# 模块级单例:整个进程共用这一个存储(所有用户的会话都在这里)
STORE = SessionStore()


def start_sweeper(interval=300):
    """
    启动后台 TTL 清理线程(每 interval 秒扫一次),生产环境只调一次。

    ⚠️ 千万别在 Flask 的 debug 重载模式下启动:重载器会杀掉后台线程
       (踩过的坑,见记忆 feedback_flask-debug-threads)。app.py 里已做判断。
    """
    def _loop():
        while True:
            time.sleep(interval)
            try:
                STORE.sweep_expired()
            except Exception:
                pass  # 清理线程绝不能因为一次异常就把自己搞挂
    t = threading.Thread(target=_loop, name="session-sweeper", daemon=True)
    t.start()
    return t
