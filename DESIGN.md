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
| 🏭 定制/ODM | opens AI chat primed for ODM | topic | `entry_intent="odm"` + context note | yes |
| 📦 看产品 | opens /products/ in new tab | link | logs a "viewed products" event | no |
| 📅 预约演示 | opens calendar in new tab | link | logs a "booking" event | no |
| ❓ 常见问题 | expands sub-questions → canned answers | faq | appends Q&A to turns (optional) | no |

Voice is **not** a quick action — it lives in the input bar as a mic button. Typing and
talking both land in the **same free AI conversation** (voice just goes through STT first).

## Data model — one session per user, keyed by `session_id`

```
session[session_id] = {
  created_at, last_seen,                 # for TTL / LRU
  entry_intents: ["odm", ...],           # ENTRY tags: every entry the visitor used (topic button / questionnaire Tab /
                                          # voice-message), appended in arrival order, DEDUPED. [0] = primary attribution.
  answers: {tab: {qid: value|[values]}}, # questionnaire answers BUCKETED per Tab (one bucket per Tab done; re-doing a
                                          # Tab overwrites its bucket, a new Tab adds one) — multiple questionnaires coexist.
  recommendations: {tab: {...}},         # per-Tab recommendation (products/link/hint); only help-me-choose produces one.
  lead: {name, email, phone, messengers:[...], company, need, missing:[...]},  # ONE record, backfilled; `need` = evolving intent
                                          # messengers = LIST (WhatsApp/WeChat/Telegram...), ONE per platform,
                                          # latest wins (different platforms union; same platform overwrites).
                                          # a usable contact = email OR phone OR any messenger (any ONE is enough)
  turns: [ {role, text, ts}, ... ],      # append per message (bounded)
  slack_thread_ts,                       # root msg ts of this convo's Slack thread
  meta: {page_url, lang}
}
```

- `session_id` is the master key: (1) RAM dict key, (2) Slack thread owner, (3) frontend
  identity (stored in browser localStorage → survives page reload).
- `lead` + `entry_intents` + `answers` = small durable **facts**, merged as the chat reveals info
  (NOT one entry per turn). `entry_intents` = which entries they used (accumulated, deduped, first = primary);
  `answers` = what they picked in each questionnaire Tab; `lead.need` = what they want now (evolves each
  turn) — a mid-chat pivot updates `need`, not `entry_intents`.
- `turns` = verbose, disposable **history**.

## Memory management (§Memory)

**Two layers, one disposable:**
- **Working memory (RAM):** only the live conversation; can be evicted any time.
- **Archive (Slack):** every turn forwarded in real time → permanent source of truth.
  Because Slack has everything, RAM can be trimmed/evicted losslessly.

**Sent to the LLM each turn (bounded prompt):**
`system prompt + entry_intents + questionnaire answers + lead summary + last N turns (sliding window)` — NOT full history.
Trimmed old turns already live in Slack. Facts (entry_intents/answers/lead) survive trimming cheaply.

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
  (we store the root `ts` on first post). Fields: entry, contact ✅/❌, one-line need,
  status, source page.
- **Thread replies = detail:** voice → original audio file + transcript; text → the message.
- Channel = clean list of lead cards; open a thread to see the full exchange + audio.
- Real-time, not batched-at-end (there is no reliable "end").

## Chat and voice are now SEPARATE features (2026-07-17 split)
The chatbot is **text-only**. Voice was pulled out into a standalone **"voice message"** feature
whose entry is a 🎙️ button in the contacts row. Why split: speech-to-text mishears emails and
spelled-out letters badly, so instead of transcribing a contact from voice, the voice popup makes
the visitor **type one contact (email/phone/IM) — required — before Send is enabled**. The voice
then only carries the *need* description, where a transcription slip does no harm. This sidesteps
the whole "voice captured the wrong email" problem class.

### Chat turn (text)
```
[browser] type → POST /chat {session_id, text}
  → append "user" turn → ensure Slack card + post_detail(👤)
  → llm.respond(snapshot, faq, last-N turns) [OpenAI, Structured Outputs] → reply + extracted lead
  → update_lead + post_detail(🤖) + update_card → throwbacks (direct-contact links)
  → return {reply, contacts}
```

### Voice message (two calls)
```
[browser popup] pick contact type + type value (REQUIRED, validated) ; hold 🎙️ to record (live waveform)
  1) release → POST /voice/transcribe {audio}  → transcript (preview, editable — WeChat-style)
  2) tap Send (enabled only when contact valid + audio present)
       → POST /voice/message {session_id, contact_type, contact_value, text(edited), audio}
[backend api/routes.py::voice_message()]
  0) audio.read(MAX+1); if > MAX_AUDIO_BYTES → 413
  1) _validate_contact(type, value)  # SERVER-side gate; invalid/empty → 400 (never trust client only)
  2) transcript = edited text, else stt.transcribe(bytes) [OpenAI Whisper]
  3) get_or_create + set_entry_intent("voice-message") + update_lead(contact + need=transcript)
  4) slack.ensure_card + post_detail(🎤 original audio) [Slack]  # lead already filled → no update_card
  5) return {ok, transcript}   # no LLM — it's a message drop, not a conversation
```
The audio blob is a per-request local variable — released when the function returns, never
persisted to disk. `/event` (button clicks) creates the session + Slack card but usually returns a
canned reply without touching the LLM.

**Slack cards are tagged by source:** voice messages render `*🎙️ New VOICE message*`, chat
inquiries render `*💬 New CHAT inquiry*` (branch on `"voice-message" in entry_intents`), so the
team can tell at a glance which leads came in by voice vs typed chat.

**Slack delivery = in-process queue + single worker (`integrations/slack.py`).** Slack limits a
channel to ~1 message/sec. `ensure_card` / `update_card` / `post_detail` no longer call the API
inline — they build a job and enqueue it, then return immediately (so `/chat` and `/voice/message`
never block on Slack). One background worker (started/stopped in the app lifespan) drains the FIFO
queue, spacing sends by `SLACK_MIN_INTERVAL` (1.1s) and retrying on HTTP 429 up to
`SLACK_MAX_RETRIES` (5), honoring `Retry-After`. This stops bursty traffic (e.g. an ad spike) from
silently dropping leads — the old code swallowed 429s. FIFO + single worker also guarantees a
session's card is created before its thread replies go out.

## How one bot serves many users
One async process, one dict keyed by `session_id`. Each user's messages route to their own
entry — never mix. A single async process handles many concurrent conversations because each
request spends its time `await`-ing external APIs (STT/LLM/Slack); while user A waits on Groq,
the event loop serves B and C. `SessionStore` methods are synchronous & non-awaiting, so they
are atomic on the event loop (no data races).

Real ceilings to know before scaling:
1. **Do NOT naively add worker processes.** The session dict lives in *process* memory; multiple
   uvicorn workers would split sessions → a user's 2nd turn could hit a worker that has no record
   of them. Horizontal scale needs a shared store (Redis) or sticky sessions. The design is
   deliberately **single-process** (async single-process already handles solid concurrency).
2. **The real bottleneck is the external APIs, not our code.** More users → more Groq/OpenAI
   calls → possible quota/rate-limit hits (see the 2026-06-29 OpenAI key-exhaustion incident).
3. **No per-user rate limiting yet.** Someone spamming `/chat` or `/voice/message` burns OpenAI
   spend (LLM + STT) with no throttle. Add per-session/IP limiting before scaling. Target today:
   ~200 concurrent users (single async worker is comfortable there).

Bounds already in place: `MAX_SESSIONS` (LRU) + TTL sweep keep memory finite regardless of load.

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
