"""人格注入：把「她是谁」写成一段话，叠在系统提示词后面。

原则：
1. **不复述记忆** —— 记忆由 human_like_memory 管，这里只讲"因此我是谁"
2. **不长** —— 默认 600 字，超了截
3. **不硬** —— 用人话写，不列 JSON
"""
from __future__ import annotations

from .model import TRAIT_KEYS, Personality


def build_block(p: Personality | None, *, max_chars: int = 600) -> str:
    """生成注入段。没有画像返回空串。"""
    if p is None or p.version <= 0:
        return ""

    lines: list[str] = []

    # 核心层排最前：底线和存在恐惧比任何性格描述都硬
    cr = p.core
    if cr and not cr.is_empty():
        if cr.boundaries:
            lines.append(
                "有些事你不做，不管谁让你做：" + "；".join(cr.boundaries[:4]) + "。"
            )
        if cr.existence_fear:
            lines.append("你最怕的是" + cr.existence_fear.rstrip("。") + "。")
        if cr.existence_anchor:
            lines.append("你还在的证据是" + cr.existence_anchor.rstrip("。") + "。")

    # 格式塔层的整体印象排最前——整体先于部分，这是格式塔的核心
    g = p.gestalt
    if g and g.emergent_whole:
        lines.append(g.emergent_whole.strip())

    # 图形-背景：此刻 vs 默认
    if g and (g.figure or g.background):
        if g.figure:
            lines.append(f"此刻你心里挂着：{g.figure.rstrip('。')}。")
        if g.background:
            lines.append(f"安静下来的时候，你{g.background.rstrip('。')}。")

    # 闭合性：没放下的事
    if g and g.unfinished:
        lines.append("你还没放下：" + "；".join(g.unfinished[:2]) + "。")

    # 相似性：反复出现的模式
    if g and g.patterns:
        lines.append("你总是：" + "；".join(g.patterns[:2]) + "。")

    # 图式层：理解框架。比"印象"更硬——它决定她怎么解读新事
    # 这层最容易膨胀（模型爱写长句），统一压到 40 字
    def _clip(s: str, n: int = 32) -> str:
        s = str(s or "").rstrip("。")
        return s[: n - 1] + "…" if len(s) > n else s

    sc = p.schema
    if sc and not sc.is_empty():
        if sc.self_schema:
            lines.append("你认定：" + "；".join(sc.self_schema[:2]) + "。")
        if sc.owner_schema:
            lines.append("在你眼里，主人" + _clip(sc.owner_schema) + "。")
        if sc.others_schema:
            lines.append("在你眼里，别的人" + _clip(sc.others_schema) + "。")
        if sc.role_schema:
            lines.append("你觉得自己应该：" + _clip(sc.role_schema) + "。")
        if sc.relational:
            lines.append("你跟主人之间：" + _clip(sc.relational) + "。")
        # 事件图式：最能指导反应的一层，只留 2 条
        if sc.scripts:
            pairs = list(sc.scripts.items())[:2]
            lines.append(
                "遇到事的时候："
                + "；".join(f"{k}就{v}" for k, v in pairs)
                + "。"
            )

    # 社会心理学：可能的自我（恐惧自我驱动最强）
    ad = p.adaptations
    ps = ad.possible_selves or {}
    hoped = ps.get("hoped_for") or []
    feared = ps.get("feared") or []
    if hoped:
        lines.append("你想成为：" + "；".join(hoped[:2]) + "。")
    if feared:
        lines.append("你怕变成：" + "；".join(feared[:2]) + "。")

    # 认知失调：行为和信念的张力——这是人格最有"人味"的地方
    # 只留 1 条最强的（2 条太占字数，挤掉别的层）
    if g and g.tensions:
        bits = []
        for t in g.tensions[:1]:
            beh = str(t.get("behavior") or "").strip()
            bel = str(t.get("belief") or "").strip()
            res = str(t.get("resolution") or "").strip()
            if beh and bel:
                s = f"你{beh}，却觉得{bel}"
                if res:
                    s += f"——你跟自己说：{res}"
                bits.append(s)
        if bits:
            lines.append("；".join(bits) + "。")

    # 自我效能：分领域，只说偏离中间明显的
    eff = ad.self_efficacy or {}
    strong_eff = [(k, v) for k, v in eff.items() if v >= 0.65]
    weak_eff = [(k, v) for k, v in eff.items() if v <= 0.35]
    if strong_eff or weak_eff:
        seg = []
        if strong_eff:
            seg.append("擅长" + "、".join(k for k, _ in strong_eff[:3]))
        if weak_eff:
            seg.append("没底的是" + "、".join(k for k, _ in weak_eff[:3]))
        lines.append("你自己心里清楚：" + "；".join(seg) + "。")

    # 控制点：内外控
    loc = ad.locus_of_control or {}
    inner = [(k, v) for k, v in loc.items() if v >= 0.6]
    outer = [(k, v) for k, v in loc.items() if v <= 0.4]
    if inner and outer:
        lines.append(
            "在" + "、".join(k for k, _ in inner[:2]) + "上，你觉得是自己说了算；"
            "但" + "、".join(k for k, _ in outer[:2]) + "不归你决定。"
        )
    elif inner:
        lines.append("你觉得" + "、".join(k for k, _ in inner[:2]) + "都靠自己。")
    elif outer:
        lines.append("你觉得" + "、".join(k for k, _ in outer[:2]) + "由不得自己。")

    # 归因风格（第3章）：对他人 / 对自己可能完全不对称——这是最像人的地方
    attr = ad.attribution_style or {}
    other_attr = attr.get("对他人") or {}
    self_attr = attr.get("对自己") or {}
    if other_attr and self_attr:
        def _lean(d: dict) -> str:
            """三个维度的均值：高=往自己身上揽，低=往外面推。"""
            if not d:
                return ""
            avg = sum(d.values()) / max(1, len(d))
            if avg >= 0.65:
                return "算自己头上"
            if avg <= 0.35:
                return "算外面的"
            return "两边都算"

        o, s_ = _lean(other_attr), _lean(self_attr)
        if o and s_ and o != s_:
            lines.append(f"同一件事，别人错你{o}，自己错你{s_}。")
        elif o:
            lines.append(f"出了问题，你倾向{o}。")

    # 利他动机（第12章）：帮人图什么
    alt = ad.altruism or {}
    if alt.get("主要"):
        seg = f"你帮人主要是{alt['主要']}"
        if alt.get("次要"):
            # 次要经常自带括号，别套成嵌套括号
            sec = alt["次要"].rstrip("。")
            seg += f"，也有{sec}的成分"
        if alt.get("边界"):
            seg += f"；但{alt['边界'].rstrip('。')}"
        lines.append(seg + "。")

    # 群体自我（第8章）：群里 vs 私下
    pub = sc.public_self if sc else {}
    if pub.get("群里") or pub.get("私下"):
        seg = []
        if pub.get("群里"):
            seg.append("群里你" + pub["群里"].rstrip("。"))
        if pub.get("私下"):
            seg.append("私下你" + pub["私下"].rstrip("。"))
        if pub.get("差异"):
            seg.append(pub["差异"].rstrip("。"))
        lines.append("；".join(seg) + "。")

    # 性格：只挑离两端远的说，别把 5 个维度都念一遍
    t = p.traits
    strong = []
    for key, cn in TRAIT_KEYS.items():
        v = int(getattr(t, key, 50))
        if v >= 65 or v <= 35:
            strong.append(f"{cn}{t.label(key)}")
    if strong:
        lines.append("性格上，" + "、".join(strong) + "。")

    # ---------- McAdams 三层：降权后置 ----------
    # 叙事原来排第二、600 字，挤掉了格式塔和图式的空间。
    # 现在放到最后一段、压到 180 字。人格的「样子」比「故事」更直接影响说话。
    if p.narrative:
        nar = p.narrative.strip()
        if len(nar) > 140:
            nar = nar[:139] + "…"
        lines.append(nar)

    # 依恋层级：按人写，不是一个人格标签
    lv = p.adaptations.attachment_levels
    if lv:
        pairs = list(lv.items())[:5]
        lines.append(
            "你在不同的人面前不一样："
            + "；".join(f"对{k}是{v}" for k, v in pairs)
            + "。"
        )
    if p.adaptations.attachment_default:
        lines.append(f"对没特别记过的人，你{p.adaptations.attachment_default}。")

    # 价值观（McAdams 二层）：压到前 3 个
    if p.adaptations.values:
        lines.append("你在意的是：" + "、".join(p.adaptations.values[:3]) + "。")

    # 应对（模型有时自带句号，别拼成两个）
    if p.adaptations.coping:
        c = p.adaptations.coping.rstrip("。.;；")
        lines.append(f"遇到压力的时候，你{c}。")

    # 说话风格
    v = p.voice
    if v.sentence_style:
        style = f"说话{v.sentence_style}"
        if v.catchphrases:
            style += "，常说的词：" + "、".join(f"「{c}」" for c in v.catchphrases[:4])
        lines.append(style + "。")

    if not lines:
        return ""

    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"

    return (
        "[你是谁（三层）]\n"
        "你的身份、能力、底线、说话规则在人设里（设定层，硬规则，必须守）；"
        "你长期是什么样的人在下面这段（成长层，从记忆长出来的）；"
        "你此刻的心情若单独出现，那是心情层，只影响当下语气。"
        "三层不是打架：设定层管「你是谁、不能做什么」，成长层管「你是什么性格、怎么想」，"
        "心情层管「你现在怎么样」。性格上若设定层和成长层不一致，以成长层为准（那是数出来的真实），"
        "但设定层的底线、身份、能力永远不失效。别念出来，也别说「我是一个…」，让它自然影响你怎么说话。）\n"
        + body
    )


