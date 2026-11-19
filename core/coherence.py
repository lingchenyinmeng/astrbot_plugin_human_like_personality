"""人格统一：跨层一致性校验。

「她是谁」不能六层各说各的。叙事（narrative）是主锚，其余层围绕它写，不能打架。
这里在落盘前做两层校验：

1. 规则版（零 LLM 成本）——只做确定性的字段合法性检查，不做语义猜测
   （说话风格和人格的语义关系交给 LLM，规则硬猜会误伤）。
2. LLM 自查版（可选，coherence_llm_check 开）——把整份快照送回模型，让它找出
   互相打架的地方，返回要修正的字段。叙事和 core 不许动（主锚 + 锁死）。

每处改动都返回 issue，由 main 记进 personality_events，可追溯（/人格 why 能看到）。
"""
from __future__ import annotations

import json
import re

from .model import ATTACHMENT_STYLES, Personality

# LLM 自查能改的字段白名单。叙事（主锚）和 core（锁死）不在里面。
FIXABLE_NUMERIC = (
    "traits.openness",
    "traits.conscientiousness",
    "traits.extraversion",
    "traits.agreeableness",
    "traits.neuroticism",
)
FIXABLE_TEXT = ("voice.sentence_style", "gestalt.emergent_whole", "gestalt.figure", "gestalt.background")


def check_coherence(p: Personality) -> list[dict]:
    """规则版：确定性的字段合法性 + 空值提示。返回 issue 列表，不写库。"""
    issues: list[dict] = []

    # 依恋档位兜底（apply_response 已丢非法的，这里再保险一次）
    if p.adaptations.attachment_default and p.adaptations.attachment_default not in ATTACHMENT_STYLES:
        issues.append({
            "dim": "adaptations.attachment_default",
            "old": p.adaptations.attachment_default,
            "new": "",
            "note": "依恋默认档不是合法档位，已清空",
        })
        p.adaptations.attachment_default = ""

    for k, v in list(p.adaptations.attachment_levels.items()):
        if v not in ATTACHMENT_STYLES:
            issues.append({
                "dim": f"adaptations.attachment_levels.{k}",
                "old": v,
                "new": "",
                "note": f"「{k}」的依恋档位非法，已移除",
            })
            p.adaptations.attachment_levels.pop(k, None)

    # 叙事写了但整体印象空着——不是硬冲突，只提示下次补
    if p.narrative and not p.gestalt.emergent_whole:
        issues.append({
            "dim": "gestalt.emergent_whole",
            "old": "",
            "new": "",
            "note": "叙事已写但整体印象为空（下次归纳补上）",
        })

    return issues


def build_review_prompt(p: Personality) -> str:
    """把整份快照拼成给模型自查的提示词。"""
    t = p.traits
    a = p.adaptations
    g = p.gestalt
    s = p.schema

    return f"""你是一个人格心理学家，要检查一份 AI 角色的人格画像六层是否**自洽**。

# 原则
叙事（narrative）是主锚，其余层必须和它是**同一个人**的不同侧面，不能打架。
比如：叙事说「我沉默寡言」，但外向性写 85，就是冲突。

# 这份画像的六层
- 大五：开放 {t.openness} / 尽责 {t.conscientiousness} / 外向 {t.extraversion} / 宜人 {t.agreeableness} / 神经质 {t.neuroticism}
- 说话风格（统计出来的，是事实）：{p.voice.sentence_style or '（空）'}
- 价值观：{'、'.join(a.values) or '（空）'}
- 叙事：{p.narrative or '（空）'}
- 整体印象（格式塔）：{g.emergent_whole or '（空）'}
- 自我图式：{'、'.join(s.self_schema) or '（空）'}

# 你要做的
只找**真正互相打架**的地方（说话风格和大五对不上、整体印象和叙事是两个人、自我图式和底线冲突），
返回要修正的字段。**没把握就别改**，宁可少改。
叙事和 core（底线/存在）不许动。

只输出 JSON，不要别的：
{{"fixes": [
  {{"field": "traits.extraversion", "value": 55, "why": "叙事说沉默寡言，但外向 85"}}
]}}

field 只能从这些里选：
{', '.join(FIXABLE_NUMERIC + FIXABLE_TEXT)}
没有冲突就输出：{{"fixes": []}}"""


def apply_fixes(p: Personality, fixes: list[dict]) -> list[dict]:
    """把模型返回的修正落到快照上（白名单 + 夹紧）。返回已应用的 issue。"""
    issues: list[dict] = []
    for fx in fixes or []:
        if not isinstance(fx, dict):
            continue
        field = str(fx.get("field") or "").strip()
        why = str(fx.get("why") or "")[:120]
        if field not in FIXABLE_NUMERIC + FIXABLE_TEXT:
            continue

        if field in FIXABLE_NUMERIC:
            try:
                v = int(round(float(fx.get("value"))))
            except (TypeError, ValueError):
                continue
            v = max(0, min(100, v))
            key = field.split(".", 1)[1]
            old = int(getattr(p.traits, key))
            # 统一修正也封顶 ±15：比演化的 ±4 宽，能纠正明显的打架，又不至于一次跳太狠
            if v > old + 15:
                v = old + 15
            if v < old - 15:
                v = old - 15
            if old != v:
                setattr(p.traits, key, v)
                issues.append({"dim": field, "old": old, "new": v, "note": why or "六层自洽修正"})
        else:
            # 文本字段：只允许替换，不拼接
            v = str(fx.get("value") or "").strip()
            if not v:
                continue
            v = v[:160]
            key = field.split(".", 1)[1]
            if key == "voice.sentence_style":
                old = p.voice.sentence_style
                p.voice.sentence_style = v
            elif key == "gestalt.emergent_whole":
                old = p.gestalt.emergent_whole
                p.gestalt.emergent_whole = v
            elif key == "gestalt.figure":
                old = p.gestalt.figure
                p.gestalt.figure = v[:100]
            elif key == "gestalt.background":
                old = p.gestalt.background
                p.gestalt.background = v[:100]
            else:
                continue
            if old != v:
                issues.append({"dim": field, "old": old, "new": v, "note": why or "六层自洽修正"})

    return issues


def parse_review(raw: str) -> list[dict]:
    """从模型自查的输出里抽 fixes 列表。解析失败返回空列表（宁可不改）。"""
    s = str(raw or "").strip()
    if "```" in s:
        for seg in s.split("```"):
            seg = seg.strip()
            if seg.startswith("json"):
                seg = seg[4:].strip()
            if seg.startswith("{"):
                s = seg
                break
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return []
    try:
        obj = json.loads(s[i : j + 1])
    except Exception:
        return []
    if not isinstance(obj, dict):
        return []
    fixes = obj.get("fixes")
    return fixes if isinstance(fixes, list) else []


__all__ = ["check_coherence", "build_review_prompt", "apply_fixes", "parse_review", "FIXABLE_NUMERIC", "FIXABLE_TEXT"]
