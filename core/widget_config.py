"""
加载 config/widget.json —— 快捷按钮 + FAQ 的配置。

设计意图:把"按钮长什么样、跳去哪、FAQ 答什么"都做成数据放在 json 里,
团队(marketing)直接改 json 就行,不用改代码。
"""
import os
import json

# config/widget.json 相对本文件在 ../config/ 下
_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "widget.json")

with open(_PATH, encoding="utf-8") as _f:
    CONFIG = json.load(_f)

# 把按钮列表转成"按 id 查"的字典,方便路由里 O(1) 找到某个按钮。
# 例:ACTIONS["odm"] → {"id":"odm","label":"🏭 定制/ODM","type":"topic","intent":"odm","opener":"..."}
ACTIONS = {a["id"]: a for a in CONFIG.get("quickActions", [])}
