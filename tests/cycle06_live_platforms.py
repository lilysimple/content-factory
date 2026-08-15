"""Цикл 6: живая модель на трёх площадках."""
from __future__ import annotations

import asyncio
import logging

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import strategy                                 # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)
logging.basicConfig(level=logging.WARNING, format="%(name)s %(message)s")


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

    manual = [r for r in rows if r["plat"] not in strategy.AUTO_PUBLISH]
    check("ручной нагрузки не больше четырёх за неделю", len(manual) <= 4,
          f"вне Telegram слотов: {len(manual)}")

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
