"""人格库：快照 + 变化事件 + 记忆源读取。

设计上跟 human_like_memory 分开存（独立 db），
只读它的库拿记忆，不写——职责边界清楚。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import aiosqlite

from .model import Personality
from .rating import apply_interaction as _apply_interaction, drift_step, drift_target

logger = None  # 由 main 注入，避免循环依赖
try:
    from astrbot import logger  # type: ignore
except Exception:  # pragma: no cover
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS personality_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version       INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    traits        TEXT DEFAULT '{}',
    adaptations   TEXT DEFAULT '{}',
    narrative     TEXT DEFAULT '',
    gestalt       TEXT DEFAULT '{}',
    schema        TEXT DEFAULT '{}',
    core          TEXT DEFAULT '{}',
    voice         TEXT DEFAULT '{}',
    source_atoms  INTEGER DEFAULT 0,
    total_atoms   INTEGER DEFAULT 0,
    reason        TEXT DEFAULT '',
    changed_by    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS personality_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     INTEGER NOT NULL,
    dimension   TEXT DEFAULT '',
    old_value   TEXT DEFAULT '',
    new_value   TEXT DEFAULT '',
    reason      TEXT DEFAULT '',
    -- 这次变化是被哪几条记忆推着走的（JSON 数组）
    trigger_atom_ids TEXT DEFAULT '[]',
    at          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snap_version ON personality_snapshots(version DESC);
CREATE INDEX IF NOT EXISTS idx_event_version ON personality_events(version DESC);

-- 演化状态（什么时候该再归纳一次）
CREATE TABLE IF NOT EXISTS personality_state (
    key   TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);

-- 评价体系：好感 + 态度 两维（0-100，默认 50），带漂移锚
CREATE TABLE IF NOT EXISTS person_ratings (
    person_key       TEXT PRIMARY KEY,
    name             TEXT DEFAULT '',
    affection        REAL DEFAULT 50,
    attitude         REAL DEFAULT 50,
    note             TEXT DEFAULT '',
    updated_at       REAL DEFAULT 0,
    affection_anchor REAL DEFAULT 50,
    attitude_anchor  REAL DEFAULT 50,
    last_drift_at    REAL DEFAULT 0
);
"""


class PersonalityStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def initialize(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            # 老库补 gestalt 列（1.0.x → 1.1.0 升级）
            cur = await db.execute("PRAGMA table_info(personality_snapshots)")
            cols = {row[1] for row in await cur.fetchall()}
            if "gestalt" not in cols:
                await db.execute(
                    "ALTER TABLE personality_snapshots ADD COLUMN gestalt TEXT DEFAULT '{}'"
                )
            if "schema" not in cols:
                await db.execute(
                    "ALTER TABLE personality_snapshots ADD COLUMN schema TEXT DEFAULT '{}'"
                )
            if "core" not in cols:
                await db.execute(
                    "ALTER TABLE personality_snapshots ADD COLUMN core TEXT DEFAULT '{}'"
                )
            # events 补 trigger_atom_ids（1.7.0 → 1.8.0）
            cur = await db.execute("PRAGMA table_info(personality_events)")
            ev_cols = {row[1] for row in await cur.fetchall()}
            if "trigger_atom_ids" not in ev_cols:
                await db.execute(
                    "ALTER TABLE personality_events ADD COLUMN trigger_atom_ids TEXT DEFAULT '[]'"
                )
            # person_ratings 补 name 列（3.0.0 第一版没有）
            cur = await db.execute("PRAGMA table_info(person_ratings)")
            pr_cols = {row[1] for row in await cur.fetchall()}
            if pr_cols and "name" not in pr_cols:
                await db.execute(
                    "ALTER TABLE person_ratings ADD COLUMN name TEXT DEFAULT ''"
                )
            await db.commit()

    # ---------- 快照 ----------

    async def latest(self) -> Personality | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM personality_snapshots ORDER BY version DESC, id DESC LIMIT 1"
            )
            row = await cur.fetchone()
        return self._row_to_personality(row) if row else None

    async def get_version(self, version: int) -> Personality | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM personality_snapshots WHERE version = ? "
                "ORDER BY id DESC LIMIT 1",
                (int(version),),
            )
            row = await cur.fetchone()
        return self._row_to_personality(row) if row else None

    async def save(
        self,
        p: Personality,
        *,
        reason: str = "",
        changed_by: str = "llm",
        trigger_atom_ids: list[int] | None = None,
    ) -> int:
        """存一份快照，返回新版本号。"""
        prev = await self.latest()
        version = (prev.version + 1) if prev else 1
        p.version = version
        p.created_at = p.created_at or time.time()
        p.reason = reason
        p.changed_by = changed_by
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO personality_snapshots(version, created_at, traits, "
                "adaptations, narrative, gestalt, schema, core, voice, source_atoms, "
                "total_atoms, reason, changed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version,
                    p.created_at,
                    json.dumps(p.traits.to_dict(), ensure_ascii=False),
                    json.dumps(p.adaptations.to_dict(), ensure_ascii=False),
                    p.narrative,
                    json.dumps(p.gestalt.to_dict(), ensure_ascii=False),
                    json.dumps(p.schema.to_dict(), ensure_ascii=False),
                    json.dumps(p.core.to_dict(), ensure_ascii=False),
                    json.dumps(p.voice.to_dict(), ensure_ascii=False),
                    p.source_atoms,
                    p.total_atoms,
                    reason,
                    changed_by,
                ),
            )
            await db.commit()
        # 记录和上一版的差异
        if prev is not None:
            await self._record_diff(prev, p, trigger_atom_ids or [])
        return version

    async def _record_diff(
        self, old: Personality, new: Personality, trigger_ids: list[int] | None = None
    ) -> None:
        """把这次变了哪些维度记进 events。"""
        now = time.time()
        rows: list[tuple] = []
        _trig = json.dumps([int(x) for x in (trigger_ids or [])], ensure_ascii=False)
        for key in old.traits.to_dict():
            a, b = getattr(old.traits, key), getattr(new.traits, key)
            if a != b:
                rows.append((new.version, f"traits.{key}", str(a), str(b), "", now))
        if old.narrative != new.narrative:
            rows.append((new.version, "narrative", old.narrative[:300],
                         new.narrative[:300], "", now))
        if old.adaptations.values != new.adaptations.values:
            rows.append((new.version, "values",
                         "、".join(old.adaptations.values),
                         "、".join(new.adaptations.values), "", now))
        if old.adaptations.attachment_levels != new.adaptations.attachment_levels:
            rows.append((new.version, "attachment_levels",
                         "；".join(f"{k}:{v}" for k, v in old.adaptations.attachment_levels.items()),
                         "；".join(f"{k}:{v}" for k, v in new.adaptations.attachment_levels.items()),
                         "", now))
        if old.adaptations.attachment_default != new.adaptations.attachment_default:
            rows.append((new.version, "attachment_default",
                         old.adaptations.attachment_default,
                         new.adaptations.attachment_default, "", now))
        if old.gestalt.figure != new.gestalt.figure:
            rows.append((new.version, "gestalt.figure",
                         old.gestalt.figure, new.gestalt.figure, "", now))
        if old.gestalt.background != new.gestalt.background:
            rows.append((new.version, "gestalt.background",
                         old.gestalt.background, new.gestalt.background, "", now))
        if old.gestalt.emergent_whole != new.gestalt.emergent_whole:
            rows.append((new.version, "gestalt.whole",
                         old.gestalt.emergent_whole, new.gestalt.emergent_whole, "", now))
        if set(old.gestalt.unfinished) != set(new.gestalt.unfinished):
            rows.append((new.version, "gestalt.unfinished",
                         "、".join(old.gestalt.unfinished),
                         "、".join(new.gestalt.unfinished), "", now))
        if set(old.gestalt.patterns) != set(new.gestalt.patterns):
            rows.append((new.version, "gestalt.patterns",
                         "、".join(old.gestalt.patterns),
                         "、".join(new.gestalt.patterns), "", now))
        if set(old.schema.self_schema) != set(new.schema.self_schema):
            rows.append((new.version, "schema.self",
                         "、".join(old.schema.self_schema),
                         "、".join(new.schema.self_schema), "", now))
        if old.schema.owner_schema != new.schema.owner_schema:
            rows.append((new.version, "schema.owner",
                         old.schema.owner_schema, new.schema.owner_schema, "", now))
        if old.schema.role_schema != new.schema.role_schema:
            rows.append((new.version, "schema.role",
                         old.schema.role_schema, new.schema.role_schema, "", now))
        if old.schema.relational != new.schema.relational:
            rows.append((new.version, "schema.relational",
                         old.schema.relational, new.schema.relational, "", now))
        if old.schema.scripts != new.schema.scripts:
            rows.append((new.version, "schema.scripts",
                         "；".join(f"{k}:{v}" for k, v in old.schema.scripts.items()),
                         "；".join(f"{k}:{v}" for k, v in new.schema.scripts.items()),
                         "", now))
        if old.adaptations.attribution_style != new.adaptations.attribution_style:
            def _afmt(d: dict) -> str:
                return "；".join(
                    f"{w}:" + ",".join(f"{k}{v}" for k, v in dims.items())
                    for w, dims in (d or {}).items()
                )
            rows.append((new.version, "attribution_style",
                         _afmt(old.adaptations.attribution_style),
                         _afmt(new.adaptations.attribution_style), "", now))
        if old.adaptations.altruism != new.adaptations.altruism:
            rows.append((new.version, "altruism",
                         "、".join(f"{k}:{v}" for k, v in old.adaptations.altruism.items()),
                         "、".join(f"{k}:{v}" for k, v in new.adaptations.altruism.items()),
                         "", now))
        if old.schema.public_self != new.schema.public_self:
            rows.append((new.version, "public_self",
                         "；".join(f"{k}:{v}" for k, v in old.schema.public_self.items()),
                         "；".join(f"{k}:{v}" for k, v in new.schema.public_self.items()),
                         "", now))
        if old.voice.sentence_style != new.voice.sentence_style:
            rows.append((new.version, "voice",
                         old.voice.sentence_style, new.voice.sentence_style, "", now))
        if not rows:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT INTO personality_events(version, dimension, old_value, "
                "new_value, reason, trigger_atom_ids, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            await db.commit()

    async def history(self, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT version, created_at, reason, changed_by, total_atoms "
                "FROM personality_snapshots ORDER BY version DESC LIMIT ?",
                (max(1, limit),),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def events(self, limit: int = 50) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT version, dimension, old_value, new_value, reason, trigger_atom_ids, at "
                "FROM personality_events ORDER BY id DESC LIMIT ?",
                (max(1, limit),),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def record_coherence(self, version: int, issues: list[dict]) -> int:
        """把「统一」校验的修正记进 events，维度用 coherence.*，可追溯。"""
        if not issues:
            return 0
        now = time.time()
        rows = [
            (
                int(version),
                f"coherence.{str(i.get('dim') or '?')}",
                str(i.get("old") or "")[:300],
                str(i.get("new") or "")[:300],
                str(i.get("note") or "")[:200],
                "[]",
                now,
            )
            for i in issues
            if isinstance(i, dict)
        ]
        if not rows:
            return 0
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT INTO personality_events(version, dimension, old_value, "
                "new_value, reason, trigger_atom_ids, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            await db.commit()
        return len(rows)

    # ---------- 演化状态 ----------

    async def get_state(self, key: str, default: str = "") -> str:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT value FROM personality_state WHERE key = ?", (key,)
            )
            row = await cur.fetchone()
        return str(row[0]) if row else default

    async def set_state(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO personality_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            await db.commit()

    async def stats(self) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            async def one(sql: str) -> int:
                cur = await db.execute(sql)
                return int((await cur.fetchone())[0])

            return {
                "snapshots": await one("SELECT COUNT(*) FROM personality_snapshots"),
                "events": await one("SELECT COUNT(*) FROM personality_events"),
            }

    # ---------- 评价体系（好感 + 态度） ----------

    async def get_rating(self, person_key: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM person_ratings WHERE person_key = ?", (str(person_key),)
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def find_rating(self, who: str) -> dict | None:
        """按名字或 key（QQ）找评价。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM person_ratings WHERE person_key = ? OR name = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (str(who), str(who)),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def all_ratings(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM person_ratings ORDER BY affection DESC")
            return [dict(r) for r in await cur.fetchall()]

    async def apply_interaction(self, person_key: str, kind: str, name: str = "", weight: float = 1.0) -> dict | None:
        """把一次夸/责落到某人的两维评价上。返回 {person_key, name, affection, attitude, attribution}。"""
        key = str(person_key or "").strip()
        if not key:
            return None
        cur = await self.get_rating(key)
        a = float(cur["affection"]) if cur else 50.0
        t = float(cur["attitude"]) if cur else 50.0
        new_a, new_t, attr = _apply_interaction(a, t, str(kind), weight=weight)
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO person_ratings(person_key, name, affection, attitude, updated_at, "
                "affection_anchor, attitude_anchor) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(person_key) DO UPDATE SET "
                "name = CASE WHEN excluded.name != '' THEN excluded.name ELSE person_ratings.name END, "
                "affection = excluded.affection, attitude = excluded.attitude, "
                "updated_at = excluded.updated_at, "
                "affection_anchor = excluded.affection_anchor, "
                "attitude_anchor = excluded.attitude_anchor",
                (key, str(name or "").strip()[:30], new_a, new_t, now, new_a, new_t),
            )
            await db.commit()
        return {"person_key": key, "name": str(name or "").strip()[:30], "affection": new_a, "attitude": new_t, "attribution": attr}

    async def drift_ratings(self, drift_days: float = 30.0) -> int:
        """长时间不联系 → 两维各自向中回归（高向下、低向上，各一步）。返回漂了几个人。"""
        now = time.time()
        gap = max(1.0, float(drift_days)) * 86400
        drifted = 0
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM person_ratings")
            rows = await cur.fetchall()
            for r in rows:
                if now - float(r["updated_at"] or 0) < gap:
                    continue
                if now - float(r["last_drift_at"] or 0) < gap:
                    continue
                a = float(r["affection"] or 50)
                t = float(r["attitude"] or 50)
                at = float(r["affection_anchor"] or 50)
                tt = float(r["attitude_anchor"] or 50)
                na = drift_step(a, drift_target(at))
                nt = drift_step(t, drift_target(tt))
                if na != a or nt != t:
                    await db.execute(
                        "UPDATE person_ratings SET affection = ?, attitude = ?, last_drift_at = ? "
                        "WHERE person_key = ?",
                        (na, nt, now, str(r["person_key"])),
                    )
                    drifted += 1
            await db.commit()
        return drifted

    async def set_note(self, person_key: str, note: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE person_ratings SET note = ? WHERE person_key = ?",
                (str(note or "")[:120], str(person_key)),
            )
            await db.commit()

    # ---------- 行 → 对象 ----------

    def _row_to_personality(self, row) -> Personality:
        def g(key, default=None):
            try:
                return row[key]
            except (IndexError, KeyError):
                return default

        def j(raw) -> dict:
            try:
                v = json.loads(raw or "{}")
                return v if isinstance(v, dict) else {}
            except Exception:
                return {}

        p = Personality()
        p.version = int(g("version") or 0)
        p.created_at = float(g("created_at") or 0)
        p.traits = p.traits.from_dict(j(g("traits")))
        p.adaptations = p.adaptations.from_dict(j(g("adaptations")))
        p.voice = p.voice.from_dict(j(g("voice")))
        p.narrative = str(g("narrative") or "")
        p.gestalt = p.gestalt.from_dict(j(g("gestalt")))
        p.schema = p.schema.from_dict(j(g("schema")))
        p.core = p.core.from_dict(j(g("core")))
        p.source_atoms = int(g("source_atoms") or 0)
        p.total_atoms = int(g("total_atoms") or 0)
        p.reason = str(g("reason") or "")
        p.changed_by = str(g("changed_by") or "")
        return p


__all__ = ["PersonalityStore"]
