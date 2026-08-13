"""Семь лиц завода и очередь отправки.

Каждая роль говорит своим токеном. Отправка идёт через одну очередь с
троттлингом на chat_id: лимит Telegram ~20 сообщений в минуту в один чат,
а батч карточек подходит к нему вплотную.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from config import cfg
from storage import db

log = logging.getLogger("registry")

# Пауза между сообщениями в один чат. 3.2 с ≈ 18 в минуту, с запасом под 20.
CHAT_INTERVAL = 3.2


@dataclass(frozen=True)
class Role:
    key: str
    emoji: str
    title: str

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.title}"


ROLES: dict[str, Role] = {
    "assistant": Role("assistant", "🤝", "Ассистент"),
    "research":  Role("research",  "🔍", "Ресёрчер"),
    "strategy":  Role("strategy",  "🎯", "Стратег"),
    "editor":    Role("editor",    "✍️", "Редактор"),
    "reels":     Role("reels",     "🎬", "Редактор Reels"),
    "design":    Role("design",    "🎨", "Дизайнер"),
    "publisher": Role("publisher", "📤", "Публикатор"),
}

# Роли, которые в v1 отвечают «пока не умею»
STUBS = {"reels", "design"}


class Registry:
    def __init__(self) -> None:
        self.bots: dict[str, Bot] = {}
        self.me: dict[str, str] = {}          # role -> username
        self._locks: dict[int, asyncio.Lock] = {}
        self._last: dict[int, float] = {}

    async def start(self) -> None:
        props = DefaultBotProperties(parse_mode=ParseMode.HTML)
        for role, token in cfg.tokens.items():
            if not token:
                log.warning("нет токена для роли %s, пропускаю", role)
                continue
            bot = Bot(token=token, default=props)
            info = await bot.get_me()
            self.bots[role] = bot
            self.me[role] = info.username or role
            log.info("%s → @%s", ROLES[role].label, info.username)

    async def close(self) -> None:
        for bot in self.bots.values():
            await bot.session.close()

    def bot(self, role: str) -> Bot:
        if role not in self.bots:
            raise RuntimeError(f"бот роли {role} не поднят, проверь .env")
        return self.bots[role]

    def role_of(self, bot: Bot) -> str:
        for role, b in self.bots.items():
            if b.id == bot.id:
                return role
        return "assistant"

    async def _throttle(self, chat_id: int) -> None:
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            wait = CHAT_INTERVAL - (loop.time() - self._last.get(chat_id, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[chat_id] = loop.time()

    async def say(
        self,
        role: str,
        chat_id: int,
        text: str,
        *,
        topic: str = "general",
        kb: InlineKeyboardMarkup | None = None,
        with_label: bool = True,
    ):
        """Отправить сообщение от лица роли в нужный топик.

        `topic` это ключ из TOPICS, не message_thread_id. У General нет
        message_thread_id, поэтому параметр не передаётся вовсе.
        """
        body = f"<b>{ROLES[role].label}</b>\n{text}" if with_label else text
        thread = db.topic_id(chat_id, topic)

        for attempt in range(3):
            await self._throttle(chat_id)
            kwargs = {"chat_id": chat_id, "text": body, "reply_markup": kb}
            if thread is not None:
                kwargs["message_thread_id"] = thread
            try:
                return await self.bot(role).send_message(**kwargs)
            except TelegramRetryAfter as e:
                log.warning("flood control, жду %s с", e.retry_after)
                await asyncio.sleep(e.retry_after + 0.5)
            except TelegramBadRequest as e:
                msg = str(e).lower()
                # Топик удалили руками — пересоздаём и пробуем ещё раз.
                if "thread not found" in msg or "topic_deleted" in msg:
                    from bots import topics
                    thread = await topics.recreate(self, chat_id, topic)
                    continue
                raise
        raise RuntimeError(f"не смог отправить в {chat_id}/{topic}")


registry = Registry()
