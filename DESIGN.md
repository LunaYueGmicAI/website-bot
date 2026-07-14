# GMIC Website Bot — Design Blueprint

A turn-based chat + voice consultation bot embedded on gmic.ai. Users can **type or
talk**; every inquiry is forwarded to Slack with the original audio, transcript, and an
AI-extracted lead summary. **Not** a realtime voice agent — no LiveKit/SIP/RTP. Voice is
just an input method (ChatGPT-style mic dictation): record a clip → STT → same free chat.

## Why a bot (not the old HubSpot chat / not a plain FAQ menu)
- Guides low-friction visitors, guarantees we ask for contact info, captures leads to Slack.
- Fully isolated vertical slice — does **not** touch Meng's site code. Only touchpoint is
  the fabMic button on gmic.ai, which opens this widget.

## UI

```
┌──────────────────────────────┐
│  GMIC AI 助手            ✕    │
│  Hi 👋 想了解点什么?          │
│                              │
│  [🏭 定制/ODM]  [📦 看产品]   │  ← 4 quick-action buttons
│  [📅 预约演示]  [❓ 常见问题] │
│                              │
│  ┄┄ conversation ┄┄          │
│                              │
│  [ 输入你的问题…       ] [🎤] │  ← persistent input: type OR tap mic
└──────────────────────────────┘
```

### Quick actions (4) — shortcuts, NOT the site nav
| Button | Behavior | Type | Seeds into memory | LLM cost |
|---|---|---|---|---|
| 🏭 定制/ODM | opens AI chat primed for ODM | topic | `intent="odm"` + context note | yes |
| 📦 看产品 | opens /products/ in new tab | link | logs a "viewed products" event | no |
| 📅 预约演示 | opens calendar in new tab | link | logs a "booking" event | no |
| ❓ 常见问题 | expands sub-questions → canned answers | faq | appends Q&A to turns (optional) | no |

Voice is **not** a quick action — it lives in the input bar as a mic button. Typing and
talking both land in the **same free AI conversation** (voice just goes through STT first).

## Data model — one session per user, keyed by `session_id`

```
session[session_id] = {
  created_at, last_seen,                 # for TTL / LRU
  intent: "odm" | null,                  # seeded by a topic button (single value)
  lead: {name, email, phone, company, need, missing:[...]},  # ONE record, backfilled
  turns: [ {role, text, ts}, ... ],      # append per message (bounded)
  slack_thread_ts,                       # root msg ts of this convo's Slack thread
  meta: {page_url, lang}
}
```

- `session_id` is the master key: (1) RAM dict key, (2) Slack thread owner, (3) frontend
  identity (stored in browser localStorage → survives page reload).
- `lead` + `intent` = small durable **facts**, merged/overwritten as the chat reveals info
  (NOT one entry per turn).
- `turns` = verbose, disposable **history**.

## Memory management (§Memory)

**Two layers, one disposable:**
- **Working memory (RAM):** only the live conversation; can be evicted any time.
- **Archive (Slack):** every turn forwarded in real time → permanent source of truth.
  Because Slack has everything, RAM can be trimmed/evicted losslessly.

**Sent to the LLM each turn (bounded prompt):**
`system prompt + intent + lead summary + last N turns (sliding window)` — NOT full history.
Trimmed old turns already live in Slack. Facts (intent/lead) survive trimming cheaply.

**Four bounds on RAM growth:**
1. Per-session turn cap (`MAX_TURNS_IN_MEMORY`, keep newest).
2. TTL eviction — background sweeper drops sessions idle > `SESSION_TTL_SECONDS`.
3. Global session cap + LRU (`MAX_SESSIONS`).
4. Per-message limits: audio ≤ `MAX_AUDIO_SECONDS`; **audio blob deleted right after STT +
   Slack upload** (never held in RAM/disk).

**Lifecycle:** new → active (turns grow, lead fills) → idle → TTL/LRU evict (nothing lost).

## Slack forwarding — one channel, one thread per conversation

`#gmic-web-voice-leads`:
- **Thread root = lead card (condensed)**, updated in real time via `chat.update(ts)`
  (we store the root `ts` on first post). Fields: intent, contact ✅/❌, one-line need,
  status, source page.
- **Thread replies = detail:** voice → original audio file + transcript; text → the message.
- Channel = clean list of lead cards; open a thread to see the full exchange + audio.
- Real-time, not batched-at-end (there is no reliable "end").

## Interaction flow (voice turn)
```
widget ──POST /event {sid,intent}──► backend: create session + Slack card(root)→store ts
widget ──POST /voice {sid,audio}──► backend: Groq STT → append turn
                                     → LLM(window+intent+lead) → reply + lead update
                                     → Slack: postMessage(thread: audio+transcript+reply)
                                     → Slack: chat.update(card)
                                     → delete audio blob
       ◄── reply (text) ────────────
```

## How one bot serves many users
One async process, one dict keyed by `session_id`. Each user's messages route to their own
entry — never mix. A single async process handles many concurrent conversations because each
request spends its time `await`-ing external APIs (STT/LLM/Slack). Keep it **single process**
so the in-memory dict stays authoritative (multi-process would split it → session loss; add
Redis/sqlite only when scaling out).

## Language (multilingual, English default)
- Voice: Groq Whisper auto-detects language (`language=None`) → transcribes in whatever the
  visitor spoke (Chinese voice → Chinese text, English → English).
- Reply: the LLM is instructed to reply in the SAME language as the visitor's latest message.
- Default: when language is unclear/empty, fall back to English. All prompt text is written
  in English; code comments are Chinese; the seed `widget.json` UI copy is English.

## Stack
- Backend: **FastAPI (async)** on EC2, reverse-proxied via Cloudflare Tunnel to a stable
  subdomain (EC2 IP changes on restart — never point the widget at a raw IP). CORS restricted
  to gmic.ai. Async fits this I/O-bound workload (STT/LLM/Slack are all network waits) and a
  single async process keeps the in-memory session dict valid (no multi-worker split).
- STT: Groq Whisper (`whisper-large-v3`), async client.
- LLM: OpenAI (swappable) — reply + structured lead extraction, async client.
- Slack: `slack_sdk` bot token (chat:write, files:write).
- Config: `config/widget.json` — buttons + FAQ as data the team edits without code.

## Code layout (split by concern, for extensibility)
```
app.py            entry: create app + CORS + register routes (kept thin)
core/             domain: sessions.py (memory mgmt), widget_config.py
ai/               STT (stt.py), LLM (llm.py), prompts.py
integrations/     external services — slack.py now; WhatsApp/etc. later
api/routes.py     HTTP routes (Blueprint)
config/widget.json  buttons + FAQ (team-editable data)
tests/            memory-management checks
```

## Phases
- **P0 (Luna):** create Slack app → bot token + `#gmic-web-voice-leads` → fill `.env`.
- **P1:** backend (this repo) — sessions/memory, STT, LLM, Slack. ← in progress
- **P2:** Cloudflare Tunnel + systemd (stable URL).
- **P3:** widget frontend (chat UI + mic + quick actions).
- **P4:** hook to fabMic on gmic.ai + end-to-end test + deploy via wp-site patch.
- **P5 (later):** TTS voice reply, Firestore mirror, WhatsApp, multi-language.
