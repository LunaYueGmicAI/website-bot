"""Memory-management sanity checks — no API keys required. Run: python tests/test_memory.py"""
import os
os.environ["MAX_TURNS_IN_MEMORY"] = "4"
os.environ["MAX_SESSIONS"] = "3"
os.environ["SESSION_TTL_SECONDS"] = "1800"
os.environ["LLM_HISTORY_TURNS"] = "3"

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from core import sessions  # noqa: E402


def main():
    S = sessions.SessionStore()
    ok = True

    S.get_or_create("u1", {"page_url": "/"})
    for i in range(6):
        S.append_turn("u1", "user", f"m{i}")
    turns = [t["text"] for t in S.snapshot("u1")["turns"]]
    ok &= _check("turn cap keeps newest 4", turns == ["m2", "m3", "m4", "m5"], turns)

    win = [t["text"] for t in S.window("u1")]
    ok &= _check("sliding window = last 3", win == ["m3", "m4", "m5"], win)

    S.set_entry_intent("u1", "odm")
    # 必填 = need + 一种联系方式。先只给 need → 应还缺 contact。
    S.update_lead("u1", {"need": "recorder"})
    ok &= _check("missing=contact when only need given",
                 S.snapshot("u1")["lead"]["missing"] == ["contact"], S.snapshot("u1")["lead"])
    # 再补上 email → need + contact 都齐 → missing 应为空(name 不是必填,不进 missing)。
    S.update_lead("u1", {"email": "a@x.com"})
    lead = S.snapshot("u1")["lead"]
    ok &= _check("missing empty once need+contact present", lead["missing"] == [] and lead["email"] == "a@x.com", lead)

    S.set_entry_intent("u1", "products")
    ok &= _check("first entry_intent not clobbered", S.snapshot("u1")["entry_intent"] == "odm", S.snapshot("u1")["entry_intent"])

    # ---- messengers(列表联系方式,按平台去重、同平台留最新)----
    # 只给一个 IM(没 email/phone)→ 也算有联系方式 → missing 不含 contact。
    S.get_or_create("m1")
    S.update_lead("m1", {"need": "mic", "messengers": ["WeChat: wx_old"]})
    lead = S.snapshot("m1")["lead"]
    ok &= _check("messenger alone satisfies contact",
                 lead["missing"] == [] and lead["messengers"] == ["WeChat: wx_old"], lead)
    # 补一个【不同平台】→ 并集,两个都留。
    S.update_lead("m1", {"messengers": ["WhatsApp: +1 (669) 900-0008"]})
    plats = {sessions.messenger_platform(m) for m in S.snapshot("m1")["lead"]["messengers"]}
    ok &= _check("different platform kept (union)", plats == {"wechat", "whatsapp"},
                 S.snapshot("m1")["lead"]["messengers"])
    # 【同平台】再报(纠错)→ 覆盖旧的,只留最新;总数不变(仍是 wechat+whatsapp 两条)。
    S.update_lead("m1", {"messengers": ["WeChat: wx_new"]})
    msgr = S.snapshot("m1")["lead"]["messengers"]
    wx = [m for m in msgr if sessions.messenger_platform(m) == "wechat"]
    ok &= _check("same platform keeps latest only", wx == ["WeChat: wx_new"] and len(msgr) == 2, msgr)

    # ---- 甩直连链接的两个匹配器(不碰 LLM/Slack)----
    from core import widget_config as WC
    # 路A:用户留自己的号 → match_our_channels(按平台匹配我们有入口的渠道,Line 不甩)
    hit = [c["id"] for c in WC.match_our_channels(["WhatsApp: +1..", "Line: abc", "WhatsApp: +1.."])]
    ok &= _check("match_our_channels: WA hit, Line skip, dedup", hit == ["whatsapp"], hit)
    # 路B:用户问起我们渠道 → contact_for_channel(口头说法归一到 contacts id)。
    # 注:只打印 id(contacts 配置里含 emoji 图标,直接 print 整个 dict 会在 Windows GBK 控制台炸)。
    ok &= _check("contact_for_channel: phone->call", (WC.contact_for_channel("phone") or {}).get("id") == "call",
                 (WC.contact_for_channel("phone") or {}).get("id"))
    ok &= _check("contact_for_channel: unknown->None", WC.contact_for_channel("line") is None and WC.contact_for_channel("") is None,
                 "line->None, ''->None")

    S.get_or_create("u2"); S.get_or_create("u3"); S.get_or_create("u4")
    ok &= _check("LRU evicts u1", S.snapshot("u1") is None and S.snapshot("u4") is not None, S.stats())

    # backdate u2 past the TTL (>1800s idle); fresh u3/u4 should survive the sweep
    S._data["u2"]["last_seen"] -= 4000
    n = S.sweep_expired()
    ok &= _check("TTL sweep drops idle only", S.snapshot("u2") is None and S.snapshot("u4") is not None and n == 1, f"evicted={n}")

    print("\nALL PASS" if ok else "\nSOME FAILED")
    sys.exit(0 if ok else 1)


def _check(name, cond, detail):
    print(f"[{'OK' if cond else 'FAIL'}] {name}: {detail}")
    return cond


if __name__ == "__main__":
    main()
