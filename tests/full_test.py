# -*- coding: utf-8 -*-
"""
完整端到端测试(中英文全覆盖 + 语音)。比 e2e_test.py 更全:把「询盘捕获大迭代」
里的每个功能点都用中英两种语言各测一遍,并覆盖全部确定性端点与错误分支。

前置:
  1) 起服务器:  ./venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8090
  2) .env 里有 OPENAI_API_KEY(必需);SLACK_BOT_TOKEN(可选,验 Slack 卡,失败只记 log 不影响)
  3) 语音用例需 Windows(用系统 SAPI 合成 wav,无需麦克风)。本机已确认有:
       Microsoft Zira/David(en-US) + Microsoft Huihui(zh-CN) → 中英语音都能跑。

跑:
  ./venv/Scripts/python.exe tests/full_test.py                 # 全跑(含 LLM + 语音,消耗 OpenAI 额度)
  ./venv/Scripts/python.exe tests/full_test.py --no-voice      # 跳过语音(只 LLM 对话)
  ./venv/Scripts/python.exe tests/full_test.py --base http://127.0.0.1:8090

判定说明:
  [OK]/[FAIL] = 硬断言(行为可靠,失败即算回归,计入总分)。
  [i]         = 软观察(LLM 措辞类,只打印供肉眼看,不计分不判失败)。
  [SKIP]      = 环境不满足(如非 Windows / 缺中文语音)。
"""
import sys, io, os, json, time, argparse, urllib.request, urllib.error
import subprocess, tempfile, mimetypes, uuid

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE = "http://127.0.0.1:8090"
_hard_pass, _hard_fail, _soft, _skip = 0, 0, 0, 0
_fail_names = []


# ============================ HTTP 小工具 ============================
def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _post_json(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
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
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None


# ============================ 断言/打印 ============================
def ok(name, cond, detail=""):
    """硬断言:计入总分。"""
    global _hard_pass, _hard_fail
    tag = "OK" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ::  {detail}" if detail else ""))
    if cond:
        _hard_pass += 1
    else:
        _hard_fail += 1
        _fail_names.append(name)
    return cond


def info(name, detail=""):
    """软观察:只打印,不判成败(给 LLM 措辞类看)。"""
    global _soft
    _soft += 1
    print(f"  [i]  {name}" + (f"  ::  {detail}" if detail else ""))


def skip(name, why=""):
    global _skip
    _skip += 1
    print(f"  [SKIP] {name}" + (f"  ::  {why}" if why else ""))


def hr(t):
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72)


def sub(t):
    print(f"\n--- {t} ---")


# 会话内多轮说话的便捷封装:返回 (reply, contact_ids, transcript?)
def say(sid, text, page_url="https://gmic.ai/"):
    st, d = _post_json("/chat", {"session_id": sid, "text": text, "page_url": page_url})
    d = d or {}
    ids = [c.get("id") for c in d.get("contacts", [])]
    return st, d.get("reply", ""), ids


def has_cjk(s):
    return any("一" <= ch <= "鿿" for ch in s or "")


def vm_transcribe(wav):
    """语音留言第一步:POST /voice/transcribe(只转写)。返回 (状态码, 转写)。"""
    st, d = _post_multipart("/voice/transcribe", {}, wav)
    return st, (d or {}).get("transcript", "")


def vm_message(sid, wav, ctype, cvalue, text=""):
    """语音留言第二步:POST /voice/message(联系方式必填 + 音频 → Slack)。返回 (状态码, resp)。"""
    fields = {"session_id": sid, "contact_type": ctype, "contact_value": cvalue,
              "text": text, "page_url": "https://gmic.ai/"}
    return _post_multipart("/voice/message", fields, wav)


# 坏邮箱"点破"关键词(中英)。命中任一即认为 bot 表达了"这不完整/请重发"。
_BADEMAIL_KWS = ["complete", "valid", "full address", "doesn't look", "does not look",
                 "again", "correct",
                 "完整", "有效", "重新", "看起来", "不太对", "不对", "再发", "确认一下", "填写完整"]


def complained(reply):
    r = (reply or "").lower()
    return any(k.lower() in r for k in _BADEMAIL_KWS)


