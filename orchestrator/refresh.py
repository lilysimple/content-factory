"""Пересборка профиля после онбординга.

Файл или ссылка, присланные в любой момент, перечитываются и уточняют
существующее ЯДРО — а не создают новый бренд. Slug остаётся прежним:
на нём завязаны пути ко всем артефактам.

Текущий профиль уходит в промпт первым, поэтому модель правит его, а не
пишет заново. Подтверждённое человеком не теряется при каждом уточнении.
"""
from __future__ import annotations

import logging

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import cfg
from orchestrator import agent, sources, unpack
from storage import brand as brand_store
from storage import db

log = logging.getLogger("refresh")

store = brand_store.Store(cfg.brands_path)
_pending: dict[int, unpack.Draft] = {}      # черновики, ждущие подтверждения


def _brand(chat_id: int):
    row = db.one("SELECT brand_slug FROM tenants WHERE chat_id = ?", chat_id)
    return store.get(row["brand_slug"]) if row and row["brand_slug"] else None


def wants_refresh(msg: Message) -> bool:
    """Есть ли в сообщении материалы для перечитывания."""
    text = msg.text or msg.caption or ""
    return bool(msg.document or sources.extract_urls(text))


async def start(reg, msg: Message) -> None:
    from orchestrator import onboarding            # общий загрузчик файлов

    chat_id = msg.chat.id
    text = (msg.text or msg.caption or "").strip()
    b = _brand(chat_id)
    if b is None:
        await reg.say("assistant", chat_id,
                      "Профиля ещё нет — сначала пройдём онбординг.")
        return

    names: list[str] = []
    if msg.document:
        item = await onboarding._save_upload(reg, chat_id, msg.document)
        if item is None:
            return
        if not item.ok:
            await reg.say("research", chat_id, item.summary())
            return
        names.append(item.name)

    urls = sources.extract_urls(text)
    what = []
    if urls:
        what.append(f"{len(urls)} ссыл.")
    if names:
        what.append(f"{len(names)} файл.")

    await reg.say("research", chat_id,
                  f"Взял {' и '.join(what)}. Перечитываю профиль — "
                  "вернусь через пару минут.")

    try:
        draft = await unpack.run(
            chat_id, urls, text,
            onboarding._stored_uploads(chat_id, names),
            current=b.read("core"))
    except agent.BudgetExceeded as e:
        await reg.say("research", chat_id, f"Остановился: {e}")
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("пересборка упала")
        await reg.say("research", chat_id,
                      f"Не прочиталось: {type(e).__name__}. "
                      "Профиль остался прежним, ничего не потеряно.")
        return

    if not draft.data:
        await reg.say("research", chat_id,
                      "Из материалов ничего нового не собралось. "
                      "Профиль оставил как был.")
        return

    if draft.read:
        await reg.say("research", chat_id, "Прочитал:\n" +
                      "\n".join(f"· {r}" for r in draft.read), topic="research")
    if draft.failed:
        await reg.say("research", chat_id, "Не открылось:\n" +
                      "\n".join(f"· {f}" for f in draft.failed), topic="research")

    _pending[chat_id] = draft
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Обновить профиль", callback_data="refresh:ok"),
        InlineKeyboardButton(text="❌ Оставить как было", callback_data="refresh:no"),
    ]])
    await reg.say("assistant", chat_id,
                  f"Вот что поменялось после перечитывания.\n\n{draft.card()}",
                  kb=kb)


# ── правка профиля словами ────────────────────────────────────────────

EDIT_WORDS = (
    "обнови", "обновим", "обновить", "поправь", "поправим", "исправь",
    "измени", "перепиши ядро", "добавь стоп-слово", "убери из ядра",
    "уточни профиль", "перепиши профиль", "актуализируй",
)
PROFILE_WORDS = ("ядро", "профиль", "стратеги", "голос", "аудитори",
                 "стоп-слов", "позиционирован", "бренд")


def wants_edit(text: str) -> bool:
    """Просьба поправить профиль словами, без присланных материалов."""
    low = (text or "").lower()
    return (any(w in low for w in EDIT_WORDS)
            and any(w in low for w in PROFILE_WORDS))


async def edit(reg, chat_id: int, instruction: str) -> None:
    """Изменить профиль по указанию человека и показать результат."""
    b = _brand(chat_id)
    if b is None:
        await reg.say("assistant", chat_id, "Профиля ещё нет.")
        return

    current = b.read("core")
    await reg.say("assistant", chat_id, "Понял. Правлю профиль, минуту.")

    try:
        out = await agent.ask(
            "research", chat_id,
            "Ниже профиль бренда и просьба человека его изменить.\n\n"
            "Верни ВЕСЬ файл целиком, в том же формате markdown, с внесённой "
            "правкой. Ничего не выдумывай и не дополняй сверх просьбы: что "
            "она не затрагивает — оставь дословно как есть. Пометки "
            "[уточнить факт] сохрани. Без markdown-обёртки и пояснений, "
            "только содержимое файла.\n\n"
            f"## Просьба\n\n{instruction}\n\n## Текущий профиль\n\n{current}",
            max_tokens=16000)
    except agent.BudgetExceeded as e:
        await reg.say("assistant", chat_id, f"Остановился: {e}")
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("правка не собралась")
        await reg.say("assistant", chat_id,
                      f"Не смог: {type(e).__name__}. Профиль не тронут.")
        return

    text = out.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    if not text.lstrip().startswith("#"):
        await reg.say("assistant", chat_id,
                      "Ответ не похож на файл профиля, ничего не менял.")
        return

    _pending_edit[chat_id] = text
    before, after = len(current), len(text)
    delta = f"{after - before:+d}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Записать", callback_data="edit:ok"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="edit:no"),
    ]])
    await reg.say("assistant", chat_id,
                  f"Готово. Было {before} знаков, стало {after} ({delta}).\n\n"
                  "Записать? Прошлая версия сохранится, откатить можно.",
                  kb=kb)


_pending_edit: dict[int, str] = {}


async def on_edit_callback(reg, chat_id: int, action: str) -> None:
    text = _pending_edit.pop(chat_id, None)
    if text is None:
        await reg.say("assistant", chat_id, "Эта правка уже неактуальна.")
        return
    if action == "no":
        await reg.say("assistant", chat_id, "Отменил, профиль не тронут.")
        return

    b = _brand(chat_id)
    if b is None:
        return
    version = b.write("core", text, reason="правка профиля из чата")
    await reg.say("assistant", chat_id,
                  f"Записал. Версия <code>{version}</code>. "
                  "Посмотреть: «покажи ядро».")


async def on_callback(reg, chat_id: int, action: str) -> None:
    draft = _pending.pop(chat_id, None)
    if draft is None:
        await reg.say("assistant", chat_id,
                      "Этот черновик уже неактуален. Пришли материалы заново.")
        return

    if action == "no":
        await reg.say("assistant", chat_id, "Оставил профиль как был.")
        return

    b = _brand(chat_id)
    if b is None:
        return

    # Slug не трогаем — на нём завязаны пути. Меняется только содержимое
    # и отображаемое имя внутри файла.
    name = draft.brand_name() or b.name()
    core = draft.to_core_md(name, owner=b.owner())
    version = b.write("core", core, reason="профиль уточнён по новым материалам")
    db.set_tenant(chat_id, brand_name=name)

    await reg.say(
        "assistant", chat_id,
        f"Профиль обновлён. Версия <code>{version}</code>.\n\n"
        "Прошлая версия никуда не делась — история хранится, откатить можно.\n"
        "Посмотреть целиком: «покажи ядро».")
