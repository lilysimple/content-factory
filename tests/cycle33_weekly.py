"""Цикл 33: сводка по понедельникам — окно, повтор, обложка.

Четыре границы, из-за которых расписание сводки сделано отдельно от
автопубликации, а обложка отдельно от сводки.

День и окно держит код. «Каждый понедельник в девять» это не только
понедельник: Мак спит, завод стоит, и первый тик после пробуждения
случается в час дня. Сводка, пришедшая понедельничной рубрикой в
понедельник вечером, хуже пропуска, названного вслух.

Повтор ловится базой, а не памятью процесса. Это главная разница с
`publisher._told`: там перезапуск повторяет важное и это правильно,
здесь перезапуск посреди утра дал бы вторую сводку за то же окно.

Логотипы на обложке берутся из закрытого набора и ищутся в тексте
сводки. Марка, которой на этой неделе не было, в кадр не попадает;
марки, которой мы не умеем называть, не бывает вовсе.

Ключа ChatPlace нет — это строка человеку, а не упавший понедельник.
Сводка без картинки остаётся сводкой.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import harness
from harness import check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import bridge, virale, weekly                   # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

CHAT = -1009000000033
TZ = ZoneInfo("Europe/Moscow")


class Said:
    """Запись вместо отправки: тик обязан говорить, а не молчать."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    async def say(self, role, chat_id, text, **kw):
        self.lines.append(text)


def monday(hour: int, minute: int = 0) -> datetime:
    """Ближайший понедельник в поясе тенанта. Дата относительная:
    вшитая переезжает в прошлое и уносит с собой весь цикл."""
    now = datetime.now(TZ)
    day = now.date() - timedelta(days=now.weekday())
    return datetime.combine(day, datetime.min.time(), TZ).replace(
        hour=hour, minute=minute)


async def main() -> None:
    db.ensure_tenant(CHAT, "Europe/Moscow")
    db.set_tenant(CHAT, brand_slug="lily-space", digest_at="",
                  digest_last="")

    # ── расписание выключено ─────────────────────────────────────────
    ok, note = weekly.due_now(CHAT, monday(9, 0))
    check("выключенное расписание молчит", not ok and not note, note)

    weekly.set_at(CHAT, "09:00")
    check("включение записалось", weekly.at(CHAT) == "09:00",
          weekly.at(CHAT))

    # ── день и окно ──────────────────────────────────────────────────
    ok, _ = weekly.due_now(CHAT, monday(8, 59))
    check("до назначенной минуты не пора", not ok)

    ok, _ = weekly.due_now(CHAT, monday(9, 0) + timedelta(days=1))
    check("вторник расписание не поднимает", not ok)

    ok, note = weekly.due_now(CHAT, monday(9, 1))
    check("минута пришла — пора", ok, note)

    db.set_tenant(CHAT, digest_last="")
    late = monday(9, 0) + timedelta(minutes=weekly.GRACE_MIN + 1)
    ok, note = weekly.due_now(CHAT, late)
    check("окно закрылось — пропуск, а не сводка", not ok and bool(note),
          note)
    check("пропуск назван человеку словами",
          "окно" in note and "/research" in note, note)
    check("закрытое окно отмечено, чтобы не повторять каждую минуту",
          weekly.due_now(CHAT, late + timedelta(minutes=1))[1] == "")

    # ── повтор ловится базой ─────────────────────────────────────────
    db.set_tenant(CHAT, digest_last="")
    reg = Said()
    first = await weekly.tick(reg, CHAT, monday(9, 1))
    check("первый тик ставит сводку в очередь", first)
    check("человек узнаёт о прогоне",
          any("сводку недели" in s.lower() for s in reg.lines),
          "\n".join(reg.lines))

    second = await weekly.tick(reg, CHAT, monday(9, 2))
    check("второй тик того же утра сводку не повторяет", not second)

    rows = [r for r in bridge.waiting(CHAT) if (r["ask"] or "") == weekly.ASK]
    check("в очереди ровно одна сводка", len(rows) == 1, str(len(rows)))
    check("сводка ушла рабочим потоком Ресёрчера",
          rows and rows[0]["workflow"] == "research",
          rows[0]["workflow"] if rows else "")

    # ── отметка переживает перезапуск ────────────────────────────────
    row = db.one("SELECT digest_last FROM tenants WHERE chat_id = ?", CHAT)
    check("отметка лежит в базе, а не в памяти процесса",
          (row["digest_last"] or "") == monday(9, 1).date().isoformat(),
          str(row["digest_last"]))

    # ── логотипы обложки ─────────────────────────────────────────────
    text = ("В Claude Code появился беспризорный прогон. OpenAI показал "
            "именные внедрения. Gemini 4 Argon раскатан на верхнем тарифе.")
    marks = virale.tags(text)
    check("марки недели сняты с текста сводки", len(marks) == 3, str(marks))
    check("порядок марок не зависит от порядка абзацев",
          marks == virale.tags("Gemini. OpenAI. Claude."), str(marks))
    check("марки вне набора в кадр не попадают",
          virale.tags("Новая модель от неизвестной лаборатории") == [])

    ask = virale.prompt("СТИЛЬ", marks)
    check("больше трёх знаков на обложку не ставим",
          len(virale.prompt("СТИЛЬ", list(virale.LOGOS))
              .split("app-icon tiles")[1].split(".\n")[0].split(";")) == 3)
    check("стиль бренда попадает в запрос", "СТИЛЬ" in ask)
    check("слов на обложке нет, и это сказано",
          "No text" in ask and "no captions" in ask)

    # ── правило стиля на диске ───────────────────────────────────────
    from storage.brand import Brand
    b = Brand("lily-space", cfg.brands_path / "lily-space")
    look, gap = virale.style(b)
    check("правило обложки лежит в папке бренда", not gap, gap)
    check("правило не пустое", len(look) > 100, look[:80])

    missing = Brand("нет-такого", cfg.brands_path / "нет-такого")
    _, gap = virale.style(missing)
    check("правила нет — названа дыра, а не молчание", bool(gap), gap)

    # ── ключа нет: строка, а не поломка ──────────────────────────────
    key = cfg.chatplace_key
    object.__setattr__(cfg, "chatplace_key", "")
    try:
        check("без ключа Virale не готов", not virale.ready())
        try:
            virale.call("virale_image_generate", {})
            check("без ключа вызов отказывает", False, "вызов прошёл")
        except virale.NoCover as e:
            check("без ключа вызов отказывает словами",
                  "ChatPlace" in str(e), str(e))
    finally:
        object.__setattr__(cfg, "chatplace_key", key)

    # ── адрес обложки проверяется ────────────────────────────────────
    try:
        virale.fetch("http://example.com/cover.jpg")
        check("адрес не https отвергается", False, "скачал")
    except virale.NoCover as e:
        check("адрес не https отвергается", "https" in str(e), str(e))

    # ── обложку подвешиваем только расписанию ────────────────────────
    import inspect

    from bots import handlers
    src = inspect.getsource(handlers._serve)
    check("обложка идёт только понедельничной сводке",
          "weekly.ASK" in src, src[-300:])
    check("обложка идёт перед текстом, а не подписью под ним",
          "_cover(chat_id, tkey, res.text)" in
          inspect.getsource(handlers._deliver))

    # ── расписание тикает не за выключателем автопубликации ──────────
    clock = inspect.getsource(handlers.clock)
    check("сводка тикает отдельно от автопубликации",
          "weekly.tick" in clock and "digest_at" in clock)

    report()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
