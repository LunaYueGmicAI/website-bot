# -*- coding: utf-8 -*-
"""
端到端测试脚本(需要服务器在跑 + OpenAI key + 可选 Slack token)。
和 test_memory.py 的区别:test_memory.py 是纯内存单元测试(零依赖);本脚本打真实 HTTP,
会真的调 OpenAI(LLM+STT),用来验收"询盘捕获大迭代"的对话行为。

前置:
  1) 起服务器:  ./venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8090
  2) .env 里有 OPENAI_API_KEY(必需);SLACK_BOT_TOKEN(可选,验 Slack 卡)
  3) 语音用例需要 Windows(用系统 SAPI 合成 wav,无需麦克风)。非 Windows 会自动跳过语音。

跑:  ./venv/Scripts/python.exe tests/e2e_test.py [--base http://127.0.0.1:8090]
"""
import sys, io, os, json, argparse, urllib.request, subprocess, tempfile, mimetypes, uuid

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE = "http://127.0.0.1:8090"
_fails = []


def _check(name, cond, detail=""):
    tag = "OK" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  ::  {detail}" if detail else ""))
    if not cond:
        _fails.append(name)
    return cond


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _post_json(path, payload, expect_ok=True):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None


def _post_multipart(path, fields, filepath, filefield="audio"):
    boundary = "----wbtest" + uuid.uuid4().hex
    body = io.BytesIO()

    def w(s):
        body.write(s.encode("utf-8") if isinstance(s, str) else s)

    for k, v in fields.items():
        w(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
    fname = os.path.basename(filepath)
    ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    w(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{filefield}\"; filename=\"{fname}\"\r\n")
    w(f"Content-Type: {ctype}\r\n\r\n")
    with open(filepath, "rb") as f:
        w(f.read())
    w(f"\r\n--{boundary}--\r\n")
    req = urllib.request.Request(BASE + path, data=body.getvalue(),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None


def sapi_wav(text, path):
    """用 Windows SAPI 合成一段语音 wav(无需麦克风)。非 Windows 返回 False。"""
    if os.name != "nt":
        return False
    ps = (f'Add-Type -AssemblyName System.Speech; '
          f'$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; '
          f'$s.SetOutputToWaveFile("{path}"); $s.Speak("{text}"); $s.Dispose()')
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   capture_output=True)
    return os.path.exists(path)


def hr(t):
    print("\n" + "=" * 68 + f"\n{t}\n" + "=" * 68)


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    BASE = ap.parse_args().base

    # ---------- 确定性端点(不调 LLM) ----------
    hr("A. 确定性端点(健康/配置/按钮/错误分支)")
    st, d = _get("/health")
    _check("/health 200 + status ok", st == 200 and d.get("status") == "ok", d)
    st, d = _get("/config")
    _check("/config 有 greeting+quickActions+faq",
           st == 200 and "greeting" in d and len(d.get("quickActions", [])) >= 4 and len(d.get("faq", [])) >= 1)

    st, d = _post_json("/event", {"session_id": "e2e_odm", "action": "topic", "id": "odm", "page_url": "https://gmic.ai/"})
    _check("/event topic=odm 回 opener", st == 200 and "ODM" in (d or {}).get("reply", ""))
    st, d = _post_json("/event", {"session_id": "e2e_faq", "action": "faq", "index": 1})
    _check("/event faq index=1 回写死答案", st == 200 and (d or {}).get("reply"))
    st, d = _post_json("/event", {"session_id": "e2e_link", "action": "link", "id": "products"})
    _check("/event link=products 回 ok", st == 200 and (d or {}).get("ok") is True)
    st, _ = _post_json("/event", {"session_id": "e2e_bad", "action": "topic", "id": "nope"})
    _check("/event 坏 topic id -> 400", st == 400)
    st, _ = _post_json("/event", {"session_id": "e2e_bad", "action": "faq", "index": 99})
    _check("/event faq 越界 -> 400", st == 400)
    st, _ = _post_json("/chat", {"session_id": "e2e_bad", "text": "   "})
    _check("/chat 空 text -> 400", st == 400)
    st, _ = _post_json("/chat", {"session_id": "e2e_bad"})
    _check("/chat 缺字段 -> 422", st == 422)

    # ---------- 对话行为(调 LLM,验收大迭代) ----------
    hr("B. /chat 询盘捕获(禁脑补 / readback / messenger 甩链 / wants_channel / 多语言)")
    sid = "e2e_chatA"
    st, d = _post_json("/chat", {"session_id": sid, "text": "I want a custom AI voice recorder, ~2000 units."})
    _check("A1 need 场景有回复", st == 200 and bool((d or {}).get("reply")), (d or {}).get("reply"))
    st, d = _post_json("/chat", {"session_id": sid, "text": "email me at john(at)acme"})
    r = (d or {}).get("reply", "").lower()
    _check("A2 坏邮箱被点破(不脑补 john@acme)", "@acme" not in r and ("complete" in r or "valid" in r or "full" in r), (d or {}).get("reply"))
    st, d = _post_json("/chat", {"session_id": sid, "text": "sorry, it's john@acme.com"})
    _check("A3 干净邮箱被 readback", "john@acme.com" in (d or {}).get("reply", ""), (d or {}).get("reply"))

    st, d = _post_json("/chat", {"session_id": "e2e_chatB", "text": "500 wearable mics. Reach me on WhatsApp +1 650 555 1234"})
    ids = [c.get("id") for c in (d or {}).get("contacts", [])]
    _check("B 留 WhatsApp -> 路A 甩 whatsapp", "whatsapp" in ids, ids)

    st, d = _post_json("/chat", {"session_id": "e2e_chatC", "text": "What's your WhatsApp number?"})
    ids = [c.get("id") for c in (d or {}).get("contacts", [])]
    _check("C 问我们的号 -> 路B 甩 whatsapp", "whatsapp" in ids, ids)

    st, d = _post_json("/chat", {"session_id": "e2e_chatD", "text": "你好,我想定制会议录音麦,大概一千个"})
    reply = (d or {}).get("reply", "")
    _check("D 中文进 -> 中文回", any("一" <= ch <= "鿿" for ch in reply), reply)
    st, d = _post_json("/chat", {"session_id": "e2e_chatD", "text": "我的微信是 luna_gmic369"})
    ids = [c.get("id") for c in (d or {}).get("contacts", [])]
    _check("D 报微信 -> 甩 wechat", "wechat" in ids, ids)

    # ---------- 语音(SAPI 合成,无需麦克风) ----------
    hr("C. /voice 语音链路(真实语音 / 兜底 / 413)")
    tmp = tempfile.gettempdir()
    wav = os.path.join(tmp, "e2e_voice.wav")
    if sapi_wav("Hello, I want to order two thousand AI voice recorders for my company.", wav):
        st, d = _post_multipart("/voice", {"session_id": "e2e_voice", "page_url": "https://gmic.ai/"}, wav)
        _check("真实语音 -> 转写非空 + 有回复",
               st == 200 and bool((d or {}).get("transcript")) and bool((d or {}).get("reply")),
               (d or {}).get("transcript"))
    else:
        print("[SKIP] 非 Windows,跳过真实语音用例")

    garbage = os.path.join(tmp, "e2e_garbage.wav")
    with open(garbage, "wb") as f:
        f.write(os.urandom(2000))
    st, d = _post_multipart("/voice", {"session_id": "e2e_voice2"}, garbage)
    _check("垃圾音频 -> 兜底话术(不 500)", st == 200 and (d or {}).get("transcript") == "", (d or {}).get("reply"))

    big = os.path.join(tmp, "e2e_big.wav")
    with open(big, "wb") as f:
        f.write(b"\0" * 9_000_000)
    st, _ = _post_multipart("/voice", {"session_id": "e2e_voice3"}, big)
    _check("超大文件 -> 413", st == 413, st)

    hr("结果")
    if _fails:
        print(f"SOME FAILED ({len(_fails)}): " + ", ".join(_fails))
        sys.exit(1)
    print("ALL PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
