"""Бот распаковки: личка с клиентом, который заводу ещё не тенант.

Своё лицо, свой токен, свой диспетчер. Восьмым лицом завода он не
становится намеренно: семь ролей живут в супергруппе бренда, говорят в
топики и работают по собранному профилю. Здесь профиля ещё нет — есть
человек, у которого надо спросить.

Разделение обязанностей такое же, как везде в заводе: разговор здесь,
работа в `orchestrator/intake.py`. Этот файл знает про кнопки и апдейты
и не знает ни про одну границу профиля.

**Личка, а не группа.** Файлы, которые клиент приносит о себе, — это его
стратегия, цены и статистика. В группе их увидит вся команда бренда, и
это решает не бот.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from config import cfg
from orchestrator import agent, files, intake, materials, sources, unpack
from storage import db

log = logging.getLogger("intake-bot")

MAX_UPLOAD_MB = 20

HELLO = (
    "Привет. Я собираю описание бренда, по которому потом работает завод "
    "контента: план недели, тексты, сценарии, макеты.\n\n"
    "<b>Шаг первый — принеси всё, что уже есть.</b> Ссылку на сайт, ссылку "
    "на телеграм-канал, файл с позиционированием, презентацию, стратегию, "
    "старые посты. Годятся pdf, docx, txt, markdown, csv, выгрузка чата. "
    "Можно просто рассказать о себе текстом.\n\n"
    "Читаю сам: телеграм-канал и сайт. Instagram публично не читается — "
    "по нему попрошу выгрузку. LinkedIn и TikTok без входа не отдают "
    "ничего, про них спрошу словами.\n\n"
    "Кидай по одному, я буду подтверждать каждое. Как закончишь — жми "
    "кнопку."
)

READY = "Собрал всё, распаковывай"


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in rows])


READY_KB = _kb([[(READY, "in:unpack")]])


class Face:
    """Один бот, одна отправка. Троттлинга завода тут не нужно: личка."""

    def __init__(self, token: str) -> None:
        self.bot = Bot(token=token,
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.username = ""

    async def say(self, chat_id: int, text: str,
                  kb: InlineKeyboardMarkup | None = None) -> None:
        chunks = _split(text)
        for i, chunk in enumerate(chunks):
            await self.bot.send_message(
                chat_id, chunk, disable_web_page_preview=True,
                reply_markup=kb if i == len(chunks) - 1 else None)


def _split(text: str, limit: int = 3800) -> list[str]:
    """Потолок сообщения 4096 знаков. Рвём по абзацам, не по знакам."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    for para in text.split("\n\n"):
        if out and len(out[-1]) + len(para) + 2 <= limit:
            out[-1] += "\n\n" + para
        else:
            out.append(para[:limit])
    return out


face: Face | None = None


def _state(user_id: int) -> dict:
    return db.intake_state(user_id)


def _save(st: dict, user_id: int, step: str | None = None,
          slug: str | None = None) -> None:
    db.intake_save(user_id, step or st["step"], st["answers"], st["raw"], slug)


# ── шаг 1: материалы ──────────────────────────────────────────────────

async def on_start(msg: Message) -> None:
    st = _state(msg.chat.id)
    _save(st, msg.chat.id, "collect")
    await face.say(msg.chat.id, HELLO, READY_KB)