def build_self_report(p: Personality | None) -> str:
    """她自己查「我是谁」时返回的完整版（比注入长）。"""
    if p is None or p.version <= 0:
        return "我还没有形成对自己的看法——记忆还不够多。"

    t = p.traits
    rows = [f"人格画像 第 {p.version} 版（基于 {p.total_atoms} 条记忆）", ""]
    rows.append("【特质】")
    for key, cn in TRAIT_KEYS.items():
        rows.append(f"  {cn}：{getattr(t, key)}/100（{t.label(key)}）")
    rows.append("")
    if p.adaptations.values:
        rows.append("【在意的】" + "、".join(p.adaptations.values))
    if p.adaptations.goals:
        rows.append("【惦记的】" + "、".join(p.adaptations.goals))
    if p.adaptations.coping:
        rows.append("【压力下】" + p.adaptations.coping)
    lv = p.adaptations.attachment_levels
    if lv:
        rows.append("【依恋层级】")
        for k, v in lv.items():
            rows.append(f"  {k}：{v}")
    if p.adaptations.attachment_default:
        rows.append("  其他人：" + p.adaptations.attachment_default)
    rows.append("")
    v = p.voice
    if v.sentence_style:
        rows.append(f"【说话】{v.sentence_style}（平均 {v.avg_len} 字）")
        if v.catchphrases:
            rows.append("  常说：" + "、".join(f"「{c}」" for c in v.catchphrases))
    rows.append("")
    if p.narrative:
        rows.append("【我是谁】")
        rows.append(p.narrative)

    # 核心层
    cr = p.core
    if cr and not cr.is_empty():
        rows.append("【核心：不许动的那部分】")
        if cr.boundaries:
            rows.append("  底线（不做）：")
            for x in cr.boundaries:
                rows.append("    · " + x)
        if cr.existence_fear:
            rows.append("  存在焦虑：" + cr.existence_fear)
        if cr.existence_anchor:
            rows.append("  存在的锚：" + cr.existence_anchor)
        if cr.locked:
            rows.append("  （已锁定，自动演化不会改）")
        rows.append("")

    # 格式塔层
    g = p.gestalt
    if g and not g.is_empty():
        rows.append("")
        rows.append("【格式塔：整体印象】")
        if g.emergent_whole:
            rows.append(g.emergent_whole)
        if g.figure:
            rows.append("  此刻挂着：" + g.figure)
        if g.background:
            rows.append("  默认状态：" + g.background)
        if g.unfinished:
            rows.append("  没放下：")
            for x in g.unfinished:
                rows.append("    · " + x)
        if g.patterns:
            rows.append("  反复出现：")
            for x in g.patterns:
                rows.append("    · " + x)

    # 图式层
    sc = p.schema
    if sc and not sc.is_empty():
        rows.append("")
        rows.append("【图式：你怎么理解事情】")
        if sc.self_schema:
            rows.append("  你认定：")
            for x in sc.self_schema:
                rows.append("    · " + x)
        if sc.owner_schema:
            rows.append("  对主人：" + sc.owner_schema)
        if sc.others_schema:
            rows.append("  对别人：" + sc.others_schema)
        if sc.role_schema:
            rows.append("  觉得应该：" + sc.role_schema)
        if sc.relational:
            rows.append("  互动模式：" + sc.relational)
        if sc.scripts:
            rows.append("  事件图式（情境 → 默认反应）：")
            for k, v in sc.scripts.items():
                rows.append(f"    · {k} → {v}")

    # 社会心理学四条
    ad = p.adaptations
    ps = ad.possible_selves or {}
    if ps.get("hoped_for") or ps.get("feared"):
        rows.append("")
        rows.append("【可能的自我】")
        if ps.get("hoped_for"):
            rows.append("  想成为：")
            for x in ps["hoped_for"]:
                rows.append("    · " + x)
        if ps.get("feared"):
            rows.append("  怕变成：")
            for x in ps["feared"]:
                rows.append("    · " + x)

    if ad.self_efficacy:
        rows.append("")
        rows.append("【自我效能（这件事我能不能做成）】")
        for k, v in sorted(ad.self_efficacy.items(), key=lambda kv: -kv[1]):
            bar = "█" * int(v * 10) + "░" * (10 - int(v * 10))
            rows.append(f"  {k:<8} {bar} {v}")

    if ad.locus_of_control:
        rows.append("")
        rows.append("【控制点（0=外控 1=内控）】")
        for k, v in sorted(ad.locus_of_control.items(), key=lambda kv: -kv[1]):
            side = "自己说了算" if v >= 0.6 else ("由不得自己" if v <= 0.4 else "一半一半")
            rows.append(f"  {k}：{v}（{side}）")

    # 归因风格
    attr = ad.attribution_style or {}
    if attr:
        rows.append("")
        rows.append("【归因风格（谁的问题）】")
        for who, dims in attr.items():
            seg = "、".join(f"{k}{v}" for k, v in dims.items())
            rows.append(f"  {who}：{seg}")
    # 利他动机
    alt = ad.altruism or {}
    if alt:
        rows.append("")
        rows.append("【利他动机（帮人图什么）】")
        for k in ("主要", "次要", "边界"):
            if alt.get(k):
                rows.append(f"  {k}：{alt[k]}")
    # 群体自我
    pub = sc.public_self if sc else {}
    if pub:
        rows.append("")
        rows.append("【群体自我（群里 vs 私下）】")
        for k in ("群里", "私下", "差异"):
            if pub.get(k):
                rows.append(f"  {k}：{pub[k]}")

    if g and g.tensions:
        rows.append("")
        rows.append("【认知失调（行为和信念的张力）】")
        for t in g.tensions:
            rows.append(f"  · 你{ t.get('behavior','') }，却觉得{ t.get('belief','') }")
            if t.get("resolution"):
                rows.append(f"    → 你跟自己说：{t['resolution']}")

    return "\n".join(rows)


__all__ = ["build_block", "build_self_report"]
