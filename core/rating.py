"""评价体系：好感 + 态度 两维，带漂移。**长期评价，不被一两句话带跑。**

纯逻辑、无 DB、无 IO，方便单测。所有分数 0-100，默认 50。

两维独立、允许背离：
- 好感高 + 态度冷（喜欢但怕被拒）
- 好感低 + 态度热（讨厌但对方脸皮厚，能随便损）

长期化（v3.1.3）：
- 单次互动只做「小步 EMA」——向长期目标靠近一点点，中间一步约 1 分，
  越接近两端越接近不动（90 分时夸一句才 +0.2）。
- 夸 10 次大概从 50 走到 59，夸 50 次才到 86——一两句话改不动一个人。
- 第一次从 0 开始也不会被一句好话拉到 80。

漂移（长时间不联系向中回归，但不归到 50）：
- 高（>50）：最多回落到 50 + (原值-50)*0.75 → 90 最多降到 80
- 低（<50）：最多上涨到 50 - (50-原值)*0.5  → 36 最多涨到 43
"""
from __future__ import annotations

DEFAULT = 50.0
HIGH = 60.0   # 好感 >= HIGH：被责怪 → 自责（几乎不掉）
LOW = 40.0    # 好感 <= LOW：被责怪 → 评价降低

# 长期评价的步长：越小越稳。改这些就能调「多久才被一两句话打动」。
PRAISE_A_ALPHA = 0.020    # 好感向 100 靠近 2%
PRAISE_T_ALPHA = 0.012    # 态度向 80 靠近 1.2%（态度更钝）
SCOLD_SELF_ALPHA = 0.008  # 喜欢的人凶她：怪自己，评价几乎不动
SCOLD_OTHER_ALPHA = 0.020 # 不喜欢的人凶她：评价往 10 掉
SCOLD_MID_ALPHA = 0.012   # 说不清：小掉一点


def clamp(v: float) -> float:
    return round(max(0.0, min(100.0, float(v))), 1)


def _ema(v: float, target: float, alpha: float) -> float:
    """向 target 靠近 alpha 比例。天然饱和：越近目标改得越少。"""
    a = max(0.0, min(1.0, float(alpha)))
    return clamp(float(v) + (float(target) - float(v)) * a)


def apply_interaction(affection: float, attitude: float, kind: str, weight: float = 1.0) -> tuple[float, float, str]:
    """把一次「夸 / 责」落到两维上。返回 (新好感, 新态度, 归因)。

    归因：praise 空串；scold 返回 self(自责) / other(评价降低) / ambiguous。
    weight：按相关性打折（direct=1，mention=0.5，about=0.3，unrelated=0）。

    单次改动很小（大约 0.1~1 分），长期累积才看得出方向——这就是「长期评价」。
    """
    a, t = float(affection), float(attitude)
    w = max(0.0, min(1.0, float(weight)))
    if kind == "praise":
        return (
            _ema(a, 100.0, PRAISE_A_ALPHA * w),
            _ema(t, 80.0, PRAISE_T_ALPHA * w),
            "",
        )
    if kind == "scold":
        if a >= HIGH:
            return (
                _ema(a, 70.0, SCOLD_SELF_ALPHA * w),
                _ema(t, 45.0, SCOLD_SELF_ALPHA * w),
                "self",
            )
        if a <= LOW:
            return (
                _ema(a, 10.0, SCOLD_OTHER_ALPHA * w),
                _ema(t, 10.0, SCOLD_OTHER_ALPHA * w),
                "other",
            )
        return (
            _ema(a, 30.0, SCOLD_MID_ALPHA * w),
            _ema(t, 30.0, SCOLD_MID_ALPHA * w),
            "ambiguous",
        )
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


__all__ = [
    "DEFAULT", "HIGH", "LOW",
    "PRAISE_A_ALPHA", "PRAISE_T_ALPHA",
    "SCOLD_SELF_ALPHA", "SCOLD_OTHER_ALPHA", "SCOLD_MID_ALPHA",
    "clamp", "apply_interaction", "drift_target", "drift_step",
]
