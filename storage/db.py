"""SQLite: горячее состояние завода.

Профиль бренда (core.md, goals.md, platforms.md) сюда не попадает — он живёт
файлами. Здесь только то, что пишется часто и конкурентно: топики, темы,
публикации, метрики, задачи, прогресс онбординга.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tenants (
    chat_id       INTEGER PRIMARY KEY,
    brand_slug    TEXT,
    brand_name    TEXT,
    persona       TEXT DEFAULT 'leopold',
    tz            TEXT DEFAULT 'Europe/Moscow',
    plan          TEXT DEFAULT 'demo',
    status        TEXT DEFAULT 'new',      -- new | onboarding | ready | paused
    created_at    TEXT DEFAULT (datetime('now'))
);

-- topic_id IS NULL означает General: у него нет message_thread_id
CREATE TABLE IF NOT EXISTS topics (
    chat_id   INTEGER NOT NULL,
    key       TEXT    NOT NULL,
    topic_id  INTEGER,
    PRIMARY KEY (chat_id, key)
);

CREATE TABLE IF NOT EXISTS onboarding (
    chat_id         INTEGER PRIMARY KEY,
    step            TEXT DEFAULT 'O0',
    answers_json    TEXT DEFAULT '{}',
    raw_inputs_json TEXT DEFAULT '[]',     -- сырьё: что прислали, чем ответили
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS themes (
    id           TEXT PRIMARY KEY,          -- ГГГГ-ММ-ДД-площадка-NN
    chat_id      INTEGER NOT NULL,
    date         TEXT,
    plat         TEXT,
    format       TEXT,
    rubric       TEXT,
    goal         TEXT,                      -- warm | prod | pers
    arch         TEXT,
    funnel_stage TEXT,
    cta_id       TEXT,
    title        TEXT,
    hook         TEXT,
    why          TEXT,
    angle        TEXT,
    charge       TEXT,
    src          TEXT DEFAULT 'plan',       -- plan | adhoc
    status       TEXT DEFAULT 'idea',       -- idea|draft|ready|pub|skip|failed
    skip_reason  TEXT,
    asset        TEXT,
    core_version TEXT,
    updated_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS posts (
    theme_id     TEXT PRIMARY KEY,
    chat_id      INTEGER NOT NULL,
    platform     TEXT,
    scheduled_at TEXT,
    state        TEXT DEFAULT 'ready',      -- ready|sending|pub|failed|skip
    external_id  TEXT UNIQUE,               -- защита от двойной публикации
    link         TEXT,
    published_at TEXT,
    attempts     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS metrics (
    post_id      TEXT NOT NULL,
    window       TEXT NOT NULL,             -- 24h | 72h | 7d | 30d
    collected_at TEXT DEFAULT (datetime('now')),
    views        INTEGER, reach INTEGER, likes INTEGER, comments INTEGER,
    saves        INTEGER, shares INTEGER, watch_pct REAL, subs_delta INTEGER,
    source       TEXT,
    PRIMARY KEY (post_id, window)
);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    stage         TEXT,
    role          TEXT,
    payload_json  TEXT,
    status        TEXT DEFAULT 'queued',    -- queued|running|done|failed
    attempts      INTEGER DEFAULT 0,
    last_error    TEXT,
    next_retry_at TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

-- Прогоны моста в Claude Code. Отдельно от tasks: у той другие колонки,
-- в живых базах она уже создана, а CREATE TABLE IF NOT EXISTS их не
-- добавит. Механизма миграций в проекте нет, новая таблица дешевле.
--
-- estimated_api_cost — то, что вернул CLI в total_cost_usd. Это оценка
-- «во что обошлось бы по API-тарифу», а НЕ подтверждённое списание: на
-- подписке считаются лимиты, а не деньги. Имя длинное намеренно, чтобы
-- через месяц никто не прочитал его как счёт.
CREATE TABLE IF NOT EXISTS bridge_runs (
    task_id            TEXT PRIMARY KEY,        -- ГГГГ-ММ-ДД-workflow-NN
    chat_id            INTEGER NOT NULL,
    workflow           TEXT,
    status             TEXT DEFAULT 'running',  -- running|done|failed|timeout
    started_at         TEXT DEFAULT (datetime('now')),
    finished_at        TEXT,
    duration_s         REAL,
    estimated_api_cost REAL,
    session_id         TEXT,
    error              TEXT
);

-- Очередь задач к мосту. Мост берёт одну за раз — это потолок процесса
-- Claude Code, а не пожелание, — но человек ставит задачи пачкой: «напиши
-- пост», «ещё один», «и сценарий». До очереди вторая просьба получала
-- отказ и пропадала: чтобы завод её увидел, надо было сидеть и повторять.
--
-- Здесь лежит только просьба, не задача. Папку и `input.md` собирает
-- `create_task` в момент, когда очередь дошла: факты в контракте
-- (свободные слоты, статусы тем) устаревают, пока строка ждёт, и задача,
-- слепленная при постановке, ушла бы работать по позавчерашнему плану.
CREATE TABLE IF NOT EXISTS bridge_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    workflow   TEXT NOT NULL,
    ask        TEXT NOT NULL,
    topic      TEXT,                        -- ключ топика, куда отвечать
    status     TEXT DEFAULT 'waiting',      -- waiting|taken|done|failed|dropped
    created_at TEXT DEFAULT (datetime('now')),
    taken_at   TEXT,
    task_id    TEXT,                        -- id прогона, когда дошла очередь
    error      TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
    chat_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    calls   INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, day)
);
"""

