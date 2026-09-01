"""Цикл 4: живая модель после правки.

Проверка ровно одна и важная: не выбрасывает ли новая валидация настоящий
план. Если модель форматирует дату хоть немного иначе, _fit отбросит всё,
и человек получит «не собрался» на ровном месте.
"""
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

    rows = db.q("SELECT id, date, plat, goal, title FROM themes "
                "WHERE chat_id = ?", CHAT)
    check("план собрался", len(rows) >= 5, f"тем {len(rows)}")
    check("все даты внутри окна", {r["date"] for r in rows} <= win,
          str(sorted({r["date"] for r in rows} - win)))
    # Слот это пара «дата плюс площадка», а не день: в один день выходят и
    # пост в Telegram, и карусель в Instagram — так написано в профиле и в
    # NOTES.md. Проверка «одна тема в день» пришла из августа, когда
    # площадка была одна; после того как 31.08 завели `platforms.md` с
    # Instagram, она стала запрещать ровно то поведение, которого профиль
    # требует.
    check("слот не задвоен",
          len({(r["date"], r["plat"]) for r in rows}) == len(rows),
          "две темы в один слот")
    check("id собрались правильно",
          all(r["id"] == f"{r['date']}-{r['plat']}-01" for r in rows),
          str([r["id"] for r in rows][:3]))

    card = reg.last()
    rejected = [u for u in card.text.split("Не сошлось:")[-1].split(";")
                if "отброшена" in u] if "Не сошлось" in card.text else []
    check("живой план ничего не потерял на валидации", not rejected,
          str(rejected))
    print(f"\n  тем: {len(rows)}, даты: {sorted(r['date'] for r in rows)}")


asyncio.run(main())
raise SystemExit(report())
