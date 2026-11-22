"""言语趋同：她会慢慢学亲近的人怎么说话。

只做统计，不调模型。
- 只对「主人 + 评价与态度双高的人」生效。
- 排除 AI（非数字 QQ / 配置里的 AI 名单）。
- 有自我保底：最多靠 40%，自己的口头禅不参与。
"""
from __future__ import annotations

import re
from typing import Any

PARTICLES = "呀啦嘛哦啊呢吧哈嘿唉诶噢"
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F600-\U0001F64F]"
)


def style_vector(texts: list[str]) -> dict[str, float]:
    """一把话 → 说话风格向量。"""
    out = {
        "avg_len": 0.0,
        "short_rate": 0.0,
        "question_rate": 0.0,
        "exclaim_rate": 0.0,
        "emoji_rate": 0.0,
        "particle_rate": 0.0,
    }
    items = [str(t or "").strip() for t in (texts or []) if str(t or "").strip()]
    if not items:
        return out
    n = len(items)
    lens = [len(t) for t in items]
    out["avg_len"] = round(sum(lens) / n, 2)
    out["short_rate"] = round(sum(1 for l in lens if l <= 8) / n, 3)
    out["question_rate"] = round(
        sum(1 for t in items if ("?" in t or "？" in t or re.search(r"[吗呢么嘛]$", t))) / n, 3
    )
    out["exclaim_rate"] = round(sum(1 for t in items if ("!" in t or "！" in t)) / n, 3)
    out["emoji_rate"] = round(sum(1 for t in items if EMOJI_RE.search(t)) / n, 3)
    out["particle_rate"] = round(sum(1 for t in items if any(c in t for c in PARTICLES)) / n, 3)
    return out


def converge(
    mine: dict[str, float],
    theirs: dict[str, float],
    *,
    intimacy: float,
    max_pull: float = 0.40,
    alpha: float = 0.02,
) -> dict[str, float]:
    """把她的向量慢慢拉向对方。intimacy 0-1。返回新向量（只含数字字段）。"""
    if not mine or not theirs:
        return dict(mine or {})
    pull = max(0.0, min(float(max_pull), float(alpha) * float(intimacy) * 10.0))
    out: dict[str, float] = {}
    for k, v in mine.items():
        try:
            tv = float(theirs.get(k, v))
            out[k] = round(float(v) + (tv - float(v)) * pull, 3)
        except Exception:
            out[k] = v
    return out


def intimacy_of(affection: float, attitude: float, interactions: int = 0) -> float:
    """亲密度 0-1：好感与态度双高才算亲近，互动多再补一点。"""
    a = max(0.0, min(100.0, float(affection))) / 100.0
    t = max(0.0, min(100.0, float(attitude))) / 100.0
    base = (a * 0.6 + t * 0.4)
    base += min(0.15, max(0, int(interactions)) * 0.005)
    return round(max(0.0, min(1.0, base)), 3)


def is_ai(person_key: str, name: str, ai_qqs: set[str], ai_names: set[str]) -> bool:
    """区别「人」和「AI」：非数字 QQ、配置名单、或名字命中。"""
    key = str(person_key or "").strip()
    nm = str(name or "").strip()
    if key in ai_qqs or nm in ai_names:
        return True
    if key and not key.isdigit():
        return True
    return False
