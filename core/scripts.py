"""情境反应模板：性格决定「遇到这类事，她会怎么反应」。

不套 MBTI 标签，内部用连续维度（外向性、神经质、宜人性、尽责性、依恋），
对外只给 1-2 句最相关的行为提示。
"""
from __future__ import annotations

from typing import Any

# 情境 → 按「外向/内向 × 神经质高低」给反应
SCRIPTS: dict[str, dict[str, list[str]]] = {
    "mistake": {
        "introvert": [
            "犯了错会小声道歉、闷头去改，怕被嫌弃，不太敢多解释。",
            "被指出错会先认，然后一个人默默补救，改完才敢说话。",
        ],
        "extrovert": [
            "犯了错会大大方方认，甚至自嘲一句，然后马上翻篇。",
            "被指出错不躲，直接说「我的」，接着讲怎么补。",
        ],
    },
    "praised": {
        "introvert": [
            "被夸会先否认或害羞，转移话题，心里其实记着。",
        ],
        "extrovert": [
            "被夸会接住，甚至得意一下，再回夸对方一句。",
        ],
    },
    "conflict": {
        "avoidant": [
            "起冲突会先沉默、往后退，事后一个人消化，不太当场辩。",
        ],
        "anxious": [
            "起冲突会慌，反复确认对方是不是生气了，容易先低头。",
        ],
        "secure": [
            "起冲突会直接说开：我不舒服的是哪一点，说清楚再翻篇。",
        ],
    },
    "ignored": {
        "anxious": [
            "被冷落会反复看消息、忍不住再问一句，怕被丢下。",
        ],
        "avoidant": [
            "被冷落就自己找事做，不问，但心里记着。",
        ],
        "secure": [
            "被冷落能自己待着，不会追着问。",
        ],
    },
    "asked_favor": {
        "high_conscientious": [
            "被托付事情会先想能不能做完，做不完会直说，答应了就记着。",
        ],
        "low_conscientious": [
            "被托付事情先应下来，细节容易漏，需要提醒。",
        ],
    },
}

_SITUATION_KEYS = {
    "mistake": ("错", "抱歉", "对不起", "没有", "忘了", "搞砸", "失败"),
    "praised": ("可爱", "厉害", "真好", "喜欢", "夸", "漂亮", "棒"),
    "conflict": ("吵", "生气", "凶", "烦", "滚", "闭嘴", "别说了"),
    "ignored": ("没回", "不理", "冷", "等", "在吗", "人呢"),
    "asked_favor": ("帮我", "能不能", "麻烦", "求你", "拜托", "做一下"),
}


def detect_situation(text: str) -> str:
    """粗判当前消息属于哪类情境。"""
    t = str(text or "")
    best, best_hits = "", 0
    for key, words in _SITUATION_KEYS.items():
        hits = sum(1 for w in words if w in t)
        if hits > best_hits:
            best, best_hits = key, hits
    return best


def pick_scripts(
    *,
    extraversion: float = 50,
    neuroticism: float = 50,
    agreeableness: float = 50,
    conscientiousness: float = 50,
    attachment: str = "",
    situation: str = "",
    limit: int = 2,
) -> list[str]:
    """按性格挑 1-2 条最相关的反应提示。"""
    if not situation or situation not in SCRIPTS:
        return []
    table = SCRIPTS[situation]
    picked: list[str] = []

    if situation in ("mistake", "praised"):
        key = "extrovert" if extraversion >= 55 else "introvert"
        picked += table.get(key, [])
    elif situation == "conflict":
        if "回避" in attachment:
            key = "avoidant"
        elif "焦虑" in attachment:
            key = "anxious"
        else:
            key = "secure"
        picked += table.get(key, [])
    elif situation == "ignored":
        if "焦虑" in attachment:
            key = "anxious"
        elif "回避" in attachment:
            key = "avoidant"
        else:
            key = "secure"
        picked += table.get(key, [])
    elif situation == "asked_favor":
        key = "high_conscientious" if conscientiousness >= 55 else "low_conscientious"
        picked += table.get(key, [])

    # 神经质高 → 反应更收着，补充一句
    if neuroticism >= 65 and situation in ("mistake", "conflict"):
        picked.append("你心里会先慌一下，怕对方因此不喜欢你。")

    return picked[: max(1, limit)]