_conn: sqlite3.Connection | None = None


def init(path: Path) -> None:
    global _conn
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    _conn.commit()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    assert _conn is not None, "db.init() не вызван"
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


def q(sql: str, *args: Any) -> list[sqlite3.Row]:
    assert _conn is not None
    return _conn.execute(sql, args).fetchall()


def one(sql: str, *args: Any) -> sqlite3.Row | None:
    rows = q(sql, *args)
    return rows[0] if rows else None


# ── тенанты ───────────────────────────────────────────────────────────

def ensure_tenant(chat_id: int, tz: str) -> sqlite3.Row:
    with tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO tenants (chat_id, tz) VALUES (?, ?)",
            (chat_id, tz))
    return one("SELECT * FROM tenants WHERE chat_id = ?", chat_id)  # type: ignore


def set_tenant(chat_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE tenants SET {cols} WHERE chat_id = ?",
                  (*fields.values(), chat_id))


# ── топики ────────────────────────────────────────────────────────────

def save_topic(chat_id: int, key: str, topic_id: int | None) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO topics (chat_id, key, topic_id) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, key) DO UPDATE SET topic_id = excluded.topic_id",
            (chat_id, key, topic_id))


def topic_id(chat_id: int, key: str) -> int | None:
    row = one("SELECT topic_id FROM topics WHERE chat_id = ? AND key = ?",
              chat_id, key)
    return row["topic_id"] if row else None


def topics_ready(chat_id: int) -> bool:
    row = one("SELECT COUNT(*) n FROM topics WHERE chat_id = ?", chat_id)
    return bool(row and row["n"] > 0)


# ── онбординг ─────────────────────────────────────────────────────────

def onboarding_state(chat_id: int) -> dict[str, Any]:
    row = one("SELECT * FROM onboarding WHERE chat_id = ?", chat_id)
    if row is None:
        with tx() as c:
            c.execute("INSERT INTO onboarding (chat_id) VALUES (?)", (chat_id,))
        return {"step": "O0", "answers": {}, "raw": []}
    return {
        "step": row["step"],
        "answers": json.loads(row["answers_json"]),
        "raw": json.loads(row["raw_inputs_json"]),
    }


def onboarding_save(chat_id: int, step: str, answers: dict[str, Any],
                    raw: list[Any]) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO onboarding (chat_id, step, answers_json, raw_inputs_json)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET"
            " step = excluded.step, answers_json = excluded.answers_json,"
            " raw_inputs_json = excluded.raw_inputs_json,"
            " updated_at = datetime('now')",
            (chat_id, step, json.dumps(answers, ensure_ascii=False),
             json.dumps(raw, ensure_ascii=False)))


# ── бюджет вызовов модели ─────────────────────────────────────────────

def bump_llm(chat_id: int, day: str) -> int:
    with tx() as c:
        c.execute(
            "INSERT INTO llm_usage (chat_id, day, calls) VALUES (?, ?, 1) "
            "ON CONFLICT(chat_id, day) DO UPDATE SET calls = calls + 1",
            (chat_id, day))
    row = one("SELECT calls FROM llm_usage WHERE chat_id = ? AND day = ?",
              chat_id, day)
    return row["calls"] if row else 0