async def on_message(msg: Message) -> None:
    user_id = msg.chat.id
    st = _state(user_id)
    text = (msg.text or msg.caption or "").strip()
    answers = st["answers"]

    # Сырьё сохраняем всегда и до разбора: после правки промптов распаковку
    # можно прогнать заново, не спрашивая человека второй раз.
    st["raw"].append({
        "step": st["step"], "text": text or None,
        "file_id": msg.document.file_id if msg.document else None,
        "name": msg.document.file_name if msg.document else None,
    })

    if st["step"] == "quiz":
        await _answer_quiz(user_id, st, text)
        return

    if st["step"] == "fix":
        key = answers.pop("awaiting_fix", "")
        _save(st, user_id, "done")
        if key in intake.BY_KEY and text:
            await _rebuild(user_id, key, text)
        else:
            await face.say(user_id, "Не понял, к какому файлу это. "
                                    "Нажмите «Поправить словами» ещё раз.")
        return

    if st["step"] == "deep":
        await _answer_deep(user_id, st, text)
        return

    if st["step"] == "done" and text.lower().strip(" .!") in {
            "усилим", "усиление", "продолжим", "давай усилим", "продолжаем"}:
        await _ask_deep(user_id)
        return

    if st["step"] == "done":
        answers.setdefault("after", []).append(text)
        _save(st, user_id)
        await face.say(user_id, "Профиль уже собран. Что прислал — записал в "
                                "материалы, перечитаю по команде /again.")
        return

    said: list[str] = []

    if msg.document:
        got = await _take_file(user_id, msg.document)
        if got is None:
            return
        item, meta = got
        said.append(item.line())
        if meta:
            answers["meta_export"] = meta
        elif item.ok:
            answers.setdefault("uploads", [])
            if item.name not in answers["uploads"]:
                answers["uploads"].append(item.name)

    if urls := sources.extract_urls(text):
        mine, theirs = intake.sort_links(urls)
        if mine:
            answers["urls"] = sorted(set(answers.get("urls", []) + mine))
            said.append(f"✔️ ссылок на чтение: {len(mine)}")
        for url in theirs:
            answers.setdefault("manual", [])
            if url not in answers["manual"]:
                answers["manual"].append(url)
            if "instagram.com" in url:
                answers.setdefault("instagram", url)
                said.append(intake.ASK_EXPORT)
            else:
                where = url.split("/")[2] if "//" in url else url
                said.append(intake.ASK_MANUAL.format(where=where))

    if text and not urls and not msg.document:
        if len(text) > intake.AS_MATERIAL:
            answers.setdefault("materials", []).append(text)
            said.append(f"✔️ текст на {len(text)} знаков — беру материалом")
        else:
            answers["self_text"] = (answers.get("self_text", "")
                                    + "\n\n" + text).strip()
            said.append("✔️ записал")

    _save(st, user_id, "collect")
    if not said:
        said.append("Не нашёл ни ссылки, ни файла, ни текста. Пришли ещё раз.")
    await face.say(user_id, "\n\n".join(said), READY_KB)


async def _take_file(user_id: int, doc):
    """Скачать и вытащить текст. Возвращает (материал, выгрузка Meta или None).

    Выгрузка отдаётся наружу, а не пишется в состояние здесь: своё чтение
    состояния внутри загрузчика затирается `_save` вызывающего, и CSV
    принимался на словах, не оставаясь в базе.
    """
    name = doc.file_name or f"{doc.file_unique_id}.bin"
    if doc.file_size and doc.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        await face.say(user_id,
                       f"Файл больше {MAX_UPLOAD_MB} МБ — Telegram не отдаёт "
                       "такие ботам. Пришли частями или выжимкой.")
        return None

    buf = await face.bot.download(doc.file_id)
    blob = buf.read()
    folder = materials.stage_dir(user_id)
    (folder / name).write_bytes(blob)

    item = await files.aextract(blob, name)
    if not item.ok:
        return (intake.Material("file", name,
                                note=item.error or "не прочитался"), None)

    if intake.is_meta_export(item.text):
        posts, nick = intake.meta_posts(item.text)
        who = f" @{nick}" if nick else ""
        return (intake.Material("meta-export",
                                f"выгрузка Instagram{who}: {len(posts)} постов"),
                item.text)

    return (intake.Material("file", name, chars=len(item.text)), None)


# ── шаг 2: срез и первичный профиль ───────────────────────────────────

