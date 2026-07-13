"""
Session store + memory management.

Working memory (RAM) holds only the live conversation; Slack is the permanent archive
(every turn is forwarded there in real time), so RAM can be trimmed/evicted losslessly.

Bounds on growth (see DESIGN.md §Memory):
  1. per-session turn cap        -> keep newest MAX_TURNS
  2. TTL eviction                -> sweep sessions idle > TTL_SECONDS
  3. global cap + LRU            -> evict least-recently-used beyond MAX_SESSIONS
  (4. per-message size limits    -> enforced by callers / app.py, not here)

The store is keyed by session_id, which is also the Slack-thread owner and the browser's
identity. One process, one store, many users -> never mix (separated by key).
"""
import os
import time
import threading
from collections import OrderedDict

TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
MAX_TURNS = int(os.getenv("MAX_TURNS_IN_MEMORY", "20"))
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "500"))
HISTORY_TURNS = int(os.getenv("LLM_HISTORY_TURNS", "8"))

_LEAD_FIELDS = ("name", "email", "phone", "company", "need")


def _now():
    return time.time()


class SessionStore:
    def __init__(self):
        # OrderedDict gives us cheap LRU: most-recently-touched at the end.
        self._data = OrderedDict()
        self._lock = threading.Lock()

    # ---- lifecycle ----------------------------------------------------------
    def get_or_create(self, sid, meta=None):
        with self._lock:
            s = self._data.get(sid)
            if s is None:
                s = {
                    "id": sid,
                    "created_at": _now(),
                    "last_seen": _now(),
                    "intent": None,
                    "lead": {},              # backfilled over time, ONE record
                    "turns": [],             # append per message, bounded
                    "slack_thread_ts": None,
                    "meta": meta or {},
                }
                self._data[sid] = s
                self._evict_over_cap_locked()
            else:
                s["last_seen"] = _now()
                self._data.move_to_end(sid)   # mark most-recently-used
            return s

    def touch(self, sid):
        with self._lock:
            s = self._data.get(sid)
            if s:
                s["last_seen"] = _now()
                self._data.move_to_end(sid)

    # ---- mutations ----------------------------------------------------------
    def set_intent(self, sid, intent):
        with self._lock:
            s = self._data.get(sid)
            if s and not s["intent"]:      # first topic button wins; don't clobber
                s["intent"] = intent

    def append_turn(self, sid, role, text):
        with self._lock:
            s = self._data.get(sid)
            if not s:
                return
            s["turns"].append({"role": role, "text": text, "ts": _now()})
            # per-session turn cap: keep newest MAX_TURNS (older ones are in Slack)
            if len(s["turns"]) > MAX_TURNS:
                s["turns"] = s["turns"][-MAX_TURNS:]
            s["last_seen"] = _now()
            self._data.move_to_end(sid)

    def update_lead(self, sid, fields):
        """Merge non-empty fields into the single lead record; recompute `missing`."""
        with self._lock:
            s = self._data.get(sid)
            if not s or not fields:
                return
            for k in _LEAD_FIELDS:
                v = fields.get(k)
                if v:
                    s["lead"][k] = v
            has_contact = bool(s["lead"].get("email") or s["lead"].get("phone"))
            missing = []
            if not s["lead"].get("name"):
                missing.append("name")
            if not has_contact:
                missing.append("contact")
            s["lead"]["missing"] = missing

    def set_slack_ts(self, sid, ts):
        with self._lock:
            s = self._data.get(sid)
            if s:
                s["slack_thread_ts"] = ts

    # ---- reads --------------------------------------------------------------
    def window(self, sid, n=None):
        """Last n turns to feed the LLM (sliding window; older turns live in Slack)."""
        n = n or HISTORY_TURNS
        with self._lock:
            s = self._data.get(sid)
            return list(s["turns"][-n:]) if s else []

    def snapshot(self, sid):
        with self._lock:
            s = self._data.get(sid)
            return dict(s) if s else None

    def stats(self):
        with self._lock:
            return {"sessions": len(self._data)}

    # ---- eviction -----------------------------------------------------------
    def _evict_over_cap_locked(self):
        # caller holds the lock. Drop least-recently-used beyond MAX_SESSIONS.
        while len(self._data) > MAX_SESSIONS:
            self._data.popitem(last=False)

    def sweep_expired(self):
        """Remove sessions idle beyond TTL. Returns count evicted. Losless (Slack has all)."""
        cutoff = _now() - TTL_SECONDS
        with self._lock:
            dead = [sid for sid, s in self._data.items() if s["last_seen"] < cutoff]
            for sid in dead:
                self._data.pop(sid, None)
            return len(dead)


# module-level singleton
STORE = SessionStore()


def start_sweeper(interval=300):
    """
    Background TTL sweeper. Call ONCE from production startup.
    NOTE: do not start under Flask's debug reloader — the reloader kills background
    threads (see feedback_flask-debug-threads). app.py guards this.
    """
    def _loop():
        while True:
            time.sleep(interval)
            try:
                STORE.sweep_expired()
            except Exception:
                pass  # never let the sweeper crash the process
    t = threading.Thread(target=_loop, name="session-sweeper", daemon=True)
    t.start()
    return t
