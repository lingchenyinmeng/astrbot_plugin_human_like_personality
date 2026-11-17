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
import time
from typing import Any

import aiosqlite

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.analyzer import analyze_voice, behavior_signals
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

    def _plugin_data_dir(self) -> str:
        try:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            data = os.path.join(base, "data", "plugin_data", PLUGIN_NAME)
            if os.path.isdir(os.path.join(base, "data", "plugin_data")):
                return data
        except Exception:
            pass
        # 兜底：AstrBot 默认位置
        return os.path.join(
            "A:\\dafeiyu\\astrbot\\data\\plugin_data", PLUGIN_NAME
        )

    def _find_memory_db(self) -> str | None:
        explicit = str(self.conf.get("memory_db_path") or "").strip()
        if explicit and os.path.isfile(explicit):
            return explicit
        # 按插件数据目录的兄弟目录找
        try:
            base = os.path.join("A:\\dafeiyu\\astrbot\\data\\plugin_data", MEMORY_PLUGIN)
            p = os.path.join(base, "memory.db")
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
                await self._maybe_auto_evolve()
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
        )
        if p is None:
            logger.warning("[hlp] 模型没给出有效人格，保留旧版")
            return None

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
        self.current = await self.store.latest()
        await self.store.set_state("last_total_atoms", str(total))
        await self.store.set_state("last_evolve_at", str(time.time()))
        logger.info(
            f"[hlp] 人格更新到第 {version} 版（叙事相似度 {drift:.2f}）"
        )
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
        bot_qq = str(self.conf.get("bot_qq") or "1226735574").strip()
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
        for p in (
            r"A:\dafeiyu\astrbot\data\data_v4.db",
            os.path.join(os.getcwd(), "data", "data_v4.db"),
        ):
            if os.path.isfile(p):
                return p
        return None

    def _find_people_db(self) -> str | None:
        """human-like memory 的人库（有 relations.tone）。"""
        explicit = str(self.conf.get("people_db_path") or "").strip()
        if explicit and os.path.isfile(explicit):
            return explicit
        p = r"A:\dafeiyu\astrbot\data\plugin_data\astrbot_plugin_human_like_memory\people.db"
        return p if os.path.isfile(p) else None

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


__all__ = ["HumanLikePersonality"]