async def on_unpack(cb: CallbackQuery) -> None:
    user_id = cb.message.chat.id
    st = _state(user_id)
    answers = st["answers"]
    urls = answers.get("urls", [])
    names = answers.get("uploads", [])
    self_text = "\n\n".join(filter(None, [answers.get("self_text", ""),
                                          *answers.get("materials", [])]))

    if not (urls or names or self_text):
        await cb.answer("Пока нечего читать", show_alert=True)
        return

    await cb.answer()
    await face.say(user_id, "Читаю. Это пара минут — файлы и каналы целиком, "
                            "а не по верхам.")

    uploads = _stored(user_id, names)
    try:
        draft = await unpack.run(user_id, urls, self_text, uploads)
    except Exception as e:                       # noqa: BLE001
        log.exception("распаковка упала")
        await face.say(user_id, "Распаковка не прошла: "
                                f"{agent.reason(e)}\n\nПопробуй ещё раз "
                                "кнопкой — материалы я не потерял.",
                       READY_KB)
        return

    answers["draft"] = draft.data
    answers["read"] = draft.read
    answers["failed"] = draft.failed
    answers["suggested_name"] = draft.suggested_name
    _save(st, user_id, "draft")

    if not draft.data:
        await face.say(user_id, "Ни один источник не открылся. Что не вышло:\n"
                       + "\n".join(f"— {f}" for f in draft.failed)
                       + "\n\nПришли файлом или расскажи словами.", READY_KB)
        return

    head = ["<b>Вот что я увидел</b>", ""]
    if draft.read:
        head.append("Прочитано: " + "; ".join(draft.read))
    if draft.failed:
        head.append("Не открылось: " + "; ".join(draft.failed))
    body = "\n".join(head) + "\n\n" + draft.card()

    await face.say(user_id, body, _kb([
        [("Верно, спрашивай дальше", "in:quiz")],
        [("Тут неправда, поправлю", "in:fix")],
        [("Перечитай материалы", "in:unpack")],
    ]))


def _stored(user_id: int, names: list[str]) -> list[files.Extracted]:
    folder = materials.stage_dir(user_id)
    out = []
    for name in names:
        path = folder / name
        if path.exists():
            out.append(files.extract(path.read_bytes(), name))
    return out


async def on_fix(cb: CallbackQuery) -> None:
    user_id = cb.message.chat.id
    st = _state(user_id)
    _save(st, user_id, "collect")
    await cb.answer()
    await face.say(user_id, "Напиши, что не так — своими словами, одной "
                            "репликой. Перечитаю вместе с материалами.",
                   READY_KB)


# ── шаг 3: опросник ───────────────────────────────────────────────────

async def on_quiz(cb: CallbackQuery) -> None:
    await cb.answer()
    await _next_question(cb.message.chat.id, first=True)


async def _next_question(user_id: int, *, first: bool = False) -> None:
    st = _state(user_id)
    answers = st["answers"]
    draft = unpack.Draft(data=answers.get("draft", {}),
                         suggested_name=answers.get("suggested_name", ""))

    queue = answers.get("queue", [])
    if not queue and first:
        need = intake.gaps(draft, answers)
        await face.say(user_id, "Теперь добор. "
                       f"Вопросов будет не больше {min(len(need), intake.QUIZ_LIMIT)}, "
                       "каждый на минуту. Не знаешь ответа — так и напиши, "
                       "помечу дырой, а не выдумаю.")
        try:
            questions = await intake.quiz(user_id, draft, answers, need)
        except Exception as e:                   # noqa: BLE001
            log.exception("опросник не собрался")
            await face.say(user_id, f"Опросник не собрался: {agent.reason(e)}")
            return
        queue = [q.__dict__ for q in questions]
        answers["queue"] = queue
        _save(st, user_id, "quiz")

    if not queue:
        await _finish(user_id)
        return

    q = intake.Question(**queue[0])
    _save(st, user_id, "quiz")
    await face.say(user_id, f"<b>{intake.BY_KEY[q.key].title}</b>\n\n{q.text()}",
                   _kb([[("Пропустить", "in:skip")]]))


