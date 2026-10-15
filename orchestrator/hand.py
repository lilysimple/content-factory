"""Тема, заведённая рукой.

Третий вход в план, и самый короткий. Стратег ставит слоты неделей
вперёд, монтаж заводит тему по факту съёмки — а здесь человек, которому
есть что рассказать сегодня, пишет это одной строкой в чат.

Что делает код, а что остаётся ролям:

- код берёт дату, заголовок и заметки, занимает слот и кладёт тему;
- текст по ней пишет Редактор обычным путём, через мост;
- утверждает человек кнопкой, публикует Публикатор или автомат.

**Слот тема получает сразу.** Без даты её не возьмёт ни очередь
(`publisher.due`), ни автомат (`publisher.due_now`), и «добавь в
автопостинг» означало бы «публикуй по кнопке». Свободен ли слот и в окне
ли он, считает `strategy.hand_slot` — дом у правила занятости один.

**Модель здесь не зовётся вовсе.** Разобрать «завтра» и отрезать
заголовок от заметок это арифметика, а вызов модели на каждую тему
стоил бы минуты ожидания и денег там, где достаточно регулярки. Сам
текст поста при этом по-прежнему пишет Редактор: заголовок рукой это не
пост рукой.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import desk, publisher, research, strategy
from orchestrator.desk import NoWork
from storage import db

log = logging.getLogger("hand")

PLAT = "telegram"            # рукой заводится пост в канал бренда
FMT = "пост"

# Заголовок длиннее этого человек писал не как заголовок, а как рассказ.
# Резать его отказом было бы грубо: он уже всё написал. Поэтому первая
# фраза становится заголовком, остальное уезжает в заметки Редактору.
TITLE_MAX = 120

WHEN_WORDS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}

_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s|$)")
_DMY = re.compile(r"^(\d{1,2})[.](\d{1,2})(?:[.](\d{2,4}))?(?:\s|$)")


def _day_from_dmy(chat_id: int, d: int, m: int, y: str | None) -> str:
    """«16.10» в дату. Год не назван — этот, а если он уже прошёл, следующий.

    Декабрьское «05.01» это январь следующего года, а не прошлогодний
    январь: тема в прошлом не публикуется вовсе, и тихо завести её значит
    потерять просьбу.
    """
    today = date.fromisoformat(desk.today(chat_id))
    year = int(y) if y else today.year
    if y and year < 100:
        year += 2000
    try:
        when = date(year, m, d)
    except ValueError:
        raise NoWork(f"такой даты нет: {d:02d}.{m:02d}")
    if not y and when < today:
        try:
            when = date(year + 1, m, d)
        except ValueError:
            raise NoWork(f"такой даты нет: {d:02d}.{m:02d}")
    return when.isoformat()


def _split_long(text: str) -> tuple[str, str]:
    """Длинная одна строка: заголовком первая фраза, остальное в заметки."""
    if len(text) <= TITLE_MAX:
        return text, ""
    if m := re.search(r"[.!?…]\s", text[:TITLE_MAX + 40]):
        return text[:m.end()].strip(" .!?…"), text[m.end():].strip()
    cut = text.rfind(" ", 0, TITLE_MAX)
    cut = cut if cut > 40 else TITLE_MAX
    return text[:cut].strip(), text[cut:].strip()


def parse(chat_id: int, raw: str) -> tuple[str, str, str]:
    """Разобрать просьбу в «день, заголовок, заметки».

    День необязателен: без него слот выберет `strategy.hand_slot`.
    Заметки это всё, что человек дописал после первой строки — именно
    они отличают «заведи тему» от «заведи тему и расскажи вот про это».
    """
    text = (raw or "").strip()
    if not text:
        raise NoWork("пустая просьба")

    day = ""
    low = text.lower()
    for word, shift in WHEN_WORDS.items():
        if low.startswith(word) and (len(text) == len(word)
                                     or not text[len(word)].isalpha()):
            day = (date.fromisoformat(desk.today(chat_id))
                   + timedelta(days=shift)).isoformat()
            text = text[len(word):].lstrip(" ,:—-")
            break
    else:
        if m := _ISO.match(text):
            day = m.group(1)
            text = text[m.end(1):].lstrip(" ,:—-")
        elif m := _DMY.match(text):
            day = _day_from_dmy(chat_id, int(m.group(1)), int(m.group(2)),
                                m.group(3))
            text = text[m.end(2) if m.group(3) is None
                        else m.end(3):].lstrip(" ,:—-")

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise NoWork("дата есть, а темы нет: напиши, о чём рассказать")
    title, notes = lines[0], "\n".join(lines[1:]).strip()
    if not notes:
        title, notes = _split_long(title)
    return day, title, notes


def add(chat_id: int, raw: str) -> dict:
    """Завести тему рукой. Отказ — `NoWork` или `strategy.NoSlots`."""
    day, title, notes = parse(chat_id, raw)
    slot = strategy.hand_slot(chat_id, PLAT, day)
    theme = desk.adhoc(chat_id, plat=PLAT, fmt=FMT, title=title,
                       notes=notes, day=slot)
    log.info("тема рукой %s на %s: %s", theme["id"], slot, title[:60])
    return theme


def drop(chat_id: int, theme_id: str) -> bool:
    """Убрать тему, заведённую рукой. True — убрала.

    Убирается только своя и только нетронутая: у темы с текстом или с
    собранным макетом на диске лежат артефакты, и строка, исчезнувшая из
    базы, оставила бы их сиротами. Такую человек пропускает Публикатором,
    с причиной.
    """
    row = db.one("SELECT src, status, asset FROM themes "
                 "WHERE id = ? AND chat_id = ?", theme_id, chat_id)
    if row is None or row["src"] != "adhoc" or row["status"] != "idea" \
            or row["asset"]:
        return False
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE id = ? AND chat_id = ?",
                  (theme_id, chat_id))
    log.info("тема рукой %s убрана", theme_id)
    return True


def ask_for_text(theme: dict) -> str:
    """Просьба к мосту: написать текст по этой теме.

    Заметки едут в просьбу текстом, а не только лежат в базе: таблица
    тем в `input.md` колонки заметок не несёт, и Редактор написал бы
    пост по заголовку, мимо того, что человек хотел сказать.
    """
    ask = f"напиши пост по теме {theme['id']}"
    if notes := (theme.get("notes") or "").strip():
        ask += ". Человек просил рассказать именно это, своими словами: " \
               + notes
    return ask


# ── карточка человеку ─────────────────────────────────────────────────

def human_day(chat_id: int, day: str) -> str:
    """«2026-10-16» → «16 октября, пятница». Названия месяцев берём у
    Ресёрчера: второй список разъехался бы с первым."""
    when = date.fromisoformat(day)
    today = date.fromisoformat(desk.today(chat_id))
    out = f"{when.day} {research.MONTHS[when.month - 1]}"
    if when == today:
        return out + ", сегодня"
    if when == today + timedelta(days=1):
        return out + ", завтра"
    return f"{out}, {strategy.WEEKDAYS[when.weekday()]}"


def kb(theme_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Напиши текст",
                             callback_data=f"hand:write:{theme_id}"),
        InlineKeyboardButton(text="🗑 Убрать",
                             callback_data=f"hand:drop:{theme_id}"),
    ]])


def card(chat_id: int, theme: dict) -> str:
    """Что завела и что будет дальше.

    Про автомат говорится фактом его состояния, а не обещанием: «уйдёт
    сам в десять» при выключенном автомате это неправда, которую человек
    заметит пустым каналом.
    """
    day = theme.get("date") or ""
    on, at = publisher.settings(chat_id)
    out = [f"Завела тему <code>{theme['id']}</code> на "
           f"{human_day(chat_id, day)}.", "", f"«{theme['title']}»"]
    if notes := (theme.get("notes") or "").strip():
        out += ["", f"<i>{notes}</i>"]

    out.append("")
    if on:
        tail = ""
        if day == desk.today(chat_id):
            now = desk.now(chat_id)
            slot = publisher.slot_moment(chat_id, at, day)
            if now > slot + timedelta(minutes=publisher.AUTO_GRACE_MIN):
                tail = (f" Слот {at} на сегодня уже прошёл — этот покажу "
                        "и дождусь кнопки.")
        out.append(f"Напишу текст, ты утвердишь кнопкой — и в свой день "
                   f"пост уйдёт в канал сам, в {at}.{tail}")
    else:
        out.append("Напишу текст, ты утвердишь кнопкой — и в свой день "
                   "покажу комплект к публикации. Автопубликация "
                   "выключена, отправляю по кнопке: включить — "
                   "<code>/auto</code>.")
    return "\n".join(out)


def help_text(chat_id: int) -> str:
    """Как пользоваться, и какие дни свободны."""
    free = strategy.hand_free(chat_id, PLAT)
    days = (", ".join(human_day(chat_id, d) for d in free[:4])
            if free else "свободных нет, все дни заняты планом")
    return ("Тема рукой — то, что хочешь рассказать сама:\n\n"
            "<code>/tema Как я выбирала ноутбук и зачем вернула</code>\n"
            "<code>/tema завтра Три ошибки в промптах</code>\n"
            "<code>/tema 16.10 Заголовок</code> — и со второй строки "
            "заметки Редактору: о чём именно рассказать.\n\n"
            f"Свободные дни Telegram: {days}.\n"
            "Без даты ставлю в ближайший свободный.")
