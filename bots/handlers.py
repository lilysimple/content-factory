"""Обработчики обновлений.

Разделение по allowed_updates:
  assistant — message, my_chat_member, chat_member, callback_query
  остальные — только callback_query

callback_query приходит тому боту, который ОТПРАВИЛ сообщение с кнопкой,
поэтому нажатия слушают все семеро, а сообщения только Ассистент.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from bots import topics
from bots.registry import ROLES, STUBS, registry
from bots.router import resolve
from config import cfg
from orchestrator import (design, editor, onboarding, publisher, refresh,
                          reply, strategy)
from storage import db

log = logging.getLogger("handlers")


def topic_key_of(chat_id: int, thread_id: int | None) -> str | None:
    if thread_id is None:
        return "general"
    for row in db.q("SELECT key, topic_id FROM topics WHERE chat_id = ?", chat_id):
        if row["topic_id"] == thread_id:
            return row["key"]
    return None


def register(dp_assistant: Dispatcher, dp_workers: Dispatcher) -> None:

    # ── бота добавили в группу ────────────────────────────────────────
    @dp_assistant.my_chat_member()
    async def on_my_status(ev: ChatMemberUpdated, bot: Bot) -> None:
        chat_id = ev.chat.id
        if not cfg.chat_allowed(chat_id):
            log.warning("чат %s не в allowlist, игнорирую", chat_id)
            return
        if ev.new_chat_member.status not in {"administrator", "member"}:
            return

        db.ensure_tenant(chat_id, cfg.default_tz)

        ok, why = await topics.can_manage(registry, chat_id)
        if not ok:
            await bot.send_message(
                chat_id,
                "🤝 <b>Ассистент</b>\nЧтобы собрать структуру, мне нужно: "
                f"{why}.\n\nПоправь и напиши сюда «готово».")
            return

        created, errors = await topics.provision(registry, chat_id)
        if errors:
            await registry.say("assistant", chat_id,
                               "Часть топиков не создалась:\n" + "\n".join(errors))
            return

        await onboarding.start(registry, chat_id, created)

    # ── команда «готово» после исправления прав ───────────────────────
    @dp_assistant.message(F.text.lower().in_({"готово", "/start"}))
    async def on_ready(msg: Message) -> None:
        chat_id = msg.chat.id
        if not cfg.chat_allowed(chat_id):
            return
        db.ensure_tenant(chat_id, cfg.default_tz)

        ok, why = await topics.can_manage(registry, chat_id)
        if not ok:
            await registry.say("assistant", chat_id, f"Пока не хватает: {why}.")
            return
        created, _ = await topics.provision(registry, chat_id)
        await onboarding.start(registry, chat_id, created)

    # ── кто-то зашёл или вышел ────────────────────────────────────────
    @dp_assistant.chat_member()
    async def on_member(ev: ChatMemberUpdated) -> None:
        if not ev.new_chat_member.user.is_bot:
            return
        if ev.new_chat_member.status not in {"member", "administrator"}:
            return
        if not cfg.chat_allowed(ev.chat.id):
            return
        await onboarding.crew_joined(registry, ev.chat.id,
                                     ev.new_chat_member.user.username or "")

    # ── обычное сообщение ─────────────────────────────────────────────
    @dp_assistant.message(F.text | F.voice | F.photo | F.document)
    async def on_message(msg: Message) -> None:
        chat_id = msg.chat.id
        if not cfg.chat_allowed(chat_id):
            return
        if not db.topics_ready(chat_id):
            return

        tenant = db.ensure_tenant(chat_id, cfg.default_tz)
        text = (msg.text or msg.caption or "").strip().lower()

        # Работа с профилем доступна всегда, а не только внутри анкеты.
        if text in {"покажи ядро", "выгрузи всё", "выгрузи все"}:
            await onboarding.send_files(registry, chat_id,
                                        zip_it="выгрузи" in text)
            return

        if tenant["status"] in {"new", "onboarding"}:
            await onboarding.handle(registry, msg)
            return

        raw = msg.text or msg.caption or ""
        tkey = topic_key_of(chat_id, msg.message_thread_id)

        # Человек нажал «Правки» под планом и сейчас пишет, что поправить.
        # Это ответ Стратегу, а не новая задача: маршрутизировать заново
        # значит потерять правку в общем разборе.
        if strategy.wants_fix(chat_id):
            await strategy.revise(registry, chat_id, raw,
                                  topic=tkey or "general")
            return

        if editor.wants_fix(chat_id):
            await editor.revise(registry, chat_id, raw,
                                topic=tkey or "review")
            return

        if design.wants_fix(chat_id):
            await design.revise(registry, chat_id, raw,
                                topic=tkey or "design")
            return

        if publisher.wants_reason(chat_id):
            await publisher.take_reason(registry, chat_id, raw,
                                        topic=tkey or "queue")
            return

        # Материалы после онбординга уточняют существующий профиль,
        # а не создают новый бренд.
        if refresh.wants_refresh(msg):
            await refresh.start(registry, msg)
            return

        # Просьба поправить профиль словами: «давай обновим стратегию»,
        # «добавь стоп-слово». Иначе такое уходило Стратегу в заглушку.
        if refresh.wants_edit(raw):
            await refresh.edit(registry, chat_id, raw)
            return

        route = resolve(raw, tkey, registry.me)

        if route.role is None:
            await onboarding.ask_which_role(registry, chat_id)
            return

        if route.role == "strategy":
            await strategy.run(registry, chat_id, raw, topic=tkey or "strategy")
            return

        if route.role == "editor":
            await editor.run(registry, chat_id, raw, topic=tkey or "review")
            return

        if route.role == "design":
            await design.run(registry, chat_id, raw, topic=tkey or "design")
            return

        if route.role == "publisher":
            await publisher.run(registry, chat_id, raw, topic=tkey or "queue")
            return

        # Всё, что не производственная задача, разбирает Ассистент — и
        # разбирает по-настоящему: с профилем бренда и состоянием из базы.
        if route.role == "assistant":
            await reply.answer(registry, chat_id, msg.text or msg.caption or "",
                               topic=tkey or "general")
            return

        # Роли ещё не подключены к работе. Врать «принял» нельзя: человек
        # будет ждать результат, которого не будет.
        pending = {
            "research": "внешний ресёрч и метрики",
            "reels": "сценарии",
        }
        what = pending.get(route.role, "эту работу")
        await registry.say(
            route.role, chat_id,
            f"Понял, это ко мне — {what}. Но я ещё не подключён к работе, "
            "это следующий шаг сборки.\n\nПока работает: «покажи ядро», "
            "«выгрузи всё».",
            topic=tkey or "general")

    # ── нажатие кнопки: слушают все семеро ────────────────────────────
    async def on_callback(cb: CallbackQuery, bot: Bot) -> None:
        # Ответить надо сразу, иначе у человека вечно крутится часик.
        await cb.answer()
        if cb.message is None or not cfg.chat_allowed(cb.message.chat.id):
            return
        role = registry.role_of(bot)
        chat_id = cb.message.chat.id
        log.info("callback %s от роли %s", cb.data, role)

        kind, _, action = (cb.data or "").partition(":")
        if kind == "refresh":
            await refresh.on_callback(registry, chat_id, action)
            return
        if kind == "edit":
            await refresh.on_edit_callback(registry, chat_id, action)
            return
        if kind == "plan":
            tkey = topic_key_of(chat_id, cb.message.message_thread_id)
            await strategy.on_callback(registry, chat_id, action,
                                       topic=tkey or "strategy")
            return
        if kind == "post":
            tkey = topic_key_of(chat_id, cb.message.message_thread_id)
            await editor.on_callback(registry, chat_id, action,
                                     topic=tkey or "review")
            return
        if kind == "pub":
            tkey = topic_key_of(chat_id, cb.message.message_thread_id)
            await publisher.on_callback(registry, chat_id, action,
                                        topic=tkey or "queue")
            return
        if kind == "art":
            tkey = topic_key_of(chat_id, cb.message.message_thread_id)
            await design.on_callback(registry, chat_id, action,
                                     topic=tkey or "design")
            return
        await onboarding.on_callback(registry, cb, role)

    dp_assistant.callback_query()(on_callback)
    dp_workers.callback_query()(on_callback)