async def _answer_quiz(user_id: int, st: dict, text: str) -> None:
    answers = st["answers"]
    queue = answers.get("queue", [])
    if not queue:
        _save(st, user_id)
        await _finish(user_id)
        return

    q = queue.pop(0)
    answers.setdefault("quiz", {})[q["ask"]] = text[:intake.ANSWER_CHARS]
    answers["queue"] = queue
    _save(st, user_id)
    await _next_question(user_id)


async def on_skip(cb: CallbackQuery) -> None:
    user_id = cb.message.chat.id
    st = _state(user_id)
    queue = st["answers"].get("queue", [])
    if queue:
        q = queue.pop(0)
        st["answers"].setdefault("quiz", {})[q["ask"]] = "не ответил"
        st["answers"]["queue"] = queue
        _save(st, user_id)
    await cb.answer("Пропустил")
    await _next_question(user_id)


# ── усиление: остальные пункты спеки ──────────────────────────────────

async def on_deep(cb: CallbackQuery) -> None:
    await cb.answer()
    await _ask_deep(cb.message.chat.id)


async def on_enough(cb: CallbackQuery) -> None:
    user_id = cb.message.chat.id
    st = _state(user_id)
    _save(st, user_id, "done")
    left = intake.deep_left(st["answers"])
    await cb.answer()
    await face.say(user_id,
                   "Хорошо. База на месте, завод по ней работает.\n\n"
                   f"Незакрытыми остались {len(left)} вопросов — напишите "
                   "«усилим», когда будет десять минут, продолжу с того же "
                   "места.")


async def _ask_deep(user_id: int) -> None:
    st = _state(user_id)
    left = intake.deep_left(st["answers"])
    if not left:
        await _finish_deep(user_id)
        return

    d = left[0]
    st["answers"]["deep_now"] = d.oid
    _save(st, user_id, "deep")
    body = f"<b>{d.title}</b>\n\n{d.ask}"
    if d.example:
        body += f"\n\n<i>{d.example}</i>"
    await face.say(user_id, f"{body}\n\nОсталось вопросов: {len(left)}",
                   _kb([[("Пропустить", "in:dskip")],
                        [("Хватит на сегодня", "in:enough")]]))


async def _answer_deep(user_id: int, st: dict, text: str) -> None:
    oid = st["answers"].get("deep_now", "")
    if oid:
        st["answers"].setdefault("deep", {})[oid] = text[:intake.ANSWER_CHARS]
        st["answers"].pop("deep_now", None)
    _save(st, user_id)
    await _ask_deep(user_id)


async def on_deep_skip(cb: CallbackQuery) -> None:
    user_id = cb.message.chat.id
    st = _state(user_id)
    if oid := st["answers"].get("deep_now", ""):
        st["answers"].setdefault("deep", {})[oid] = "не ответил"
        st["answers"].pop("deep_now", None)
        _save(st, user_id)
    await cb.answer("Пропустил")
    await _ask_deep(user_id)


