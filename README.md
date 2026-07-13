# website-bot

Chat + voice consultation bot for **gmic.ai**. Users type or talk; each inquiry is
forwarded to Slack (original audio + transcript + AI lead summary). Turn-based, not a
realtime voice agent.

See **[DESIGN.md](DESIGN.md)** for the full blueprint (UI, memory management, Slack layout,
interaction flow, phases).

## Layout
```
app.py              Flask API: /health /config /event /chat /voice
config/widget.json  quick-action buttons + FAQ (team-editable data)
bot/
  sessions.py       session store + memory management (TTL / LRU / sliding window)
  stt.py            Groq Whisper speech-to-text
  llm.py            OpenAI reply + structured lead extraction
  slack.py          lead card (chat.update) + thread detail replies
  prompts.py        system prompt + FAQ context
web/                widget frontend (P3)
```

## Run (local dev)
```bash
python -m venv venv
source venv/Scripts/activate      # Windows Git Bash;  source venv/bin/activate on *nix
pip install -r requirements.txt
cp .env.example .env               # fill in keys
python app.py                      # dev only
# production: gunicorn -w 1 -b 0.0.0.0:$PORT app:app   (single worker — see DESIGN.md)
```

## Status
P1 backend scaffold. Blocked on P0 (Slack app token) for end-to-end Slack test; everything
else runs with placeholder env.
