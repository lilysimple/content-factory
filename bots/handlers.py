"""Обработчики обновлений.

Разделение по allowed_updates:
  assistant — message, my_chat_member, chat_member, callback_query
  остальные — только callback_query

callback_query приходит тому боту, который ОТПРАВИЛ сообщение с кнопкой,
поэтому нажатия слушают все семеро, а сообщения только Ассистент.
"""
from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from bots import topics
from bots.registry import registry
from bots.router import is_footage, resolve
from config import cfg
from orchestrator import (bridge, design, desk, editor, montage, onboarding,
                          publisher, reels, refresh, reply, research, strategy)
from orchestrator.desk import NoWork
from storage import db

log = logging.getLogger("handlers")

# Куда отвечать, если команду дали в General: у каждой задачи свой топик.
TOPIC = {"plan": "review", "post": "review", "reels": "reels",
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

# Задача, которая ждёт ответа человека, прежде чем уйти в мост.
# chat_id → (запрос, workflow, топик). Спрашивать в момент прогона нельзя:
# конец хода Director это конец процесса, будить его некому.
_await_events: dict[int, tuple[str, str, str]] = {}

# Человек нажал «Правки» под планом, собранным мостом, и сейчас пишет,
# что поправить. Старый Стратег держит своё ожидание сам
# (`strategy.wants_fix`), у моста роли-хозяина нет — состояние живёт здесь.
_await_plan_fix: dict[int, str] = {}

# То же для текста, собранного мостом: chat_id → (тема, топик). Старый
# Редактор держит своё ожидание в `Desk`, у моста дома для него нет.
_await_post_fix: dict[int, tuple[str, str]] = {}


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

        # Прогрев к вебинару, о котором Стратег не знает, поставить нельзя,
        # а спросить его самого посреди прогона некому. Поэтому спрашиваем
        # здесь, до запуска, и только когда про это окно ещё не спрашивали.
        if workflow == "plan" and not bridge.events_known(chat_id):
            _await_events[chat_id] = (ask, workflow, tkey)
            await registry.say("assistant", chat_id,
                               bridge.events_question(chat_id), topic=tkey)
            return

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

        # Одной фразы на пять минут мало: человек не отличает «идёт работа»
        # от «зависло». Мост зовёт это на каждый вход и выход субагента.
        async def step(text: str) -> None:
            await registry.say("assistant", chat_id, text, topic=tkey,
                               with_label=False)

        res = await bridge.run(task_id, on_step=step)

        if not res.ok:
            await registry.say("assistant", chat_id,
                               f"Не довела задачу {task_id} до конца: "
                               f"{res.error}", topic=tkey)
            return

        # Посаженное согласуется теми же кнопками, что и у старых ролей.
        # Кнопка без посадки была бы обманом: соглашаться не с чем, пока
        # в базе ничего не изменилось.
        #
        # Кнопки выбираются по тому, что **село**, а не по тому, какой
        # workflow объявлен в шапке задачи. Director вправе свернуть план
        # до одного текста, и в прогоне 2026-08-31-plan-04 так и вышло:
        # задача звалась `plan`, отработал Редактор, а кнопок под текстом
        # не было — их искали по слову «post», которого в шапке не стояло.
        # План уходит не туда, где спросили, а в «✍️ На ревью»: черновик
        # живёт там, где с ним спорят, а «🎯 Стратегия» держит только
        # утверждённое. Путь моста и путь старого Стратега кладут его
        # одинаково — `strategy.show_draft` один на двоих.
        if res.plan_ids:
            strategy.remember(chat_id, res.plan_ids)
            await strategy.show_draft(registry, chat_id, res.text,
                                      role="assistant", prefix="bplan")
        else:
            kb = (editor.kb(res.post_ids[0], "bpost")
                  if len(res.post_ids) == 1 else None)
            await registry.say("assistant", chat_id, res.text, topic=tkey,
                               kb=kb)

        # Макет уезжает человеку картинками и файлами, а не строкой в
        # чате: показывает его тот же `design.show`, что и у старого
        # Дизайнера, поэтому и кнопки под ним те же самые.
        if res.landed_obj is not None:
            await design.show(registry, chat_id, res.landed_obj,
                              topic=tkey or "design")

        # Текстов может быть несколько, а клавиатура у сообщения одна:
        # тогда каждая тема получает свою строку со своими кнопками.
        if len(res.post_ids) > 1:
            for tid in res.post_ids:
                await registry.say(
                    "assistant", chat_id,
                    f"Текст по теме <code>{tid}</code> записан, "
                    "тема в статусе draft.",
                    topic=tkey, kb=editor.kb(tid, "bpost"))

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

    # ── видео к сценарию reels ────────────────────────────────────────
    MAX_VIDEO_MB = 20        # потолок скачивания у обычного Bot API

    async def handle_footage(chat_id: int, msg: Message, tkey: str) -> None:
        """Принять отснятое видео и отложить на монтаж.

        Не разбор ролика и не правка: материал ждёт ближайшей фразы вроде
        «смонтируй» — `montage.py` сам подберёт под него утверждённую
        тему, здесь только сохранить байты рядом с профилем бренда.
        """
        b = desk.brand(chat_id)
        if b is None:
            return

        media = msg.video or msg.document
        if media.file_size and media.file_size > MAX_VIDEO_MB * 1024 * 1024:
            await registry.say(
                "reels", chat_id,
                f"Файл больше {MAX_VIDEO_MB} МБ — обычный Bot API такие не "
                "отдаёт. Сожмите или обрежьте дубль и пришлите снова.",
                topic=tkey)
            return

        buf = await registry.bot("reels").download(media.file_id)
        suffix = Path(getattr(media, "file_name", "") or "").suffix or ".mp4"
        montage.stage_video(b, buf.read(), suffix=suffix)
        await registry.say(
            "reels", chat_id,
            "Видео принято. Напишите «смонтируй» — соберу ролик по "
            "ближайшему утверждённому сценарию.", topic=tkey)

    # ── статистика в топике Метрики ───────────────────────────────────
    MAX_STATS_MB = 20        # тот же потолок скачивания у Bot API

    async def handle_stats(chat_id: int, msg: Message) -> bool:
        """Принять скрин кабинета или выгрузку, брошенную в 📊 Метрики.

        Без этой ветки документ уходил в `refresh` и перечитывал профиль
        бренда, а фото не сохранялось вовсе: `msg.photo` вне онбординга не
        обрабатывал никто. Ресёрчер читает эту папку сам — `research.snapshot`
        называет ему файлы в разделе «Скрины статистики».
        """
        b = desk.brand(chat_id)
        if b is None:
            return False

        media = msg.document or (msg.photo[-1] if msg.photo else None)
        if media is None:
            return False
        if media.file_size and media.file_size > MAX_STATS_MB * 1024 * 1024:
            await registry.say(
                "research", chat_id,
                f"Файл больше {MAX_STATS_MB} МБ — Telegram не отдаёт такие "
                "ботам. Пришлите выжимкой или частями.", topic="metrics")
            return True

        buf = await registry.bot("research").download(media.file_id)
        name = getattr(media, "file_name", None) or (
            f"{desk.today(chat_id)}-{media.file_unique_id}.jpg")
        path = research.stash_stats(b, buf.read(), name)
        await registry.say(
            "research", chat_id,
            f"Забрал <code>{path.name}</code> в статистику бренда. "
            "Разберу в ближайшей сводке — напишите «собери сводку», "
            "когда неделя закроется.", topic="metrics")
        return True

    # ── фото в топике Фотобанк ────────────────────────────────────────
    MAX_PHOTO_MB = 20        # тот же потолок скачивания у Bot API

    async def handle_photo(chat_id: int, msg: Message) -> bool:
        """Принять снимок, брошенный в 📸 Фотобанк.

        Топик стоял в структуре с первого дня и не делал ничего: фото из
        него уходило в `refresh` перечитывать профиль, а фотобанк
        пополнялся только с ноутбука, из альбома «Фото»
        (`tools/photos_pull.py`). Снято при этом на телефон, и человек
        оттуда же и кидает.
        """
        b = desk.brand(chat_id)
        if b is None:
            return False

        doc = msg.document
        if doc is not None and not (doc.mime_type or "").startswith("image/"):
            # Не картинка — значит материал профиля, и разбирать его
            # должен `refresh`, а не фотобанк.
            return False
        media = doc or (msg.photo[-1] if msg.photo else None)
        if media is None:
            return False
        if media.file_size and media.file_size > MAX_PHOTO_MB * 1024 * 1024:
            await registry.say(
                "design", chat_id,
                f"Файл больше {MAX_PHOTO_MB} МБ — Telegram не отдаёт такие "
                "ботам. Пришлите пожатым.", topic="photos")
            return True

        buf = await registry.bot("design").download(media.file_id)
        name = (msg.caption or "").strip() or Path(
            getattr(media, "file_name", "") or "").stem
        try:
            got = design.stash_photo(
                b, buf.read(), name=name,
                key=f"tg:{media.file_unique_id}",
                suffix=Path(getattr(media, "file_name", "") or "").suffix)
        except NoWork as e:
            await registry.say("design", chat_id, str(e), topic="photos")
            return True

        if got.seen:
            await registry.say(
                "design", chat_id,
                f"Это фото уже в банке: <code>{got.name}</code>. Дублем не "
                "кладу — на дублях слепнет ротация фона.", topic="photos")
            return True

        note = ""
        if got.side < design.PHOTO_MIN:
            # Фото, отправленное картинкой, телеграм жмёт до 1280 по
            # длинной стороне, а холст снимается вдвое крупнее. Сказать
            # надо сейчас, пока человек у телефона и может переслать
            # файлом: на обложке это видно, а в чате уже поздно.
            note = (f" Только длинная сторона {got.side} точек — на обложке "
                    "будет мылом. Под фон пришлите тот же кадр файлом.")
        await registry.say(
            "design", chat_id,
            f"Забрал в фотобанк: <code>{got.name}</code>, всего "
            f"{got.total} фото.{note} Под какую цель ставить — "
            "строкой в <code>design/photos.md</code>; что не вписано, идёт "
            "ротацией по всей папке.", topic="photos")
        return True

    # ── обычное сообщение ─────────────────────────────────────────────
    @dp_assistant.message(F.text | F.voice | F.photo | F.document | F.video)
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

        if msg.video or (msg.document and (msg.document.mime_type or "")
                        .startswith("video/")):
            await handle_footage(chat_id, msg, tkey or "reels")
            return

        # Файл в 📊 Метрики это цифры, а не материал профиля: разбирать
        # его должен Ресёрчер, а не распаковка ЯДРА.
        if tkey == "metrics" and (msg.photo or msg.document):
            if await handle_stats(chat_id, msg):
                return

        # Фото в 📸 Фотобанк это сырьё для обложек, а не материал профиля.
        if tkey == "photos" and (msg.photo or msg.document):
            if await handle_photo(chat_id, msg):
                return

        # Ответ на вопрос про события недели: он не новая задача, а
        # недостающий факт для той, что уже ждёт запуска.
        if chat_id in _await_events:
            pending_ask, wf, ekey = _await_events.pop(chat_id)
            has = bridge.save_events(chat_id, raw)
            await registry.say(
                "assistant", chat_id,
                ("Записала события в <code>plans/events.md</code>, "
                 "прогрев поставлю от них." if has else
                 "Хорошо, неделя без событий — планирую без прогрева."),
                topic=ekey)
            await bridge_task(chat_id, pending_ask, wf, ekey)
            return

        # Правка к тексту, собранному мостом. Тот же порядок, что у плана:
        # договорить с субагентом нельзя, поэтому правка это новый прогон.
        if chat_id in _await_post_fix:
            tid, pkey = _await_post_fix.pop(chat_id)
            await bridge_task(chat_id,
                              f"Правка к тексту темы {tid}: {raw}",
                              "post", pkey)
            return

        # Правка к плану, собранному мостом. Пересборка это новый прогон:
        # субагент живёт ровно один ход, договорить с ним нельзя.
        if chat_id in _await_plan_fix:
            fkey = _await_plan_fix.pop(chat_id)
            strategy.forget(chat_id)
            await bridge_task(chat_id, f"Правка к прошлому плану: {raw}",
                              "plan", fkey)
            return

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

        if montage.wants_fix(chat_id):
            await montage.revise(registry, chat_id, raw, topic=tkey or "reels")
            return

        if publisher.wants_reason(chat_id):
            await publisher.take_reason(registry, chat_id, raw,
                                        topic=tkey or "queue")
            return

        # Материалы после онбординга уточняют существующий профиль,
        # а не создают новый бренд.
        #
        # Исключение — ссылка на запись в топике Reels: это материал на
        # монтаж, и `router.resolve` разводит её тем же фактом. Но
        # `wants_refresh` срабатывает на **любую** ссылку и стоит раньше
        # маршрутизации, поэтому до монтажа такая ссылка не доезжала
        # вовсе: человек кидал дубль в Reels и получал перечитывание
        # профиля Ресёрчером.
        if refresh.wants_refresh(msg) and not is_footage(raw, tkey):
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

        if route.role == "montage":
            # Замена словами под готовой карточкой — правка субтитров:
            # тот же ролик, тот же дубль, другие слова.
            if montage.wants_relex(chat_id, raw):
                await montage.revise(registry, chat_id, raw,
                                     topic=tkey or "reels")
                return
            # Одна запись на несколько роликов — отдельная работа: там
            # зовётся Редактор Reels, а монтаж режет по его списку.
            if montage.wants_split(raw):
                await montage.run_split(registry, chat_id, raw,
                                        topic=tkey or "reels")
            else:
                await montage.run(registry, chat_id, raw, topic=tkey or "reels")
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
        if kind == "bplan":
            tkey = (topic_key_of(chat_id, cb.message.message_thread_id)
                    or "strategy")
            ids = strategy.batch(chat_id)
            if not ids:
                await registry.say("assistant", chat_id,
                                   "Этот план уже неактуален, собери заново.",
                                   topic=tkey)
                return
            if action == "ok":
                # Темы остаются `idea`: утверждён смысл, текстов ещё нет.
                strategy.remember(chat_id, [])
                await strategy.approve(
                    registry, chat_id, ids, role="assistant",
                    nudge="Дальше за текстами: скажи «напиши пост», "
                          "и возьму ближайшую тему.")
                return
            if action == "fix":
                _await_plan_fix[chat_id] = tkey
                await registry.say(
                    "assistant", chat_id,
                    "Напиши одним сообщением, что поправить: тему, день, "
                    "перекос по воронке. Пересоберу план — это новый "
                    "прогон, займёт несколько минут.", topic=tkey)
                return
            if action == "redo":
                dropped = strategy.forget(chat_id)
                await registry.say(
                    "assistant", chat_id,
                    f"Убрала прошлый батч ({dropped} тем), собираю другой.",
                    topic=tkey)
                await bridge_task(
                    chat_id, "Прошлый батч не подошёл. Дай другие темы: "
                             "другие углы и другие заходы, не перестановку "
                             "тех же.", "plan", tkey)
            return

        if kind == "bpost":
            tkey = (topic_key_of(chat_id, cb.message.message_thread_id)
                    or "review")
            act, _, tid = action.partition(":")
            theme = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                           tid, chat_id)
            if theme is None:
                await registry.say("assistant", chat_id,
                                   "Этой темы уже нет в плане.", topic=tkey)
                return
            if act == "fix":
                _await_post_fix[chat_id] = (tid, tkey)
                await registry.say(
                    "assistant", chat_id,
                    "Напиши одним сообщением, что поправить. Перепишу "
                    "текст — это новый прогон, займёт несколько минут.",
                    topic=tkey)
                return
            if act not in {"ok", "design"}:
                return
            # «В дизайн» это и приёмка текста тоже: Дизайнер работает
            # только с `ready`, верстать неутверждённое незачем.
            desk.ready(chat_id, tid)
            if act == "ok":
                await registry.say(
                    "assistant", chat_id,
                    f"Готово, <code>{tid}</code> в статусе ready. "
                    + editor.handoff(dict(theme)), topic=tkey)
                return
            await registry.say("assistant", chat_id,
                               f"Приняла текст <code>{tid}</code>, "
                               "передаю в вёрстку.", topic=tkey)
            await bridge_task(chat_id, f"свёрстай макет по теме {tid}",
                              "design", "design")
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
        if kind == "mont":
            tkey = topic_key_of(chat_id, cb.message.message_thread_id)
            await montage.on_callback(registry, chat_id, action,
                                      topic=tkey or "reels")
            return
        await onboarding.on_callback(registry, cb, role)

    dp_assistant.callback_query()(on_callback)
    dp_workers.callback_query()(on_callback)
