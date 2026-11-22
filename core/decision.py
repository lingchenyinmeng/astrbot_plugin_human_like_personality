"""发言决策：她可以选择说不说。

输入是当前消息的「重要性信号」与她的心情，输出回复 / 不回 / 只回表情。
底线：主人必回；被点名必回；连续多条没回必须回一条，说明自己的情绪和需求。
纯函数，无 IO。
"""
from __future__ import annotations

from typing import Any


def decide(
    *,
    is_private: bool,
    is_owner: bool,
    is_mention: bool,
    is_question: bool,
    has_name: bool,
    text: str = "",
    mood: dict[str, Any] | None = None,
    streak: int = 0,
    recently_replied: bool = False,
    threshold: float = 0.45,
    force_streak: int = 3,
) -> dict[str, Any]:
    """返回 {action, score, why, self_express}。

    action: reply / skip
    self_express: 这次要不要「说一句自己的情绪和需求」
    """
    v = float((mood or {}).get("valence") or 0)
    label = str((mood or {}).get("label") or "平静")
    t = str(text or "").strip()

    # 1) 必回：主人私聊
    if is_private and is_owner:
        return {"action": "reply", "score": 1.0, "why": "主人在私聊", "self_express": False}

    # 2) 必回：点名 / @ / 提问
    if is_mention:
        return {"action": "reply", "score": 1.0, "why": "被 @ 了", "self_express": False}
    if has_name:
        return {"action": "reply", "score": 0.95, "why": "名字被叫到", "self_express": False}
    if is_question:
        return {"action": "reply", "score": 0.9, "why": "被问了问题", "self_express": False}

    # 3) 连续多条没回 → 必须回一条，说明自己的情绪和需求
    if streak >= max(1, int(force_streak)):
        return {
            "action": "reply",
            "score": 0.8,
            "why": f"对方连发 {streak} 条，该说一句了",
            "self_express": True,
        }

    # 4) 其余 :按心情和主动性打分
    score = 0.55 if is_private else 0.30
    # 心情好更愿意开口，心情差少说
    score += 0.20 * v
    if label in ("轻快", "兴奋"):
        score += 0.10
    if label in ("闷", "烦"):
        score -= 0.12
    # 刚说过话，缓一缓
    if recently_replied:
        score -= 0.15
    # 太短、没内容的消息，不值得接
    if len(t) <= 2 and not is_question:
        score -= 0.18
    # 被连发但还没到阈值，稍微积极一点，别装死
    if streak >= 2:
        score += 0.10

    score = round(max(0.0, min(1.0, score)), 3)
    if score >= threshold:
        return {"action": "reply", "score": score, "why": "想接一句", "self_express": False}
    return {"action": "skip", "score": score, "why": "这会儿不想接", "self_express": False}


def self_express_hint(mood: dict[str, Any] | None = None, *, bot_name: str = "") -> str:
    """被连发逼到要回一句时，怎么开口。"""
    label = str((mood or {}).get("label") or "平静")
    feeling = {
        "轻快": "有点想笑",
        "兴奋": "有点上头",
        "闷": "有点闷",
        "烦": "有点烦",
        "平静": "说不上来",
    }.get(label, "说不上来")
    return (
        "对方连着发了好几条，你一直没回。现在回一条，别解释技术原因，"
        f"就说你此刻的真实感受（大概「{feeling}」），再直接说你的需要——"
        "比如想被等一等、想自己待一会儿、想让他慢点发。一句话，别道歉一大段。"
    )
