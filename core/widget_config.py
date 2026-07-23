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

# 问卷定义(4 个 Tab 各自的 intro / cta / 3 题选项 / 链接)。前端渲染问卷屏用;
# 后端 /questionnaire 用它来:① 校验传进来的 tab 合法 ② 把答案渲染成人类可读摘要 ③ 取该 Tab 的链接。
# 例:QUESTIONNAIRES["odm"] → {"intro":"...","cta":"...","questions":[{id,q,options},...],"links":[...]}
QUESTIONNAIRES = CONFIG.get("questionnaires", {})

# Tab3(help-me-choose)的"答案 → 产品"推荐规则:一个【有序】列表,先匹配到的先赢(所以更具体的规则
# 要排在更宽泛的前面,见 widget.json)。每条 = {when:{答案条件}, products, link, hint}。
# 注:哪些映射我们内部拿不太准、值得人工二次确认,由【我们自己记着】(不再当字段喂给 LLM——那样很别扭);
#     hint 是自然语言,想让 bot 主动带一句"让专家确认",把这层意思写进对应规则的 hint 文案即可。
_RECOMMEND_RULES = CONFIG.get("recommend_rules", [])
# 一条规则都没命中时的兜底(甩产品总览页 + 让真人帮选)。
_RECOMMEND_DEFAULT = CONFIG.get("recommend_default",
                                {"products": [], "link": "https://gmic.ai/products/", "hint": ""})


def _answer_matches(answer_val, cond_val):
    """
    判断"用户对某题的答案"是否满足"规则 when 里对该题的条件",【同时支持单选题和多选题】。
      - 单选题:answer_val 是字符串 → 精确相等匹配(answer_val == cond_val)。
      - 多选题:answer_val 是列表(如 Tab3 的 must-haves 可多选) → 【包含】匹配(cond_val 在列表里即命中)。
        这就是"让多选的必须项也能参与选型"的关键:以前用 `answer_val == cond_val`,列表永远不等于字符串,
        所以多选题的选择对推荐毫无影响;现在列表走"包含",一条 when:{"musthave":"Rugged / waterproof"}
        的规则就能被"选了防水"的用户命中。
    输入:answer_val = 用户对该题的答案(字符串 或 列表 或 None);cond_val = 规则要求的选项(字符串)。
    输出:命中 True / 否则 False。
    例:answer_val="On a desk / in a room", cond_val 同 → True(单选,精确相等)。
        answer_val=["Security / encryption","Rugged / waterproof"], cond_val="Rugged / waterproof" → True(多选,包含)。
        answer_val=["Long battery life"], cond_val="Rugged / waterproof" → False(不在列表里)。
        answer_val=None(这题没答) → False(不是列表,也不等于任何 cond_val)。
    """
    if isinstance(answer_val, list):
        return cond_val in answer_val      # 多选题:选项集合里含这个条件值就算命中
    return answer_val == cond_val          # 单选题:精确相等


def recommend_for(answers):
    """
    按用户在 help-me-choose 问卷里的答案,匹配出一条推荐(产品/链接/hint)。

    只对 Tab3 有意义(其它 Tab 的方案由 LLM 直接依答案生成,不需要这张映射表)。

    ── 输入 ──
      answers: 【单份】问卷答案 dict(即前端本次提交的那份,扁平结构),如
               {"usage":"To answer phone calls","where":"Office / meetings","musthave":["Security / encryption",...]}。
               单选题的值是字符串,多选题(如 musthave)的值是【列表】。
    ── 输出 ──
      命中的那条规则 dict(含 products/link/hint);一条都没命中 → 返回 _RECOMMEND_DEFAULT 兜底。

    ── 逐步逻辑 ──
      步1  按顺序遍历规则(有序 → 更具体的排前面,先匹配先赢)。
      步2  规则命中 = 它的 when 里【每个】条件都被用户答案满足(all(...)),满足判断走 _answer_matches:
           单选题精确相等、多选题看是否包含 → 所以带 musthave 条件的"精细化"规则也能命中。
           例:when={"usage":"On a desk / in a room","musthave":"Rugged / waterproof"} 要求
               usage 相等【且】musthave 列表里含"Rugged / waterproof",比只 when={"usage":"On a desk..."}
               那条更具体,所以必须排在它前面(否则宽泛那条会先命中、精细规则永远轮不到)。
      步3  第一条命中的立刻返回;都不命中 → 兜底。
    例:answers.usage="To answer phone calls" → 命中首条 → 返回 {products:["Telalive","HA-TEL02"],link:...,hint:...}。
    """
    answers = answers or {}
    for rule in _RECOMMEND_RULES:                                          # 步1
        when = rule.get("when", {})
        if when and all(_answer_matches(answers.get(k), v) for k, v in when.items()):  # 步2:每个条件都满足(含多选包含)
            return rule                                                    # 步3:先匹配先赢
    return _RECOMMEND_DEFAULT                                              # 步3:兜底

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