async def _finish_deep(user_id: int) -> None:
    """Ответы усиления переписывают те файлы, которых они касаются."""
    st = _state(user_id)
    answers = st["answers"]
    draft = unpack.Draft(data=answers.get("draft", {}),
                         suggested_name=answers.get("suggested_name", ""))

    touched: list[str] = []
    for d in intake.DEEP:
        said = answers.get("deep", {}).get(d.oid, "")
        if said and said != "не ответил" and d.key not in touched:
            touched.append(d.key)
    if not touched:
        await face.say(user_id, "Ничего нового не прибавилось — оставляю как "
                                "есть.")
        return

    # Ответы усиления это те же ответы человека: складываем их туда же,
    # откуда сборка берёт фактуру, с номером пункта в вопросе.
    for d in intake.DEEP:
        if said := answers.get("deep", {}).get(d.oid, ""):
            if said != "не ответил":
                answers.setdefault("quiz", {})[f"{d.title}. {d.ask}"] = said
    _save(st, user_id, "done")

    await face.say(user_id, "Спасибо. Переписываю файлы, которых это "
                            "касается: " + ", ".join(touched) + ".")
    try:
        out = await intake.land(user_id, st, draft, touched)
    except Exception as e:                       # noqa: BLE001
        log.exception("пересборка после усиления упала")
        await face.say(user_id, f"Не вышло: {agent.reason(e)}")
        return

    b = intake.brand_for({"slug": out.slug}, draft.brand_name())
    await face.say(user_id, out.card() or "Изменений не понадобилось.")
    await _show_files(user_id, b, out.files_by_key)


# ── запись файлов ─────────────────────────────────────────────────────

async def _finish(user_id: int) -> None:
    st = _state(user_id)
    answers = st["answers"]
    draft = unpack.Draft(data=answers.get("draft", {}),
                         read=answers.get("read", []),
                         failed=answers.get("failed", []),
                         suggested_name=answers.get("suggested_name", ""))

    await face.say(user_id, "Собираю файлы профиля. Это дольше опросника: "
                            "каждый файл собирается отдельно и проверяется.")
    keys = ["core"] + [k for k in intake.gaps(draft, answers) if k != "core"]
    try:
        out = await intake.land(user_id, st, draft, keys)
    except Exception as e:                       # noqa: BLE001
        log.exception("запись профиля упала")
        await face.say(user_id, f"Не записал: {agent.reason(e)}")
        return

    answers["written"] = sorted(set(answers.get("written", []))
                                | {k for k in keys if k not in out.denied})
    _save(st, user_id, "done", out.slug)

    b = intake.brand_for({"slug": out.slug}, draft.brand_name())
    moved, samples = materials.adopt(user_id, b, st["raw"])
    tail = materials.summary(moved, samples)

    await face.say(user_id, f"<b>Готово.</b> Собрал профиль.\n\n"
                   + out.card() + (f"\n\n{tail}" if tail else "")
                   + "\n\nСейчас покажу каждый файл целиком — читайте и "
                     "правьте. Пока вы их не подтвердили, это черновик.")
    await _show_files(user_id, b, out.files_by_key)
    await _offer_deep(user_id)


async def _show_files(user_id: int, b, written: dict) -> None:
    """Показать собранное текстом, разделами, с кнопками правки.

    Файл на диске человек не откроет и вычитывать markdown не станет.
    Первый живой прогон кончился строкой «записал: пять файлов» — и
    человек не увидел ни одной буквы того, что записано под его именем.
    """
    for key in written:
        for chunk in intake.as_messages(key, b.read(key)):
            await face.say(user_id, chunk)
        await face.say(user_id, "Что с этим делать?", _kb([
            [("Так и оставим", f"in:ok:{key}")],
            [("Поправить словами", f"in:edit:{key}")],
            [("Перепиши заново", f"in:redo:{key}")],
        ]))


async def _offer_deep(user_id: int) -> None:
    st = _state(user_id)
    left = intake.deep_left(st["answers"])
    if not left:
        await face.say(user_id, "Это всё, что я спрашиваю. Папку читает "
                                "завод — сводка, план недели, тексты.")
        return
    await face.say(
        user_id,
        f"<b>База собрана — на ней завод уже работает.</b>\n\n"
        f"Дальше можно усилить: ещё {len(left)} вопросов про голос, цифры, "
        "площадки и оформление. Всего их в полном разборе четырнадцать, "
        f"{intake.SPEC_QUESTIONS - len(left)} вы уже прошли.\n\n"
        "Идут по одному, прерваться можно в любой момент — что успеем, то "
        "и уточню.", _kb([
            [("Давайте усилим", "in:deep")],
            [("Пока хватит", "in:enough")],
        ]))


