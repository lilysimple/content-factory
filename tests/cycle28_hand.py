"""Цикл 28: тема, заведённая рукой.

Проверяется то, что у этого входа ошибается дороже всего: слот. Тема без
даты не попадает ни в очередь, ни к автомату — то есть «добавь в
автопостинг» молча не срабатывает; тема в занятый день ставит второй пост
на ту же площадку в тот же день; тема задним числом не публикуется вовсе.

Отдельно проверяется, что просьба человека своими словами доезжает до
Редактора: заголовок она не заменяет, и потерять её значит написать пост
не про то, о чём человек хотел рассказать.

Модель здесь не участвует: разбор даты и слот — арифметика.
"""
from __future__ import annotations

from datetime import date, timedelta

import harness
from harness import CHAT, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import bridge, desk, hand, publisher, strategy   # noqa: E402
from orchestrator.desk import NoWork                              # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

TODAY = desk.today(CHAT)
TOMORROW = (date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()


def wipe() -> None:
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        c.execute("DELETE FROM posts WHERE chat_id = ?", (CHAT,))


def seed_plan(day: str, plat: str = "telegram") -> None:
    """Занятый слот плана: такой же, как ставит Стратег."""
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO themes (id, chat_id, date, plat, "
                  "format, status, title, src) VALUES (?,?,?,?,'пост','idea',"
                  "'Плановая тема','plan')",
                  (f"{day}-{plat}-09", CHAT, day, plat))


def main() -> int:
    # ── 1. разбор просьбы ─────────────────────────────────────────────
    print("1. Разбор просьбы")
    day, title, notes = hand.parse(CHAT, "Как я выбирала ноутбук")
    check("без даты день не выдумывается", day == "", day)
    check("заголовок взят целиком", title == "Как я выбирала ноутбук", title)
    check("заметок нет", notes == "", notes)

    day, title, notes = hand.parse(
        CHAT, "завтра Три ошибки в промптах\nПро то, как я неделю "
              "воевала с одним агентом\nи чем это кончилось")
    check("«завтра» это завтра", day == TOMORROW, day)
    check("заголовок отрезан по первой строке",
          title == "Три ошибки в промптах", title)
    check("заметки собраны со второй строки",
          notes.startswith("Про то, как я неделю")
          and notes.endswith("кончилось"), notes)

    day, _, _ = hand.parse(CHAT, "сегодня Что-то рассказать")
    check("«сегодня» это сегодня", day == TODAY, day)

    nxt = (date.fromisoformat(TODAY) + timedelta(days=3))
    day, title, _ = hand.parse(
        CHAT, f"{nxt.day:02d}.{nxt.month:02d} Заголовок дня")
    check("дата числом разбирается", day == nxt.isoformat(), day)
    check("дата из заголовка убрана", title == "Заголовок дня", title)

    day, _, _ = hand.parse(CHAT, f"{nxt.isoformat()} Заголовок")
    check("дата в ISO разбирается", day == nxt.isoformat(), day)

    # Год не назван, число уже прошло — значит следующий год, а не
    # прошлогодний: тема в прошлом не публикуется вовсе.
    past = date.fromisoformat(TODAY) - timedelta(days=10)
    day, _, _ = hand.parse(CHAT, f"{past.day:02d}.{past.month:02d} Заголовок")
    check("прошедшее число уезжает на следующий год",
          day > TODAY, day)

    long = ("Расскажу про то, как я три недели собирала контент-завод и "
            "почему в итоге переписала половину. Там вышло смешно, потому "
            "что сломалось ровно то, что я проверяла первым.")
    _, title, notes = hand.parse(CHAT, long)
    check("длинная строка режется на заголовок и заметки",
          len(title) <= hand.TITLE_MAX and bool(notes),
          f"{len(title)} / {notes[:40]}")

    bad = False
    try:
        hand.parse(CHAT, "   ")
    except NoWork:
        bad = True
    check("пустая просьба — отказ", bad)

    bad = False
    try:
        hand.parse(CHAT, "завтра")
    except NoWork:
        bad = True
    check("дата без темы — отказ", bad)

    # ── 2. слот ───────────────────────────────────────────────────────
    print("\n2. Слот")
    wipe()
    free = strategy.hand_free(CHAT, "telegram")
    check("окно руки начинается с сегодня", free[0] == TODAY, free[0])
    check("окно руки восемь дней", len(free) == strategy.HAND_DAYS, str(len(free)))

    seed_plan(TODAY)
    check("занятый день из свободных ушёл",
          TODAY not in strategy.hand_free(CHAT, "telegram"))
    check("ближайший свободный это завтра",
          strategy.hand_slot(CHAT, "telegram") == TOMORROW)

    busy = False
    try:
        strategy.hand_slot(CHAT, "telegram", TODAY)
    except strategy.NoSlots as e:
        busy = "занят" in str(e)
    check("названный занятый день — отказ словами", busy)

    out = False
    far = (date.fromisoformat(TODAY) + timedelta(days=30)).isoformat()
    try:
        strategy.hand_slot(CHAT, "telegram", far)
    except strategy.NoSlots as e:
        out = "окна" in str(e)
    check("день вне окна — отказ", out)

    check("чужая площадка считается отдельно",
          TODAY in strategy.hand_free(CHAT, "instagram"))

    # ── 3. тема в базе ────────────────────────────────────────────────
    print("\n3. Тема в базе")
    wipe()
    theme = hand.add(CHAT, "завтра Три ошибки в промптах\nпро агента, "
                           "который спорил")
    row = db.one("SELECT * FROM themes WHERE id = ?", theme["id"])
    check("слот записан датой", row["date"] == TOMORROW, str(row["date"]))
    check("помечена как вне плана", row["src"] == "adhoc", row["src"])
    check("статус idea: работа впереди", row["status"] == "idea", row["status"])
    check("площадка telegram", row["plat"] == "telegram", str(row["plat"]))
    check("формат пост", row["format"] == "пост", str(row["format"]))
    check("просьба легла своим полем",
          "спорил" in (row["notes"] or ""), str(row["notes"]))
    check("просьба не подменила «кому и зачем»", not (row["why"] or ""),
          str(row["why"]))

    second = False
    try:
        hand.add(CHAT, f"{TOMORROW} Вторая тема на тот же день")
    except strategy.NoSlots:
        second = True
    check("второй пост в тот же день не встаёт", second)

    back = False
    try:
        desk.adhoc(CHAT, plat="telegram", fmt="пост", title="Задним числом",
                   day=(date.fromisoformat(TODAY)
                        - timedelta(days=1)).isoformat())
    except NoWork as e:
        back = "прошло" in str(e)
    check("дата в прошлом — отказ", back)

    check("тема без даты заводится по-прежнему",
          desk.adhoc(CHAT, plat="instagram", fmt="reels",
                     title="Дубль из головы", status="ready")["date"] is None)

    # ── 4. уборка ─────────────────────────────────────────────────────
    print("\n4. Уборка")
    wipe()
    t = hand.add(CHAT, "Передумаю через минуту")
    check("убирается своя нетронутая", hand.drop(CHAT, t["id"]))
    check("день снова свободен",
          t["date"] in strategy.hand_free(CHAT, "telegram"))
    check("строки в базе нет",
          db.one("SELECT id FROM themes WHERE id = ?", t["id"]) is None)

    t = hand.add(CHAT, "Текст уже написан")
    desk.drafted(CHAT, t["id"], f"posts/{t['id']}.md")
    check("тема с текстом так не убирается", not hand.drop(CHAT, t["id"]))
    seed_plan(TOMORROW)
    check("плановую тему рукой не убрать",
          not hand.drop(CHAT, f"{TOMORROW}-telegram-09"))

    # ── 5. дорога к автомату ──────────────────────────────────────────
    print("\n5. Автомат")
    wipe()
    t = hand.add(CHAT, f"{TODAY} Пост на сегодня\nзаметка для Редактора")
    b = desk.brand(CHAT)
    b.artifact(f"posts/{t['id']}.md", "Готовый текст поста.")
    desk.drafted(CHAT, t["id"], f"posts/{t['id']}.md")
    desk.ready(CHAT, t["id"])

    check("очередь Публикатора её видит",
          t["id"] in [x["id"] for x in publisher.due(CHAT)])
    now = publisher.slot_moment(CHAT, publisher.settings(CHAT)[1], TODAY)
    items, late = publisher.due_now(CHAT, now)
    check("автомат берёт её в свой слот",
          t["id"] in [x["id"] for x in items] and late == [])

    # ── 6. просьба доезжает до Редактора ──────────────────────────────
    print("\n6. Просьба до Редактора")
    ask = hand.ask_for_text(t)
    check("просьба к мосту несёт id темы", t["id"] in ask, ask[:80])
    check("просьба к мосту несёт заметки", "заметка для Редактора" in ask,
          ask[:120])

    themes_md = "\n".join(bridge._themes(CHAT))
    check("input.md называет просьбу человека",
          "Просьба человека по теме" in themes_md
          and "заметка для Редактора" in themes_md, themes_md[-300:])

    wipe()
    seed_plan(TOMORROW)
    plain = "\n".join(bridge._themes(CHAT))
    check("у плановых тем блока просьбы нет",
          "Просьба человека по теме" not in plain)

    # ── 7. карточка человеку ──────────────────────────────────────────
    print("\n7. Карточка")
    wipe()
    publisher.arm(CHAT, False)
    t = hand.add(CHAT, "завтра Карточка на проверку\nзаметки видны")
    text = hand.card(CHAT, t)
    check("карточка называет id", t["id"] in text, text)
    check("карточка называет день по-человечески",
          "завтра" in text, text)
    check("карточка показывает заметки", "заметки видны" in text, text)
    check("при выключенном автомате обещает кнопку",
          "выключена" in text and "/auto" in text, text)
    check("кнопки на карточке две",
          len(hand.kb(t["id"]).inline_keyboard[0]) == 2)

    db.set_tenant(CHAT, auto=1, auto_at="10:00")
    on_text = hand.card(CHAT, t)
    check("при включённом автомате называет время слота",
          "10:00" in on_text and "сам" in on_text, on_text)
    publisher.arm(CHAT, False)

    help_text = hand.help_text(CHAT)
    check("подсказка показывает свободные дни",
          "/tema" in help_text and "Свободные дни" in help_text, help_text)

    return report()


raise SystemExit(main())
