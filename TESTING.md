# Website Bot — 测试文稿 (Test Playbook)

gmic.ai 网站询盘 bot 的全套测试手册。覆盖三层:纯内存单元测试、确定性 HTTP 端点、
调真实 OpenAI 的对话/语音行为。最后一层验收的是「询盘捕获大迭代」(禁脑补/readback/
messenger 甩链/wants_channel/多语言)。

> 最近一次全量执行:**2026-07-16,本地 `127.0.0.1:8090`,全部 PASS**(单测 13/13 + 端到端 21/21)。

---

## 0. 前置

```bash
cd /c/Users/Luna/repos/website-bot
# venv 已就绪;.env 需含 OPENAI_API_KEY(必需)、SLACK_BOT_TOKEN + SLACK_CHANNEL(可选,验 Slack 卡)
```

启动后端(本地 8090;生产 EC2 是 8092 + systemd `website-bot.service`):

```bash
./venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8090
```

Widget 本地地址:`http://127.0.0.1:8090/widget/` ｜ 线上:`https://web-bot.telalive.us/widget/`

---

## 1. 层一 — 内存单元测试(零依赖,不调 API)

```bash
./venv/Scripts/python.exe tests/test_memory.py
```

验:轮数上限、滑动窗口、`missing=need+contact` 口径、entry_intent 首次锁定、
messengers 并集/同平台留最新、`match_our_channels`/`contact_for_channel`、LRU、TTL 清理。
**期望:`ALL PASS`(13 项)。**

---

## 2. 层二+三 — 一键端到端脚本(推荐)

```bash
# 服务器必须在跑。需要 OpenAI key;Windows 上会用系统 SAPI 合成语音(无需麦克风)。
./venv/Scripts/python.exe tests/e2e_test.py --base http://127.0.0.1:8090
```

**期望:`ALL PASS`(21 项)。** 下面是逐条清单(手动复现用 curl 也列在第 3 节)。

### A. 确定性端点(不调 LLM,秒回)

| 用例 | 请求 | 期望 |
|---|---|---|
| 健康 | `GET /health` | 200,`status:ok`,带 `sessions` 计数 |
| 配置 | `GET /config` | 200,含 greeting + ≥4 quickActions + ≥1 faq |
| 话题按钮 | `POST /event topic=odm` | 200,`reply` 为 ODM opener,**建 Slack 卡** |
| FAQ | `POST /event faq index=1` | 200,写死答案 + link |
| 跳转 | `POST /event link=products` | 200,`{"ok":true}` |
| 坏 topic id | `POST /event topic id=nope` | **400** |
| faq 越界 | `POST /event faq index=99` | **400** |
| 空消息 | `POST /chat text="   "` | **400** |
| 缺字段 | `POST /chat {无 text}` | **422**(Pydantic) |

### B. /chat 对话(调 LLM,验收大迭代)

| 用例 | 用户说 | 期望行为 |
|---|---|---|
| A1 need | "custom AI voice recorder, ~2000 units" | 抓到 need,自然追问联系方式 |
| **A2 禁脑补** ⭐ | "email me at john(at)acme" | **点破"看起来不完整",绝不脑补成 john@acme** |
| A3 readback | "sorry, it's john@acme.com" | 逐字复述 `john@acme.com` 请求确认 |
| B 路A 甩链 | "…WhatsApp +1 650 555 1234" | 抓 messenger + `contacts:[whatsapp]` 甩一次 |
| C 路B wants | "What's your WhatsApp number?" | **不自己编号**,甩我们的 `[whatsapp]` |
| D 多语言 | "你好,我想定制会议录音麦…" | 用中文回复 |
| D IM | "我的微信是 luna_gmic369" | 甩 `[wechat]` |

### C. /voice 语音(SAPI 合成,无需麦克风)

| 用例 | 输入 | 期望 |
|---|---|---|
| 真实语音 ⭐ | SAPI 合成英文询盘 wav | 200,`transcript` 非空,有 `reply`;**语音听错邮箱时同样触发禁脑补** |
| 兜底 | 2KB 随机字节 | 200,`transcript:""` + STT 兜底话术(**不 500**) |
| 超大 | 9MB 文件 | **413** |

> ⭐ 语音说邮箱 "sarah at gmic dot ai" → Whisper 实测转成 `saraa.mic.ai`(电话/语音 ASR 听邮箱极差,
> 已知坑)→ **bot 拒绝入库并要求重打**。这正是「禁脑补 + readback」为语音场景设计的核心价值。

---

## 3. 手动 curl 速查(逐个复现)

> Git Bash 下是 Windows curl:上传文件用 `C:/...` 正斜杠路径;中文 JSON 直接看会走管道乱码,
> 用第 2 节的 Python 脚本看最准。

```bash
B=http://127.0.0.1:8090

# 健康 / 配置
curl -s $B/health
curl -s $B/config

# 按钮
curl -s -X POST $B/event -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","action":"topic","id":"odm","page_url":"https://gmic.ai/"}'
curl -s -X POST $B/event -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","action":"faq","index":1}'

# 对话(禁脑补验收)
curl -s -X POST $B/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"s2","text":"email me at john(at)acme"}'

# 错误分支
curl -s -o /dev/null -w '%{http_code}\n' -X POST $B/event -H 'Content-Type: application/json' \
  -d '{"session_id":"s3","action":"topic","id":"nope"}'      # 400

# 语音(先用 PowerShell SAPI 合成 voice.wav,见 tests/e2e_test.py 的 sapi_wav)
curl -s -X POST $B/voice \
  -F "session_id=s4" -F "audio=@C:/path/voice.wav;type=audio/wav"
```

---

## 4. Slack 侧(需肉眼确认)

后端用 `.env` 里的 bot token 直发,自动化测不进 Slack UI。跑完 §2 后到频道 **`#web-bot`** 确认:

- 每个 topic/chat/voice 会话 = 一张**线索卡**(thread 根):`Entry / Email / Phone / Messengers /
  Name / Need / Source / Missing` 分行;`chat.update` 实时回填。
- thread 内明细:`👤 用户` / `🤖 bot` / `🎤 语音(附原始音频文件 + 转写)`。
- 判据:§2 全 PASS 且后端日志无 `Slack ... failed` / `Traceback`(Slack 失败会 try/except 吞掉,
  只在 log 留痕,不影响给用户的响应)。

---

## 5. 已知非缺陷 / 注意

- **垃圾音频日志里的 `openai.BadRequestError: audio could not be decoded`** = 预期:Whisper 拒解非语音字节,
  代码 try/except 兜底回 200 + 兜底话术。这是容错路径被走到,不是 bug。
- **OpenAI key 是生产共用那把**([[openai-key-pool-emergency]]);跑 LLM/语音用例会消耗额度。
- **`sessions` 计数只增不减**属正常:TTL 后台协程每 5 分钟清一次闲置会话。
- **待补测试项**:按 session/IP 限流(尚未实现);真实麦克风录音(苹果本)端到端。
