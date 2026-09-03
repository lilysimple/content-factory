"""Публикатор: из готового комплекта в канал.

Единственная роль, которая **не зовёт модель**. Её работа это собрать
комплект, прогнать проверки и отправить. Всё это детерминировано, а
просить модель решить, публиковать ли, значит поставить рассуждение туда,
где нужна граница. Мы трижды убедились, что промпт это просьба.

Режим один: `approve`. Человек видит превью ровно таким, каким пост
выйдет, и нажимает кнопку. Автопубликация по журналу решений включается
не раньше двух недель и десяти утверждений подряд — до этого её нет.

Защита от двойной публикации держится на `posts.external_id UNIQUE`:
перезапуск бота или второе нажатие не создадут второй пост.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import cfg
from orchestrator import desk
from orchestrator.desk import ID_RX
from orchestrator.strategy import AUTO_PUBLISH
from storage import db

log = logging.getLogger("publisher")

TG_LIMIT = 4096              # потолок текста поста
TG_CAPTION = 1024            # потолок подписи под картинкой


class NotReady(RuntimeError):
    """Публиковать нечего или комплект не собран."""


class NoChannel(RuntimeError):
    """Канал не настроен."""


@dataclass
class Package:
    """Комплект под одну тему: текст плюс картинки."""
    theme: dict[str, Any]
    text: str = ""
    images: list[Any] = field(default_factory=list)   # пути к PNG
    problems: list[str] = field(default_factory=list)

    @property
    def plat(self) -> str:
        return self.theme.get("plat") or "telegram"

    @property
    def auto(self) -> bool:
        """Умеем ли мы вообще публиковать на эту площадку."""
        return self.plat in AUTO_PUBLISH


# ── сборка комплекта ──────────────────────────────────────────────────

def due(chat_id: int) -> list[dict[str, Any]]:
    """Темы, готовые к публикации: текст утверждён, дата наступила."""
    rows = db.q("SELECT * FROM themes WHERE chat_id = ? AND status = 'ready' "
                "AND asset IS NOT NULL AND date <= ? ORDER BY date",
                chat_id, desk.today(chat_id))
    return [dict(r) for r in rows]


def overdue(chat_id: int) -> list[dict[str, Any]]:
    """Слоты, дата которых прошла, а публикации не было.

    Пропуск это событие, о нём говорят. Молчаливо пропущенный слот
    выясняется через неделю по дырке в статистике.
    """
    rows = db.q("SELECT * FROM themes WHERE chat_id = ? AND date < ? "
                "AND status IN ('idea', 'draft', 'ready') ORDER BY date",
                chat_id, desk.today(chat_id))
    return [dict(r) for r in rows]


def collect(chat_id: int, theme: dict[str, Any]) -> Package:
    """Собрать комплект и честно перечислить, чего не хватает."""
    b = desk.brand(chat_id)
    pkg = Package(theme=theme)
    if b is None:
        pkg.problems.append("профиля бренда нет")
        return pkg

    if not theme.get("asset"):
        pkg.problems.append("текста нет")
    else:
        raw = b.read(theme["asset"])
        pkg.text = (raw.split("-->", 1)[-1].strip()
                    if raw.startswith("<!--") else raw.strip())
        if not pkg.text:
            pkg.problems.append(f"файл {theme['asset']} пуст")

    # Макеты не обязательны: пост без обложки это нормальный пост.
    pkg.images = sorted(b.path("posts").glob(f"{theme['id']}-*.png"))

    limit = TG_CAPTION if pkg.images else TG_LIMIT
    if len(pkg.text) > limit:
        pkg.problems.append(
            f"текст {len(pkg.text)} знаков, потолок {limit}"
            + (" из-за картинки" if pkg.images else ""))

    row = db.one("SELECT state, link FROM posts WHERE theme_id = ?", theme["id"])
    if row and row["state"] == "pub":
        pkg.problems.append(f"уже опубликовано: {row['link'] or 'ссылки нет'}")

    if not pkg.auto:
        pkg.problems.append(f"{pkg.plat} автоматом не публикуется, "
                            "комплект отдаётся человеку")
    return pkg


# ── отправка ──────────────────────────────────────────────────────────

def _link(chat_id_of_channel: Any, message_id: int) -> str:
    """Ссылка на пост. Для приватных каналов работает форма c/<id>."""
    raw = str(chat_id_of_channel)
    if raw.startswith("@"):
        return f"https://t.me/{raw[1:]}/{message_id}"
    return f"https://t.me/c/{raw.removeprefix('-100')}/{message_id}"


async def send(reg, chat_id: int, pkg: Package) -> str:
    """Отправить в канал и записать факт. Возвращает ссылку.

    Запись в `posts` идёт **до** отправки со статусом `sending`: если
    процесс умрёт между отправкой и записью, повторная попытка упрётся в
    строку и не создаст второй пост.
    """
    if not cfg.publish_channel:
        raise NoChannel("канал не задан, PUBLISH_CHANNEL пуст")
    if pkg.problems:
        raise NotReady("; ".join(pkg.problems))

    tid = pkg.theme["id"]
    with db.tx() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO posts (theme_id, chat_id, platform, state, "
            "attempts) VALUES (?,?,?,'sending',0)", (tid, chat_id, pkg.plat))
        if cur.rowcount == 0:
            row = c.execute("SELECT state, link FROM posts WHERE theme_id = ?",
                            (tid,)).fetchone()
            if row and row["state"] == "pub":
                raise NotReady(f"уже опубликовано: {row['link']}")
        c.execute("UPDATE posts SET attempts = attempts + 1 "
                  "WHERE theme_id = ?", (tid,))

    bot = reg.bot("publisher")
    try:
        if pkg.images:
            from aiogram.types import BufferedInputFile
            msg = await bot.send_photo(
                chat_id=cfg.publish_channel,
                photo=BufferedInputFile(pkg.images[0].read_bytes(),
                                        pkg.images[0].name),
                caption=pkg.text)
        else:
            msg = await bot.send_message(chat_id=cfg.publish_channel,
                                         text=pkg.text)
    except Exception as e:                                   # noqa: BLE001
        with db.tx() as c:
            c.execute("UPDATE posts SET state = 'failed' WHERE theme_id = ?",
                      (tid,))
        log.exception("публикация не прошла")
        raise NotReady(f"Telegram отказал: "
                       f"{getattr(e, 'message', None) or e}") from e

    link = _link(cfg.publish_channel, msg.message_id)
    with db.tx() as c:
        c.execute("UPDATE posts SET state = 'pub', external_id = ?, link = ?, "
                  "published_at = datetime('now') WHERE theme_id = ?",
                  (f"{cfg.publish_channel}:{msg.message_id}", link, tid))
        c.execute("UPDATE themes SET status = 'pub', "
                  "updated_at = datetime('now') WHERE id = ?", (tid,))
    log.info("опубликовано %s → %s", tid, link)
    return link


def skip(chat_id: int, theme_id: str, reason: str) -> None:
    """Пропуск с обязательной причиной. Задним числом не публикуем."""
    with db.tx() as c:
        c.execute("UPDATE themes SET status = 'skip', skip_reason = ?, "
                  "updated_at = datetime('now') WHERE id = ? AND chat_id = ?",
                  (reason, theme_id, chat_id))
        c.execute("INSERT INTO posts (theme_id, chat_id, state) "
                  "VALUES (?,?,'skip') ON CONFLICT(theme_id) "
                  "DO UPDATE SET state = 'skip'", (theme_id, chat_id))


# ── карточка ──────────────────────────────────────────────────────────

def _kb(theme_id: str, ready: bool) -> InlineKeyboardMarkup:
    row = []
    if ready:
        row.append(InlineKeyboardButton(text="📤 Опубликовать",
                                        callback_data=f"pub:go:{theme_id}"))
    row.append(InlineKeyboardButton(text="⏭ Пропустить",
                                    callback_data=f"pub:skip:{theme_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def _head(pkg: Package) -> str:
    """Строка про слот: дата, площадка, длина, чем закрыт."""
    t = pkg.theme
    n = len(pkg.images)
    tail = (" · без картинки" if not n else
            " · картинка" if n == 1 else f" · {n} картинок, выйдет первая")
    return (f"📤 <b>{t.get('date')} · {pkg.plat} · {t.get('format')}</b>\n"
            f"<code>{t['id']}</code> · {len(pkg.text)} знаков" + tail)


def _tail(pkg: Package) -> list[str]:
    """Чего не хватает и почему может не уйти."""
    out: list[str] = []
    if pkg.problems:
        out += ["", "⚠️ " + "; ".join(pkg.problems)]
    if not cfg.publish_channel:
        out += ["", "Канал не настроен: <code>PUBLISH_CHANNEL</code> пуст. "
                    "Опубликовать не смогу, комплект отдам файлами."]
    return out


def card(pkg: Package) -> str:
    """Шапка комплекта без текста поста.

    Текста тут нет намеренно: пост с картинкой выходит подписью под фото,
    и вторая копия текста выше сломала бы главное обещание превью —
    человек утверждает то, что увидит канал, а не пересказ.
    """
    return "\n".join([_head(pkg), *_tail(pkg)])


def preview(pkg: Package) -> str:
    """Пост ровно так, как он выйдет. Тут ловится оформление.

    Форма для поста без картинки: текст в теле сообщения. Пост с
    картинкой показывается фотографией — см. `_show`.
    """
    out = [_head(pkg), "", "— — — так выйдет — — —", "",
           pkg.text or "[текста нет]", "", "— — — — — — — — — — —",
           *_tail(pkg)]
    return "\n".join(out)


_pending: dict[int, str] = {}          # чат → id темы, ждущей причины пропуска


def wants_reason(chat_id: int) -> bool:
    return chat_id in _pending


async def run(reg, chat_id: int, ask: str, topic: str = "queue") -> None:
    """Показать, что готово к публикации.

    В `ask` может прийти id темы — так передаёт комплект Дизайнер. Тогда
    показываем ровно её, даже если дата ещё не наступила: человек только
    что принял макет и спрашивает про этот пост, а не про очередь.
    """
    _pending.pop(chat_id, None)

    if m := ID_RX.search(ask or ""):
        row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                     m.group(), chat_id)
        if row is None:
            await reg.say("publisher", chat_id,
                          f"Темы <code>{m.group()}</code> у меня нет.",
                          topic=topic)
            return
        await _show(reg, chat_id, dict(row), topic)
        return

    if late := overdue(chat_id):
        await reg.say("publisher", chat_id,
                      "Слоты, у которых дата прошла, а публикации не было:\n"
                      + "\n".join(f"· {t['date']} {t.get('title') or t['id']}"
                                  for t in late[:5])
                      + "\n\nЗадним числом не публикую. Пропусти их или "
                        "перенеси Стратегом.", topic=topic)

    items = due(chat_id)
    if not items:
        await reg.say("publisher", chat_id,
                      "К публикации ничего не готово: нужен текст, "
                      "утверждённый Редактором, и наступившая дата.",
                      topic=topic)
        return

    for theme in items[:3]:
        await _show(reg, chat_id, theme, topic)


async def _show(reg, chat_id: int, theme: dict[str, Any], topic: str) -> None:
    """Превью одного комплекта с кнопками по его состоянию.

    Кнопка публикации появляется только у наступившего слота. Выйти
    раньше срока так же плохо, как выйти задним числом: план на неделю
    держится на датах, и досрочный пост ломает соседний слот.
    """
    pkg = collect(chat_id, theme)
    blocking = [p for p in pkg.problems if "автоматом не публикуется" not in p]
    early = (theme.get("date") or "") > desk.today(chat_id)

    note = ""
    if early:
        note = (f"\n\nДата слота {theme.get('date')} ещё не наступила: "
                "показываю комплект, публиковать буду в свой день.")
    kb = _kb(theme["id"],
             ready=pkg.auto and not blocking and not early
             and bool(cfg.publish_channel))

    # Пост с картинкой выходит одним сообщением: фото и подпись под ним.
    # Значит и превью такое же — то же фото, та же подпись, кнопки на нём
    # же. Текст отдельно от картинки показывал пост, которого не будет.
    # Подпись не вмещается — падаем в текстовую форму: собрать превью,
    # которое врёт про потолок, хуже, чем показать некрасиво.
    as_post = bool(pkg.images) and bool(pkg.text) and len(pkg.text) <= TG_CAPTION
    if as_post:
        await reg.say("publisher", chat_id, card(pkg) + note, topic=topic)
        first = pkg.images[0]
        await reg.send_file("publisher", chat_id, first.read_bytes(),
                            first.name, caption=pkg.text, topic=topic,
                            kb=kb, as_photo=True)
    else:
        await reg.say("publisher", chat_id, preview(pkg) + note, kb=kb,
                      topic=topic)

    # Остальные макеты идут следом справочно: в канал уходит первый.
    for img in (pkg.images[1:] if as_post else pkg.images):
        await reg.send_file("publisher", chat_id, img.read_bytes(),
                            img.name, topic=topic, as_photo=True)


async def on_callback(reg, chat_id: int, action: str,
                      topic: str = "queue") -> None:
    action, _, theme_id = action.partition(":")

    if action == "go":
        row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                     theme_id, chat_id)
        if row is None:
            await reg.say("publisher", chat_id, "Этой темы уже нет.",
                          topic=topic)
            return
        pkg = collect(chat_id, dict(row))
        try:
            link = await send(reg, chat_id, pkg)
        except (NotReady, NoChannel) as e:
            await reg.say("publisher", chat_id, f"Не опубликовал: {e}",
                          topic=topic)
            return
        await reg.say("publisher", chat_id,
                      f"Опубликовано: {link}\n<code>{theme_id}</code> в pub.\n\n"
                      "Метрики снимет Ресёрчер через 24 часа, 72 часа и "
                      "7 дней — он ещё не подключён.", topic=topic)
        return

    if action == "skip":
        _pending[chat_id] = theme_id
        await reg.say("publisher", chat_id,
                      "Напиши причину пропуска одним сообщением. "
                      "Без причины не пропускаю: через месяц дырка в "
                      "статистике будет необъяснимой.", topic=topic)


async def take_reason(reg, chat_id: int, reason: str,
                      topic: str = "queue") -> None:
    theme_id = _pending.pop(chat_id, None)
    if theme_id is None:
        return
    skip(chat_id, theme_id, reason.strip())
    await reg.say("publisher", chat_id,
                  f"Пропустил <code>{theme_id}</code>: {reason.strip()}",
                  topic=topic)
