"""情绪（Mood）：最近的热记忆决定此刻心情。

原则：
- **只影响表现层**（语气、长短、表达风格、主动性），绝不改大五。
- 有半衰期，不会长期化。
- 纯函数，无 IO，方便单测。
"""
from __future__ import annotations

import math
import time
from typing import Any

# 情绪半衰期：6 小时后权重减半
MOOD_HALF_LIFE_H = 6.0
# 只看最近这么多小时
MOOD_WINDOW_H = 72.0


def _decay(hours: float, half_life_h: float = MOOD_HALF_LIFE_H) -> float:
    return 0.5 ** (max(0.0, hours) / max(0.5, half_life_h))


def compute_mood(atoms: list[dict[str, Any]], *, now: float | None = None) -> dict[str, Any]:
    """从记忆原子算此刻心情。

    atoms 需要 valence(-1..1) / arousal(0..1) / importance / said_at（或 create_time）。
    返回 {valence, arousal, label, sample, ts}
    """
    ts_now = time.time() if now is None else float(now)
    num_v = num_a = wsum = 0.0
    sample = 0
    for a in atoms or []:
        try:
            v = float(a.get("valence"))
            ar = float(a.get("arousal"))
        except Exception:
            continue
        t = float(a.get("said_at") or a.get("create_time") or 0) or ts_now
        hours = (ts_now - t) / 3600.0
        if hours > MOOD_WINDOW_H:
            continue
        imp = float(a.get("importance") or 0.5)
        w = max(0.05, imp) * _decay(hours)
        num_v += v * w
        num_a += ar * w
        wsum += w
        sample += 1
    if wsum <= 0:
        return {"valence": 0.0, "arousal": 0.35, "label": "平静", "sample": 0, "ts": ts_now}
    v = max(-1.0, min(1.0, num_v / wsum))
    ar = max(0.0, min(1.0, num_a / wsum))
    return {
        "valence": round(v, 3),
        "arousal": round(ar, 3),
        "label": mood_label(v, ar),
        "sample": sample,
        "ts": ts_now,
    }


def mood_label(valence: float, arousal: float) -> str:
    if valence >= 0.15:
        return "兴奋" if arousal >= 0.5 else "轻快"
    if valence <= -0.25:
        return "烦" if arousal >= 0.5 else "闷"
    return "平静"


def mood_hint(mood: dict[str, Any] | None) -> str:
    """给注入用的一句「此刻心情」。平静时不打扰。"""
    if not mood:
        return ""
    label = str(mood.get("label") or "平静")
    v = float(mood.get("valence") or 0)
    if label == "轻快":
        return "你现在心情不错：话会轻快一点，愿意多说两句，语气松，别端着。"
    if label == "兴奋":
        return "你现在有点兴奋：句子短、有劲，结尾可以带感叹号，别压着。"
    if label == "闷":
        return "你现在有点闷：话少、句子更短，不想硬挤热情，回得淡淡的也行。"
    if label == "烦":
        return "你现在有点烦：容易直、容易顶一句，但别对主人生气；不痛快就直说，不要阴阳怪气。"
    return ""


def mood_emoji(mood: dict[str, Any] | None) -> str:
    """心情对应的一个表情，用在「只回表情」或结尾。"""
    if not mood:
        return ""
    label = str(mood.get("label") or "")
    return {
        "轻快": "🙂",
        "兴奋": "😄",
        "闷": "😶",
        "烦": "😑",
        "平静": "",
    }.get(label, "")
