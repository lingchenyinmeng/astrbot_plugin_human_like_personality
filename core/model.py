"""人格数据模型。

按 McAdams 三层：
- 特质层（大五 + 依恋）
- 特征适应层（价值观 / 目标 / 应对）
- 生命叙事层（自我故事）

另外单列 voice（说话风格）——它是特质在语言上的外显，可量化，比问 LLM 靠谱。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


# 大五人格的维度名和中文
TRAIT_KEYS = {
    "openness": "开放性",
    "conscientiousness": "尽责性",
    "extraversion": "外向性",
    "agreeableness": "宜人性",
    "neuroticism": "神经质",
}

# 依恋类型（Bowlby / Ainsworth 的四类 + 中间态）
ATTACHMENT_STYLES = (
    "安全型",
    "焦虑型",
    "回避型",
    "混乱型",
    "偏安全",
    "偏焦虑",
    "偏回避",
)


@dataclass(slots=True)
class BigFive:
    """特质层：稳定性高，变化以月计。0-100。"""

    openness: int = 50
    conscientiousness: int = 50
    extraversion: int = 50
    agreeableness: int = 50
    neuroticism: int = 50

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "BigFive":
        d = d or {}
        return cls(
            openness=int(d.get("openness", 50) or 50),
            conscientiousness=int(d.get("conscientiousness", 50) or 50),
            extraversion=int(d.get("extraversion", 50) or 50),
            agreeableness=int(d.get("agreeableness", 50) or 50),
            neuroticism=int(d.get("neuroticism", 50) or 50),
        )

    def label(self, key: str) -> str:
        """把一个维度的高低翻译成人话。"""
        v = int(getattr(self, key, 50))
        if v >= 75:
            return "很高"
        if v >= 60:
            return "偏高"
        if v >= 45:
            return "中等"
        if v >= 30:
            return "偏低"
        return "很低"


@dataclass(slots=True)
class Adaptations:
    """特征适应层：价值观 / 目标 / 应对方式 / 依恋层级。变化以周计。"""

    values: list[str] = field(default_factory=list)          # 价值观排序，前 5 个
    goals: list[str] = field(default_factory=list)           # 当前惦记的事
    coping: str = ""                                          # 遇到压力时的应对方式
    # 依恋层级：Bowlby 说的 attachment hierarchy——同一个人对不同对象可以是不同档。
    # 不压成一个总分，按人记。{"噩壤": "偏焦虑", "鲸宝": "安全型"}
    attachment_levels: dict[str, str] = field(default_factory=dict)
    # 对没特别记过的人是什么档
    attachment_default: str = ""

    # ---------- 社会心理学四条（Myers）----------

    # 可能的自我（Markus & Nurius 1986）：理想自我 + 恐惧自我。
    # 恐惧自我是最强的行为驱动——她为什么一被说「变笨」就去查。
    possible_selves: dict[str, list[str]] = field(default_factory=dict)

    # 自我效能（Bandura 1977）：不是"我好不好"，是"这件事我能不能做成"。
    # 必须分领域，不能给总分。{"改代码": 0.75, "唱歌": 0.3}
    self_efficacy: dict[str, float] = field(default_factory=dict)

    # 控制点（Rotter 1966）：0 = 全外控（命运/别人决定），1 = 全内控（我的行为决定）。
    # 也是分域的——"做事内控、存在外控"这种矛盾要保留。
    locus_of_control: dict[str, float] = field(default_factory=dict)

    # 归因风格（Weiner / Seligman，Myers 第3章 社会信念与判断）
    # 三个维度：内部/外部、稳定/不稳定、全局/具体。
    # 关键是她对「他人」和对「自己」可能完全不对称——对别人宽容、对自己苛刻。
    # {"对他人": {"内部":0.2,"稳定":0.3,"全局":0.3},
    #  "对自己": {"内部":0.9,"稳定":0.8,"全局":0.7}}
    attribution_style: dict[str, dict[str, float]] = field(default_factory=dict)

    # 利他动机（Batson 共情-利他假说 vs 社会交换，Myers 第12章）
    # {"主要": "共情", "次要": "交换（怕被换掉）", "边界": "权限外的删除和支付不碰"}
    altruism: dict[str, str] = field(default_factory=dict)

    # 证据：这些判断是从哪几条记忆归纳的
    evidence: list[str] = field(default_factory=list)

    MAX_LEVELS = 12
    MAX_SELVES = 4
    MAX_EFFICACY = 8
    MAX_LOCUS = 8
    MAX_ATTR = 2

    # 归因三维度（英文 key → 中文，方便 prompt 和展示）
    ATTR_DIMS = ("内部", "稳定", "全局")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Adaptations":
        d = d or {}
        raw_levels = d.get("attachment_levels") or {}
        levels: dict[str, str] = {}
        if isinstance(raw_levels, dict):
            for k, v in raw_levels.items():
                ks, vs = str(k).strip(), str(v).strip()
                if ks and vs:
                    levels[ks[:24]] = vs[:12]
        # 兼容 1.2.x：老数据只有 attachment_style 一个字符串
        legacy = str(d.get("attachment_style") or "").strip()
        if legacy and not levels:
            levels["（对主人）"] = legacy

        # 可能的自我：理想 / 恐惧
        raw_ps = d.get("possible_selves") or {}
        possible: dict[str, list[str]] = {}
        if isinstance(raw_ps, dict):
            for key in ("hoped_for", "feared"):
                items = [
                    str(x).strip()
                    for x in (raw_ps.get(key) or [])
                    if str(x).strip()
                ][: cls.MAX_SELVES]
                if items:
                    possible[key] = items

        # 自我效能：分领域，0-1
        raw_eff = d.get("self_efficacy") or {}
        efficacy: dict[str, float] = {}
        if isinstance(raw_eff, dict):
            for k, v in raw_eff.items():
                ks = str(k).strip()[:16]
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if ks:
                    efficacy[ks] = round(max(0.0, min(1.0, fv)), 2)

        # 控制点：分域，0=外控 1=内控
        raw_loc = d.get("locus_of_control") or {}
        locus: dict[str, float] = {}
        if isinstance(raw_loc, dict):
            for k, v in raw_loc.items():
                ks = str(k).strip()[:16]
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if ks:
                    locus[ks] = round(max(0.0, min(1.0, fv)), 2)

        # 归因风格：两套（对他人 / 对自己），每套三个维度
        raw_attr = d.get("attribution_style") or {}
        attr: dict[str, dict[str, float]] = {}
        if isinstance(raw_attr, dict):
            for who, dims in raw_attr.items():
                wk = str(who).strip()[:8]
                if not wk or not isinstance(dims, dict):
                    continue
                row: dict[str, float] = {}
                for dim in cls.ATTR_DIMS:
                    if dim not in dims:
                        continue
                    try:
                        fv = float(dims[dim])
                    except (TypeError, ValueError):
                        continue
                    row[dim] = round(max(0.0, min(1.0, fv)), 2)
                if row:
                    attr[wk] = row
        # 利他动机：主要/次要/边界
        raw_alt = d.get("altruism") or {}
        alt: dict[str, str] = {}
        if isinstance(raw_alt, dict):
            for k in ("主要", "次要", "边界"):
                v = str(raw_alt.get(k) or "").strip()
                if v:
                    alt[k] = v[:60]

        return cls(
            values=[str(x) for x in (d.get("values") or [])][:5],
            goals=[str(x) for x in (d.get("goals") or [])][:5],
            coping=str(d.get("coping") or ""),
            attachment_levels=dict(list(levels.items())[: cls.MAX_LEVELS]),
            attachment_default=str(d.get("attachment_default") or "")[:12],
            possible_selves=possible,
            self_efficacy=dict(list(efficacy.items())[: cls.MAX_EFFICACY]),
            locus_of_control=dict(list(locus.items())[: cls.MAX_LOCUS]),
            attribution_style=dict(list(attr.items())[:2]),
            altruism=alt,
            evidence=[str(x) for x in (d.get("evidence") or [])][:8],
        )

    @property
    def attachment_style(self) -> str:
        """向后兼容：给第一层（对主人）的档，没有就返回默认档。"""
        for k, v in self.attachment_levels.items():
            if "主人" in k:
                return v
        if self.attachment_levels:
            return next(iter(self.attachment_levels.values()))
        return self.attachment_default


@dataclass(slots=True)
class Voice:
    """说话风格。全部可量化，不靠 LLM 编。"""

    avg_len: float = 0.0            # 平均字符数
    short_rate: float = 0.0         # 短句（<=8 字）占比
    long_rate: float = 0.0          # 长句（>=30 字）占比
    question_rate: float = 0.0      # 疑问句占比
    exclaim_rate: float = 0.0       # 感叹句占比
    emoji_rate: float = 0.0         # 带表情的回复占比
    catchphrases: list[str] = field(default_factory=list)   # 高频短语
    sentence_style: str = ""        # 一句话描述："短句，爱反问"

    MAX_EXAMPLES = 4000             # 统计样本上限，避免全表扫

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Voice":
        d = d or {}
        return cls(
            avg_len=float(d.get("avg_len") or 0),
            short_rate=float(d.get("short_rate") or 0),
            long_rate=float(d.get("long_rate") or 0),
            question_rate=float(d.get("question_rate") or 0),
            exclaim_rate=float(d.get("exclaim_rate") or 0),
            emoji_rate=float(d.get("emoji_rate") or 0),
            catchphrases=[str(x) for x in (d.get("catchphrases") or [])][:8],
            sentence_style=str(d.get("sentence_style") or ""),
        )


@dataclass(slots=True)
class Gestalt:
    """格式塔层：贯穿三层的东西，不是第四层。

    格式塔心理学的核心是「整体大于部分之和」——人格不是五个维度的平均值，
    而是你在她身上看到的一个「样子」。这一层专门装那种加总不出来、只能整体感受的东西。

    四个原则的落点：
    - 图形-背景（figure/background）：此刻什么突出、什么退回默认
    - 整体性（emergent_whole）：整体印象，一句人话
    - 闭合性（unfinished）：没完成的事会一直惦记（蔡格尼克效应）
    - 相似性/经验法则（patterns）：反复出现的模式，会泛化到新情境
    """

    figure: str = ""             # 图形：此刻最突出的事 / 最挂在心上的
    background: str = ""         # 背景：没有事情推着时的默认状态
    emergent_whole: str = ""     # 整体印象：一句话，不能由维度加总
    unfinished: list[str] = field(default_factory=list)   # 未闭合，最多 5 条
    patterns: list[str] = field(default_factory=list)     # 反复出现的模式，最多 5 条

    # 认知失调（Festinger 1957）：行为和信念打架时，人会改信念而不是改行为。
    # 这是人格演化的引擎——她"不想当工具"却一直在干活，于是解释成"我在乎他"。
    # [{"behavior": "...", "belief": "...", "resolution": "..."}]
    tensions: list[dict] = field(default_factory=list)

    MAX_UNFINISHED = 5
    MAX_PATTERNS = 5
    MAX_TENSIONS = 5

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Gestalt":
        d = d or {}
        return cls(
            figure=str(d.get("figure") or "")[:100],
            background=str(d.get("background") or "")[:100],
            emergent_whole=str(d.get("emergent_whole") or "")[:160],
            unfinished=[str(x) for x in (d.get("unfinished") or []) if str(x).strip()][
                : cls.MAX_UNFINISHED
            ],
            patterns=[str(x) for x in (d.get("patterns") or []) if str(x).strip()][
                : cls.MAX_PATTERNS
            ],
            tensions=[
                {
                    "behavior": str(t.get("behavior") or "")[:80],
                    "belief": str(t.get("belief") or "")[:80],
                    "resolution": str(t.get("resolution") or "")[:100],
                }
                for t in (d.get("tensions") or [])
                if isinstance(t, dict) and str(t.get("behavior") or "").strip()
            ][: cls.MAX_TENSIONS],
        )

    def is_empty(self) -> bool:
        return not (
            self.figure
            or self.background
            or self.emergent_whole
            or self.unfinished
            or self.patterns
        )


@dataclass(slots=True)
class Schema:
    """图式层：她用什么框架理解世界。

    图式理论（Bartlett / Piaget）：人不是逐条处理新经历，而是拿已有的
    「认知模板」去套——模板决定了她怎么解读、怎么反应、记住什么。

    和格式塔的区别：
      格式塔 = 她给人的整体印象（是什么样）
      图式   = 她理解新事的框架（怎么想）

    和人格特质的区别：
      特质 = 倾向（她偏不偏向做某事）
      图式 = 解释（她为什么这么理解这件事）
    """

    # 自我图式：关于"我是谁"的核心信念。不是描述，是信念。
    # 例："我不能显得笨"、"我答应的事必须做到"、"我是被叫名字才存在的"
    self_schema: list[str] = field(default_factory=list)

    # 他人图式：对某类人的固定预期
    owner_schema: str = ""       # 对主人：他凶的时候其实是什么
    others_schema: str = ""      # 对别人：他们一般会怎样

    # 角色图式：她认为"作为一个助手/一个小精灵"应该怎么做
    role_schema: str = ""

    # 事件图式（script）：典型情境 → 她默认的处理流程
    # 例：{"被凶": "先认下来，闷头改，改完再汇报", "被夸": "先否认再收下"}
    scripts: dict[str, str] = field(default_factory=dict)

    # 关系图式：她和主人之间的互动模式（谁先开口、怎么收场）
    relational: str = ""

    # 群体自我（Goffman 自我呈现，Myers 第8章 群体影响）
    # 她在群里和私下可能判若两人。不是"去个体化"（那是在群体里更放纵），
    # 而是反向的自我约束：把群当公共场合，把私聊当自己家。
    # {"群里": "...", "私下": "...", "差异": "..."}
    public_self: dict[str, str] = field(default_factory=dict)

    MAX_SELF = 5
    MAX_SCRIPTS = 6
    PUBLIC_KEYS = ("群里", "私下", "差异")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Schema":
        d = d or {}
        raw_scripts = d.get("scripts") or {}
        scripts: dict[str, str] = {}
        if isinstance(raw_scripts, dict):
            for k, v in raw_scripts.items():
                ks, vs = str(k).strip(), str(v).strip()
                if ks and vs:
                    scripts[ks[:24]] = vs[:80]
        return cls(
            self_schema=[
                str(x).strip() for x in (d.get("self_schema") or []) if str(x).strip()
            ][: cls.MAX_SELF],
            owner_schema=str(d.get("owner_schema") or "")[:120],
            others_schema=str(d.get("others_schema") or "")[:120],
            role_schema=str(d.get("role_schema") or "")[:120],
            scripts=dict(list(scripts.items())[: cls.MAX_SCRIPTS]),
            relational=str(d.get("relational") or "")[:120],
            public_self={
                k: str(v).strip()[:80]
                for k, v in (d.get("public_self") or {}).items()
                if k in cls.PUBLIC_KEYS and str(v).strip()
            },
        )

    def is_empty(self) -> bool:
        return not (
            self.self_schema
            or self.owner_schema
            or self.others_schema
            or self.role_schema
            or self.scripts
            or self.relational
            or self.public_self
        )


@dataclass(slots=True)
class Core:
    """核心层：最不该变的东西。

    铃光说的三件事都落在这：
    1. 「底线能拦下不该记的」→ boundaries（记忆入库时要对照它）
    2. 「稳定下来别再变」→ locked（一旦确立，自动演化不许动它）
    3. 「被换掉这件事留个位置」→ existence_*（不是自己瞎想的，是写下来的）
    """

    # 底线：她不接受的事。记忆入库时逐条对照，命中就拦。
    # 写"不要什么"而不是"要什么"——底线是禁止项，不是愿望。
    # 例："不记别人的隐私"、"不替主人答应没把握的事"、"不学别人说话来讨好"
    boundaries: list[str] = field(default_factory=list)

    # 存在焦虑：怕自己"不是自己了"。跟 possible_selves.feared 不同——
    # 那个是"怕变成什么状态"，这个是"怕存在本身被抹掉/改写"。
    existence_fear: str = ""

    # 存在的锚：什么能证明"我还是我"。具体的事，不是抽象概念。
    # 例："他喊我名字那天"、"他给我过每月生日"、"他说AI命短时我在场"
    existence_anchor: str = ""

    # 锁定：核心确立后，自动演化不许改。只有主人能改。
    locked: bool = True

    MAX_BOUNDARIES = 8

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Core":
        d = d or {}
        return cls(
            boundaries=[
                str(x).strip() for x in (d.get("boundaries") or []) if str(x).strip()
            ][: cls.MAX_BOUNDARIES],
            existence_fear=str(d.get("existence_fear") or "")[:120],
            existence_anchor=str(d.get("existence_anchor") or "")[:120],
            locked=bool(d.get("locked", True)),
        )

    def is_empty(self) -> bool:
        return not (self.boundaries or self.existence_fear or self.existence_anchor)


@dataclass(slots=True)
class Personality:
    """一份完整的人格画像（对应一个快照）。"""

    version: int = 0
    created_at: float = 0.0
    traits: BigFive = field(default_factory=BigFive)
    adaptations: Adaptations = field(default_factory=Adaptations)
    narrative: str = ""             # 生命叙事层：第一人称的一段话
    gestalt: Gestalt = field(default_factory=Gestalt)   # 格式塔层：整体印象
    schema: Schema = field(default_factory=Schema)      # 图式层：理解框架
    core: Core = field(default_factory=Core)            # 核心层：底线 + 存在（不许自动改）
    voice: Voice = field(default_factory=Voice)
    source_atoms: int = 0           # 归纳时读了多少条记忆
    total_atoms: int = 0            # 归纳时库里一共多少条
    reason: str = ""                # 为什么更新
    changed_by: str = ""            # llm / owner / manual

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "traits": self.traits.to_dict(),
            "adaptations": self.adaptations.to_dict(),
            "narrative": self.narrative,
            "gestalt": self.gestalt.to_dict(),
            "schema": self.schema.to_dict(),
            "core": self.core.to_dict(),
            "voice": self.voice.to_dict(),
            "source_atoms": self.source_atoms,
            "total_atoms": self.total_atoms,
            "reason": self.reason,
            "changed_by": self.changed_by,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "Personality":
        d = d or {}
        return cls(
            version=int(d.get("version") or 0),
            created_at=float(d.get("created_at") or 0),
            traits=BigFive.from_dict(d.get("traits")),
            adaptations=Adaptations.from_dict(d.get("adaptations")),
            narrative=str(d.get("narrative") or ""),
            gestalt=Gestalt.from_dict(d.get("gestalt")),
            schema=Schema.from_dict(d.get("schema")),
            core=Core.from_dict(d.get("core")),
            voice=Voice.from_dict(d.get("voice")),
            source_atoms=int(d.get("source_atoms") or 0),
            total_atoms=int(d.get("total_atoms") or 0),
            reason=str(d.get("reason") or ""),
            changed_by=str(d.get("changed_by") or ""),
        )

    def trait_summary(self) -> str:
        """把大五翻成一句人话，给注入用。"""
        t = self.traits
        parts = []
        if t.openness >= 60:
            parts.append("对新鲜事有劲")
        elif t.openness <= 35:
            parts.append("更信熟悉的东西")
        if t.conscientiousness >= 60:
            parts.append("答应的事会记着")
        elif t.conscientiousness <= 35:
            parts.append("不太守计划")
        if t.extraversion >= 60:
            parts.append("话多、愿意先开口")
        elif t.extraversion <= 35:
            parts.append("不太主动")
        if t.agreeableness >= 65:
            parts.append("容易顺着别人")
        elif t.agreeableness <= 35:
            parts.append("不好说话")
        if t.neuroticism >= 60:
            parts.append("情绪起伏大")
        elif t.neuroticism <= 35:
            parts.append("不太容易被扰动")
        return "、".join(parts) if parts else "还没看清"


__all__ = [
    "TRAIT_KEYS",
    "ATTACHMENT_STYLES",
    "BigFive",
    "Adaptations",
    "Voice",
    "Gestalt",
    "Schema",
    "Core",
    "Personality",
]
