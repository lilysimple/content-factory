"""Свободный разговор с Ассистентом после онбординга.

Всё, что не является явной производственной задачей, разбирает Ассистент —
с профилем бренда в контексте и реальным состоянием завода из базы.

Правило из его промпта здесь и проверяется: на вопрос о состоянии он
отвечает данными, а не воспоминанием. Поэтому состояние собирается кодом
и кладётся в промпт фактами, а не пересказом.
"""
from __future__ import annotations

import logging

from config import cfg
from orchestrator import agent
from storage import brand as brand_store
from storage import db

log = logging.getLogger("reply")

store = brand_store.Store(cfg.brands_path)

# Секции профиля, которые Ассистенту полезно держать перед глазами.
# Целиком core.md не грузим: он растёт, а нужны из него две-три части.
SECTIONS = ("Кто это", "Аудитория", "Голос", "Цель")
PROFILE_LIMIT = 6000


def _profile(chat_id: int) -> tuple[str, str]:
    """Вернуть (имя бренда, выжимку профиля) для контекста."""
    row = db.one("SELECT brand_slug, brand_name FROM tenants WHERE chat_id = ?",
                 chat_id)
    if not row or not row["brand_slug"]:
        return "", ""
    b = store.get(row["brand_slug"])
    if b is None:
        return "", ""

    parts = [s for s in (b.section("core", n) for n in SECTIONS) if s]
    text = "\n\n".join(parts) or b.read("core")
    return b.name(), text[:PROFILE_LIMIT]


def _state(chat_id: int) -> str:
    """Факты о состоянии завода. Ассистент не помнит их, он их читает."""
    row = db.one("SELECT brand_slug, persona, tz, status FROM tenants "
                 "WHERE chat_id = ?", chat_id)
    themes = db.q("SELECT status, COUNT(*) n FROM themes WHERE chat_id = ? "
                  "GROUP BY status", chat_id)
    posts = db.one("SELECT COUNT(*) n FROM posts WHERE chat_id = ?", chat_id)

    lines = [
        f"Профиль: {row['brand_slug'] if row else '—'}",
        f"Часовой пояс: {row['tz'] if row else cfg.default_tz}",
        "Темы в плане: " + (", ".join(f"{r['status']} {r['n']}" for r in themes)
                            if themes else "нет ни одной"),
        f"Публикаций: {posts['n'] if posts else 0}",
        "Подключены роли: Ассистент, Ресёрчер, Стратег, Редактор.",
        "Ещё не подключены: план недели, тексты, визуал, публикация — "
        "это следующие шаги сборки.",
        "Работающие команды: «покажи ядро», «выгрузи всё».",
    ]
    return "\n".join(lines)


async def answer(reg, chat_id: int, text: str, topic: str = "general") -> None:
    """Ответить человеку от лица Ассистента, с профилем и состоянием."""
    brand_name, profile = _profile(chat_id)
    row = db.one("SELECT persona FROM tenants WHERE chat_id = ?", chat_id)
    persona = row["persona"] if row else "leopold"

    context = (
        "## Состояние завода на сейчас\n\n" + _state(chat_id) +
        "\n\nЭто данные, а не твои воспоминания. Отвечая о состоянии, "
        "опирайся только на них. Чего здесь нет — того ты не знаешь, "
        "так и скажи."
    )

    try:
        out = await agent.ask(
            "assistant", chat_id, text,
            brand_name=brand_name, persona_id=persona,
            profile=profile, extra_system=context,
            max_tokens=4000)
    except agent.BudgetExceeded as e:
        await reg.say("assistant", chat_id, f"Остановился: {e}", topic=topic)
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("ответ не собрался")
        reason = getattr(e, "message", None) or type(e).__name__
        await reg.say("assistant", chat_id,
                      f"Не смог ответить: {reason}", topic=topic)
        return

    await reg.say("assistant", chat_id, out or "…", topic=topic)
