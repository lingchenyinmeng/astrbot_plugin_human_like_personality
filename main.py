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
import random
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
from .core import adapt as adapt_mod
from .core import decision as decision_mod
from .core import events as events_mod
from .core import mood as mood_mod
from .core import scripts as scripts_mod
from .core import style as style_mod

PLUGIN_NAME = "astrbot_plugin_human_like_personality"
MEMORY_PLUGIN = "astrbot_plugin_human_like_memory"


@register(PLUGIN_NAME, "噩壤", "人格不是写死的提示词，是从记忆里长出来的", "1.0.0")
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
        # 发言主动权：每个会话的连发计数与上次她回话的时间
        self._streak: dict[str, dict] = {}
        # 当前情绪缓存（2 分钟内复用，避免每条消息都查库）
        self._mood_cache: dict | None = None
        self._mood_cache_ts = 0.0
        # 群聊主动开口的冷却（每个群上次她插话的时间）
        self._group_last_speak: dict[str, float] = {}

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

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def _track_message(self, event: AstrMessageEvent) -> None:
        """数「对方连着发了几条」——主动权要用。"""
        try:
            if not self.conf.get("enabled", True):
                return
            if not bool(self.conf.get("initiative_enabled", True)):
                return
            sid = event.unified_msg_origin or event.session_id or ""
            if not sid:
                return
            now = time.time()
            st = self._streak.setdefault(sid, {"count": 0, "last_ts": 0.0, "replied_at": 0.0})
            # 超过 3 分钟没动静，重新计数
            if now - float(st.get("last_ts") or 0) > 180:
                st["count"] = 0
            st["count"] = int(st.get("count") or 0) + 1
            st["last_ts"] = now
            event.set_extra("hlp_streak", st["count"])
        except Exception as e:
            logger.debug(f"[hlp] 连发计数失败: {e!r}")

    def _addressed_to_other(self, event: AstrMessageEvent) -> bool:
        """这句话是不是冲着群里别人说的：@了别人 / 引用回复 / 点名别人。"""
        try:
            bot_qq = str(self.conf.get("bot_qq") or "").strip()
            for comp in event.get_messages() or []:
                t = str(getattr(comp, "type", "") or "")
                data = getattr(comp, "data", {}) or {}
                if t == "at":
                    qq = str(data.get("qq") or "").strip()
                    if qq and bot_qq and qq != bot_qq:
                        return True
                if t == "reply":
                    return True
        except Exception:
            pass
        return False

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=9)
    async def _group_initiative(self, event: AstrMessageEvent):
        """群里没点她，也可能自己接一句——但先分清「是不是对她说的」。"""
        try:
            if not self.conf.get("enabled", True):
                return
            if not bool(self.conf.get("group_initiative_enabled", False)):
                return
            if event.is_at_or_wake_command:
                return
            gid = event.get_group_id() or event.session_id or ""
            now = time.time()
            cd = float(self.conf.get("group_initiative_cooldown", 600) or 600)
            if now - float(self._group_last_speak.get(gid, 0.0)) < cd:
                return
            text = event.get_message_str() or ""
            if len(text) < 6:
                return

            bot_name = str(self.conf.get("bot_name") or "").strip()
            aliases = [x.strip() for x in str(self.conf.get("bot_aliases") or "").split(",") if x.strip()]
            names = [bot_name] + aliases if bot_name else aliases
            called_me = any(n and n in text for n in names)

            # 冲别人说的：@别人 / 引用回复 / 点名别人 —— 除非叫了她名字，否则不插嘴
            if not called_me and self._addressed_to_other(event):
                return

            mood = await self._current_mood()
            v = float((mood or {}).get("valence") or 0)
            if v < -0.2:
                return
            chance = float(self.conf.get("group_initiative_chance", 0.15) or 0.15)
            score = chance + 0.10 * v
            if random.random() > max(0.02, score):
                return

            provider = self._provider()
            if provider is None:
                return
            speaker = str(event.get_sender_name() or "").strip() or "群友"

            # 先让模型判断「该不该接」——它知道这句不是对她说的
            decide_prompt = (
                "你在一个群聊里，是群成员之一，不是客服。\n"
                f"群友「{speaker}」刚说：「{text}」\n"
                "这句话不是对你说的，你只是在旁边看着。你要不要自然接一句？\n"
                "只有你确实有话想说、或这句其实跟你有关、或你能帮上忙时才接；"
                "没话说就别硬接。\n"
                "只回答两个字：接 或 不接。"
            )
            resp = await provider.text_chat(prompt=decide_prompt, contexts=[])
            ans = str(getattr(resp, "completion_text", "") or "").strip()
            if not ans or ans.startswith("不") or "不接" in ans:
                return

            self._group_last_speak[gid] = now
            logger.info(f"[hlp] 群里主动接一句（{gid}，{speaker}）")
            yield event.request_llm(
                prompt=(
                    f"（群聊里，「{speaker}」刚说：「{text}」。"
                    "这句本来不是对你说的，你在群里可以自然接一句，也可以只回一个表情；"
                    "就当平常聊天，别自我介绍、别总结、别说教。）"
                ),
                session_id=event.session_id or gid,
            )
        except Exception as e:
            logger.debug(f"[hlp] 群主动开口失败: {e!r}")

    @filter.on_llm_request(priority=40)
    async def _decide_reply(self, event: AstrMessageEvent, request) -> None:
        """主动权：她可以选择不回。主人、被点名、被提问、连发过多 → 必须回。"""
        try:
            if not self.conf.get("enabled", True):
                return
            if not bool(self.conf.get("initiative_enabled", True)):
                return
            sid = event.unified_msg_origin or event.session_id or ""
            st = self._streak.get(sid, {})
            text = event.get_message_str() or ""
            mood = await self._current_mood()
            owner_qq = str(self.conf.get("owner_qq") or "").strip()
            bot_name = str(self.conf.get("bot_name") or "").strip()
            sender_id = str(event.get_sender_id() or "")
            is_owner = bool(owner_qq) and sender_id == owner_qq
            is_question = ("?" in text or "？" in text or text.rstrip().endswith(("吗", "呢", "么", "嘛")))
            recently = (time.time() - float(st.get("replied_at") or 0)) < 60
            d = decision_mod.decide(
                is_private=event.is_private_chat(),
                is_owner=is_owner,
                is_mention=bool(event.is_wake_up()),
                is_question=is_question,
                has_name=bool(bot_name) and bot_name in text,
                text=text,
                mood=mood,
                streak=int(st.get("count") or 0),
                recently_replied=recently,
                threshold=float(self.conf.get("initiative_threshold", 0.45) or 0.45),
                force_streak=int(self.conf.get("streak_force_reply", 3) or 3),
            )
            event.set_extra("hlp_decision", d)
            if d.get("action") == "skip":
                logger.info(f"[hlp] 这次不回（{d.get('why')}，score={d.get('score')}）")
                event.stop_event()
                return
            event.set_extra("hlp_self_express", bool(d.get("self_express")))
            if sid:
                st = self._streak.setdefault(sid, {"count": 0, "last_ts": 0.0, "replied_at": 0.0})
                st["count"] = 0
                st["replied_at"] = time.time()
        except Exception as e:
            logger.debug(f"[hlp] 发言决策失败: {e!r}")

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
            extra = await self._dynamic_hints(event, p)
            if extra:
                block = block + "\n\n" + extra
            sp = getattr(request, "system_prompt", "") or ""
            if block in sp:
                return
            setattr(request, "system_prompt", (sp.rstrip() + "\n\n" + block).strip())
        except Exception as e:
            logger.debug(f"[hlp] 注入失败: {e!r}")

    # ---------- 动态层：心情 / 表达风格 / 情境 / 主动权 ----------

    async def _current_mood(self) -> dict | None:
        """最近 72 小时的热记忆 → 此刻心情。缓存 2 分钟。"""
        if not bool(self.conf.get("mood_enabled", True)):
            return None
        now = time.time()
        if self._mood_cache is not None and (now - self._mood_cache_ts) < 120:
            return self._mood_cache
        try:
            atoms = await self._recent_emotional_atoms()
            m = mood_mod.compute_mood(atoms, now=now)
            self._mood_cache = m
            self._mood_cache_ts = now
            return m
        except Exception as e:
            logger.debug(f"[hlp] 算心情失败: {e!r}")
            return None

    async def _recent_emotional_atoms(self, limit: int = 60) -> list[dict]:
        if not self.memory_db:
            return []
        try:
            uri = f"file:{self.memory_db.replace(os.sep, '/')}?mode=ro"
            async with aiosqlite.connect(uri, uri=True) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT id, valence, arousal, importance, said_at, create_time FROM atoms "
                    "WHERE valence IS NOT NULL AND status IN ('active','candidate') "
                    "AND COALESCE(said_at, create_time) >= ? "
                    "ORDER BY COALESCE(said_at, create_time) DESC LIMIT ?",
                    (time.time() - 72 * 3600, max(10, limit)),
                )
                return [dict(r) for r in await cur.fetchall()]
        except Exception:
            return []

    async def _style_for(self, event: AstrMessageEvent, p) -> dict | None:
        """当前对话对象的表达风格。"""
        try:
            sender = str(event.get_sender_id() or "")
            sender_name = str(event.get_sender_name() or "")
            aff = 50.0
            att = 50.0
            if sender and self.store is not None:
                r = await self.store.find_rating(sender) or await self.store.find_rating(sender_name)
                if r:
                    aff = float(r.get("affection") or 50)
                    att = float(r.get("attitude") or 50)
            t = p.traits if p else None
            attachment = ""
            if p is not None:
                attachment = str(p.adaptations.attachment_default or "")
                try:
                    levels = p.adaptations.attachment_levels or {}
                    for k in (sender, sender_name):
                        if k in levels:
                            attachment = str(levels[k])
                            break
                except Exception:
                    pass
            mood = await self._current_mood()
            return style_mod.compute_style(
                extraversion=t.extraversion if t else 50,
                neuroticism=t.neuroticism if t else 50,
                agreeableness=t.agreeableness if t else 50,
                conscientiousness=t.conscientiousness if t else 50,
                attachment=attachment,
                affection=aff,
                attitude=att,
                mood=mood,
            )
        except Exception as e:
            logger.debug(f"[hlp] 算表达风格失败: {e!r}")
            return None

    async def _dynamic_hints(self, event: AstrMessageEvent, p) -> str:
        """把「此刻心情 / 表达倾向 / 情境反应 / 自我表达 / 学到的说话方式」拼成一段。"""
        out: list[str] = []
        try:
            mood = await self._current_mood()
            mh = mood_mod.mood_hint(mood)
            if mh:
                out.append(mh)

            style = await self._style_for(event, p)
            if style:
                sh = style_mod.style_hint(style, to_owner=False)
                if sh:
                    out.append(sh)

            # 情境反应
            if bool(self.conf.get("script_hint_enabled", True)) and p is not None:
                text = event.get_message_str() or ""
                situation = scripts_mod.detect_situation(text)
                if situation:
                    picked = scripts_mod.pick_scripts(
                        extraversion=p.traits.extraversion,
                        neuroticism=p.traits.neuroticism,
                        agreeableness=p.traits.agreeableness,
                        conscientiousness=p.traits.conscientiousness,
                        attachment=str(p.adaptations.attachment_default or ""),
                        situation=situation,
                    )
                    if picked:
                        out.append("遇到这类事你大概是：" + "".join(picked))

            # 被连发逼到要回一句：说明自己的情绪和需求
            if event.get_extra("hlp_self_express"):
                out.append(decision_mod.self_express_hint(mood, bot_name=str(self.conf.get("bot_name") or "")))

            # 言语趋同：跟这个人说话时，稍微像一点
            if bool(self.conf.get("adapt_enabled", True)) and self.store is not None:
                sender = str(event.get_sender_id() or "")
                if sender:
                    ad = await self.store.get_adapt(sender)
                    if ad:
                        try:
                            vec = json.loads(ad.get("vector") or "{}")
                        except Exception:
                            vec = {}
                        if vec and float(ad.get("intimacy") or 0) >= 0.6:
                            out.append(
                                "跟这个人说话，你会不自觉带一点他的习惯："
                                f"句子大概 {vec.get('avg_len', 0)} 字，"
                                f"短句占 {int(float(vec.get('short_rate') or 0) * 100)}%，"
                                f"问句占 {int(float(vec.get('question_rate') or 0) * 100)}%，"
                                f"爱用语气词 {'是' if float(vec.get('particle_rate') or 0) >= 0.3 else '不多'}。"
                                "别刻意模仿到丢了自己的口头禅。"
                            )
        except Exception as e:
            logger.debug(f"[hlp] 拼动态提示失败: {e!r}")
        return "\n".join(x for x in out if x)

    @filter.on_llm_response()
    async def _rewrite_response(self, event: AstrMessageEvent, response) -> None:
        """表达风格二次改写：只在风格明显时，把已经写好的回复再改一遍。"""
        if not self.conf.get("enabled", True):
            return
        if not bool(self.conf.get("style_rewrite_enabled", True)):
            return
        try:
            text = str(getattr(response, "completion_text", "") or "").strip()
            if not text or len(text) > 300:
                return
            p = self.current
            style = await self._style_for(event, p)
            mood = await self._current_mood()
            if not style_mod.needs_rewrite(style, mood):
                return
            provider = self._provider()
            if provider is None:
                return
            prompt = style_mod.build_rewrite_prompt(
                text,
                style=style or {},
                mood=mood,
                bot_name=str(self.conf.get("bot_name") or ""),
                context=event.get_message_str() or "",
            )
            resp = await provider.text_chat(prompt=prompt, contexts=[])
            new = str(getattr(resp, "completion_text", "") or "").strip()
            if not new:
                return
            new = new.strip("\"'“”「」").strip()
            if len(new) < 2:
                return
            response.completion_text = new
            logger.debug(f"[hlp] 风格改写（{style.get('label')}）：{text[:30]} → {new[:30]}")
        except Exception as e:
            logger.debug(f"[hlp] 风格改写失败: {e!r}")

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
                await self._update_adapt()
                await self._scan_events()
                await self._maybe_promote_shadow()
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

        # ---------- 事件驱动：把最近发生的事并进性格（小步、有上限） ----------
        try:
            raw_n = json.loads(await self.store.get_state("pending_nudges", "{}") or "{}")
            if raw_n:
                from .core.model import BigFive

                p.traits = BigFive(
                    **events_mod.apply_nudges(p.traits.to_dict(), raw_n)
                )
                await self.store.set_state("pending_nudges", "{}")
                logger.info(f"[hlp] 事件偏移并入：{raw_n}")
        except Exception as e:
            logger.debug(f"[hlp] 并入事件偏移失败: {e!r}")

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
        # 影子模式：新版本先不生效，注入继续用旧版，等时间到或手动转正
        if bool(self.conf.get("shadow_mode", False)):
            prev_v = prev.version if prev else 0
            await self.store.set_state("shadow_version", str(version))
            await self.store.set_state("shadow_since", str(time.time()))
            self.current = prev if prev is not None else await self.store.latest()
            logger.info(
                f"[hlp] 影子版 v{version} 已生成，暂不生效（仍用 v{prev_v}）"
            )
        else:
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

    async def _texts_by_sender(self, sender_id: str, limit: int = 200) -> list[str]:
        """某个人说过的话（用于言语趋同）。"""
        hist_db = self._find_history_db()
        if not hist_db or not sender_id:
            return []
        out: list[str] = []
        try:
            uri = f"file:{hist_db.replace(os.sep, '/')}?mode=ro"
            async with aiosqlite.connect(uri, uri=True) as db:
                cur = await db.execute(
                    "SELECT content FROM platform_message_history WHERE sender_id = ? "
                    "ORDER BY rowid DESC LIMIT ?",
                    (str(sender_id), max(20, limit)),
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
            logger.debug(f"[hlp] 读某人消息失败: {e!r}")
        return out

    async def _update_adapt(self) -> None:
        """言语趋同：只学主人 + 评价与态度双高的人；AI 不学。"""
        if self.store is None:
            return
        if not bool(self.conf.get("adapt_enabled", True)):
            return
        try:
            last = float(await self.store.get_state("last_adapt_at", "0") or 0)
            if last and (time.time() - last) < 7 * 86400:
                return
            my_texts = await self._load_utterances(300)
            mine = adapt_mod.style_vector(my_texts)
            if not mine:
                return
            owner_qq = str(self.conf.get("owner_qq") or "").strip()
            ai_qqs = {x.strip() for x in str(self.conf.get("ai_qqs") or "").split(",") if x.strip()}
            ai_names = {x.strip() for x in str(self.conf.get("ai_names") or "").split(",") if x.strip()}
            ratings = await self.store.all_ratings()
            n = 0
            for r in ratings:
                key = str(r.get("person_key") or "")
                name = str(r.get("name") or "")
                if not key:
                    continue
                if adapt_mod.is_ai(key, name, ai_qqs, ai_names):
                    continue
                aff = float(r.get("affection") or 50)
                att = float(r.get("attitude") or 50)
                is_owner = bool(owner_qq) and key == owner_qq
                if not is_owner and not (aff >= 65 and att >= 60):
                    continue
                theirs = adapt_mod.style_vector(await self._texts_by_sender(key, 200))
                if not theirs:
                    continue
                intimacy = adapt_mod.intimacy_of(aff, att)
                existing = await self.store.get_adapt(key)
                base = mine
                if existing:
                    try:
                        base = json.loads(existing.get("vector") or "{}") or mine
                    except Exception:
                        base = mine
                new = adapt_mod.converge(base, theirs, intimacy=intimacy)
                await self.store.save_adapt(key, name, new, intimacy)
                n += 1
            await self.store.set_state("last_adapt_at", str(time.time()))
            if n:
                logger.info(f"[hlp] 言语趋同更新 {n} 人")
        except Exception as e:
            logger.debug(f"[hlp] 言语趋同失败: {e!r}")

    async def _scan_events(self) -> None:
        """扫新记忆里的事件（吵架/和好/关心/被夸/承诺），累积成性格偏移。"""
        if not self.memory_db or self.store is None:
            return
        if not bool(self.conf.get("event_drive_enabled", True)):
            return
        try:
            last = int(await self.store.get_state("last_event_scan_id", "0") or 0)
            uri = f"file:{self.memory_db.replace(os.sep, '/')}?mode=ro"
            # 第一次扫描：只把游标推到当前最大，不把历史事件一次性算进来
            if last <= 0:
                async with aiosqlite.connect(uri, uri=True) as db:
                    cur = await db.execute("SELECT MAX(id) FROM atoms")
                    row = await cur.fetchone()
                max_id = int((row or [0])[0] or 0)
                await self.store.set_state("last_event_scan_id", str(max_id))
                logger.info(f"[hlp] 事件扫描从当前最大 id {max_id} 开始，不回溯历史")
                return
            async with aiosqlite.connect(uri, uri=True) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT id, content, nature FROM atoms WHERE id > ? "
                    "AND status IN ('active','candidate') ORDER BY id ASC LIMIT 500",
                    (last,),
                )
                rows = await cur.fetchall()
            if not rows:
                return
            try:
                acc = json.loads(await self.store.get_state("pending_nudges", "{}") or "{}")
            except Exception:
                acc = {}
            n = 0
            for r in rows:
                kind = events_mod.detect_event(str(r["content"] or ""), str(r["nature"] or ""))
                if kind:
                    acc = events_mod.merge_nudges(acc, events_mod.nudges_for(kind))
                    n += 1
            if n:
                await self.store.set_state("pending_nudges", json.dumps(acc, ensure_ascii=False))
            await self.store.set_state(
                "last_event_scan_id", str(max(int(r["id"]) for r in rows))
            )
            if n:
                logger.info(f"[hlp] 记下 {n} 个事件，性格偏移待并入：{acc}")
        except Exception as e:
            logger.debug(f"[hlp] 事件扫描失败: {e!r}")

    async def _maybe_promote_shadow(self) -> None:
        """影子模式：够时间就转正。"""
        if self.store is None:
            return
        try:
            sv = int(await self.store.get_state("shadow_version", "0") or 0)
            if not sv:
                return
            since = float(await self.store.get_state("shadow_since", "0") or 0)
            hours = float(self.conf.get("shadow_hours", 48) or 48)
            if since and (time.time() - since) < hours * 3600:
                return
            await self._promote_shadow()
        except Exception as e:
            logger.debug(f"[hlp] 影子转正检查失败: {e!r}")

    async def _promote_shadow(self) -> int | None:
        if self.store is None:
            return None
        sv = int(await self.store.get_state("shadow_version", "0") or 0)
        if not sv:
            return None
        p = await self.store.get_version(sv)
        if p is None:
            return None
        self.current = p
        await self.store.set_state("shadow_version", "")
        await self.store.set_state("shadow_since", "")
        logger.info(f"[hlp] 影子版 v{sv} 转正")
        return sv

    async def _restore_version(self, version: int) -> int | None:
        """回滚：把某一版的内容重新存成最新版（可追溯，不删历史）。"""
        if self.store is None:
            return None
        old = await self.store.get_version(int(version))
        if old is None:
            return None
        new_v = await self.store.save(
            old,
            reason=f"回滚到 v{version}",
            changed_by="rollback",
        )
        self.current = await self.store.latest()
        return new_v

    async def _change_summary(self, limit: int = 8) -> str:
        """最近的变化摘要（给 /人格 摘要 用）。"""
        if self.store is None:
            return "人格库还没准备好。"
        evs = await self.store.events(limit=limit)
        if not evs:
            return "还没有变化记录。"
        lines = ["最近变的："]
        for e in evs[:limit]:
            lines.append(
                f"v{e['version']} · {e['dimension']}：{str(e['old_value'])[:40]} → {str(e['new_value'])[:40]}"
            )
        return "\n".join(lines)

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


    @filter.command("摘要", alias={"summary"})
    async def cmd_summary(self, event: AstrMessageEvent):
        """最近这几版她变了什么。"""
        yield event.plain_result(await self._change_summary())

    @filter.command("回滚", alias={"rollback"})
    async def cmd_rollback(self, event: AstrMessageEvent, version: int = 0):
        """回滚到某一版（存成新的最新版，历史不丢）。"""
        if not version:
            yield event.plain_result("用法：/人格 回滚 版本号（用 /人格 history 看）")
            return
        v = await self._restore_version(int(version))
        if v is None:
            yield event.plain_result(f"第 {version} 版不存在。")
            return
        yield event.plain_result(f"已回滚到 v{version}，现在是 v{v}。")

    @filter.command("转正", alias={"promote"})
    async def cmd_promote(self, event: AstrMessageEvent):
        """把影子版转正。"""
        v = await self._promote_shadow()
        if v is None:
            yield event.plain_result("没有待转正的影子版。")
            return
        yield event.plain_result(f"影子版 v{v} 已转正。")

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
