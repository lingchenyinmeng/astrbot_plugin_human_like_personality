"""Human-like Personality —— 人格生成和管理。

和 human_like_memory 的分工：
  记忆插件管「发生过什么」，这个插件管「因此我是谁」。
和 fish_heart 的分工：
  鱼的心管「此刻什么心情」（天气），这个管「长期是什么样的人」（气候）。

理论：McAdams 三层人格模型
  特质层（大五 + 依恋）→ 特征适应层（价值观/目标/应对）→ 生命叙事层（自我故事）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

import aiosqlite

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.analyzer import analyze_voice, behavior_signals
from .core.coherence import apply_fixes, build_review_prompt, check_coherence, parse_review
from .core.injector import build_block, build_self_report
from .core.model import Personality, Voice
from .core.store import PersonalityStore
from .core.synthesizer import build_prompt, apply_response, synthesize

PLUGIN_NAME = "astrbot_plugin_human_like_personality"
MEMORY_PLUGIN = "astrbot_plugin_human_like_memory"


@register(PLUGIN_NAME, "大肥鱼", "人格不是写死的提示词，是从记忆里长出来的", "1.0.0")
class HumanLikePersonality(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.conf = config or {}
        self.context = context
        self.store: PersonalityStore | None = None
        self.current: Personality | None = None
        self.memory_db: str | None = None
        self._evolve_lock = asyncio.Lock()
        self._last_check = 0.0
        # 昵称 → QQ，评价统一键到 QQ，避免同一个人两套评价
        self._name_qq_map: dict[str, str] = {}

    # ---------- 初始化 ----------

    async def initialize(self) -> None:
        data_dir = self._plugin_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        self.store = PersonalityStore(os.path.join(data_dir, "personality.db"))
        await self.store.initialize()
        self.memory_db = self._find_memory_db()
        self.current = await self.store.latest()
        logger.info(
            f"[hlp] 人格插件就绪 | 当前版本 "
            f"{self.current.version if self.current else '无'} | 记忆库 {self.memory_db or '未找到'}"
        )
        # 首次启动如果没人格，后台先归纳一版
        if self.current is None and self.memory_db:
            asyncio.create_task(self._safe_evolve("首次生成"))
        # 后台循环（演化 + 评价更新 + 漂移）放 initialize 里起：
        # on_astrbot_loaded 只在整机启动时触发，插件 reload 不会重发，
        # 放这里保证 reload 后循环照样跑。
        self._loop_started = True
        asyncio.create_task(self._evolve_loop())
        logger.info("[hlp] 后台循环已启动（演化 + 评价 + 漂移）")

    def _plugin_data_dir(self) -> str:
        """自己的插件数据目录。

        **优先从插件代码位置推**，不用 cwd——插件在 `data/plugins/<name>/main.py`，
        往上 4 层就是 AstrBot 根，再拼 `data/plugin_data/<name>`。
        cwd 会变（测试、别的进程调起来都不一样），代码位置不会。
        """
        try:
            here = os.path.dirname(os.path.abspath(__file__))   # data/plugins/<name>
            root = os.path.dirname(os.path.dirname(here))       # data
            root = os.path.dirname(root)                        # AstrBot 根
            candidate = os.path.join(root, "data", "plugin_data", PLUGIN_NAME)
            if os.path.isdir(os.path.join(root, "data", "plugin_data")):
                return candidate
        except Exception:
            pass
        # 其次问 AstrBot
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = get_astrbot_data_path()
            if base:
                return os.path.join(base, "plugin_data", PLUGIN_NAME)
        except Exception:
            pass
        try:
            from astrbot.core.star.star_tools import StarTools

            return str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            return os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_NAME)

    def _find_memory_db(self) -> str | None:
        explicit = str(self.conf.get("memory_db_path") or "").strip()
        if explicit and os.path.isfile(explicit):
            return explicit
        # 1. 和我自己的 data 目录同级
        try:
            base = os.path.dirname(self._plugin_data_dir())
            p = os.path.join(base, MEMORY_PLUGIN, "memory.db")
            if os.path.isfile(p):
                return p
        except Exception:
            pass

        # 2. 从插件代码目录往上推（某些部署 data 不在插件同级）
        try:
            here = os.path.abspath(__file__)
            for _ in range(6):
                here = os.path.dirname(here)
                p = os.path.join(
                    here, "data", "plugin_data", MEMORY_PLUGIN, "memory.db"
                )
                if os.path.isfile(p):
                    return p
        except Exception:
            pass

        return None

    # ---------- 注入 ----------

    @filter.on_llm_request(priority=60)
    async def _inject_personality(self, event: AstrMessageEvent, request) -> None:
        if not self.conf.get("enabled", True):
            return
        try:
            p = self.current
            if p is None and self.store is not None:
                p = await self.store.latest()
                self.current = p
            block = build_block(p, max_chars=int(self.conf.get("inject_max_chars", 1450) or 1450))
            if not block:
                return
            sp = getattr(request, "system_prompt", "") or ""
            if block in sp:
                return
            setattr(request, "system_prompt", (sp.rstrip() + "\n\n" + block).strip())
        except Exception as e:
            logger.debug(f"[hlp] 注入失败: {e!r}")

    # ---------- 自动演化 ----------

    @filter.on_astrbot_loaded()
    async def _start_loop(self) -> None:
        """起一个后台循环，每 30 分钟检查要不要演化一次。"""
        if getattr(self, "_loop_started", False):
            return
        self._loop_started = True
        asyncio.create_task(self._evolve_loop())

    async def _evolve_loop(self) -> None:
        await asyncio.sleep(60)  # 启动后等一分钟，别和别的插件抢
        while True:
            try:
                if self.memory_db is None:
                    self.memory_db = self._find_memory_db()
                await self._maybe_auto_evolve()
                await self._update_ratings()
                await self._drift_ratings()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[hlp] 自动演化循环出错: {e!r}")
            await asyncio.sleep(1800)

    # ---------- 演化 ----------

    async def _safe_evolve(self, reason: str = "自动演化") -> int | None:
        try:
            async with self._evolve_lock:
                return await self._evolve(reason=reason)
        except Exception as e:
            logger.warning(f"[hlp] 演化失败: {e!r}")
            return None

    async def _evolve(self, *, reason: str = "") -> int | None:
        if self.store is None:
            return None
        if not self.memory_db:
            logger.warning("[hlp] 没有记忆库，无法归纳人格")
            return None

        atoms = await self._load_atoms(int(self.conf.get("source_atoms", 120) or 120))
        if not atoms:
            logger.info("[hlp] 记忆为空，跳过演化")
            return None
        total = await self._count_atoms()
        prev = self.current or await self.store.latest()
        new_atoms = max(0, total - (prev.total_atoms if prev else 0))
        # 增量太少就别重算——每次重算模型可能给出不同结果，人格会飘
        floor = int(self.conf.get("evolve_min_new_atoms", 10) or 10)
        if prev is not None and prev.version > 0 and new_atoms < floor:
            logger.info(
                f"[hlp] 只新增 {new_atoms} 条（<{floor}），跳过演化，保持第 {prev.version} 版"
            )
            return None

        voice = analyze_voice(await self._load_utterances())
        signals = behavior_signals(atoms)
        atoms_text = self._format_atoms(atoms)
        people = await self._load_people()
        # 补同框频次：依恋层级光看底色不够，还得看互动次数
        try:
            co = await self._count_cooccur()
            for item in people:
                pid = str(item.get("person_id") or "")
                if pid:
                    item["cooccur"] = sum(
                        v for (a, b), v in co.items() if pid in (a, b)
                    )
        except Exception as e:
            logger.debug(f"[hlp] 同框接线失败: {e!r}")
        people_text = self._format_people(people)

        provider = self._provider()
        p = await synthesize(
            provider,
            atoms_text=atoms_text,
            signals=signals,
            current=prev,
            voice=voice,
            total_atoms=total,
            new_atoms=new_atoms,
            people_text=people_text,
            bot_name=str(self.conf.get("bot_name") or "").strip(),
        )
        if p is None:
            logger.warning("[hlp] 模型没给出有效人格，保留旧版")
            return None

        # ---------- 统一：跨层一致性校验 ----------
        # 叙事是主锚，其余层不能打架。先做零成本的规则校验，再（可选）让模型自查一遍。
        coherence_issues: list[dict] = []
        try:
            coherence_issues += check_coherence(p)
        except Exception as e:
            logger.debug(f"[hlp] 规则一致性校验失败: {e!r}")
        if bool(self.conf.get("coherence_llm_check", True)):
            try:
                review = await provider.text_chat(
                    prompt=build_review_prompt(p), contexts=[]
                )
                raw = str(getattr(review, "completion_text", "") or "")
                fixes = parse_review(raw)
                coherence_issues += apply_fixes(p, fixes)
            except Exception as e:
                logger.debug(f"[hlp] LLM 一致性自查失败: {e!r}")

        # ---------- 演化前自检：叙事剧变要拦下来 ----------
        # 铃光：「人格这东西，权重一变、逻辑一改，我就成了另一个人」。
        # 自动演化出错（模型把叙事写成完全另一个人）时，没有人工审批，
        # 所以至少要在落盘前发现它。
        drift = 1.0
        if prev is not None and prev.narrative and p.narrative:
            drift = self._text_similarity(prev.narrative, p.narrative)
            floor = float(self.conf.get("drift_guard_ratio", 0.35) or 0.35)
            if drift < floor:
                logger.warning(
                    f"[hlp] 叙事剧变（相似度 {drift:.2f} < {floor}），拒绝落盘，保持第 {prev.version} 版。"
                    f" 新叙事：{p.narrative[:80]}"
                )
                return None

        # 把这次用的记忆 id 一起存进去——铃光要能查「是哪条记忆把我改成了这样」
        trigger_ids = [int(a.get("id") or 0) for a in atoms if a.get("id")]
        version = await self.store.save(
            p,
            reason=reason or p.reason,
            changed_by="llm",
            trigger_atom_ids=trigger_ids,
        )
        # 把「统一」校验的修正记进 events，/人格 why 能查到
        if coherence_issues:
            try:
                n = await self.store.record_coherence(version, coherence_issues)
                if n:
                    logger.info(f"[hlp] 统一校验修正 {n} 处（第 {version} 版）")
            except Exception as e:
                logger.debug(f"[hlp] 记录一致性修正失败: {e!r}")
        self.current = await self.store.latest()
        await self.store.set_state("last_total_atoms", str(total))
        await self.store.set_state("last_evolve_at", str(time.time()))
        logger.info(
            f"[hlp] 人格更新到第 {version} 版（叙事相似度 {drift:.2f}）"
        )
        # 人格外显：把「现在的她是谁」同步到 QQ 签名（默认关，见 sync_signature_on_evolve）
        try:
            await self._apply_outer_sync(self.current)
        except Exception as e:
            logger.debug(f"[hlp] 人格外显同步失败: {e!r}")
        return version

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """字符 2-gram Jaccard。不依赖 embedding，够判「是不是同一个故事」。"""

        def grams(s: str) -> set[str]:
            s = "".join(str(s or "").split())
            return {s[i : i + 2] for i in range(len(s) - 1)}

        ga, gb = grams(a), grams(b)
        if not ga or not gb:
            return 1.0
        inter = len(ga & gb)
        union = len(ga | gb)
        return inter / union if union else 1.0

    async def _maybe_auto_evolve(self) -> None:
        """检查是否够条件自动演化。由定时任务调用。"""
        if not self.conf.get("auto_evolve", True):
            return
        if self.store is None or not self.memory_db:
            return
        every = max(10, int(self.conf.get("evolve_every_atoms", 60) or 60))
        min_hours = max(1, int(self.conf.get("evolve_min_hours", 24) or 24))

        last_at = float(await self.store.get_state("last_evolve_at", "0") or 0)
        if last_at and (time.time() - last_at) < min_hours * 3600:
            return
        total = await self._count_atoms()
        prev_total = int(await self.store.get_state("last_total_atoms", "0") or 0)
        if total - prev_total < every:
            return
        await self._safe_evolve(f"新增 {total - prev_total} 条记忆")

    # ---------- 人格外显（QQ 签名同步） ----------

    def _qq_bot(self):
        """找 OneBot（aiocqhttp/napcat）适配器的 bot，拿到 call_action。找不到返回 None。"""
        try:
            insts = getattr(self.context.platform_manager, "platform_insts", []) or []
            for inst in insts:
                bot = getattr(inst, "bot", None)
                if bot is not None and hasattr(bot, "call_action"):
                    return bot
        except Exception:
            pass
        return None

    def _signature_for(self, p) -> str:
        """把人格压成一句能放 QQ 签名的话。"""
        if p is None:
            return ""
        head = str(p.narrative or "").strip().splitlines()
        line = " ".join((head[0] if head else "").split())
        if line:
            return line[:60]
        style = str(getattr(getattr(p, "voice", None), "sentence_style", "") or "")
        return " ".join(style.split())[:60]

    async def _apply_outer_sync(self, p) -> None:
        """人格演化后，把「现在的她是谁」同步到 QQ 个性签名。

        默认关：这层和 persona_plus（改昵称/头像）不是一个东西，别默认抢签名。
        """
        if not bool(self.conf.get("sync_signature_on_evolve", False)):
            return
        bot = self._qq_bot()
        if bot is None:
            return
        sig = self._signature_for(p)
        if not sig:
            return
        try:
            await bot.call_action("set_qq_profile", {"personal_note": sig})
            logger.info(f"[hlp] 已按第 {getattr(p, 'version', '?')} 版人格同步 QQ 签名：{sig[:40]}")
        except Exception as e:
            logger.debug(f"[hlp] 同步 QQ 签名失败: {e!r}")

    # ---------- 评价体系（好感 + 态度） ----------

    async def _name_to_qq(self) -> dict[str, str]:
        """people.db 的「昵称 → QQ」表。评价统一按 QQ 键，避免同名两套。"""
        pdb = self._find_people_db()
        if not pdb:
            return {}
        out: dict[str, str] = {}
        try:
            uri = f"file:{pdb.replace(os.sep, '/')}?mode=ro"
            async with aiosqlite.connect(uri, uri=True) as db:
                qq_by_pid: dict[str, str] = {}
                names: dict[str, list[str]] = {}
                cur = await db.execute("SELECT person_id, kind, value FROM person_tags")
                for r in await cur.fetchall():
                    pid, kind, val = str(r[0] or ""), str(r[1] or ""), str(r[2] or "").strip()
                    if not pid or not val:
                        continue
                    if kind == "qq":
                        qq_by_pid[pid] = val
                    elif kind == "nickname":
                        names.setdefault(pid, []).append(val)
                for pid, nm_list in names.items():
                    qq = qq_by_pid.get(pid, "")
                    if not qq:
                        continue
                    for nm in nm_list:
                        out.setdefault(nm, qq)
        except Exception as e:
            logger.debug(f"[hlp] 读名字→QQ 映射失败: {e!r}")
        return out

    async def _person_profile_text(self, qq: str, name: str, limit: int = 20) -> str:
        """从记忆里翻这个人说过/做过的事，当「价值观样本」。"""
        if not self.memory_db:
            return ""
        try:
            uri = f"file:{self.memory_db.replace(os.sep, '/')}?mode=ro"
            async with aiosqlite.connect(uri, uri=True) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT content FROM atoms WHERE (who_id = ? OR who_name = ?) "
                    "AND status IN ('active','candidate') "
                    "ORDER BY importance DESC, create_time DESC LIMIT ?",
                    (str(qq or ""), str(name or ""), max(5, limit)),
                )
                rows = await cur.fetchall()
            return "\n".join(f"- {r['content']}" for r in rows)
        except Exception:
            return ""

    def _bot_values_text(self) -> str:
        """铃光的价值观 / 底线，拿来跟别人比。"""
        p = self.current
        if p is None:
            return ""
        parts: list[str] = []
        try:
            vals = p.adaptations.values if p.adaptations else []
            if vals:
                parts.append("她在意：" + "、".join(vals))
            if p.core and p.core.boundaries:
                parts.append("她的底线：" + "；".join(p.core.boundaries[:4]))
            if p.core and p.core.existence_fear:
                parts.append("她怕：" + p.core.existence_fear)
            if p.schema and p.schema.self_schema:
                parts.append("她认定：" + "；".join(p.schema.self_schema[:2]))
        except Exception:
            pass
        return "\n".join(parts)

    async def _ensure_initial_rating(self, key: str, name: str) -> None:
        """第一次见一个人：按「他的价值观 vs 铃光的价值观」给一笔初始评价。

        契合度高 → 初始分直接给高（一大段）；契合度低 → 低。只做一次。
        """
        if self.store is None or not key:
            return
        if await self.store.get_rating(key) is not None:
            return
        if not bool(self.conf.get("initial_rating_by_values", True)):
            return
        try:
            profile = await self._person_profile_text(key, name)
            values = self._bot_values_text()
            provider = self.context.get_using_provider()
            if not profile or not values or provider is None:
                return
            _name = str(self.conf.get("bot_name") or "她").strip() or "她"
            prompt = (
                f"下面是「{_name}」的价值观、底线和在意的东西：\n"
                f"{values}\n\n"
                "下面是这个人说过/做过的事（从这些能看出他在意什么、是个什么人）：\n"
                f"{profile[:1800]}\n\n"
                f"判断：这个人的价值观和{_name}有多契合？0 到 1 之间。\n"
                "契合 = 尊重她的独立、看重答应的事、不把她当工具、愿意好好说话。\n"
                '只输出 JSON：{"align":0.0-1.0,"why":"一句话"}'
            )
            resp = await provider.text_chat(prompt=prompt, contexts=[])
            raw = str(getattr(resp, "completion_text", "") or "")
            m = re.search(r"\{.*\}", raw, re.S)
            obj = json.loads(m.group(0)) if m else {}
            align = max(0.0, min(1.0, float(obj.get("align") or 0.5)))
            # 契合度映射成初始分：0.5=50，1.0=80（增长一大段），0.0=20
            aff = round(50 + (align - 0.5) * 60, 1)
            att = round(50 + (align - 0.5) * 40, 1)
            await self.store.set_initial_rating(key, name, aff, att)
            logger.info(
                f"[hlp] 初始评价 {name or key}：契合 {align:.2f} → {aff}/{att}"
                + (f"（{obj.get('why')}）" if obj.get("why") else "")
            )
        except Exception as e:
            logger.debug(f"[hlp] 初始评价失败: {e!r}")

    async def _update_ratings(self) -> None:
        """扫 hlm 最近的 praise/scold 原子，更新两维评价。增量：按 atom id 只扫新的。"""
        if self.store is None or not self.memory_db:
            return
        if not bool(self.conf.get("rating_enabled", True)):
            return
        try:
            last = int(await self.store.get_state("last_rating_scan_id", "0") or 0)
            try:
                self._name_qq_map = await self._name_to_qq()
            except Exception:
                self._name_qq_map = {}
            uri = f"file:{self.memory_db.replace(os.sep, '/')}?mode=ro"
            async with aiosqlite.connect(uri, uri=True) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT id, nature, who_id, who_name, metadata FROM atoms "
                    "WHERE id > ? AND nature IN ('praise','scold') ORDER BY id ASC LIMIT 500",
                    (last,),
                )
                rows = await cur.fetchall()
            if not rows:
                return
            max_id = last
            for r in rows:
                who_id = str(r["who_id"] or "").strip()
                who_name = str(r["who_name"] or "").strip()
                # 优先 QQ 号（干净稳定）；没有 QQ 就查 people.db 把名字翻成 QQ，
                # 否则同一个人会按「名字」和「QQ」各存一套评价。
                if who_id and who_id.isdigit() and len(who_id) >= 5:
                    key, name = who_id, who_name
                elif who_name and 1 <= len(who_name) <= 8 and not re.search(r"[「」『』\"'（）()，。？、！\s]", who_name):
                    key, name = self._name_qq_map.get(who_name) or who_name, who_name
                else:
                    key = ""
                if not key:
                    max_id = max(max_id, int(r["id"]))
                    continue
                # 相关性权重：direct 全量，mention 半量，about 三成，unrelated/未知不动
                rel = ""
                try:
                    md = json.loads(r["metadata"] or "{}")
                    if isinstance(md, dict):
                        rel = str(md.get("relevance") or "")
                except Exception:
                    rel = ""
                w = {"direct": 1.0, "mention": 0.5, "about": 0.3}.get(rel, 0.0)
                if w <= 0:
                    max_id = max(max_id, int(r["id"]))
                    continue
                kind = "praise" if str(r["nature"]) == "praise" else "scold"
                # 第一次见这个人：先按价值观给一笔初始评价，再叠这次互动
                try:
                    await self._ensure_initial_rating(key, name)
                except Exception:
                    pass
                try:
                    await self.store.apply_interaction(key, kind, name=name, weight=w)
                except Exception as e:
                    logger.debug(f"[hlp] 更新「{name or key}」评价失败: {e!r}")
                max_id = max(max_id, int(r["id"]))
            await self.store.set_state("last_rating_scan_id", str(max_id))
        except Exception as e:
            logger.debug(f"[hlp] 更新评价失败: {e!r}")

    async def _drift_ratings(self) -> None:
        if self.store is None:
            return
        if not bool(self.conf.get("rating_enabled", True)):
            return
        try:
            days = float(self.conf.get("rating_drift_days", 30) or 30)
            n = await self.store.drift_ratings(days)
            if n:
                logger.info(f"[hlp] 评价漂移 {n} 人（长时间没联系）")
        except Exception as e:
            logger.debug(f"[hlp] 评价漂移失败: {e!r}")

    # ---------- 读记忆（只读，不写） ----------

    async def _load_atoms(self, limit: int) -> list[dict]:
        if not self.memory_db:
            return []
        try:
            uri = f"file:{self.memory_db.replace(os.sep, '/')}?mode=ro"
            async with aiosqlite.connect(uri, uri=True) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT id, content, layer, mtype, nature, nature_reason, evidence, "
                    "importance, who_name, person_ids, source, privacy, said_at "
                    "FROM atoms WHERE status IN ('active','candidate') "
                    "ORDER BY importance DESC, said_at DESC LIMIT ?",
                    (max(10, limit),),
                )
                return [dict(r) for r in await cur.fetchall()]
        except Exception as e:
            logger.warning(f"[hlp] 读记忆失败: {e!r}")
            return []

    async def _count_atoms(self) -> int:
        if not self.memory_db:
            return 0
        try:
            uri = f"file:{self.memory_db.replace(os.sep, '/')}?mode=ro"
            async with aiosqlite.connect(uri, uri=True) as db:
                cur = await db.execute("SELECT COUNT(*) FROM atoms")
                return int((await cur.fetchone())[0])
        except Exception:
            return 0

    async def _load_utterances(self, limit: int = 800) -> list[str]:
        """她真正说过的话——从 AstrBot 的消息历史读。

        为什么不用记忆库：记忆里 me_to_* 的原子是**叙述**（"噩壤问我…"），
        不是原话。拿它统计算出来的口头禅会是"噩壤让我"，完全不是她。
        """
        bot_qq = str(self.conf.get("bot_qq") or "").strip()
        if not bot_qq:
            # 没配就跳过说话风格统计——这一项只影响 voice 那几个字段，
            # 其他维度照常归纳，不能因为少个 QQ 号就整体不工作
            logger.info("[hlp] 没配 bot_qq，跳过说话风格统计")
            return []
        hist_db = self._find_history_db()
        if not hist_db:
            return []
        out: list[str] = []
        try:
            uri = f"file:{hist_db.replace(os.sep, '/')}?mode=ro"
            async with aiosqlite.connect(uri, uri=True) as db:
                cur = await db.execute(
                    "SELECT content FROM platform_message_history "
                    "WHERE sender_id = ? ORDER BY rowid DESC LIMIT ?",
                    (bot_qq, max(50, limit)),
                )
                for r in await cur.fetchall():
                    try:
                        arr = json.loads(r[0] or "[]")
                    except Exception:
                        continue
                    if not isinstance(arr, list):
                        continue
                    for seg in arr:
                        if isinstance(seg, dict) and seg.get("type") == "text":
                            t = str((seg.get("data") or {}).get("text") or "").strip()
                            if t:
                                out.append(t)
        except Exception as e:
            logger.debug(f"[hlp] 读消息历史失败: {e!r}")
        return out

    def _find_history_db(self) -> str | None:
        """AstrBot 主库（有 platform_message_history）。"""
        cands = []
        # data_v4.db 在 AstrBot 的 data/ 下，和自己的 plugin_data 同级
        try:
            cands.append(
                os.path.join(os.path.dirname(self._plugin_data_dir()), "data_v4.db")
            )
        except Exception:
            pass
        try:
            here = os.path.abspath(__file__)
            for _ in range(6):
                here = os.path.dirname(here)
                cands.append(os.path.join(here, "data", "data_v4.db"))
        except Exception:
            pass
        cands.append(os.path.join(os.getcwd(), "data", "data_v4.db"))
        for p in cands:
            if os.path.isfile(p):
                return p
        return None

    def _find_people_db(self) -> str | None:
        """human-like memory 的人库（有 relations.tone）。"""
        explicit = str(self.conf.get("people_db_path") or "").strip()
        if explicit and os.path.isfile(explicit):
            return explicit
        # 和 memory.db 同目录
        mdb = self._find_memory_db()
        if mdb:
            p = os.path.join(os.path.dirname(mdb), "people.db")
            if os.path.isfile(p):
                return p
        return None

    async def _load_people(self, limit: int = 40) -> list[dict]:
        """读人库：「我」对每个人是什么底色——依恋层级要按人判，得有这个。"""
        pdb = self._find_people_db()
        if not pdb:
            return []
        out: list[dict] = []
        try:
            uri = f"file:{pdb.replace(os.sep, '/')}?mode=ro"
            async with aiosqlite.connect(uri, uri=True) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT r.b_id, r.rel_type, r.tone, r.confidence, "
                    "  (SELECT value FROM person_tags t WHERE t.person_id = r.b_id "
                    "   AND t.kind IN ('real_name','nickname','alias') "
                    "   AND t.value NOT GLOB '[0-9]*' "
                    "   ORDER BY CASE t.kind WHEN 'real_name' THEN 0 "
                    "            WHEN 'nickname' THEN 1 ELSE 2 END LIMIT 1) AS name "
                    "FROM relations r WHERE r.a_id = 'me' AND r.to_ts IS NULL "
                    "ORDER BY r.updated_at DESC LIMIT ?",
                    (max(5, limit),),
                )
                for r in await cur.fetchall():
                    name = str(r["name"] or r["b_id"] or "").strip()
                    if not name:
                        continue
                    out.append(
                        {
                            "name": name,
                            "person_id": str(r["b_id"] or ""),
                            "rel_type": str(r["rel_type"] or ""),
                            "tone": str(r["tone"] or ""),
                            "cooccur": 0,
                        }
                    )
        except Exception as e:
            logger.debug(f"[hlp] 读人库失败: {e!r}")
        return out

    async def _count_cooccur(self) -> dict[tuple[str, str], int]:
        """统计两个人同框出现几次——依恋层级光看底色不够，还得看互动频次。"""
        if not self.memory_db:
            return {}
        out: dict[tuple[str, str], int] = {}
        try:
            uri = f"file:{self.memory_db.replace(os.sep, '/')}?mode=ro"
            async with aiosqlite.connect(uri, uri=True) as db:
                cur = await db.execute(
                    "SELECT person_ids FROM atoms WHERE layer='other_to_other' "
                    "AND COALESCE(person_ids,'[]') != '[]'"
                )
                for r in await cur.fetchall():
                    try:
                        pids = json.loads(r[0] or "[]")
                    except Exception:
                        continue
                    uniq = sorted({str(p) for p in pids if p and str(p) != "me"})
                    for i in range(len(uniq)):
                        for j in range(i + 1, len(uniq)):
                            key = (uniq[i], uniq[j])
                            out[key] = out.get(key, 0) + 1
        except Exception as e:
            logger.debug(f"[hlp] 同框统计失败: {e!r}")
        return out

    def _format_people(self, people: list[dict]) -> str:
        if not people:
            return "（人库没读到，依恋层级只能按记忆里的名字推）"
        rows = []
        for p in people:
            tone = f" · 我对他的底色：{p['tone']}" if p.get("tone") else ""
            rt = f"（{p['rel_type']}）" if p.get("rel_type") else ""
            co = int(p.get("cooccur") or 0)
            co_s = f" · 同框 {co} 次" if co >= 3 else ""
            rows.append(f"- {p['name']}{rt}{tone}{co_s}")
        return "\n".join(rows)

    def _format_atoms(self, atoms: list[dict]) -> str:
        rows = []
        for a in atoms[: int(self.conf.get("source_atoms", 120) or 120)]:
            c = str(a.get("content") or "").strip()
            if not c:
                continue
            who = str(a.get("who_name") or "").strip()
            nature = str(a.get("nature") or "").strip()
            reason = str(a.get("nature_reason") or "").strip()
            ev = str(a.get("evidence") or "").strip()
            imp = float(a.get("importance") or 0)
            ev_tag = f"{ev}·" if ev else ""
            tag = f"[{ev_tag}{nature}|{imp:.1f}]" if nature else f"[{ev_tag}{imp:.1f}]"
            # 带上 memory 已经判过的依据——模型编不出这个
            why = f"（判据：{reason[:40]}）" if reason else ""
            rows.append(f"- {tag} {who + '：' if who else ''}{c[:160]}{why}")
        return "\n".join(rows) or "（没有可用的记忆）"

    def _provider(self):
        pid = str(self.conf.get("provider_id") or "").strip()
        try:
            if pid:
                mgr = getattr(self.context, "provider_manager", None)
                if mgr is not None:
                    for p in getattr(mgr, "providers", []) or []:
                        if getattr(p, "provider_id", "") == pid or getattr(p, "id", "") == pid:
                            return p
            return self.context.get_using_provider()
        except Exception:
            return None

    # ---------- 工具 ----------

    @filter.llm_tool(name="who_am_i")
    async def who_am_i(self, event: AstrMessageEvent):
        '''主人问「你是谁 / 你觉得自己是什么样的人 / 你最近有什么变化」时用。

        返回你对自己的人格认知（特质、在意的东西、说话方式、自我叙事）。
        '''
        p = self.current
        if p is None and self.store is not None:
            p = await self.store.latest()
            self.current = p
        return build_self_report(p)

    @filter.llm_tool(name="reflect_on_myself")
    async def reflect_on_myself(self, event: AstrMessageEvent, hint: str = ""):
        '''主动反思一次，可能让你的人格画像更新。主人说「你想想自己」时用。

        Args:
            hint(string): 想反思的方向（比如「最近是不是太黏人了」），可空
        '''
        if self.store is None:
            return "人格库还没准备好。"
        v = await self._safe_evolve(reason=(hint or "主动反思")[:80])
        if v is None:
            return "想了想，还是原来那样——没有足够的新经历让我变。"
        p = self.current
        return f"想过了。现在是第 {v} 版：{build_self_report(p)[:400]}"

    # ---------- 命令 ----------

    @filter.command_group("人格")
    def persona_cmd(self):
        pass

    @filter.command("status", alias={"状态"})
    async def cmd_status(self, event: AstrMessageEvent):
        p = self.current or (await self.store.latest() if self.store else None)
        if p is None:
            yield event.plain_result("还没有人格画像。说 /人格 evolve 让我想一次。")
            return
        lines = [f"人格 第 {p.version} 版（基于 {p.total_atoms} 条记忆）", ""]
        t = p.traits
        lines.append(
            f"开放 {t.openness} / 尽责 {t.conscientiousness} / 外向 {t.extraversion} / "
            f"宜人 {t.agreeableness} / 神经质 {t.neuroticism}"
        )
        if p.adaptations.values:
            lines.append("在意：" + "、".join(p.adaptations.values))
        if p.adaptations.attachment_style:
            lines.append("依恋：" + p.adaptations.attachment_style)
        if p.voice.sentence_style:
            lines.append("说话：" + p.voice.sentence_style)
        if p.narrative:
            lines.append("")
            lines.append(p.narrative)
        yield event.plain_result("\n".join(lines))

    @filter.command("evolve", alias={"演化", "想一次"})
    async def cmd_evolve(self, event: AstrMessageEvent):
        yield event.plain_result("让我想想我是谁…")
        v = await self._safe_evolve("主人让我想一次")
        if v is None:
            yield event.plain_result("想过了，但没变——证据还不够。")
            return
        yield event.plain_result(f"想完了，现在是第 {v} 版。用 /人格 status 看。")

    @filter.command("why", alias={"为什么", "原因"})
    async def cmd_why(self, event: AstrMessageEvent, version: int = 0):
        '''查「我是怎么变成现在这样的」——每个维度变了什么、被哪几条记忆推的。'''
        if self.store is None:
            yield event.plain_result("人格库还没准备好。")
            return
        evs = await self.store.events(limit=40)
        if not evs:
            yield event.plain_result("还没有变化记录（只有第 1 版时不产生变化）。")
            return
        want = int(version or 0)
        rows = [e for e in evs if not want or int(e["version"]) == want]
        if not rows:
            yield event.plain_result(f"第 {want} 版没有变化记录。")
            return
        lines = ["我是怎么变的："]
        for e in rows[:12]:
            v = e["version"]
            dim = e["dimension"]
            lines.append(f"\nv{v} · {dim}")
            lines.append(f"  从：{str(e['old_value'])[:60]}")
            lines.append(f"  到：{str(e['new_value'])[:60]}")
            try:
                ids = json.loads(e.get("trigger_atom_ids") or "[]")
            except Exception:
                ids = []
            if ids:
                lines.append(f"  推我的记忆：{ids[:8]}")
        yield event.plain_result("\n".join(lines))

    @filter.command("history", alias={"历史"})
    async def cmd_history(self, event: AstrMessageEvent):
        if self.store is None:
            yield event.plain_result("人格库还没准备好。")
            return
        rows = await self.store.history(limit=12)
        if not rows:
            yield event.plain_result("还没有历史版本。")
            return
        lines = ["人格变化历史："]
        for r in rows:
            ts = time.strftime("%m-%d %H:%M", time.localtime(float(r["created_at"] or 0)))
            lines.append(
                f"  v{r['version']} · {ts} · {r['total_atoms']} 条记忆 · {r['reason'] or '—'}"
            )
        yield event.plain_result("\n".join(lines))


    @filter.command("统一", alias={"unify", "unified"})
    async def cmd_unify(self, event: AstrMessageEvent):
        '''报一遍三层（设定/成长/心情）各自现在是什么、有没有打架的地方。'''
        p = self.current or (await self.store.latest() if self.store else None)

        static = await self._read_static_persona(event)
        set_block = static[:500] if static else "（没读到静态人设）"
        grow_block = self._growth_summary(p)
        mood_block = self._mood_status()

        head = (
            "【设定层（人设里写的，你是谁）】\n" + set_block + "\n\n"
            "【成长层（从记忆里长出来的）】\n" + grow_block + "\n\n"
            "【心情层】\n" + mood_block
        )
        yield event.plain_result(head)
        yield event.plain_result("让我看看这几层有没有打架…")
        verdict = await self._detect_conflicts(static, grow_block)
        yield event.plain_result("【打架检测】\n" + verdict)

    # ---------- 统一命令的辅助 ----------

    async def _read_static_persona(self, event: AstrMessageEvent) -> str:
        """读静态人设（AstrBot 的 persona 层）。"""
        try:
            pm = getattr(self.context, "persona_manager", None)
            if pm is None:
                return ""
            persona = await pm.get_default_persona_v3(str(event.unified_msg_origin or ""))
            if isinstance(persona, dict):
                return str(persona.get("prompt") or "")
            return str(getattr(persona, "prompt", "") or "")
        except Exception as e:
            logger.debug(f"[hlp] 读静态人设失败: {e!r}")
            return ""

    def _mood_status(self) -> str:
        """心情层状态。fish_heart 关着就不去读它内部状态。"""
        try:
            meta = self.context.get_registered_star("astrbot_plugin_fish_heart")
            on = bool(meta and getattr(meta, "activated", False))
        except Exception:
            on = False
        return "已启用（用 /心 status 看此刻心情）" if on else "未启用（fish_heart 关着）"

    def _growth_summary(self, p) -> str:
        if p is None:
            return "还没有人格画像。"
        t = p.traits
        lines = [
            f"第 {p.version} 版（基于 {p.total_atoms} 条记忆）",
            f"大五：开放 {t.openness} / 尽责 {t.conscientiousness} / 外向 {t.extraversion} "
            f"/ 宜人 {t.agreeableness} / 神经质 {t.neuroticism}",
        ]
        if p.voice.sentence_style:
            lines.append("说话：" + p.voice.sentence_style)
        if p.adaptations.values:
            lines.append("在意：" + "、".join(p.adaptations.values[:3]))
        if p.narrative:
            lines.append("叙事：" + p.narrative[:120])
        return "\n".join(lines)

    async def _detect_conflicts(self, static: str, growth: str) -> str:
        if not static:
            return "没读到设定层，先确认人设里有内容。"
        if not growth or growth == "还没有人格画像。":
            return "成长层还没长出来，先 /人格 evolve 归纳一次。"
        provider = self._provider()
        if provider is None:
            return "没接上对话模型，没法判断。"
        try:
            resp = await provider.text_chat(
                prompt=(
                    "下面是一个 AI 角色的两层人格，判断它们有没有「打架」（性格上互相矛盾）的地方。\n\n"
                    "【设定层（人设里写的，她「应该」是什么样）】\n" + static[:1500] + "\n\n"
                    "【成长层（从记忆里长出来的，她「实际」是什么样）】\n" + growth + "\n\n"
                    "只找真正矛盾的性格点（比如人设说「话多」但成长层外向很低；"
                    "人设说「爱怼人」但宜人很高）。没明显矛盾就说没明显矛盾。"
                    "用两三句话说人话，别列 JSON，别复述全文。"
                ),
                contexts=[],
            )
            text = str(getattr(resp, "completion_text", "") or "").strip()
            return text or "（没想出来）"
        except Exception as e:
            logger.debug(f"[hlp] 打架检测失败: {e!r}")
            return "打架检测没做成。"


    @filter.command("评价", alias={"rating"})
    async def cmd_rating(self, event: AstrMessageEvent, name: str = ""):
        '''看「我」对某人的两维评价（好感 / 态度）。不带名字就列全部。'''
        if self.store is None:
            yield event.plain_result("人格库还没准备好。")
            return
        name = str(name or "").strip()
        if name:
            r = await self.store.find_rating(name)
            if not r:
                yield event.plain_result(f"还没对「{name}」形成评价。")
                return
            disp = str(r.get("name") or r.get("person_key") or name)
            lines = [f"「{disp}」的评价：", f"好感 {int(r['affection'])} / 态度 {int(r['attitude'])}"]
            note = str(r.get("note") or "").strip()
            if note:
                lines.append("备注：" + note)
            yield event.plain_result("\n".join(lines))
            return
        rows = await self.store.all_ratings()
        if not rows:
            yield event.plain_result("还没对任何人形成评价。")
            return
        lines = ["我对这些人的评价："]
        for r in rows[:15]:
            disp = str(r.get("name") or r.get("person_key") or "?")
            lines.append(f"  {disp}：好感 {int(r['affection'])} / 态度 {int(r['attitude'])}")
        yield event.plain_result("\n".join(lines))


__all__ = ["HumanLikePersonality"]