# ── правка файла словами ──────────────────────────────────────────────

async def on_file_ok(cb: CallbackQuery) -> None:
    key = cb.data.split(":")[-1]
    st = _state(cb.message.chat.id)
    st["answers"].setdefault("confirmed", [])
    if key not in st["answers"]["confirmed"]:
        st["answers"]["confirmed"].append(key)
    _save(st, cb.message.chat.id)
    await cb.answer("Принято")


async def on_file_edit(cb: CallbackQuery) -> None:
    key = cb.data.split(":")[-1]
    user_id = cb.message.chat.id
    st = _state(user_id)
    st["answers"]["awaiting_fix"] = key
    _save(st, user_id, "fix")
    await cb.answer()
    await face.say(user_id,
                   f"Что поправить в «{intake.BY_KEY[key].title}»? Напишите "
                   "своими словами — можно про один абзац, можно про весь "
                   "файл. Соберу заново с вашей правкой.")


async def on_file_redo(cb: CallbackQuery) -> None:
    key = cb.data.split(":")[-1]
    await cb.answer()
    await _rebuild(cb.message.chat.id, key,
                   "Собери заново, другими словами и ближе к тому, что "
                   "человек говорил о себе.")


async def _rebuild(user_id: int, key: str, fix: str) -> None:
    st = _state(user_id)
    answers = st["answers"]
    answers.setdefault("fix", {})[key] = fix
    _save(st, user_id, "done")

    draft = unpack.Draft(data=answers.get("draft", {}),
                         suggested_name=answers.get("suggested_name", ""))
    await face.say(user_id, "Переписываю.")
    try:
        out = await intake.land(user_id, st, draft, [key])
    except Exception as e:                       # noqa: BLE001
        log.exception("пересборка файла упала")
        await face.say(user_id, f"Не вышло: {agent.reason(e)}")
        return

    if key in out.denied:
        await face.say(user_id, "Переписал, но проверку файл не прошёл: "
                       f"{out.denied[key]}. На диске остался прежний — "
                       "скажите иначе, попробую ещё раз.")
        return

    b = intake.brand_for({"slug": out.slug}, draft.brand_name())
    await _show_files(user_id, b, {key: None})


# ── регистрация и запуск ──────────────────────────────────────────────

def register(dp: Dispatcher) -> None:
    private = F.chat.type == "private"
    dp.message.register(on_start, CommandStart(), private)
    dp.message.register(on_start, Command("again"), private)
    dp.message.register(on_message, private)
    dp.callback_query.register(on_unpack, F.data == "in:unpack")
    dp.callback_query.register(on_quiz, F.data == "in:quiz")
    dp.callback_query.register(on_fix, F.data == "in:fix")
    dp.callback_query.register(on_skip, F.data == "in:skip")
    dp.callback_query.register(on_deep, F.data == "in:deep")
    dp.callback_query.register(on_enough, F.data == "in:enough")
    dp.callback_query.register(on_deep_skip, F.data == "in:dskip")
    dp.callback_query.register(on_file_ok, F.data.startswith("in:ok:"))
    dp.callback_query.register(on_file_edit, F.data.startswith("in:edit:"))
    dp.callback_query.register(on_file_redo, F.data.startswith("in:redo:"))


async def run() -> None:
    """Поднять бота и поллить. Токена нет — не поднимаемся вовсе."""
    global face
    if not cfg.intake_token:
        log.info("BOT_INTAKE не задан — распаковщик не поднят")
        return

    face = Face(cfg.intake_token)
    info = await face.bot.get_me()
    face.username = info.username or "intake"
    log.info("📥 Распаковщик → @%s", face.username)

    dp = Dispatcher()
    register(dp)
    try:
        await dp.start_polling(face.bot,
                               allowed_updates=["message", "callback_query"])
    finally:
        await face.bot.session.close()
