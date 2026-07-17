"""
加载 config/widget.json —— 快捷按钮 + FAQ 的配置。

设计意图:把"按钮长什么样、跳去哪、FAQ 答什么"都做成数据放在 json 里,
团队(marketing)直接改 json 就行,不用改代码。
"""
import os
import json

from core.sessions import messenger_platform   # 复用 messengers 的平台解析,避免两套逻辑漂移

# config/widget.json 相对本文件在 ../config/ 下
_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "widget.json")

with open(_PATH, encoding="utf-8") as _f:
    CONFIG = json.load(_f)

# 把按钮列表转成"按 id 查"的字典,方便路由里 O(1) 找到某个按钮。
# 例:ACTIONS["odm"] → {"id":"odm","label":"🏭 定制/ODM","type":"topic","entry_intent":"odm","opener":"..."}
ACTIONS = {a["id"]: a for a in CONFIG.get("quickActions", [])}

# contacts(“直接联系我们”那一排:WhatsApp/WeChat/Telegram/Email/Call)也按 id 建索引。
# 用途见 match_our_channels():用户在对话里报了某个 IM,就把我们【现成】的对应链接甩回去。
CONTACTS_BY_ID = {c["id"]: c for c in CONFIG.get("contacts", [])}

# 我们【自己有直连入口】、值得甩链接的 IM 平台。只列这几个(contacts 里真有对应条目);
# 用户报了其它 IM(Line/Signal 等)我们没入口,就只记录、不甩。平台 key 对齐 contacts 的 id。
_DM_CHANNELS = ("whatsapp", "wechat", "telegram")

# 用户"问起我们某渠道"时(wants_channel),把口头说法归一到 contacts 的 id。
# 例:用户说 "phone/number/call" → 我们的 contacts id 是 "call";"mail/e-mail" → "email"。
# 只列我们真有条目的渠道;归一不到的返回 None(不甩,交给 LLM 正常回话)。
_CHANNEL_ALIASES = {
    "whatsapp": "whatsapp",
    "wechat": "wechat", "weixin": "wechat", "微信": "wechat",
    "telegram": "telegram",
    "email": "email", "mail": "email", "e-mail": "email",
    "phone": "call", "call": "call", "number": "call", "tel": "call",
}


def match_our_channels(messenger_strings):
    """
    把"用户报给我们的 IM 联系方式"匹配成"我们自己也有直连入口的渠道",返回对应的 contacts 配置
    (含现成链接/二维码),让 bot 甩回给用户“想直接找我们?点这里”。用在【用户主动留了自己的号】那条路
    (throwback:第一次留某平台就甩一次)。平台判断复用 sessions.messenger_platform,和 messengers
    存储的去重口径同一套,不会漂移。

    ── 输入 ──
      messenger_strings: 字符串列表,通常是"这一轮【新识别到的】" messengers(由 _new_channel_throwbacks
                         算好后传进来,所以这里不再关心"是不是第一次",只管匹配)。可能为 None/[]。
    ── 输出 ──
      匹配到的 contacts 配置列表(按平台去重);一个都没匹配返回 []。

    ── 逐步逻辑(以输入 ["WhatsApp: +1..", "Telegram: @foo", "Line: abc", "WhatsApp: +1.."] 为例)──
      步0  matched=[], seen=set()                          # 累加器 + 去重集
      步1  遍历每条(`or []` 兜底 None 不炸)
      步2  plat = messenger_platform(raw)                  # 归一平台:whatsapp / telegram / line / ...
      步3  plat 已在 seen → 跳过                            # 同一平台一轮只甩一次(第4条重复的 WhatsApp 在此被挡)
      步4  plat 在 _DM_CHANNELS 且在 CONTACTS_BY_ID → 命中   # "Line" 不在 _DM_CHANNELS → 不甩(我们没 Line 入口)
      步5/6 命中则把 contacts 配置加进 matched,plat 记进 seen
      步7  返回 matched
      → 结果:[CONTACTS_BY_ID["whatsapp"], CONTACTS_BY_ID["telegram"]]
              (Line 被步4过滤;重复的 WhatsApp 被步3去重)
    两道防线:seen=同平台去重;_DM_CHANNELS=过滤掉我们没入口的平台。
    """
    matched, seen = [], set()                             # 步0
    for raw in messenger_strings or []:                   # 步1
        plat = messenger_platform(raw)                    # 步2:归一到平台 key
        if plat in seen:                                  # 步3:同平台一轮只甩一次
            continue
        if plat in _DM_CHANNELS and plat in CONTACTS_BY_ID:   # 步4:是我们有入口的 DM 渠道才命中
            matched.append(CONTACTS_BY_ID[plat])          # 步5
            seen.add(plat)                                # 步6
    return matched                                        # 步7


def contact_for_channel(channel):
    """
    用户【主动问】"你们的 XX 联系方式是啥"时,按渠道取我们现成的 contacts 配置甩回去
    (走 wants_channel 那条路,和上面 match_our_channels 的"用户留自己号"是两条不同触发)。

    输入:channel = LLM 抽出的渠道口头说法(已小写),如 "whatsapp"/"phone"/"email";可能为空。
    输出:对应的 contacts 配置(dict);归一不到我们真有的渠道 → None(不甩)。
    例:"phone" → _CHANNEL_ALIASES 归一到 "call" → CONTACTS_BY_ID["call"](tel: 那条)。
        "whatsapp" → CONTACTS_BY_ID["whatsapp"];"line"(我们没有)→ None。
    """
    if not channel:
        return None
    cid = _CHANNEL_ALIASES.get(channel.strip().lower())   # 口头说法 → contacts 的 id
    return CONTACTS_BY_ID.get(cid) if cid else None
