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
from bots.registry import registry
from bots.router import resolve
from config import cfg
from orchestrator import (bridge, design, desk, editor, onboarding, publisher,
                          reels, refresh, reply, research, strategy)
from storage import db

log = logging.getLogger("handlers")

# Куда отвечать, если команду дали в General: у каждой задачи свой топик.
TOPIC = {"plan": "strategy", "post": "review", "reels": "reels",
         "research": "research", "design": "design", "idea": "strategy"}

# Роль, которую распознал старый маршрутизатор, → workflow моста.
#
# Это не выбор субагентов: Python называет, **что** попросили, ровно как
# это делает `/plan`, а кого звать, по-прежнему решает Director.
# Публикатора здесь нет намеренно — он не AI-роль, собрать комплект и не
# допустить дубля это детерминированная работа `publisher.py`. Ассистента
# нет по той же причине с другой стороны: разговор о состоянии завода не
# производственная задача.
AS_WORKFLOW = {"strategy": "plan", "editor": "post", "reels": "reels",
               "design": "design", "research": "research"}


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

    # ── команды моста: задача уезжает в Claude Code ───────────────────
    # Регистрируется ДО общего обработчика: aiogram отдаёт сообщение
    # первому подошедшему, и ниже стоит фильтр, который ловит любой текст.
    #
    # Отдельные команды, а не фразы в общем разборе, — чтобы новый путь не
    # перехватывал существующую логику. Старые роли остаются доступны
    # ровно как были, и результаты двух путей можно сравнивать.
    #
    # Набор строится из `bridge.WORKFLOWS`, а не переписывается здесь:
    # два списка команд разъехались бы на первой же новой.
    #
    # `@имя_бота` в шаблоне не для красоты. В супергруппе с восемью ботами
    # Telegram дописывает суффикс сам, и прежний `^/plan(\s|$)` такое
    # сообщение не ловил вовсе — оно проваливалось в общий разбор, и вместо
    # моста человеку отвечал моделью Ассистент. Найдено при разборе, живьём
    # не воспроизводилось: лог текст сообщений не пишет.
    _CMDS = "|".join(bridge.WORKFLOWS)

    async def bridge_task(chat_id: int, ask: str, workflow: str,
                          tkey: str) -> None:
        """Довезти задачу до Director и вернуть ответ человеку.

        Один код на два входа — команду `/plan` и обычную просьбу словами.
        Пока это лежало в одном обработчике, второй вход означал бы вторую
        копию ожидания, отказов и журнала, и чинить их пришлось бы парой.
        """
        tenant = db.ensure_tenant(chat_id, cfg.default_tz)
        b = desk.brand(chat_id)

        try:
            task_id = bridge.create_task(
                chat_id, ask, workflow=workflow, today=desk.today(chat_id),
                brand_slug=tenant["brand_slug"] or "",
                brand_path=str(b.root) if b else "")
        except bridge.Busy as e:
            await registry.say("assistant", chat_id,
                               f"Уже работаю над задачей {e}. "
                               "Пока беру по одной за раз.", topic=tkey)
            return

        # Ждать молча минуты нельзя: молчание неотличимо от поломки.
        await registry.say("assistant", chat_id,
                           f"Приняла задачу: {bridge.WORKFLOWS[workflow]}. "
                           "Собираю команду и начинаю работу.\n"
                           f"Задача <code>{task_id}</code>, это займёт "
                           "несколько минут.", topic=tkey)

        res = await bridge.run(task_id)

        if not res.ok:
            await registry.say("assistant", chat_id,
                               f"Не довела задачу {task_id} до конца: "
                               f"{res.error}", topic=tkey)
            return

        await registry.say("assistant", chat_id, res.text, topic=tkey)

        tail = [f"Задача <code>{task_id}</code>, {res.secs} с"]
        if res.cost is not None:
            # Это диагностика CLI, а не счёт: на подписке считаются лимиты.
            tail.append(f"оценка по API-тарифу ${res.cost:.3f}")
        if res.artifacts:
            tail.append("файлы: " + ", ".join(res.artifacts))
        await registry.say("assistant", chat_id, " · ".join(tail),
                           topic="logs", with_label=False)

    @dp_assistant.message(F.text.regexp(rf"^/({_CMDS})(@\S+)?(\s|$)"))
    async def on_bridge(msg: Message) -> None:
        chat_id = msg.chat.id
        if not cfg.chat_allowed(chat_id) or not db.topics_ready(chat_id):
            return
        head, _, ask = (msg.text or "").partition(" ")
        workflow = head[1:].split("@")[0].lower()
        tkey = (topic_key_of(chat_id, msg.message_thread_id)
                or TOPIC.get(workflow, "strategy"))
        await bridge_task(chat_id, ask.strip(), workflow, tkey)

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

        if reels.wants_fix(chat_id):
            await reels.revise(registry, chat_id, raw, topic=tkey or "reels")
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

        # ── просьба словами уходит в мост ─────────────────────────────
        # Критерий MVP — человек пишет «Собери контент-план на неделю», а
        # не команду. Пока входом был только `/plan`, такая фраза уезжала
        # старому Стратегу: `resolve` ловит её по слову «план», и Director
        # о ней не узнавал вовсе.
        #
        # Старый путь остаётся доступен и не переписан: он поднимается
        # по **явному упоминанию** бота роли (`@lily_cf_strategy_bot …`),
        # то есть `reason == "mention"`. Так два пути можно гонять рядом и
        # сравнивать — этап 7 миграции ровно про это.
        #
        # Ответ Ассистента разговором (`role == "assistant"`) сюда не
        # попадает: «покажи ядро» и «что в очереди» это состояние завода,
        # а не производственная задача, и полчаса Claude Code на них
        # тратить нечем.
        if (wf := AS_WORKFLOW.get(route.role)) and route.reason != "mention":
            await bridge_task(chat_id, raw, wf, tkey or TOPIC[wf])
            return

        if route.role == "research":
            await research.run(registry, chat_id, raw, topic=tkey or "research")
            return

        if route.role == "strategy":
            await strategy.run(registry, chat_id, raw, topic=tkey or "strategy")
            return

        if route.role == "editor":
            await editor.run(registry, chat_id, raw, topic=tkey or "review")
            return

        if route.role == "reels":
            await reels.run(registry, chat_id, raw, topic=tkey or "reels")
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

        # Сюда попадать больше некому: все семь ролей разобраны выше.
        # Если роль всё же добавили и забыли ветку, врать «принял» нельзя.
        log.warning("роль %s без обработчика", route.role)
        await registry.say(
            route.role, chat_id,
            "Понял, это ко мне, но я ещё не подключён к работе.\n\n"
            "Пока работает: «покажи ядро», «выгрузи всё».",
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
        if kind == "reel":
            tkey = topic_key_of(chat_id, cb.message.message_thread_id)
            await reels.on_callback(registry, chat_id, action,
                                    topic=tkey or "reels")
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
