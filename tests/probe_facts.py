"""Разведка: живая фактура под тему и текст, который её использует."""
from __future__ import annotations

import asyncio, logging, time
import harness
from harness import CHAT, FakeRegistry

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import desk, editor, research                   # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)
logging.basicConfig(level=logging.WARNING)

THEMES = [
    ("2026-09-10-telegram-01", "telegram", "пост",
     "Роль настроена, а текст всё равно не мой",
     "Агент пишет грамотно, а отправить это в канал неловко."),
    ("2026-09-10-instagram-01", "instagram", "карусель",
     "Неделя из двенадцати выходов: как это помещается в жизнь",
     "Двенадцать выходов в неделю и ни одного вечера за клавиатурой."),
]


async def main() -> None:
    reg = FakeRegistry()
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        for tid, plat, fmt, title, hook in THEMES:
            c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                      "goal, status, title, hook, why, rubric) VALUES "
                      "(?,?,?,?,?,'warm','idea',?,?,?,?)",
                      (tid, CHAT, tid[:10], plat, fmt, title, hook,
                       "не-технарям, которые уже поставили у себя роли",
                       "Разбор ошибки"))

    b = desk.brand(CHAT)
    # `harness.setup` чистит research/ целиком, вместе со списком
    # источников. Для этой пробы он и есть предмет — возвращаем.
    live = harness.BRANDS / "lily-space" / research.WATCHLIST
    b.artifact(research.WATCHLIST, live.read_text(encoding="utf-8"))
    for tid, plat, fmt, title, _ in THEMES:
        print("\n" + "=" * 72)
        print(f"ФАКТУРА · {plat} · {fmt} — {title}")
        print("=" * 72)
        t0 = time.time()
        theme = dict(db.one("SELECT * FROM themes WHERE id = ?", tid))
        fx = await research.facts(CHAT, theme)
        b.artifact(research.FACTS_FILE.format(id=tid), research.facts_markdown(fx))
        print(research.facts_markdown(fx))
        print(f"[{time.time() - t0:.0f} с]")

        print("\n" + "-" * 72)
        print(f"ТЕКСТ ПО НЕЙ")
        print("-" * 72)
        t0 = time.time()
        draft = await editor.build(CHAT, f"напиши текст по теме {tid}")
        print(draft.text)
        print("-" * 72)
        print(f"{len(draft.text)} знаков · {time.time() - t0:.0f} с · "
              f"кругов {draft.rounds} · {draft.score()}")
        print(f"notes: {draft.notes}")


asyncio.run(main())
