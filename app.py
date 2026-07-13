"""
GMIC website bot — Flask API.

Endpoints:
  GET  /health            liveness
  GET  /config            widget config (greeting, quick-action buttons, faq)
  POST /event             quick-action clicks (topic / faq / link)
  POST /chat              a typed message  -> AI reply
  POST /voice             a recorded clip  -> STT -> AI reply (multipart)

Every user is a `session_id` (sent by the widget). One process, one store, many users.
Each turn is forwarded to Slack (card + thread detail) in real time.

Dev:  python app.py
Prod: gunicorn -w 1 -b 0.0.0.0:$PORT app:app     (single worker — see DESIGN.md)
"""
import os
import json

from dotenv import load_dotenv
load_dotenv()  # before reading any env / importing modules that read env at import time

from flask import Flask, request, jsonify
from flask_cors import CORS

from bot import sessions, stt, llm, slack

# ---- config ----------------------------------------------------------------
_CFG_PATH = os.path.join(os.path.dirname(__file__), "config", "widget.json")
with open(_CFG_PATH, encoding="utf-8") as f:
    CONFIG = json.load(f)
_ACTIONS = {a["id"]: a for a in CONFIG.get("quickActions", [])}

MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(8_000_000)))  # ~ generous cap

app = Flask(__name__)
_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
CORS(app, resources={r"/*": {"origins": _origins or "*"}})

STORE = sessions.STORE


def _meta():
    body = request.get_json(silent=True) or {}
    return {"page_url": body.get("page_url"), "lang": body.get("lang")}


def _sid():
    body = request.get_json(silent=True) or {}
    sid = body.get("session_id") or request.form.get("session_id")
    return sid


# ---- routes ----------------------------------------------------------------
@app.get("/health")
def health():
    return jsonify(status="ok", **STORE.stats())


@app.get("/config")
def config():
    # Frontend loads greeting + buttons + faq from here (team edits config/widget.json).
    return jsonify(CONFIG)


@app.post("/event")
def event():
    body = request.get_json(silent=True) or {}
    sid = body.get("session_id")
    action = body.get("action")          # "topic" | "faq" | "link"
    if not sid or not action:
        return jsonify(error="session_id and action required"), 400

    STORE.get_or_create(sid, _meta())

    if action == "topic":
        act = _ACTIONS.get(body.get("id"), {})
        STORE.set_intent(sid, act.get("intent"))
        opener = act.get("opener", "")
        if opener:
            STORE.append_turn(sid, "assistant", opener)
        slack.ensure_card(STORE, sid)
        slack.update_card(STORE, sid)
        return jsonify(reply=opener)

    if action == "faq":
        faq = CONFIG.get("faq", [])
        idx = body.get("index")
        if idx is None or not (0 <= idx < len(faq)):
            return jsonify(error="bad faq index"), 400
        item = faq[idx]
        # record the canned Q&A so a follow-up can build on it (no LLM cost)
        STORE.append_turn(sid, "user", item["q"])
        STORE.append_turn(sid, "assistant", item["a"])
        return jsonify(reply=item["a"], link=item.get("link"))

    if action == "link":
        STORE.touch(sid)                 # just log the click; navigation is client-side
        return jsonify(ok=True)

    return jsonify(error="unknown action"), 400


@app.post("/chat")
def chat():
    body = request.get_json(silent=True) or {}
    sid = body.get("session_id")
    text = (body.get("text") or "").strip()
    if not sid or not text:
        return jsonify(error="session_id and text required"), 400

    STORE.get_or_create(sid, _meta())
    STORE.append_turn(sid, "user", text)
    slack.ensure_card(STORE, sid)
    slack.post_detail(STORE, sid, f"👤 {text}")

    reply, lead = _run_llm(sid)
    slack.post_detail(STORE, sid, f"🤖 {reply}")
    slack.update_card(STORE, sid)
    return jsonify(reply=reply)


@app.post("/voice")
def voice():
    sid = request.form.get("session_id")
    if not sid:
        return jsonify(error="session_id required"), 400
    f = request.files.get("audio")
    if not f:
        return jsonify(error="audio file required"), 400

    audio = f.read()
    if len(audio) > MAX_AUDIO_BYTES:
        return jsonify(error="audio too large"), 413

    meta = {"page_url": request.form.get("page_url"), "lang": request.form.get("lang")}
    STORE.get_or_create(sid, meta)

    transcript = stt.transcribe(audio, filename=f.filename or "voice.webm",
                                language=request.form.get("lang") or None)
    if not transcript:
        return jsonify(reply="没太听清,能再说一遍或直接打字吗?", transcript="")

    STORE.append_turn(sid, "user", transcript)
    slack.ensure_card(STORE, sid)
    # original audio + transcript both go to Slack (thread reply)
    slack.post_detail(STORE, sid, f"🎤 {transcript}", audio_bytes=audio,
                      filename=f.filename or "voice.webm")

    reply, lead = _run_llm(sid)
    slack.post_detail(STORE, sid, f"🤖 {reply}")
    slack.update_card(STORE, sid)
    # audio bytes are local to this request -> dropped when it returns (never retained)
    return jsonify(reply=reply, transcript=transcript)


def _run_llm(sid):
    """Run one LLM turn, apply lead updates, return (reply, lead)."""
    snap = STORE.snapshot(sid)
    window = STORE.window(sid)
    reply, lead = llm.respond(snap, CONFIG.get("faq", []), window)
    if lead:
        STORE.update_lead(sid, lead)
    if reply:
        STORE.append_turn(sid, "assistant", reply)
    return reply, lead


if __name__ == "__main__":
    # dev server. Do NOT start the sweeper under the debug reloader (it kills threads —
    # see feedback_flask-debug-threads); start it only in the real run.
    debug = os.getenv("FLASK_DEBUG") == "1"
    if not debug:
        sessions.start_sweeper()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8090")), debug=debug)
