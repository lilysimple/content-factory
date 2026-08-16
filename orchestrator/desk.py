"""Рабочий стол роли: то общее, что было скопировано в каждую.

Четыре производящие роли устроены одинаково: взять тему, позвать модель,
показать карточку с кнопками, пережить перезапуск, принять правку. Пока
это лежало четырьмя копиями, любая починка чинила одну роль из четырёх —
на этом уже трижды попались вшитые фразы про соседа.

Здесь только каркас. Что именно роль делает с темой, остаётся у неё.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from config import cfg
from storage import brand as brand_store
from storage import db

log = logging.getLogger("desk")

store = brand_store.Store(cfg.brands_path)

# Стабильный id сквозь всю систему: ГГГГ-ММ-ДД-площадка-NN. По нему тема
# связывается с текстом, макетом, публикацией и правками человека.
ID_RX = re.compile(r"\b\d{4}-\d{2}-\d{2}-[a-z]+-\d{2}\b")

# Поля темы, которые роль читает как задание. Порядок от общего к частному.
BRIEF_FIELDS = (("id", "id"), ("дата", "date"), ("площадка", "plat"),
                ("формат", "format"), ("рубрика", "rubric"), ("цель", "goal"),
                ("архетип", "arch"), ("рабочий заголовок", "title"),
                ("хук", "hook"), ("кому и зачем", "why"), ("угол", "angle"),
                ("ведущий заряд", "charge"))


def brand(chat_id: int):
    """Профиль бренда тенанта. None — профиль ещё не собран."""
    row = db.one("SELECT brand_slug FROM tenants WHERE chat_id = ?", chat_id)
    return store.get(row["brand_slug"]) if row and row["brand_slug"] else None


def today(chat_id: int) -> str:
    """Сегодня в часовом поясе тенанта, а не сервера."""
    row = db.one("SELECT tz FROM tenants WHERE chat_id = ?", chat_id)
    tz = (row["tz"] if row else None) or cfg.default_tz
    try:
        return datetime.now(ZoneInfo(tz)).date().isoformat()
    except Exception:                                        # noqa: BLE001
        return datetime.now().date().isoformat()


PROFILE_LIMIT = 8000


def profile(b, sections: tuple[str, ...], limit: int = PROFILE_LIMIT) -> str:
    """Секции профиля для кешируемого блока промпта.

    Роль читает свои секции, а не файл целиком: один запрос — один
    контекст. Секций нет вовсе — отдаём `core.md`, чтобы роль работала
    на том, что есть, а не молчала.
    """
    parts = [s for s in (b.section("core", n) for n in sections) if s]
    return ("\n\n".join(parts) or b.read("core"))[:limit]


def drafted(chat_id: int, theme_id: str, asset: str) -> None:
    """Черновик написан: путь к файлу и статус в базу."""
    with db.tx() as c:
        c.execute("UPDATE themes SET status = 'draft', asset = ?, "
                  "updated_at = datetime('now') WHERE id = ? AND chat_id = ?",
                  (asset, theme_id, chat_id))


def brief(theme: dict[str, Any]) -> list[str]:
    """Тема списком «поле: значение». Пустые поля не показываем вовсе."""
    return [f"- {label}: {theme[key]}"
            for label, key in BRIEF_FIELDS if theme.get(key)]


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w{4,}", (text or "").lower())}


def pick(chat_id: int, ask: str, *, statuses: tuple[str, ...] = ("idea",),
         fresh: str = "idea", suits: Callable[[Any], bool] | None = None,
         wrong: str = "", empty: str = "", none: str = "") -> dict[str, Any]:
    """Какую тему брать.

    По убыванию точности: явный id в сообщении, совпадение по словам
    заголовка, ближайшая по дате.

    Названная по id тема берётся в любом из `statuses`, в том числе
    начатая: правка приходит к уже написанному тексту, и требовать от
    него `idea` значит не находить ровно то, что человек сейчас правит.
    Молча, без id, берётся только `fresh` — иначе просьба «напиши ещё
    один» переписывала бы вчерашнее.

    `suits` отсекает чужие темы: Редактору не нужны ролики, Редактору
    Reels не нужны посты. Отказ формулирует вызывающий, он же знает,
    к кому отправить человека.
    """
    rows = db.q(f"SELECT * FROM themes WHERE chat_id = ? AND status IN "
                f"({','.join('?' * len(statuses))}) ORDER BY date",
                chat_id, *statuses)

    if m := ID_RX.search(ask or ""):
        for r in rows:
            if r["id"] != m.group():
                continue
            if suits and not suits(r):
                raise NoWork(wrong.format(id=m.group(), format=r["format"]
                                          or "без формата"))
            return dict(r)
        raise NoWork(none.format(id=m.group()))

    mine = [r for r in rows if r["status"] == fresh
            and (suits is None or suits(r))]
    if not mine:
        raise NoWork(empty)

    if ask_words := words(ask):
        best, hits = None, 0
        for r in mine:
            n = len(ask_words & words(r["title"]))
            if n > hits:
                best, hits = r, n
        if best is not None and hits >= 2:
            return dict(best)

    return dict(mine[0])


class NoWork(RuntimeError):
    """Подходящей темы нет. Текст сообщения роль пишет сама."""


def ready(chat_id: int, theme_id: str) -> None:
    """Тема принята человеком."""
    with db.tx() as c:
        c.execute("UPDATE themes SET status = 'ready', "
                  "updated_at = datetime('now') WHERE id = ? AND chat_id = ?",
                  (theme_id, chat_id))


def reason(e: Exception) -> str:
    """Человеческая причина отказа вместо имени класса.

    Одна точка на все роли: разбор ошибок API живёт в `agent.reason`,
    иначе «закончились средства» у Редактора и у Ресёрчера выглядели бы
    по-разному, а у одного из них дампом JSON.
    """
    from orchestrator import agent            # локально: agent тяжелее desk
    return agent.reason(e)


class Desk:
    """Что роль помнит между сообщениями: последнее сделанное и ждёт ли правку.

    Один стол на роль. Результат живёт в памяти процесса, а процесс
    перезапускается, поэтому у стола есть `recover`: поднять сделанное
    из базы по id из кнопки. Поднятое кладётся обратно на стол — иначе
    кнопка под пережившей рестарт карточкой попросит правку, а следующее
    сообщение человека упрётся в «уже неактуален».
    """

    def __init__(self, role: str, *, corrections: str,
                 recover: Callable[[int, str], Any] | None = None) -> None:
        self.role = role
        self.corrections = corrections
        self._recover = recover
        self._items: dict[int, Any] = {}
        self._fix: set[int] = set()

    def hold(self, chat_id: int, item: Any) -> None:
        self._items[chat_id] = item
        self._fix.discard(chat_id)

    def get(self, chat_id: int, theme_id: str = "") -> Any:
        """Сделанное по этому чату. Кнопка называет тему, память может врать."""
        item = self._items.get(chat_id)
        if item is not None and theme_id and item.theme["id"] != theme_id:
            item = None                    # нажали под старой карточкой
        if item is None and theme_id and self._recover:
            item = self._recover(chat_id, theme_id)
        return item

    def take(self, chat_id: int, theme_id: str = "") -> Any:
        """То же, но со стола убирает: работа закончена."""
        item = self.get(chat_id, theme_id)
        self._items.pop(chat_id, None)
        self._fix.discard(chat_id)
        return item

    def wants_fix(self, chat_id: int) -> bool:
        return chat_id in self._fix

    def await_fix(self, chat_id: int, item: Any) -> None:
        self._items[chat_id] = item
        self._fix.add(chat_id)

    def clear(self, chat_id: int | None = None) -> None:
        if chat_id is None:
            self._items.clear()
            self._fix.clear()
            return
        self._items.pop(chat_id, None)
        self._fix.discard(chat_id)

    def note(self, chat_id: int, theme_id: str, instruction: str) -> None:
        """Правка человека это обучающий сигнал, а не разовая просьба."""
        b = brand(chat_id)
        if b is not None:
            b.append(self.corrections, f"- {theme_id}: {instruction.strip()}")
