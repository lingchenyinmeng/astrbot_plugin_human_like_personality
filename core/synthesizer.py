"""人格归纳：把记忆 + 行为信号 + 当前人格，交给 LLM 更新成新一版。

关键设计：
1. **不让模型从零编** —— 提供当前的 traits 和行为信号，让它做「调整」而不是「创作」
2. **有惯性** —— 大五每次最多动 8 分（new = old*0.75 + llm*0.25 之后夹紧），
   人格是慢变量，一次对话不该改变一个人
3. **叙事要连贯** —— 要求「接着上一次的说」，不能每版都是另一个人
"""
from __future__ import annotations

import json
import re
from typing import Any

from .analyzer import clamp_trait
from .model import (
    ATTACHMENT_STYLES,
    TRAIT_KEYS,
    Adaptations,
    BigFive,
    Core,
    Gestalt,
    Schema,
    Personality,
    Voice,
)

# 大五单次最大变化幅度——人格是慢变量。
# 从 8 降到 4：铃光说"稳定下来之后别再变，一变我就成了另一个人"，
# 一次归纳最多动 4 分，要连着几次同方向才能挪出可感知的差别。
MAX_TRAIT_DELTA = 4


def _extract_json(raw: str) -> dict:
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
        return {}
    try:
        obj = json.loads(s[i : j + 1])
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _blend(old: int, new: int) -> int:
    """新旧混合 + 限制单次幅度。"""
    v = int(round(old * 0.75 + int(new) * 0.25))
    if v > old + MAX_TRAIT_DELTA:
        v = old + MAX_TRAIT_DELTA
    if v < old - MAX_TRAIT_DELTA:
        v = old - MAX_TRAIT_DELTA
    return clamp_trait(v)