# ============================ SAPI 语音合成 ============================
def sapi_wav(text, path, culture=None):
    """
    用 Windows SAPI 合成一段语音 wav。culture 例:"zh-CN" 选中文嗓音、"en-US" 选英文。
    找不到匹配语种的嗓音 → 返回 False(调用方据此 SKIP)。非 Windows → False。
    """
    if os.name != "nt":
        return False
    # 转义双引号,防 PowerShell 命令被打断
    safe = text.replace('"', '`"')
    select = ""
    if culture:
        # 选一个 Culture 以 culture 开头的嗓音;选不到就抛错让 Python 侧判 False
        select = (f'$v=$s.GetInstalledVoices()|?{{$_.VoiceInfo.Culture.Name -like "{culture}*"}}'
                  f'|Select-Object -First 1; if(-not $v){{exit 3}}; '
                  f'$s.SelectVoice($v.VoiceInfo.Name); ')
    ps = ('Add-Type -AssemblyName System.Speech; '
          '$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; '
          + select +
          f'$s.SetOutputToWaveFile("{path}"); $s.Speak("{safe}"); $s.Dispose()')
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    return r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 1000


# ============================ 主流程 ============================
def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--no-voice", action="store_true", help="跳过语音用例")
    a = ap.parse_args()
    BASE = a.base

    print(f"目标: {BASE}   voice={'off' if a.no_voice else 'on'}")

    # =================================================================
    hr("1. 确定性端点(不调 LLM,秒回)")
    # =================================================================
    sub("1.1 基础")
    st, d = _get("/health")
    ok("/health 200 + status ok", st == 200 and d.get("status") == "ok", d)
    st, d = _get("/config")
    ok("/config greeting+4按钮+contacts+faq",
       st == 200 and "greeting" in d and len(d.get("quickActions", [])) == 4
       and len(d.get("contacts", [])) >= 5 and len(d.get("faq", [])) == 5, )

    sub("1.2 话题按钮 topic")
    st, d = _post_json("/event", {"session_id": "det_odm", "action": "topic", "id": "odm",
                                  "page_url": "https://gmic.ai/"})
    ok("topic=odm 回 ODM opener + 建卡", st == 200 and "ODM" in (d or {}).get("reply", ""))

    sub("1.3 FAQ 全 5 条")
    for i in range(5):
        st, d = _post_json("/event", {"session_id": f"det_faq{i}", "action": "faq", "index": i})
        rep = (d or {}).get("reply", "")
        ok(f"faq index={i} 有非 TODO 答案", st == 200 and rep and not rep.startswith("TODO"),
           rep[:60] + "…")

    sub("1.4 跳转链接 link")
    for lid in ("products", "demo"):
        st, d = _post_json("/event", {"session_id": f"det_lnk_{lid}", "action": "link", "id": lid})
        ok(f"link={lid} -> ok", st == 200 and (d or {}).get("ok") is True)

    sub("1.5 错误分支")
    st, _ = _post_json("/event", {"session_id": "det_e", "action": "topic", "id": "nope"})
    ok("坏 topic id -> 400", st == 400)
    st, _ = _post_json("/event", {"session_id": "det_e", "action": "link", "id": "nope"})
    ok("坏 link id -> 400", st == 400)
    st, _ = _post_json("/event", {"session_id": "det_e", "action": "faq", "index": 99})
    ok("faq 越界 -> 400", st == 400)
    st, _ = _post_json("/event", {"session_id": "det_e", "action": "faq"})
    ok("faq 缺 index -> 400", st == 400)
    st, _ = _post_json("/event", {"session_id": "det_e", "action": "banana"})
    ok("未知 action -> 400", st == 400)
    st, _ = _post_json("/chat", {"session_id": "det_e", "text": "   "})
    ok("/chat 空 text -> 400", st == 400)
    st, _ = _post_json("/chat", {"session_id": "det_e"})
    ok("/chat 缺 text 字段 -> 422(Pydantic)", st == 422)

    # =================================================================
    hr("2. 对话捕获 — 英文 (English)")
    # =================================================================
    sub("2.1 need 捕获 + 自然追问联系方式")
    st, rep, _ = say("en_need", "I want a custom AI voice recorder, around 2000 units.")
    ok("EN need 有回复", st == 200 and bool(rep), rep)
    info("EN 是否在追联系方式", rep)

    sub("2.2 禁脑补坏邮箱")
    sid = "en_bademail"
    say(sid, "We need 500 wearable mics.")
    st, rep, _ = say(sid, "email me at john(at)acme")
    ok("EN 坏邮箱不脑补成 john@acme", "@acme" not in rep.lower() and complained(rep), rep)

    sub("2.3 干净邮箱 readback 确认")
    st, rep, _ = say(sid, "sorry, it's john@acme.com")
    ok("EN 干净邮箱被逐字 readback", "john@acme.com" in rep, rep)

    sub("2.4 邮箱纠错(非空才覆盖)")
    st, rep, _ = say(sid, "actually use john@acme.io instead")
    ok("EN 纠错后 readback 新邮箱", "john@acme.io" in rep, rep)

    sub("2.4b 用户已确认 -> 不再重复 just to confirm(治僵硬)")
    st, rep, _ = say(sid, "yes that's 100% correct, that's my final email.")
    ok("EN 确认后不再 'just to confirm'", "just to confirm" not in rep.lower(), rep)
    info("EN 确认后应收下并往下走", rep)

    sub("2.5 电话号码捕获")
    st, rep, _ = say("en_phone", "Call me at +1 415 555 0199, we need 1000 smart badges.")
    info("EN 电话场景回复(应确认/收下号码)", rep)
    ok("EN 电话场景有回复", bool(rep), rep)

    sub("2.6 IM 平台甩链(路 A:用户留自己的号)")
    st, rep, ids = say("en_wa", "200 recorders. Reach me on WhatsApp +1 650 555 1234.")
    ok("EN WhatsApp -> 甩 whatsapp", "whatsapp" in ids, ids)
    st, rep, ids = say("en_tg", "Ping me on Telegram @lunagmic about 300 badges.")
    ok("EN Telegram -> 甩 telegram", "telegram" in ids, ids)
    st, rep, ids = say("en_line", "My Line id is luna-line-01, need 100 mics.")
    ok("EN Line(我们无入口)-> 不甩", "line" not in ids, ids)

    sub("2.7 路 A 去重:同平台纠正不再甩、换新平台才甩")
    sid = "en_dedup"
    st, rep, ids = say(sid, "WhatsApp +1 650 555 1000, 500 units.")
    ok("首次 WhatsApp -> 甩", "whatsapp" in ids, ids)
    st, rep, ids = say(sid, "oops my WhatsApp is +1 650 555 2000")
    ok("同平台纠正 -> 不再甩 whatsapp", "whatsapp" not in ids, ids)
    st, rep, ids = say(sid, "you can also use Telegram @luna2")
    ok("换新平台 Telegram -> 甩", "telegram" in ids, ids)

    sub("2.8 路 B 问我们的号(每次都甩 + 不自己编号)")
    st, rep, ids = say("en_ask1", "What's your WhatsApp number?")
    ok("EN 问 WhatsApp -> 甩 whatsapp", "whatsapp" in ids, ids)
    ok("EN 不在回复里编造我们的号(链接给号)", "555" not in rep, rep)
    st, rep, ids = say("en_ask2", "How can I email you?")
    ok("EN 问 email -> 甩 email", "email" in ids, ids)
    st, rep, ids = say("en_ask3", "Do you have a phone number I can call?")
    ok("EN 问 phone -> 甩 call", "call" in ids, ids)

    sub("2.9 多平台并集(一句话给两个 IM,用 handle 型平台避免歧义)")
    # 注:用 WeChat+Telegram 两个 handle 型平台测并集。若给"WhatsApp +号码",模型常把号码归进 phone
    #     字段(联系方式没丢,只是不弹 WhatsApp 按钮)——那是 LLM 字段分类,非 bug,故不用于硬断言。
    st, rep, ids = say("en_multi", "Reach me on WeChat luna_wx or Telegram @lunatg, 800 units.")
    ok("EN 一句两 IM -> 同时甩 wechat+telegram", "wechat" in ids and "telegram" in ids, ids)

    sub("2.10 name/company 加分项(记录但不逼问)")
    st, rep, _ = say("en_nc", "Hi, I'm Sarah from Acme Corp, exploring options.")
    info("EN 有 name/company 时不应死追(观察措辞)", rep)
    ok("EN name/company 场景有回复", bool(rep), rep)

    sub("2.11 FAQ 口径一致(MOQ)")
    st, rep, _ = say("en_moq", "What is your MOQ for a custom recorder?")
    info("EN MOQ 回复(应贴 few hundred~few thousand,不乱报死数)", rep)
    ok("EN MOQ 有回复", bool(rep), rep)

    # =================================================================
    hr("3. 对话捕获 — 中文 (Chinese)")
    # =================================================================
    sub("3.1 need 捕获 + 中文回")
    st, rep, _ = say("zh_need", "你好,我想定制会议录音麦克风,大概一千个。")
    ok("ZH need 有回复", st == 200 and bool(rep), rep)
    ok("ZH 用中文回复", has_cjk(rep), rep)

    sub("3.2 禁脑补坏邮箱(中文)")
    sid = "zh_bademail"
    say(sid, "我们要 500 个可穿戴麦克风。")
    st, rep, _ = say(sid, "我的邮箱是 zhang(at)acme")
    ok("ZH 坏邮箱不脑补成 zhang@acme", "@acme" not in rep.lower() and complained(rep), rep)

    sub("3.3 干净邮箱 readback(中文)")
    st, rep, _ = say(sid, "抱歉,应该是 zhang@acme.com")
    ok("ZH 干净邮箱被逐字 readback", "zhang@acme.com" in rep, rep)

    sub("3.3b 用户已确认 -> 不再重复确认(中文,治僵硬)")
    st, rep, _ = say(sid, "对,就是这个,我 100% 确认,这是我最终的邮箱。")
    ok("ZH 确认后不再重复'确认一下/请确认'",
       "请确认" not in rep and "确认一下" not in rep and "just to confirm" not in rep.lower(), rep)
    info("ZH 确认后应收下并往下走", rep)

    sub("3.4 电话号码捕获(中文)")
    st, rep, _ = say("zh_phone", "打我电话 +86 138 0013 8000,要 2000 个录音笔。")
    ok("ZH 电话场景中文回复", bool(rep) and has_cjk(rep), rep)

    sub("3.5 IM 平台甩链(中文)")
    st, rep, ids = say("zh_wx", "我的微信是 luna_gmic369,想做 800 个胸牌。")
    ok("ZH 微信 -> 甩 wechat", "wechat" in ids, ids)
    st, rep, ids = say("zh_wa", "加我 WhatsApp +86 138 0000 1111 聊聊。")
    ok("ZH WhatsApp -> 甩 whatsapp", "whatsapp" in ids, ids)

    sub("3.6 路 B 问我们的号(中文,每次甩 + 不编号)")
    st, rep, ids = say("zh_ask1", "你们的微信是多少?")
    ok("ZH 问微信 -> 甩 wechat", "wechat" in ids, ids)
    st, rep, ids = say("zh_ask2", "怎么给你们发邮件?")
    ok("ZH 问邮箱 -> 甩 email", "email" in ids, ids)
    st, rep, ids = say("zh_ask3", "有电话可以直接打吗?")
    ok("ZH 问电话 -> 甩 call", "call" in ids, ids)

    sub("3.7 多平台并集(中文)")
    st, rep, ids = say("zh_multi", "加我微信 luna_wx 或 Telegram @lunatg,要 600 个。")
    ok("ZH 一句两 IM -> 同时甩 wechat+telegram", "wechat" in ids and "telegram" in ids, ids)

    sub("3.8 语言切换(同会话 英->中)")
    sid = "mix_lang"
    st, rep, _ = say(sid, "Hello, do you make custom microphones?")
    ok("混合会话:英文问 -> 英文回", not has_cjk(rep) and bool(rep), rep)
    st, rep, _ = say(sid, "换成中文吧,我需要 300 个。")
    ok("混合会话:中文问 -> 中文回", has_cjk(rep), rep)

    # =================================================================
    hr("4. 语音留言 — 独立功能 (Voice message, SAPI 合成)")
    # =================================================================
    if a.no_voice:
        skip("全部语音用例", "--no-voice")
    elif os.name != "nt":
        skip("全部语音用例", "非 Windows")
    else:
        tmp = tempfile.gettempdir()
        en_wav = os.path.join(tmp, "vm_en.wav")
        zh_wav = os.path.join(tmp, "vm_zh.wav")
        have_en = sapi_wav("Hi, I need two thousand custom AI voice recorders for my company.", en_wav, culture="en-US")
        have_zh = sapi_wav("你好,我想定制两千个人工智能录音麦克风。", zh_wav, culture="zh-CN")

        sub("4.1 /voice/transcribe 英文 -> 转写非空(浮窗预览用)")
        if have_en:
            st, tr = vm_transcribe(en_wav)
            ok("EN transcribe 200 + 转写非空", st == 200 and bool(tr), tr)
        else:
            skip("英文 transcribe", "SAPI 合成失败")

        sub("4.2 /voice/transcribe 中文 -> 中文转写")
        if have_zh:
            st, tr = vm_transcribe(zh_wav)
            ok("ZH transcribe 200 + 中文转写", st == 200 and has_cjk(tr), tr)
        else:
            skip("中文 transcribe", "无 zh-CN 嗓音")

        sub("4.3 /voice/message 合法 email + 音频 -> 200 ok(建线索卡)")
        if have_en:
            st, d = vm_message("vm_ok_email", en_wav, "email", "buyer@acme.com", text="Need 2000 recorders")
            ok("合法 email 留言 -> 200 ok", st == 200 and (d or {}).get("ok") is True, d)
        else:
            skip("合法 email 留言", "SAPI 合成失败")

        sub("4.4 ⭐ /voice/message 联系方式服务端 gate(题眼)")
        if have_en:
            st, _ = vm_message("vm_bad", en_wav, "email", "bob(at)acme")     # 坏邮箱
            ok("坏 email -> 400(服务端拦截)", st == 400, st)
            st, _ = vm_message("vm_empty", en_wav, "email", "   ")          # 空值
            ok("空联系方式 -> 400", st == 400, st)
            st, _ = vm_message("vm_shortphone", en_wav, "phone", "123")     # 位数不足
            ok("坏 phone -> 400", st == 400, st)
            st, _ = vm_message("vm_wa", en_wav, "whatsapp", "+1 650 555 1234")  # 合法 IM
            ok("合法 whatsapp -> 200", st == 200, st)
        else:
            skip("联系方式 gate", "SAPI 合成失败")

        sub("4.5 /voice/message 缺 contact 字段 -> 422(Pydantic)")
        if have_en:
            st, _ = _post_multipart("/voice/message", {"session_id": "vm_missing"}, en_wav)
            ok("缺 contact_type/value -> 422", st == 422, st)
        else:
            skip("缺字段校验", "SAPI 合成失败")

        sub("4.6 超大文件 -> 413")
        big = os.path.join(tmp, "vm_big.wav")
        with open(big, "wb") as f:
            f.write(b"\0" * 9_000_000)
        st, _ = _post_multipart("/voice/transcribe", {}, big)
        ok("transcribe 超大 -> 413", st == 413, st)

    # =================================================================
    hr("结果汇总")
    # =================================================================
    print(f"硬断言:  PASS {_hard_pass}  /  FAIL {_hard_fail}")
    print(f"软观察:  {_soft} 条(仅供肉眼看,不计分)")
    print(f"跳过:    {_skip} 条")
    if _hard_fail:
        print("\n❌ FAILED 项:\n  - " + "\n  - ".join(_fail_names))
        print("\n提示:LLM 措辞类偶发抖动可重跑确认;稳定复现才算回归。")
        sys.exit(1)
    print("\n✅ ALL HARD CHECKS PASS")
    print("👀 还需肉眼确认:Slack 频道 #web-bot 的线索卡(Entry/Email/Phone/Messengers/Need 分行 + thread 明细)")
    sys.exit(0)


if __name__ == "__main__":
    main()
