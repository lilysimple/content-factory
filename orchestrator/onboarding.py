"""Онбординг: от добавления ботов до собранного ЯДРА.

Пока реализованы шаги подключения команды, O0 (выбор маски) и O1 (ссылки и
рассказ о себе). Дальше — заглушка с явной пометкой, чтобы не создавать
ощущение, что профиль собран.

Правило из спеки: файл на диск пишется только по подтверждённому блоку.
Всё несогласованное живёт в onboarding.raw_inputs_json.
"""
from __future__ import annotations

import logging

from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from bots.registry import ROLES, registry
from orchestrator.personas import PERSONAS, default_persona
from storage import db

log = logging.getLogger("onboarding")

CREW = ["strategy", "editor", "research", "design", "reels", "publisher"]


def _crew_links() -> str:
    lines = []
    for role in CREW:
        username = registry.me.get(role)
        if not username:
            continue
        url = f"https://t.me/{username}?startgroup=true"
        lines.append(f'{ROLES[role].label} → <a href="{url}">добавить</a>')
    return "\n".join(lines)


def _crew_present() -> int:
    return sum(1 for role in CREW if role in registry.bots)


# ── вход ──────────────────────────────────────────────────────────────

async def start(reg, chat_id: int, created: int) -> None:
    db.set_tenant(chat_id, status="onboarding")
    db.onboarding_state(chat_id)

    head = (f"Собрал структуру: {created} топиков. Настраивать ничего не надо."
            if created else "Структура уже на месте.")

    await reg.say("assistant", chat_id,
                  f"{head}\n\nОсталось позвать команду. Шесть ссылок, "
                  f"каждая в один тап.\n\n{_crew_links()}\n\n"
                  f"Как все зайдут — начнём.\n\n"
                  f"Если они уже здесь, напиши «команда на месте».")


async def crew_joined(reg, chat_id: int, username: str) -> None:
    """Кто-то из ботов зашёл в группу. Отмечаем прогресс."""
    state = db.onboarding_state(chat_id)
    if state["step"] != "O0-crew":
        seen = set(state["answers"].get("crew_seen", []))
        seen.add(username)
        state["answers"]["crew_seen"] = sorted(seen)
        db.onboarding_save(chat_id, state["step"], state["answers"], state["raw"])
        log.info("в группу зашёл @%s (%s из %s)", username, len(seen), len(CREW))


# ── O0: выбор маски ───────────────────────────────────────────────────

def _persona_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=p.label, callback_data=f"persona:{p.id}")]
        for p in PERSONAS.values()
    ]
    rows.append([InlineKeyboardButton(text="Задать своего",
                                      callback_data="persona:custom")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ask_persona(reg, chat_id: int) -> None:
    state = db.onboarding_state(chat_id)
    db.onboarding_save(chat_id, "O0", state["answers"], state["raw"])
    await reg.say(
        "assistant", chat_id,
        "Команда в сборе. Прежде чем начать — кем мне быть, пока мы "
        "разбираемся?\n\nЭто влияет только на то, как я разговариваю. "
        "Тексты и визуал делаются в голосе твоего бренда, не в моём.",
        kb=_persona_kb())


# ── O1: ссылки и рассказ ──────────────────────────────────────────────

async def ask_intro(reg, chat_id: int, persona_id: str) -> None:
    persona = PERSONAS.get(persona_id, default_persona())
    state = db.onboarding_state(chat_id)
    state["answers"]["persona"] = persona.id
    db.onboarding_save(chat_id, "O1", state["answers"], state["raw"])
    db.set_tenant(chat_id, persona=persona.id)

    await reg.say("assistant", chat_id,
                  f"{persona.tagline}\n\n{persona.intro}")


# ── приём ответов ─────────────────────────────────────────────────────

async def handle(reg, msg: Message) -> None:
    chat_id = msg.chat.id
    state = db.onboarding_state(chat_id)
    text = (msg.text or "").strip()

    if text.lower() in {"команда на месте", "все на месте"}:
        await ask_persona(reg, chat_id)
        return

    if state["step"] == "O0":
        await reg.say("assistant", chat_id, "Сначала выбери, кем мне быть.",
                      kb=_persona_kb())
        return

    # Сырьё сохраняем всегда: после правки промптов распаковку можно
    # переизвлечь, не спрашивая человека заново.
    state["raw"].append({
        "step": state["step"],
        "text": text or None,
        "kind": ("voice" if msg.voice else
                 "photo" if msg.photo else
                 "document" if msg.document else "text"),
        "file_id": (msg.voice.file_id if msg.voice else
                    msg.document.file_id if msg.document else
                    msg.photo[-1].file_id if msg.photo else None),
    })
    db.onboarding_save(chat_id, state["step"], state["answers"], state["raw"])

    if state["step"] == "O1":
        await reg.say("research", chat_id,
                      "Взял. Читаю — вернусь через пару минут.")
        await reg.say(
            "assistant", chat_id,
            "Дальше идут шаги O2–O15: распаковка, голос, референсы, цифры, "
            "площадки, оформление и калибровка.\n\n"
            "Они ещё не подключены — это следующий коммит. Всё, что ты "
            "прислала, сохранено и не потеряется.")
        db.onboarding_save(chat_id, "O2", state["answers"], state["raw"])


# ── кнопки ────────────────────────────────────────────────────────────

async def on_callback(reg, cb: CallbackQuery, role: str) -> None:
    if cb.message is None or not cb.data:
        return
    chat_id = cb.message.chat.id

    if cb.data.startswith("persona:"):
        pid = cb.data.split(":", 1)[1]
        if pid == "custom":
            await reg.say("assistant", chat_id,
                          "Тогда три вопроса. Как её зовут?")
            return
        await ask_intro(reg, chat_id, pid)


async def ask_which_role(reg, chat_id: int) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Пост",  callback_data="pick:editor"),
        InlineKeyboardButton(text="🎬 Рилс",  callback_data="pick:reels"),
        InlineKeyboardButton(text="🎯 Тему",  callback_data="pick:strategy"),
    ]])
    await reg.say("assistant", chat_id,
                  "Не понял, что именно нужно. Уточни одной кнопкой.", kb=kb)
