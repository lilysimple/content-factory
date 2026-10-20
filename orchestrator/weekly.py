"""Сводка по понедельникам: расписание Ресёрчера.

Команда `/research` работала с самого начала, автозапуска у неё не было
— это стояло в «следующем шаге» `CLAUDE.md`. Здесь он появляется.

Устроено так же, как автопубликация (`publisher.due_now`), и нарочно:
две разные механики расписания в одном заводе разошлись бы молча при
первой же правке часового пояса. Отсюда и общие части — `desk.now`,
окно в 90 минут, пропуск называется человеку строкой.

**Чем отличается от автопубликации.** Та спрашивает порог
(`publisher.gate`): десять утверждений и две недели, потому что уходит
в живой канал к подписчикам. У сводки порога нет: она приходит в чат
завода человеку, а не в канал, и ошибиться ей негде.

**Отметка о прогоне живёт в базе, а не в памяти процесса.** У
автопубликации `_told` умирает с перезапуском, и это там правильно:
важное дешевле повторить. Здесь наоборот — Мак просыпается в
понедельник днём, завод поднимается заново, и память в процессе дала
бы вторую сводку за то же утро. Поэтому `tenants.digest_last`.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from orchestrator import bridge, desk
from storage import db

log = logging.getLogger("weekly")

# Понедельник. `weekday()` считает с нуля.
DOW = 0

# Окно опоздания. То же, что у автомата: Мак спал, завод стоял, время
# прошло. Шире делать нельзя — сводка, пришедшая в понедельник вечером,
# это уже не понедельничная рубрика, а просто сводка.
GRACE_MIN = 90

# Что просим. Строка сверяется в `handlers._serve`, чтобы отличить
# расписание от просьбы человека и подвесить обложку только к первому.
ASK = "Пришли дайджест недели"

TOPIC = "research"


def at(chat_id: int) -> str:
    """Во сколько ждём сводку. Пусто — расписание выключено."""
    row = db.one("SELECT digest_at FROM tenants WHERE chat_id = ?", chat_id)
    return ((row["digest_at"] if row else "") or "").strip()


def set_at(chat_id: int, value: str) -> None:
    """Включить или выключить расписание. Пустая строка это выключить."""
    db.set_tenant(chat_id, digest_at=value.strip())


def moment(chat_id: int, hhmm: str, day: str):
    """Назначенная минута этого дня в поясе тенанта."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    row = db.one("SELECT tz FROM tenants WHERE chat_id = ?", chat_id)
    tz = (row["tz"] if row else None) or "Europe/Moscow"
    h, _, m = hhmm.partition(":")
    try:
        return datetime.fromisoformat(day).replace(
            hour=int(h), minute=int(m or 0), tzinfo=ZoneInfo(tz))
    except Exception:                                        # noqa: BLE001
        # Кривое время в базе не должно останавливать расписание молча.
        return datetime.fromisoformat(day).replace(
            hour=9, minute=0, tzinfo=ZoneInfo(tz))


def due_now(chat_id: int, now=None) -> tuple[bool, str]:
    """Пора ли звать Ресёрчера. Возвращает (пора, что сказать человеку).

    Вторая половина пары это не ошибка, а пропуск: окно закрылось, пока
    Мак спал. Человек должен узнать об этом от завода, а не по тому,
    что сводки в понедельник почему-то не было.
    """
    hhmm = at(chat_id)
    if not hhmm:
        return False, ""

    now = now or desk.now(chat_id)
    if now.weekday() != DOW:
        return False, ""

    day = now.date().isoformat()
    row = db.one("SELECT digest_last FROM tenants WHERE chat_id = ?", chat_id)
    if row is not None and (row["digest_last"] or "") == day:
        return False, ""

    slot = moment(chat_id, hhmm, day)
    if now < slot:
        return False, ""

    if now > slot + timedelta(minutes=GRACE_MIN):
        mark(chat_id, day)
        return False, (
            f"Сводку недели ждали в {hhmm}, окно в {GRACE_MIN} минут "
            "закрылось: завод в это время не работал. Позови командой "
            "/research, когда будет удобно.")
    return True, ""


def mark(chat_id: int, day: str) -> None:
    """Запомнить, что за этот понедельник уже отработали."""
    db.set_tenant(chat_id, digest_last=day)


async def tick(reg, chat_id: int, now=None) -> bool:
    """Один тик расписания. True — сводка поставлена в очередь.

    Задачу ставим в ту же очередь, что и команда человека, а не зовём
    мост напрямую: полосы считает `bridge`, и расписание, лезущее мимо
    очереди, встало бы поперёк работы, которую человек уже попросил.
    """
    ok, note = due_now(chat_id, now)
    if note:
        await reg.say("assistant", chat_id, note, topic=TOPIC)
    if not ok:
        return False

    now = now or desk.now(chat_id)
    day = now.date().isoformat()
    try:
        bridge.enqueue(chat_id, ASK, workflow="research", topic=TOPIC)
    except bridge.QueueFull as e:
        # Очередь забита — значит человек и так завалил завод работой.
        # Отметку всё равно ставим: вторая попытка через минуту упёрлась
        # бы в ту же очередь и повторяла бы эту строку шестьдесят раз в час.
        mark(chat_id, day)
        await reg.say("assistant", chat_id,
                      f"Сводку недели по расписанию не поставила: {e}. "
                      "Позови командой /research, когда разберу очередь.",
                      topic=TOPIC)
        return False

    mark(chat_id, day)
    log.info("сводка недели поставлена по расписанию, чат %s", chat_id)
    await reg.say("assistant", chat_id,
                  "Понедельник: собираю сводку недели. Это займёт "
                  "несколько минут, отвечать не нужно.", topic=TOPIC)
    return True