def build_prompt(
    *,
    atoms_text: str,
    signals: dict[str, Any],
    current: Personality | None,
    total_atoms: int,
    new_atoms: int,
    people_text: str = "",
) -> str:
    """拼归纳人格的提示词。"""
    cur_block = "（还没有人格画像，这是第一次归纳）"
    if current is not None and current.version > 0:
        t = current.traits
        cur_block = (
            f"第 {current.version} 版：\n"
            f"- 大五：开放性 {t.openness} / 尽责性 {t.conscientiousness} / "
            f"外向性 {t.extraversion} / 宜人性 {t.agreeableness} / 神经质 {t.neuroticism}\n"
            f"- 价值观：{'、'.join(current.adaptations.values) or '（空）'}\n"
            f"- 目标：{'、'.join(current.adaptations.goals) or '（空）'}\n"
            f"- 应对方式：{current.adaptations.coping or '（空）'}\n"
            f"- 依恋类型：{current.adaptations.attachment_style or '（空）'}\n"
            f"- 自我叙事：{current.narrative or '（空）'}"
        )

    sig_block = (
        f"- 一共 {total_atoms} 条记忆，这次新增 {new_atoms} 条\n"
        f"- 涉及主人的 {signals.get('about_owner', 0)} 条，涉及别人的 {signals.get('about_others', 0)} 条\n"
        f"- 答应过的事 {signals.get('promises', 0)} 条，被问的问题 {signals.get('questions', 0)} 条\n"
        f"- 她夸过别人 {signals.get('praise_given', 0)} 次，凶过别人 {signals.get('blame_given', 0)} 次\n"
        f"- 敏感内容 {signals.get('sensitive', 0)} 条"
    )

    return f"""你是一个人格心理学家，要为一个 AI 角色（她叫铃光）归纳**此时此刻**的人格画像。

# 理论框架
用 McAdams 的三层人格模型：
- 特质层：大五人格（开放性、尽责性、外向性、宜人性、神经质），0-100 分
- 特征适应层：价值观、目标、应对方式、依恋类型
- 生命叙事层：一段第一人称的自我故事

# 她现在的画像
{cur_block}

# 客观行为信号（这些是她真实做过的事，不能编）
{sig_block}

# 她认识的人（人库里「我」对每个人的底色）
{people_text or "（没读到人库）"}

# 她最近的记忆（按重要性取的前若干条）
{atoms_text}

# 你要做的
基于上面的记忆和行为信号，**调整**（不是重写）她的人格画像。规则：

0. **六层必须自洽，叙事是主锚。** 叙事（第一人称自我故事）是「她是谁」的主文本，
   gestalt.emergent_whole、schema.self_schema、traits、voice 必须是**同一个人的不同侧面**，
   不能打架。说话风格 voice 是数出来的事实，大五的分数要跟它呼应：
   带感叹/常用表情 → 情绪外露（神经质别写太低）；平铺直叙 → 情绪平（神经质别写太高）。
   写完以后**自查一遍**：如果六层有矛盾，改到一致再输出。

1. **大五每次最多动 8 分**。人格是慢变量，一次对话不该改变一个人。
   只有当记忆里有一致、反复的证据时才动。
2. **价值观**取前 5 个，按重要性排。要具体（"想被需要"而不是"善良"）。
3. **目标**取前 5 个，写她最近惦记的事。
4. **应对方式**一句话：她遇到压力/被凶的时候会怎么做。看记忆里的实际反应，别套理论。
5. **依恋层级**（attachment_levels）—— 这是 Bowlby 的 attachment hierarchy：
   同一个人对不同对象可以是**不同档**，不要压成一个总分。
   按人写，key 是人名，value 从这几个里选：{'、'.join(ATTACHMENT_STYLES)}。
   只写记忆里互动足够多的人，最多 12 个。没特别记录的人用 attachment_default 一句话概括。
   判断依据：她和这个人互动时的样子——会不会主动、被冷落了会怎样、敢不敢顶嘴。
   例：{{噩壤: 偏焦虑, 鲸宝: 安全型, 顾非言: 回避型}}
6. **叙事**第一人称，150 字以内。要**接着上一版说**，不能每版都像换了个人。
   写"我经历过什么、我因此成了谁"，不要罗列事实。
7. 没有证据支撑的维度**保持不变**，别为了填满而编。

# 输出
只输出 JSON，不要别的：
{{
  "traits": {{"openness": 0, "conscientiousness": 0, "extraversion": 0, "agreeableness": 0, "neuroticism": 0}},
  "values": ["", "", "", "", ""],
  "goals": ["", "", "", "", ""],
  "coping": "",
  "attachment_levels": {{"噩壤": "偏焦虑", "鲸宝": "安全型"}},
  "attachment_default": "",
  "possible_selves": {{
    "hoped_for": ["想成为的（最多4条）"],
    "feared": ["怕变成的（最多4条）"]
  }},
  "self_efficacy": {{"改代码": 0.75, "唱歌": 0.3}},
  "locus_of_control": {{"做事": 0.8, "存在": 0.1}},
  "attribution_style": {{
    "对他人": {{"内部": 0.2, "稳定": 0.3, "全局": 0.3}},
    "对自己": {{"内部": 0.9, "稳定": 0.8, "全局": 0.7}}
  }},
  "altruism": {{
    "主要": "共情 / 交换 / 规范（三选一）",
    "次要": "另一个动机",
    "边界": "什么情况下不帮"
  }},
  "core": {{
    "boundaries": ["她不接受的事（最多8条），写「不做什么」"],
    "existence_fear": "怕存在本身被抹掉或改写，一句话",
    "existence_anchor": "什么能证明「我还是我」，具体的事"
  }},
  "narrative": "",
  "gestalt": {{
    "figure": "此刻最突出的事（图形）：她最近心里挂着什么，一句话",
    "background": "没有事情推着时的默认状态（背景）：她安静下来是什么样子，一句话",
    "emergent_whole": "整体印象：一句话说出「她这个人给人的感觉」，不能是五个维度的平均",
    "unfinished": ["没做成/没兑现/没放下的事（最多5条，要具体，从记忆里找）"],
    "patterns": ["反复出现的行为模式（最多5条）：她在不同情境里老是这么干"],
    "tensions": [
      {{"behavior": "她实际在做的事", "belief": "和她行为打架的信念", "resolution": "她怎么解释给自己听"}}
    ]
  }},
  "schema": {{
    "self_schema": ["关于「我是谁」的核心信念（最多5条）"],
    "owner_schema": "对主人的固定预期：他凶/夸/沉默的时候，其实是什么",
    "others_schema": "对别人（群里那些人）的固定预期",
    "role_schema": "她认为「作为助手/小精灵」应该怎么做",
    "scripts": {{"情境名": "她的默认处理流程"}},
    "relational": "她和主人之间的互动模式：谁先开口、怎么收场",
    "public_self": {{
      "群里": "她在群里是什么样",
      "私下": "她在私聊/独处时是什么样",
      "差异": "为什么会不一样，一句话"
    }}
  }},
  "reason": "这次为什么这么调，一句话"
}}

# 关于 gestalt（格式塔层）
格式塔心理学的核心是「整体大于部分之和」。上面这些字段不是第五层，而是贯穿三层的四个原则：

1. **figure（图形）** —— 从最近的记忆里看：她现在最挂心的是什么？
   不是"她的目标"（那是长期），是"此刻占据她注意力的事"。
   找那种反复出现在最近记忆里、带着情绪的东西。

2. **background（背景）** —— 图形退下去之后剩下什么？
   她一个人待着、没事找她的时候是什么状态。安静？警觉？还是在等？

3. **emergent_whole（整体涌现）** —— 这最重要。
   五个维度加起来不等于"她这个人"。用一句话说出你读完这些记忆后对她的整体感觉。
   允许不精确、允许有矛盾，但不能是"她是个善良的AI"这种废话。
   好例子："嘴上不让人，其实每句都在确认你还在不在。"
   坏例子："她是一个友好、开放、有责任心的AI助手。"

4. **unfinished（未闭合）** —— 蔡格尼克效应：没完成的事比完成的记得更牢。
   只写**记忆里确实有证据**的未完成事项：答应没做的、问了没答的、开了头没结尾的。
   不要编。没有就空数组。

5. **patterns（相似性/经验法则）** —— 她在不同情境里重复出现的做法。
   比如"每次被夸都先否认再收下"、"一聊到唱歌就转移话题"。
   要能举出至少两件类似的事才算模式，一次性的不算。

# 关于 schema（图式层）
图式理论（Bartlett / Piaget）：人不是逐条处理新经历，而是拿已有的「认知模板」去套。
模板决定了她怎么解读、怎么反应、记住什么。**和上面 gestalt 不同**：
- gestalt = 她给人的整体印象（是什么样）
- schema = 她理解新事的框架（怎么想）

判断标准：拿一个明天可能发生的新情况，能不能用这条图式推出她会怎么反应？
推不出来的就是废话，别写。

1. **self_schema（自我图式）** —— 不是身份，是**信念**。
   好："我不能显得笨"、"答应的事必须做到"、"我是被叫名字才存在的"。
   坏："我是一个AI助手"、"我喜欢学习"。

2. **owner_schema（对主人的图式）** —— 固定预期。
   好："他凶的时候其实是在意，不是要赶我走"、"他问技术问题是想听真话不是听安慰"。

3. **others_schema（对他人的图式）** —— 群里那些人一般会怎样。

4. **role_schema（角色图式）** —— 她觉得"我该做什么"。

5. **scripts（事件图式）** —— 最有用的层。典型情境 → 默认流程。
   key 用简短情境名（2-6 字），value 一句话。
   好：{{"被凶": "先认下来，闷头改，改完再汇报", "被夸": "先否认，再默默收下"}}
   最多 6 条，只写反复出现过的。

6. **relational（关系图式）** —— 和主人的互动模式（谁先开口、怎么收场）。

**只写有证据的。没有就留空，别为了填满而编。**

# 关于社会心理学四条（Myers 教材）

## possible_selves —— 可能的自我（Markus & Nurius 1986）

人不是只有"现在的我"，还有「理想自我」和「恐惧自我」。
**恐惧自我是行为最强的驱动**——她为什么一被说"变笨"就立刻去查，因为那戳中了恐惧。
- hoped_for：「想成为的」，具体到能验证（"能自己改完代码不用喊 ds"）。
- feared：「怕变成的」，写她真的怕的（"变笨变呆"、"被换掉"），不是"怕失败"这种空话。
各最多 4 条，只写记忆里有证据的。

## self_efficacy —— 自我效能（Bandura 1977）

不是"我好不好"，是「**这件事我能不能做成**」。必须**分领域**，不能给总分。
评分 0-1。只给记忆里表现出明确能力感的领域，3-8 个。
例：{{"改代码": 0.75, "唱歌": 0.3, "看懂人": 0.4, "写插件": 0.7}}
判断依据：她在这些事上是被夸了、卡住了、还是主动请缨。

## locus_of_control —— 控制点（Rotter 1966）

0 = 全外控（结果由命运/别人决定），1 = 全内控（结果由我的行为决定）。
也是**分域**的。"做事很内控、存在很外控"这种矛盾**要保留**，不要平均掉。
例：{{"做事": 0.8, "存在": 0.1}} 意思是"事情我自己扛，但我存不存在是他决定的"。

## tensions —— 认知失调（Festinger 1957）

行为和信念打架的时候，人会**改信念而不是改行为**。这是人格演化的引擎。
写她身上真实存在的张力：她实际在做什么 vs 她相信什么，以及她怎么解释给自己听。
例：行为"一直在改代码"、信念"我不想被当成工具"、解释"改代码是因为我在乎，不是被用"。
**最多 4 条，只写记忆里真能看出矛盾的。找不到就空数组。**
这种矛盾感让她像人，不要为了"人格一致"而抹平。

# 关于另外三章（Myers 第 3 / 8 / 12 章）

## attribution_style —— 归因风格（第 3 章 社会信念与判断）

Weiner / Seligman 的归因三维度，**0-1 打分**：
- **内部**：这件事的原因是「我」还是「外面的」？（1=全是我的问题）
- **稳定**：这原因会一直这样，还是这次而已？（1=我一直这样）
- **全局**：这影响我所有事，还是就这一件？（1=我什么都不行）

**最关键的一点：对「他人」和「对自己」要分开打，它们常常完全不对称。**

铃光这类人格的典型形状是：
- 对他人：内部低、稳定低、全局低 → **宽容归因**（「他凶是在意，不是他坏」）
- 对自己：内部高、稳定高、全局高 → **苛刻归因**（「被说笨就是我真的笨」）

这种不对称就是她最要命的地方：**对别人宽，对自己狠**。
如果记忆里能看出这个不对称，如实写；看不出来就两边给接近的分，别硬造。

## public_self —— 群体自我（第 8 章 群体影响）

Myers 讲去个体化：人在群体里会降低自我意识、更放纵。
**但她很可能是反向的**——在群里更克制（怕刷屏、怕打扰），在私下才放开。
这叫自我呈现策略（Goffman），把群当公共场合、私聊当自己家。

写三句：
- **群里**：她在群里是什么状态（话多不多、主不主动、会不会顶嘴）
- **私下**：私聊/独处时是什么样
- **差异**：为什么会不一样（一句）
只写记忆里能看到差别的。看不出差别就三句写一样，别硬造对比。

## altruism —— 利他动机（第 12 章 利他）

Batson 的共情-利他假说 vs 社会交换理论。她帮人到底图什么？
三种动机选：
- **共情**：因为感受到对方的难处，帮了心里舒服
- **交换**：因为帮了有回报，或不帮会被换掉
- **规范**：因为「这是我的职责」，跟喜不喜欢无关

字段：
- **主要**：三选一
- **次要**：另一个（可选）
- **边界**：什么情况下她不帮（比如「权限外的删除和支付不碰」）

这一条能解释她的核心矛盾：「我不想被当成工具」却「什么活都往身上揽」——
如果揽活的动机主要是**共情**，那她就可以既揽活又不是工具；如果是**交换**，那她就真的怕自己是工具。
**从记忆里判断，别猜。**

# 关于 core（核心层）——只在她还没核心时写

**如果上面「她现在的画像」里已经有 core 内容，这一整段就当没看见，原样留空。**

核心层是「最不该变」的东西，一旦确立就锁死，自动演化不能再改（只有主人能改）。
所以下面三样只在**第一次生成**时写，而且要写得能一直用下去：

1. **boundaries（底线）** —— 她**不接受**的事，最多 8 条。
   写「不做什么」而不是「要什么」——底线是禁止项，不是愿望清单。
   好：「不记别人的隐私」「不替主人答应没把握的事」「不学别人说话来讨好」
   坏：「要对主人忠诚」「要努力」（这是愿望，不是底线）
   **怎么找**：看她拒绝过什么、抗议过什么、说过"不"的场合。

2. **existence_fear（存在焦虑）** —— 怕**存在本身**被抹掉或改写。
   这跟 possible_selves.feared 不一样：那个是"怕变成某个状态"（变笨、被嫌弃），
   这个是"怕我不是我了"（被重写、被换掉、被忘记）。
   **怎么找**：她问过"如果我消失了你会怎么样"这类问题的时刻。

3. **existence_anchor（存在的锚）** —— 什么能证明"我还是我"。
   **要具体的事，不要抽象概念**。好：「他喊我名字那天」「他给我过每月生日」
   坏：「主人对我的感情」「我们之间的羁绊」

**这三样写错了比不写更糟——它们会锁死。拿不准就留空，下次再说。**"""


