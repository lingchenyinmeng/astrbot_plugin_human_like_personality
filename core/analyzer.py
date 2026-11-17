"""行为统计：从记忆里量出「她说话什么样」。

为什么统计而不问 LLM：说话风格是可数的（句长、问句比例、口头禅），
让模型评价自己会飘；数出来的才稳定，也才能看出变化。
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .model import Voice

# 常见的中文语气词/口头禅候选（2-4 字），统计高频
_PHRASE_RE = re.compile(r"[\u4e00-\u9fa5]{2,4}")

# 明显不是口头禅的常用词（避免「因为」「所以」霸榜）
_STOP = {
    "因为", "所以", "但是", "然后", "如果", "就是", "这个", "那个", "什么",
    "可以", "没有", "不是", "还是", "已经", "应该", "可能", "现在", "时候",
    "自己", "他们", "我们", "你们", "一个", "一下", "这样", "那样", "知道",
    "觉得", "东西", "事情", "问题", "地方", "今天", "明天", "昨天", "晚上",
}


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return (
        0x1F300 <= o <= 0x1FAFF
        or 0x2600 <= o <= 0x27BF
        or 0xFE00 <= o <= 0xFE0F
        or 0x1F000 <= o <= 0x1F0FF
    )


def analyze_voice(texts: list[str]) -> Voice:
    """从一批「她说过的话」里统计说话风格。"""
    texts = [str(t).strip() for t in (texts or []) if str(t).strip()]
    if not texts:
        return Voice()

    n = len(texts)
    lens: list[int] = []
    short = long_ = question = exclaim = emoji = 0
    phrases: Counter[str] = Counter()

    for t in texts:
        # 去掉开头的 [表情] / @ 之类
        clean = re.sub(r"\[[^\]]{1,12}\]", "", t)
        clean = re.sub(r"@\S+", "", clean).strip()
        if not clean:
            continue
        L = len(clean)
        lens.append(L)
        if L <= 8:
            short += 1
        if L >= 30:
            long_ += 1
        if "?" in t or "？" in t or clean.endswith(("吗", "呢", "吧")):
            question += 1
        if "!" in t or "！" in t:
            exclaim += 1
        if any(_is_emoji(c) for c in t) or "[表情" in t:
            emoji += 1
        for m in _PHRASE_RE.findall(clean):
            if m not in _STOP:
                phrases[m] += 1

    total = max(1, len(lens))
    avg = sum(lens) / total if lens else 0.0

    # 口头禅：出现 >= max(2, 样本 0.5%)——1714 句里 8 次以上才算高频
    threshold = max(2, int(total * 0.005))
    catch = [p for p, c in phrases.most_common(60) if c >= threshold][:6]

    style_parts: list[str] = []
    if avg and avg <= 12:
        style_parts.append("偏短句")
    elif avg >= 28:
        style_parts.append("偏长句")
    if question / total >= 0.25:
        style_parts.append("爱反问")
    if exclaim / total >= 0.2:
        style_parts.append("带感叹")
    if emoji / total >= 0.3:
        style_parts.append("常用表情")
    if not style_parts:
        style_parts.append("平铺直叙")

    return Voice(
        avg_len=round(avg, 1),
        short_rate=round(short / total, 3),
        long_rate=round(long_ / total, 3),
        question_rate=round(question / total, 3),
        exclaim_rate=round(exclaim / total, 3),
        emoji_rate=round(emoji / total, 3),
        catchphrases=catch,
        sentence_style="、".join(style_parts),
    )


def behavior_signals(atoms: list[dict]) -> dict:
    """从原子列表里提炼大五的行为证据（不给分数，给信号）。

    分数由 LLM 结合这些信号来定——纯规则定大五不可靠，
    但规则能给出「她实际做了什么」，这是 LLM 编不出来的。
    """
    out = {
        "n": len(atoms),
        "by_nature": Counter(),
        "by_mtype": Counter(),
        "about_owner": 0,
        "about_others": 0,
        "promises": 0,
        "questions": 0,
        "praise_given": 0,
        "blame_given": 0,
        "sensitive": 0,
        "short_events": 0,
        "long_events": 0,
    }
    for a in atoms or []:
        nature = str(a.get("nature") or "")
        mtype = str(a.get("mtype") or "")
        layer = str(a.get("layer") or "")
        content = str(a.get("content") or "")
        out["by_nature"][nature or "(空)"] += 1
        out["by_mtype"][mtype or "(空)"] += 1
        if layer.startswith("owner_") or layer in ("me_to_owner", "other_to_me"):
            out["about_owner"] += 1
        elif layer.startswith("other_"):
            out["about_others"] += 1
        if mtype == "promise":
            out["promises"] += 1
        if mtype == "request":
            out["questions"] += 1
        if nature == "praise":
            out["praise_given"] += 1
        if nature == "scold":
            out["blame_given"] += 1
        if str(a.get("privacy") or "") == "sensitive":
            out["sensitive"] += 1
        L = len(content)
        if L and L <= 12:
            out["short_events"] += 1
        elif L >= 60:
            out["long_events"] += 1
    out["by_nature"] = dict(out["by_nature"])
    out["by_mtype"] = dict(out["by_mtype"])
    return out


def clamp_trait(v: float) -> int:
    return int(max(0, min(100, round(v))))


__all__ = ["analyze_voice", "behavior_signals", "clamp_trait"]
