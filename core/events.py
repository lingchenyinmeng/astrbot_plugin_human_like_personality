"""事件驱动：发生一件「有分量」的事，性格要被它推一下。

不是每次聊天都改，只认几类事件：
- conflict    被凶 / 吵架
- reconcile   道歉 / 和好
- care        生病 / 担心 / 心疼
- praise      被夸
- promise     答应的事 / 约定

改动先累积成「待并入的偏移」，下一次演化时叠上去（有上限），
这样人格不会被一条记忆直接改掉，也留得住痕迹。
"""
from __future__ import annotations

EVENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("conflict", ("凶", "吵", "生气", "滚", "闭嘴", "骂", "烦死", "别说了", "不想理")),
    ("reconcile", ("对不起", "抱歉", "和好", "别生气", "原谅", "没事了", "是我不好")),
    ("care", ("生病", "医院", "难受", "发烧", "不舒服", "疼", "休息", "吃药")),
    ("praise", ("可爱", "厉害", "真好", "喜欢你", "棒", "夸", "好看")),
    ("promise", ("答应", "说好", "约好", "下次", "明天", "记得")),
]

# 每种事件对性格的偏移（很小，靠累积）
NUDGES: dict[str, dict[str, float]] = {
    "conflict": {"neuroticism": 1.0, "extraversion": -0.5},
    "reconcile": {"neuroticism": -1.0, "agreeableness": 0.5},
    "care": {"agreeableness": 1.0},
    "praise": {"extraversion": 0.5},
    "promise": {"conscientiousness": 0.5},
}

# 两次演化之间，单个维度最多累积到多少
MAX_NUDGE = 3.0


def detect_event(text: str, nature: str = "") -> str:
    """从一条记忆里认出事件类型。"""
    t = str(text or "")
    nat = str(nature or "")
    # 被凶的直接算冲突
    if nat in ("scold", "blame"):
        return "conflict"
    if nat == "praise" and any(w in t for w in ("可恶", "烦", "滚")):
        return "conflict"
    for kind, words in EVENT_RULES:
        if any(w in t for w in words):
            return kind
    return ""


def nudges_for(kind: str) -> dict[str, float]:
    return dict(NUDGES.get(kind, {}))


def merge_nudges(acc: dict[str, float], add: dict[str, float]) -> dict[str, float]:
    """累加并夹到上限。"""
    out = dict(acc or {})
    for k, v in (add or {}).items():
        out[k] = max(-MAX_NUDGE, min(MAX_NUDGE, float(out.get(k, 0.0)) + float(v)))
    return out


def apply_nudges(traits: dict, nudges: dict[str, float]) -> dict:
    """把偏移叠到大五上（0-100 内）。"""
    out = dict(traits or {})
    for k, v in (nudges or {}).items():
        if k in out:
            try:
                out[k] = int(max(0, min(100, round(float(out[k]) + float(v)))))
            except Exception:
                pass
    return out
