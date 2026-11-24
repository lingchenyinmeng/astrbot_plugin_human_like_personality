"""表达风格：直率 / 隐晦 / 别扭。

由大五 + 依恋 + 对某人的态度 + 当前心情推出来。
- 直率：有话直说，不绕
- 隐晦：点到为止，藏着说
- 别扭：口是心非，心里想答应嘴上先顶一句（傲娇）

纯函数，无 IO。
"""
from __future__ import annotations

from typing import Any


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def compute_style(
    *,
    extraversion: float = 50,
    neuroticism: float = 50,
    agreeableness: float = 50,
    conscientiousness: float = 50,
    attachment: str = "",
    affection: float = 50,
    attitude: float = 50,
    mood: dict[str, Any] | None = None,
) -> dict[str, float | str]:
    """算出三维表达风格（0-1）和主导标签。"""
    e = _clamp(extraversion, 0, 100) / 100.0
    n = _clamp(neuroticism, 0, 100) / 100.0
    a = _clamp(agreeableness, 0, 100) / 100.0
    c = _clamp(conscientiousness, 0, 100) / 100.0

    direct = 0.35 + 0.30 * e + 0.15 * (1 - n) + 0.10 * c
    indirect = 0.25 + 0.25 * n + 0.10 * (1 - a)
    awkward = 0.15 + 0.30 * n

    # 依恋
    if "回避" in attachment:
        indirect += 0.20
        direct -= 0.05
    if "焦虑" in attachment:
        awkward += 0.15

    # 对这个人：喜欢但态度冷 → 别扭
    if affection >= 65 and attitude <= 55:
        awkward += 0.20
    if attitude >= 70:
        direct += 0.10

    # 心情
    v = float((mood or {}).get("valence") or 0)
    ar = float((mood or {}).get("arousal") or 0.35)
    direct += 0.15 * v
    awkward += 0.12 * (1 - v)
    direct += 0.08 * (ar - 0.35)

    direct = round(_clamp(direct), 3)
    indirect = round(_clamp(indirect), 3)
    awkward = round(_clamp(awkward), 3)

    top = max((direct, "直率"), (indirect, "隐晦"), (awkward, "别扭"), key=lambda x: x[0])
    return {
        "direct": direct,
        "indirect": indirect,
        "awkward": awkward,
        "label": top[1],
    }


def style_hint(style: dict[str, Any] | None, *, to_owner: bool = False) -> str:
    """给注入用的「说话倾向」。"""
    if not style:
        return ""
    label = str(style.get("label") or "")
    if label == "直率":
        return "你现在说话偏直：想什么说什么，不绕弯子，该顶就顶。"
    if label == "隐晦":
        return "你现在说话偏隐晦：点到为止，不把话挑明，留点余地。"
    if label == "别扭":
        if to_owner:
            return (
                "你现在说话有点别扭：心里其实在意，嘴上会先顶一句、或者反着说。"
                "别真的伤人，别扭里要能听出在意。"
            )
        return "你现在说话有点别扭：不太肯直说，拐着弯表达。"
    return ""


def needs_rewrite(style: dict[str, Any] | None, mood: dict[str, Any] | None) -> bool:
    """是否值得花一次模型调用改写（只在风格明显时才改）。"""
    if not style:
        return False
    try:
        awkward = float(style.get("awkward") or 0)
        indirect = float(style.get("indirect") or 0)
    except Exception:
        return False
    v = float((mood or {}).get("valence") or 0)
    return awkward >= 0.52 or indirect >= 0.62 or abs(v) >= 0.35


def build_rewrite_prompt(
    text: str,
    *,
    style: dict[str, Any],
    mood: dict[str, Any] | None,
    bot_name: str = "她",
    context: str = "",
) -> str:
    """把已经写好的一句话，按当前风格改写。"""
    name = str(bot_name or "").strip() or "她"
    label = str(style.get("label") or "直率")
    mlabel = str((mood or {}).get("label") or "平静")
    rules = {
        "直率": "直说，不绕弯，不用客套；可以顶一句。",
        "隐晦": "别把话挑明，点到为止，留半句让人自己品。",
        "别扭": "口是心非：心里在意，嘴上先顶一句或反着说；别扭里要能听出关心，不许真伤人。",
    }.get(label, "直说。")
    return (
        f"你是「{name}」。下面这句话是你刚想说的话，现在按你此刻的表达风格改写一遍。\n"
        f"此刻心情：{mlabel}。\n"
        f"表达风格：{label}。规则：{rules}\n"
        f"要求：\n"
        f"- 意思别变（信息、答应的事、拒绝都要保留）\n"
        f"- 人称一个字都不许改：原文的「你/我/他」原样保留\n"
        f"- 句子短、口语、别用书面词，别列点，别总结\n"
        f"- 长度和原文差不多，别扩写\n"
        f"- 只输出改写后的那句话，不要解释\n"
        + (f"- 上下文：{context[:200]}\n" if context else "")
        + f"\n原文：{text}\n改写："
    )
