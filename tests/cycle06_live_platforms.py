"""Цикл 6: живая модель на трёх площадках."""
from __future__ import annotations

import asyncio
import logging
import re

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import desk, strategy                           # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)
logging.basicConfig(level=logging.WARNING, format="%(name)s %(message)s")


def manual_norm() -> int:
    """Сколько единиц в неделю человек выкладывает руками, по `platforms.md`.

    Читается таблица «Регулярность»: строка площадки, колонка «В неделю».
    Профиля нет или строка не разобралась — берём мягкий потолок, чтобы
    цикл падал на поведении модели, а не на разборе таблицы.
    """
    b = desk.brand(CHAT)
    text = b.section("platforms", "Регулярность") if b else ""
    total = 0
    for line in (text or "").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        plat = cells[0].lower()
        if plat in strategy.AUTO_PUBLISH or plat not in strategy.PLATFORMS:
            continue
        if m := re.match(r"(\d+)", cells[1]):
            total += int(m.group(1))
    return total or 5


async def main() -> None:
    reg = FakeRegistry()
    win = {d.isoformat() for d in strategy._window(CHAT)}

    await strategy.run(reg, CHAT, "план на неделю")

    rows = db.q("SELECT id, date, plat, format, goal, title FROM themes "
                "WHERE chat_id = ? ORDER BY date, plat", CHAT)
    check("план собрался", len(rows) >= 5, f"тем {len(rows)}")
    check("все даты внутри окна", {r["date"] for r in rows} <= win,
          str(sorted({r["date"] for r in rows} - win)))
    check("все площадки подключены",
          {r["plat"] for r in rows} <= set(strategy.PLATFORMS),
          str({r["plat"] for r in rows}))
    check("площадок больше одной", len({r["plat"] for r in rows}) > 1,
          f"использована только {({r['plat'] for r in rows})}")
    check("форматы из наборов площадок",
          all((r["format"] or "").lower() in strategy.PLATFORMS[r["plat"]]
              for r in rows),
          str([(r["plat"], r["format"]) for r in rows]))
    check("слот не задвоен",
          len({(r["date"], r["plat"]) for r in rows}) == len(rows),
          "две темы в один слот")

    # Потолок ручной нагрузки берётся из профиля бренда, а не из головы.
    # Раньше здесь стояла четвёрка, написанная 15.08 — когда `platforms.md`
    # ещё не существовало. Профиль завели 31.08 с нормой «Instagram 5 в
    # неделю», и тест стал непроходимым при любом поведении модели: пять
    # больше четырёх. Проверка, которая не может пройти, не держит границу,
    # а прячет её. Теперь норма читается оттуда же, откуда её читает роль.
    manual = [r for r in rows if r["plat"] not in strategy.AUTO_PUBLISH]
    cap = manual_norm()
    check("ручная нагрузка в пределах нормы профиля", len(manual) <= cap,
          f"вне Telegram слотов {len(manual)}, норма профиля {cap}")

    card = reg.last()
    rejected = [u for u in card.text.split("Не сошлось:")[-1].split(";")
                if "отброшена" in u] if "Не сошлось" in card.text else []
    check("живой план ничего не потерял на валидации", not rejected,
          str(rejected))

    print()
    for r in rows:
        print(f"  {r['date']} · {r['plat']:9} · {r['format'] or '—':9} · "
              f"{r['title']}")
    print(f"\n  по площадкам: {strategy.by_platform([dict(r) for r in rows])}")


asyncio.run(main())
raise SystemExit(report())
