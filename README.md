# website-bot

Chat + voice consultation bot for **gmic.ai**. Users type or talk; each inquiry is
forwarded to Slack (original audio + transcript + AI lead summary). Turn-based, not a
realtime voice agent.

See **[DESIGN.md](DESIGN.md)** for the full blueprint (UI, memory management, Slack layout,
interaction flow, phases).

## 目录结构(按职责分层,便于扩展)
```
app.py                    入口:创建应用 + 跨域 + 注册路由(保持瘦)
config/widget.json        4 个按钮 + FAQ(团队可直接改的数据)
core/                     核心领域
  sessions.py             会话存储 + 内存管控(轮数上限/滑动窗口/TTL/LRU/线索回填)
  widget_config.py        加载 widget.json
ai/                       AI 能力
  stt.py                  Groq Whisper 语音转文字
  llm.py                  OpenAI 对话 + 线索抽取(一次调用两件事)
  prompts.py              人设 + 上下文拼装
integrations/             外部集成(现在 Slack;以后 WhatsApp 等加在这里)
  slack.py                线索卡(chat.update) + thread 明细回复
api/                      HTTP 路由
  routes.py               /health /config /event /chat /voice(Blueprint)
tests/test_memory.py      内存管控单测(无需 key)
web/                      widget 前端(P3,待建)
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
