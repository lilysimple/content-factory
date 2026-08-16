"""Цикл 8: живой Редактор.

Стратег строит план настоящей моделью, Редактор пишет по нему текст.
Проверяется сквозная цепь тема → текст и то, что скрипт голоса на живом
тексте не срабатывает.
"""
from __future__ import annotations

import asyncio
import logging

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import desk, editor, strategy                       # noqa: E402
from storage import db                                            # noqa: E402
from validators import check_voice                                # noqa: E402

db.init(cfg.db_path)
logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


async def main() -> None:
    reg = FakeRegistry()

    print("\n1. Стратег строит план")
    await strategy.run(reg, CHAT, "план на неделю")
    themes = db.q("SELECT id, plat, format, title FROM themes "
                  "WHERE chat_id = ? ORDER BY date", CHAT)
    check("план собрался", len(themes) >= 3, f"тем {len(themes)}")

    print("\n2. Редактор пишет по теме")
    reg.clear()
    await editor.run(reg, CHAT, "напиши пост")

    row = db.one("SELECT * FROM themes WHERE chat_id = ? AND status = 'draft'",
                 CHAT)
    check("тема перешла в draft", row is not None, "ни одна не в draft")
    if row is None:
        print(reg.texts()[:400])
        return

    path = harness.TMP / "brands" / "lily-space" / row["asset"]
    check("файл текста на диске", path.exists(), str(path))
    body = path.read_text(encoding="utf-8") if path.exists() else ""
    text = body.split("-->", 1)[-1].strip()

    check("текст непустой", len(text) > 200, f"{len(text)} знаков")

    b = desk.brand(CHAT)
    findings = check_voice.check(text, stopwords=b.stopwords())
    check("скрипт голоса не даёт отказ", not findings,
          "; ".join(str(f) for f in findings[:4]))

    card = reg.last()
    check("карточка с кнопками",
          [":".join(x.split(":")[:2]) for x in card.buttons] ==
          ["post:ok", "post:fix", "post:design"], str(card.buttons))
    check("самопроверка не утекла в чат",
          "freshness" not in card.text and "hold" not in card.text)

    print(f"\n  тема: {row['title']}")
    print(f"  площадка: {row['plat']} · {row['format']} · {len(text)} знаков")
    print("  ─────────────────────────────────────")
    for line in text.split("\n"):
        print("  " + line)
    print("  ─────────────────────────────────────")

    print("\n3. Ок переводит в ready")
    reg.clear()
    await editor.on_callback(reg, CHAT, "ok")
    after = db.one("SELECT status FROM themes WHERE id = ?", row["id"])
    check("статус ready", after["status"] == "ready", after["status"])


asyncio.run(main())
raise SystemExit(report())
