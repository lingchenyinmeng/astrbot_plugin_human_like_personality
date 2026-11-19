"""评价体系：好感 + 态度 两维，带漂移。

纯逻辑、无 DB、无 IO，方便单测。所有分数 0-100，默认 50。

两维独立、允许背离：
- 好感高 + 态度冷（喜欢但怕被拒）
- 好感低 + 态度热（讨厌但对方脸皮厚，能随便损）

漂移（长时间不联系向中回归，但不归到 50）：
- 高（>50）：最多回落到 50 + (原值-50)*0.75 → 90 最多降到 80
- 低（<50）：最多上涨到 50 - (50-原值)*0.5  → 36 最多涨到 43
"""
from __future__ import annotations

DEFAULT = 50.0
HIGH = 60.0   # 好感 >= HIGH：被责怪 → 自责（几乎不掉）
LOW = 40.0    # 好感 <= LOW：被责怪 → 评价降低


def clamp(v: float) -> float:
    return round(max(0.0, min(100.0, float(v))), 1)


def apply_interaction(affection: float, attitude: float, kind: str, weight: float = 1.0) -> tuple[float, float, str]:
    """把一次「夸 / 责」落到两维上。返回 (新好感, 新态度, 归因)。

    归因：praise 空串；scold 返回 self(自责) / other(评价降低) / ambiguous。
    weight：按相关性打折（direct=1，mention=0.5，about=0.3，unrelated=0）。
    """
    a, t = float(affection), float(attitude)
    w = max(0.0, min(1.0, float(weight)))
    if kind == "praise":
        return clamp(a + 5 * w), clamp(t + 3 * w), ""
    if kind == "scold":
        if a >= HIGH:
            return clamp(a - 1 * w), clamp(t - 3 * w), "self"
        if a <= LOW:
            return clamp(a - 5 * w), clamp(t - 5 * w), "other"
        return clamp(a - 2 * w), clamp(t - 2 * w), "ambiguous"
    return a, t, ""


def drift_target(value: float) -> float:
    """长时间不联系后的回归目标（地板/天花板）。"""
    v = float(value)
    if v > 50:
        return round(50 + (v - 50) * 0.75, 1)   # 90 → 80
    if v < 50:
        return round(50 - (50 - v) * 0.5, 1)    # 36 → 43
    return 50.0


def drift_step(value: float, target: float) -> float:
    """向目标靠近 1 点（高向下、低向上）。"""
    v, tg = float(value), float(target)
    if v > tg:
        return round(max(tg, v - 1), 1)
    if v < tg:
        return round(min(tg, v + 1), 1)
    return v


__all__ = ["DEFAULT", "HIGH", "LOW", "clamp", "apply_interaction", "drift_target", "drift_step"]