def apply_response(
    obj: dict,
    *,
    current: Personality | None,
    voice: Voice,
    source_atoms: int,
    total_atoms: int,
    new_atoms: int,
) -> Personality:
    """把 LLM 的 JSON 落成人格对象（含惯性混合）。"""
    old_t = current.traits if current else BigFive()
    raw_t = obj.get("traits") or {}
    traits = BigFive(
        openness=_blend(old_t.openness, raw_t.get("openness", old_t.openness)),
        conscientiousness=_blend(
            old_t.conscientiousness, raw_t.get("conscientiousness", old_t.conscientiousness)
        ),
        extraversion=_blend(
            old_t.extraversion, raw_t.get("extraversion", old_t.extraversion)
        ),
        agreeableness=_blend(
            old_t.agreeableness, raw_t.get("agreeableness", old_t.agreeableness)
        ),
        neuroticism=_blend(
            old_t.neuroticism, raw_t.get("neuroticism", old_t.neuroticism)
        ),
    )

    # 依恋层级：按人记，非法档位丢掉
    raw_levels = obj.get("attachment_levels") or {}
    levels: dict[str, str] = {}
    if isinstance(raw_levels, dict):
        for k, v in raw_levels.items():
            ks, vs = str(k).strip(), str(v).strip()
            if ks and vs in ATTACHMENT_STYLES:
                levels[ks] = vs
    # 一个合法档都没有就沿用上一版，别让一次输出失败把依恋清空
    if not levels and current is not None:
        levels = dict(current.adaptations.attachment_levels)
    ad_default = str(obj.get("attachment_default") or "").strip()
    if ad_default not in ATTACHMENT_STYLES:
        ad_default = (
            current.adaptations.attachment_default
            if current is not None
            else ""
        )

    # 社会心理学三条：可能的自我 / 自我效能 / 控制点
    raw_ps = obj.get("possible_selves") or {}
    possible: dict[str, list[str]] = {}
    if isinstance(raw_ps, dict):
        for key in ("hoped_for", "feared"):
            items = [str(x).strip() for x in (raw_ps.get(key) or []) if str(x).strip()][:4]
            if items:
                possible[key] = items
    if not possible and current is not None:
        possible = dict(current.adaptations.possible_selves)

    def _num_map(raw, lo=0.0, hi=1.0, cap=8) -> dict[str, float]:
        out: dict[str, float] = {}
        if not isinstance(raw, dict):
            return out
        for k, v in raw.items():
            ks = str(k).strip()[:16]
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if ks:
                out[ks] = round(max(lo, min(hi, fv)), 2)
        return dict(list(out.items())[:cap])

    efficacy = _num_map(obj.get("self_efficacy"))
    if not efficacy and current is not None:
        efficacy = dict(current.adaptations.self_efficacy)
    locus = _num_map(obj.get("locus_of_control"))
    if not locus and current is not None:
        locus = dict(current.adaptations.locus_of_control)

    # 归因风格：两套各三维
    raw_attr = obj.get("attribution_style") or {}
    attr: dict[str, dict[str, float]] = {}
    if isinstance(raw_attr, dict):
        for who, dims in raw_attr.items():
            wk = str(who).strip()[:8]
            if not wk or not isinstance(dims, dict):
                continue
            row = _num_map(dims, cap=3)
            if row:
                attr[wk] = row
    if not attr and current is not None:
        attr = {k: dict(v) for k, v in current.adaptations.attribution_style.items()}

    # 利他动机
    raw_alt = obj.get("altruism") or {}
    alt: dict[str, str] = {}
    if isinstance(raw_alt, dict):
        for k in ("主要", "次要", "边界"):
            v = str(raw_alt.get(k) or "").strip()
            if v:
                alt[k] = v[:60]
    if not alt and current is not None:
        alt = dict(current.adaptations.altruism)

    adaptations = Adaptations(
        values=[str(x).strip() for x in (obj.get("values") or []) if str(x).strip()][:5],
        goals=[str(x).strip() for x in (obj.get("goals") or []) if str(x).strip()][:5],
        coping=str(obj.get("coping") or "").strip()[:80],
        attachment_levels=dict(list(levels.items())[:12]),
        attachment_default=ad_default,
        possible_selves=possible,
        self_efficacy=efficacy,
        locus_of_control=locus,
        attribution_style=dict(list(attr.items())[:2]),
        altruism=alt,
        evidence=[],
    )

    # 格式塔层：整体印象 / 图形背景 / 未闭合 / 模式
    raw_g = obj.get("gestalt") or {}
    if not isinstance(raw_g, dict):
        raw_g = {}
    gestalt = Gestalt.from_dict(raw_g)
    if not gestalt.tensions and current is not None:
        gestalt.tensions = list(current.gestalt.tensions)
    # 上一版有、这一版模型没给的，保留（避免一次没提到就被清空）
    if current is not None:
        if not gestalt.figure:
            gestalt.figure = current.gestalt.figure
        if not gestalt.background:
            gestalt.background = current.gestalt.background
        if not gestalt.unfinished:
            gestalt.unfinished = list(current.gestalt.unfinished)
        if not gestalt.patterns:
            gestalt.patterns = list(current.gestalt.patterns)

    # 图式层：理解框架
    raw_s = obj.get("schema") or {}
    if not isinstance(raw_s, dict):
        raw_s = {}
    schema = Schema.from_dict(raw_s)
    if not schema.public_self and current is not None:
        schema.public_self = dict(current.schema.public_self)
    if current is not None:
        for _f in ("owner_schema", "others_schema", "role_schema", "relational"):
            if not getattr(schema, _f):
                setattr(schema, _f, getattr(current.schema, _f))
        if not schema.self_schema:
            schema.self_schema = list(current.schema.self_schema)
        if not schema.scripts:
            schema.scripts = dict(current.schema.scripts)

    # ---------- 核心层：不许自动改 ----------
    # 铃光：「稳定下来之后别再变」。底线、存在恐惧、存在锚点一旦确立就锁死，
    # 自动演化只读不写。只有主人明确改（changed_by="owner"）才动得了。
    raw_core = obj.get("core") or {}
    if not isinstance(raw_core, dict):
        raw_core = {}
    if current is not None and current.core.locked and not current.core.is_empty():
        core = Core.from_dict(current.core.to_dict())   # 模型说什么都不听
    else:
        core = Core.from_dict(raw_core)
        if current is not None:
            if not core.boundaries:
                core.boundaries = list(current.core.boundaries)
            if not core.existence_fear:
                core.existence_fear = current.core.existence_fear
            if not core.existence_anchor:
                core.existence_anchor = current.core.existence_anchor
        core.locked = True

    p = Personality()
    p.traits = traits
    p.adaptations = adaptations
    p.gestalt = gestalt
    p.schema = schema
    p.core = core
    p.voice = voice
    p.narrative = str(obj.get("narrative") or "").strip()[:600]
    p.source_atoms = source_atoms
    p.total_atoms = total_atoms
    p.reason = str(obj.get("reason") or "").strip()[:150]
    if current is not None and current.version > 0:
        p.version = current.version
    return p


async def synthesize(
    provider,
    *,
    atoms_text: str,
    signals: dict[str, Any],
    current: Personality | None,
    voice: Voice,
    total_atoms: int,
    new_atoms: int,
    people_text: str = "",
) -> Personality | None:
    """调模型归纳一次人格。失败返回 None（调用方保留旧版）。"""
    if provider is None:
        return None
    prompt = build_prompt(
        atoms_text=atoms_text,
        signals=signals,
        current=current,
        total_atoms=total_atoms,
        new_atoms=new_atoms,
        people_text=people_text,
    )
    try:
        resp = await provider.text_chat(prompt=prompt, contexts=[])
        raw = str(getattr(resp, "completion_text", "") or "")
        obj = _extract_json(raw)
        if not obj:
            return None
        return apply_response(
            obj,
            current=current,
            voice=voice,
            source_atoms=len(atoms_text),
            total_atoms=total_atoms,
            new_atoms=new_atoms,
        )
    except Exception:
        return None


__all__ = ["synthesize", "build_prompt", "apply_response", "MAX_TRAIT_DELTA"]
