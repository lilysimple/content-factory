"""Онбординг: от добавления ботов до подтверждённого ЯДРА.

Реализовано: подключение команды, O0 маска, O1 ссылки, O2 распаковка в фоне,
O3 цель, O8 три спорных места, запись ЯДРА на диск.

Правило из спеки: файл пишется только по подтверждённому блоку. Всё
несогласованное живёт в onboarding.raw_inputs_json и answers_json.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from bots.registry import ROLES
from config import cfg
from orchestrator import agent, files, materials, sources, unpack
from orchestrator.personas import PERSONAS, default_persona
from storage import brand as brand_store
from storage import db

log = logging.getLogger("onboarding")

CREW = ["strategy", "editor", "research", "design", "reels", "publisher"]
store = brand_store.Store(cfg.brands_path)

GOALS = {
    "sales":  "💰 К заявкам",
    "growth": "📈 К аудитории",
    "expert": "🎓 К репутации",
    "launch": "🚀 К запуску",
}


# ── вспомогательное ───────────────────────────────────────────────────

def _state(chat_id: int) -> dict[str, Any]:
    return db.onboarding_state(chat_id)


def _save(chat_id: int, st: dict[str, Any], step: str | None = None) -> None:
    db.onboarding_save(chat_id, step or st["step"], st["answers"], st["raw"])


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in rows])


def _crew_links() -> str:
    out = []
    for role in CREW:
        if username := _me().get(role):
            out.append(f'{ROLES[role].label} → '
                       f'<a href="https://t.me/{username}?startgroup=true">добавить</a>')
    return "\n".join(out)


_registry = None


def _me() -> dict[str, str]:
    return _registry.me if _registry else {}


# ── вход ──────────────────────────────────────────────────────────────

async def start(reg, chat_id: int, created: int) -> None:
    global _registry
    _registry = reg
    db.set_tenant(chat_id, status="onboarding")
    _state(chat_id)

    head = (f"Собрал структуру: {created} топиков. Настраивать ничего не надо."
            if created else "Структура уже на месте.")

    await reg.say("assistant", chat_id,
                  f"{head}\n\nОсталось позвать команду. Шесть ссылок, "
                  f"каждая в один тап.\n\n{_crew_links()}\n\n"
                  "Как все зайдут, напиши «команда на месте».")


async def crew_joined(reg, chat_id: int, username: str) -> None:
    st = _state(chat_id)
    seen = set(st["answers"].get("crew_seen", []))
    seen.add(username)
    st["answers"]["crew_seen"] = sorted(seen)
    _save(chat_id, st)
    log.info("зашёл @%s — %s из %s", username, len(seen), len(CREW))


# ── O0: маска ─────────────────────────────────────────────────────────

def _persona_kb() -> InlineKeyboardMarkup:
    rows = [[(p.label, f"persona:{p.id}")] for p in PERSONAS.values()]
    rows.append([("Задать своего", "persona:custom")])
    return _kb(rows)


async def ask_persona(reg, chat_id: int) -> None:
    st = _state(chat_id)
    _save(chat_id, st, "O0")
    await reg.say(
        "assistant", chat_id,
        "Команда в сборе. Прежде чем начать — кем мне быть, пока мы "
        "разбираемся?\n\nЭто влияет только на то, как я разговариваю. "
        "Тексты и визуал делаются в голосе твоего бренда, не в моём.",
        kb=_persona_kb())


# ── O1: ссылки и рассказ ──────────────────────────────────────────────

async def ask_intro(reg, chat_id: int, persona_id: str) -> None:
    persona = PERSONAS.get(persona_id, default_persona())
    st = _state(chat_id)
    st["answers"]["persona"] = persona.id
    _save(chat_id, st, "O1")
    db.set_tenant(chat_id, persona=persona.id)
    await reg.say("assistant", chat_id, f"{persona.tagline}\n\n{persona.intro}")


# ── O2: распаковка в фоне ─────────────────────────────────────────────

MAX_UPLOAD_MB = 20          # потолок скачивания у Bot API


async def _save_upload(reg, chat_id: int, doc) -> files.Extracted | None:
    """Скачать документ и вытащить из него текст."""
    if doc.file_size and doc.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        await reg.say("research", chat_id,
                      f"Файл больше {MAX_UPLOAD_MB} МБ — Telegram не отдаёт "
                      "такие ботам. Пришли частями или выжимкой.")
        return None

    folder = materials.stage_dir(chat_id)
    name = doc.file_name or f"{doc.file_unique_id}.bin"

    buf = await reg.bot("assistant").download(doc.file_id)
    blob = buf.read()
    (folder / name).write_bytes(blob)

    item = files.extract(blob, name)
    log.info("файл %s → %s", name, item.summary())
    return item


def _stored_uploads(chat_id: int, names: list[str]) -> list[files.Extracted]:
    """Перечитать сохранённые файлы. Состояние переживает перезапуск."""
    folder = materials.stage_dir(chat_id)
    out = []
    for name in names:
        path = folder / name
        if path.exists():
            out.append(files.extract(path.read_bytes(), name))
    return out


async def _unpack_task(reg, chat_id: int, urls: list[str], text: str,
                       upload_names: list[str] | None = None) -> None:
    """Читает источники и собирает черновик. Идёт параллельно с O3."""
    uploads = _stored_uploads(chat_id, upload_names or [])
    try:
        draft = await unpack.run(chat_id, urls, text, uploads)
    except agent.BudgetExceeded as e:
        await reg.say("research", chat_id, f"Остановился: {e}")
        return
    except Exception as e:                                  # noqa: BLE001
        log.exception("распаковка упала")
        await reg.say("research", chat_id,
                      f"Распаковка не прошла: {type(e).__name__}. "
                      "Профиль соберём вопросами, ничего не потеряно.")
        return

    st = _state(chat_id)
    st["answers"]["draft"] = draft.data
    st["answers"]["read"] = draft.read
    st["answers"]["failed"] = draft.failed
    st["answers"]["suggested_name"] = draft.suggested_name
    _save(chat_id, st)

    if draft.read:
        await reg.say("research", chat_id, "Прочитал:\n" +
                      "\n".join(f"· {r}" for r in draft.read), topic="research")
    if draft.failed:
        await reg.say("research", chat_id, "Не открылось:\n" +
                      "\n".join(f"· {f}" for f in draft.failed), topic="research")

    if not draft.data:
        await reg.say("assistant", chat_id,
                      "Из источников профиль не собрался. Соберём вопросами.")
        return

    await _show_draft(reg, chat_id)


async def _show_draft(reg, chat_id: int) -> None:
    st = _state(chat_id)
    draft = unpack.Draft(data=st["answers"].get("draft", {}),
                         read=st["answers"].get("read", []),
                         failed=st["answers"].get("failed", []))
    _save(chat_id, st, "O8")

    n = len(draft.disputed)
    tail = (f"\n\n{n} места я бы уточнил. Остальное можешь не вычитывать."
            if n else "")
    await reg.say("assistant", chat_id,
                  f"Собрал. Проверь.\n\n{draft.card()}{tail}",
                  kb=_kb([[("✅ Верно", "draft:ok")],
                          [("❓ Что за места", "draft:ask")],
                          [("✏️ Поправить", "draft:fix")]]))


# ── O3: цель ──────────────────────────────────────────────────────────

async def ask_goal(reg, chat_id: int) -> None:
    await reg.say(
        "assistant", chat_id,
        "Пока он читает — вопрос, которого в постах не найти.\n"
        "Ты ведёшь блог. А блог тебя куда ведёт?",
        kb=_kb([[(GOALS["sales"], "goal:sales"), (GOALS["growth"], "goal:growth")],
                [(GOALS["expert"], "goal:expert"), (GOALS["launch"], "goal:launch")]]))


# ── O8: три спорных места ─────────────────────────────────────────────

async def _ask_disputed(reg, chat_id: int, idx: int) -> None:
    st = _state(chat_id)
    draft = unpack.Draft(data=st["answers"].get("draft", {}))
    items = draft.disputed

    if idx >= len(items):
        await _finish(reg, chat_id)
        return

    st["answers"]["disputed_idx"] = idx
    _save(chat_id, st, "O8q")

    q = items[idx]
    kb = None
    if opts := q.get("options"):
        kb = _kb([[(o, f"disp:{idx}:{i}")] for i, o in enumerate(opts[:4])])

    await reg.say("assistant", chat_id,
                  f"{idx + 1} из {len(items)}. {q['question']}", kb=kb)


# ── запись профиля ────────────────────────────────────────────────────

async def _finish(reg, chat_id: int) -> None:
    st = _state(chat_id)
    data = st["answers"].get("draft", {})
    draft = unpack.Draft(data=data,
                         read=st["answers"].get("read", []),
                         failed=st["answers"].get("failed", []),
                         suggested_name=st["answers"].get("suggested_name", ""))
    name = draft.brand_name()

    tenant = db.one("SELECT brand_slug FROM tenants WHERE chat_id = ?", chat_id)
    if tenant and tenant["brand_slug"]:
        b = store.get(tenant["brand_slug"])
    else:
        b = store.create(name)
        db.set_tenant(chat_id, brand_slug=b.slug, brand_name=name)

    answers = {k: v for k, v in st["answers"].items()
               if k in {"goal", "disputed_answers"}}
    core = draft.to_core_md(name)
    if answers.get("goal"):
        core += f"\n\n## Цель этапа\n\n{GOALS.get(answers['goal'], answers['goal'])}\n"
    if ans := answers.get("disputed_answers"):
        core += "\n\n## Уточнено на онбординге\n\n" + \
                "\n".join(f"- **{q}** {a}" for q, a in ans)

    version = b.write("core", core, reason="подтверждено ЯДРО на онбординге")
    moved, samples = materials.adopt(chat_id, b)
    if moved or samples:
        b.write("core", core, reason="исходники перенесены в бренд")
    _save(chat_id, st, "O9")
    # Без этого тенант навсегда остаётся в онбординге: каждое следующее
    # сообщение уходит в анкету, а маршрутизация по ролям не включается.
    db.set_tenant(chat_id, status="ready")

    await reg.say(
        "assistant", chat_id,
        f"Записал. Профиль <code>{b.slug}</code>, версия <code>{version}</code>.\n"
        f"{materials.summary(moved, samples)}\n\n"
        "Дальше идут цифры, площадки и оформление — они ещё не подключены, "
        "это следующий коммит.\n\n"
        "Команды, которые уже работают: «покажи ядро», «выгрузи всё».")


# ── приём сообщений ───────────────────────────────────────────────────

async def handle(reg, msg: Message) -> None:
    global _registry
    _registry = reg
    chat_id = msg.chat.id
    st = _state(chat_id)
    text = (msg.text or msg.caption or "").strip()

    if text.lower() in {"команда на месте", "все на месте"}:
        await ask_persona(reg, chat_id)
        return

    if st["step"] == "O0":
        await reg.say("assistant", chat_id, "Сначала выбери, кем мне быть.",
                      kb=_persona_kb())
        return

    # Сырьё сохраняем всегда: после правки промптов распаковку можно
    # переизвлечь, не спрашивая человека заново.
    st["raw"].append({
        "step": st["step"], "text": text or None,
        "kind": ("voice" if msg.voice else "photo" if msg.photo else
                 "document" if msg.document else "text"),
        "file_id": (msg.voice.file_id if msg.voice else
                    msg.document.file_id if msg.document else
                    msg.photo[-1].file_id if msg.photo else None),
    })
    _save(chat_id, st)

    if st["step"] == "O8q":                      # свободный ответ на спорное
        idx = st["answers"].get("disputed_idx", 0)
        await _record_disputed(reg, chat_id, idx, text)
        return

    # Ссылки и файлы запускают распаковку на любом шаге, пока профиль не
    # собран. Первая попытка могла упасть — сеть, лимит, отказ модели — и
    # человек просто присылает материалы ещё раз. Это самый естественный
    # повтор, и привязывать его к одному шагу значит терять сообщение.
    if not st["answers"].get("draft"):
        names: list[str] = st["answers"].get("uploads", [])

        if msg.document:
            item = await _save_upload(reg, msg.chat.id, msg.document)
            if item is None:
                return
            if not item.ok:
                await reg.say("research", chat_id, item.summary())
                return
            if item.name not in names:
                names.append(item.name)
            st["answers"]["uploads"] = names
            _save(chat_id, st)

        urls = sources.extract_urls(text)
        if urls or (msg.document and names):
            what = []
            if urls:
                what.append(f"{len(urls)} ссыл.")
            if names:
                what.append(f"{len(names)} файл.")
            again = "ещё раз " if st["step"] != "O1" else ""
            await reg.say("research", chat_id,
                          f"Взял {' и '.join(what)}. Читаю {again}— вернусь "
                          "через пару минут.")
            asyncio.create_task(
                _unpack_task(reg, chat_id, urls, text, names))
            if st["step"] == "O1":
                _save(chat_id, st, "O3")
                await ask_goal(reg, chat_id)
            return

        if st["step"] == "O1":
            await reg.say("assistant", chat_id,
                          "Нужна хотя бы одна ссылка: канал, сайт или пост. "
                          "Подойдёт и @имя канала.")
            return

    # Молчание хуже отказа: человек не понимает, дошло сообщение или нет.
    await reg.say("assistant", chat_id,
                  "Записал. Сейчас жду ответа на вопрос выше — нажми кнопку "
                  "или пришли ссылки, если хочешь, чтобы я перечитал.")


async def _record_disputed(reg, chat_id: int, idx: int, answer: str) -> None:
    st = _state(chat_id)
    draft = unpack.Draft(data=st["answers"].get("draft", {}))
    items = draft.disputed
    if idx < len(items):
        st["answers"].setdefault("disputed_answers", []).append(
            [items[idx]["question"], answer])
    _save(chat_id, st)
    await _ask_disputed(reg, chat_id, idx + 1)


async def send_files(reg, chat_id: int, *, zip_it: bool) -> None:
    from aiogram.types import BufferedInputFile

    row = db.one("SELECT brand_slug FROM tenants WHERE chat_id = ?", chat_id)
    b = store.get(row["brand_slug"]) if row and row["brand_slug"] else None
    if b is None:
        await reg.say("assistant", chat_id, "Профиля пока нет, он соберётся "
                                            "к концу онбординга.")
        return

    bot = reg.bot("assistant")
    thread = db.topic_id(chat_id, "general")
    kw = {"chat_id": chat_id}
    if thread is not None:
        kw["message_thread_id"] = thread

    if zip_it:
        name, blob = b.export_zip()
        await bot.send_document(document=BufferedInputFile(blob, name),
                                caption="Всё, что о тебе собрано.", **kw)
    else:
        await bot.send_document(
            document=BufferedInputFile(b.read("core").encode(), "core.md"),
            caption=f"ЯДРО, версия {b.version()}", **kw)


# ── кнопки ────────────────────────────────────────────────────────────

async def on_callback(reg, cb: CallbackQuery, role: str) -> None:
    global _registry
    _registry = reg
    if cb.message is None or not cb.data:
        return
    chat_id = cb.message.chat.id
    kind, _, rest = cb.data.partition(":")

    if kind == "persona":
        if rest == "custom":
            await reg.say("assistant", chat_id,
                          "Тогда три вопроса. Как её зовут?")
            return
        await ask_intro(reg, chat_id, rest)

    elif kind == "goal":
        st = _state(chat_id)
        st["answers"]["goal"] = rest
        _save(chat_id, st)
        if rest in {"sales", "launch"}:
            await reg.say("assistant", chat_id,
                          "Заявки на что? Одним предложением, без вводных.")
        else:
            await reg.say("assistant", chat_id, "Принял. Жду ресёрчера.")

    elif kind == "draft":
        if rest == "ok":
            await _finish(reg, chat_id)
        elif rest == "ask":
            await _ask_disputed(reg, chat_id, 0)
        else:
            await reg.say("assistant", chat_id,
                          "Скажи, что поправить. Одной фразой.")

    elif kind == "disp":
        idx_s, _, opt_s = rest.partition(":")
        st = _state(chat_id)
        draft = unpack.Draft(data=st["answers"].get("draft", {}))
        items = draft.disputed
        idx = int(idx_s)
        if idx < len(items):
            opts = items[idx].get("options") or []
            answer = opts[int(opt_s)] if opt_s.isdigit() and int(opt_s) < len(opts) else opt_s
            await _record_disputed(reg, chat_id, idx, answer)


async def ask_which_role(reg, chat_id: int) -> None:
    await reg.say("assistant", chat_id,
                  "Не понял, что именно нужно. Уточни одной кнопкой.",
                  kb=_kb([[("✍️ Пост", "pick:editor"), ("🎬 Рилс", "pick:reels"),
                           ("🎯 Тему", "pick:strategy")]]))
