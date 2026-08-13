"""Автопровижининг структуры топиков.

Пользователь ничего не настраивает: Ассистент ловит своё назначение админом
и создаёт десять топиков сам.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram.exceptions import TelegramBadRequest

from storage import db

log = logging.getLogger("topics")

# Допустимые цвета иконок форума, заданы Telegram.
BLUE, YELLOW, VIOLET, GREEN, PINK, RED = (
    0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F)


@dataclass(frozen=True)
class Topic:
    key: str
    name: str
    color: int


# General создаётся Telegram сам и не имеет message_thread_id.
GENERAL = Topic("general", "General", BLUE)

TOPICS: list[Topic] = [
    Topic("research",  "🔍 Ресёрч",    BLUE),
    Topic("strategy",  "🎯 Стратегия", YELLOW),
    Topic("review",    "✍️ На ревью",  GREEN),
    Topic("reels",     "🎬 Reels",     VIOLET),
    Topic("design",    "🎨 Дизайн",    PINK),
    Topic("photos",    "📸 Фотобанк",  PINK),
    Topic("queue",     "📤 Очередь",   BLUE),
    Topic("metrics",   "📊 Метрики",   GREEN),
    Topic("logs",      "🧾 Логи",      RED),
]


async def provision(registry, chat_id: int) -> tuple[int, list[str]]:
    """Создать недостающие топики. Возвращает (сколько создано, ошибки)."""
    bot = registry.bot("assistant")
    errors: list[str] = []

    # General: запись с topic_id = NULL, чтобы отправка не передавала thread.
    db.save_topic(chat_id, GENERAL.key, None)

    created = 0
    for t in TOPICS:
        if db.topic_id(chat_id, t.key) is not None:
            continue
        try:
            ft = await bot.create_forum_topic(
                chat_id=chat_id, name=t.name, icon_color=t.color)
            db.save_topic(chat_id, t.key, ft.message_thread_id)
            created += 1
            log.info("создан топик %s (%s)", t.name, ft.message_thread_id)
        except TelegramBadRequest as e:
            errors.append(f"{t.name}: {e.message}")
            log.error("не создался топик %s: %s", t.name, e)

    return created, errors


async def recreate(registry, chat_id: int, key: str) -> int | None:
    """Топик удалили руками — создать заново под тем же ключом."""
    if key == GENERAL.key:
        return None
    spec = next((t for t in TOPICS if t.key == key), None)
    if spec is None:
        return None

    ft = await registry.bot("assistant").create_forum_topic(
        chat_id=chat_id, name=spec.name, icon_color=spec.color)
    db.save_topic(chat_id, key, ft.message_thread_id)
    log.warning("топик %s был удалён, пересоздан", spec.name)
    return ft.message_thread_id


async def can_manage(registry, chat_id: int) -> tuple[bool, str]:
    """Проверить права Ассистента до попытки создавать топики."""
    bot = registry.bot("assistant")
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
    except TelegramBadRequest as e:
        return False, f"не читается статус: {e.message}"

    if me.status != "administrator":
        return False, "нужны права администратора"
    if not getattr(me, "can_manage_topics", False):
        return False, "нужно право «Управление темами»"

    chat = await bot.get_chat(chat_id)
    if not getattr(chat, "is_forum", False):
        return False, "в группе не включены Темы"
    return True, ""
