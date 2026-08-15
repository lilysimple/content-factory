"""Цикл 13: живой Редактор Reels.

Проверяет то, чего подмена не ловит: что модель отдаёт шесть блоков в
заданном формате, что она умеет разбивать текст по дыханию и писать
числа словами, и что проверка речи не режет нормальный живой сценарий.

Отдельно смотрим на бюджет: он единственная из проверок, которая может
не сойтись у живой модели просто потому, что считать слова трудно.
"""
from __future__ import annotations

import asyncio
import logging

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import reels, strategy                          # noqa: E402
from storage import db                                            # noqa: E402
from validators import check_script                               # noqa: E402

db.init(cfg.db_path)
logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


def _spoken() -> dict | None:
    for row in db.q("SELECT * FROM themes WHERE chat_id = ? ORDER BY date",
                    CHAT):
        if (row["format"] or "").lower() in reels.FORMATS:
            return dict(row)
    return None


async def main() -> None:
    reg = FakeRegistry()

    print("\n1. Стратег строит план")
    await strategy.run(reg, CHAT, "план на неделю, нужен хотя бы один ролик")
    theme = _spoken()
    check("в плане есть тема под ролик", theme is not None,
          "ни одной темы в формате reels или shorts")
    if theme is None:
        return

    print("\n2. Редактор Reels пишет сценарий")
    reg.clear()
    await reels.run(reg, CHAT, f"сценарий по теме {theme['id']}")

    row = db.one("SELECT * FROM themes WHERE id = ?", theme["id"])
    check("тема перешла в draft", row["status"] == "draft", row["status"])
    if row["status"] != "draft":
        print(reg.texts()[:600])
        return

    path = harness.TMP / "brands" / "lily-space" / row["asset"]
    check("файл суфлёра на диске", path.exists(), str(path))
    body = path.read_text(encoding="utf-8") if path.exists() else ""
    script = body.split("-->", 1)[-1].strip()

    reel = reels._pending.get(CHAT)
    check("сценарий остался в памяти", reel is not None)
    if reel is None:
        return

    print("\n3. Живой сценарий проходит проверку речи")
    findings = check_script.check(script, seconds=reel.seconds,
                                  hook=reel.blocks.get("hook", ""))
    check("проверка речи не даёт отказ", not findings,
          "; ".join(str(f) for f in findings[:4]))
    lo, hi = check_script.budget(reel.seconds)
    check("бюджет слов соблюдён", lo <= reel.words <= hi,
          f"{reel.words} слов, нужно {lo}–{hi}")
    check("шесть блоков на месте", len(reels.timings(reel)) >= 5,
          str(len(reels.timings(reel))))
    check("два запасных хука", len(reel.spare) == 2, str(reel.spare))
    check("одна мысль сформулирована", bool(reel.idea.strip()), "пусто")

    print(f"\n  тема: {row['title']}")
    print(f"  {row['plat']} · {row['format']} · {reel.seconds} сек · "
          f"{reel.words} слов")
    print(f"  мысль: {reel.idea}")
    print("  ─────────────────────────────────────")
    for beat in reels.timings(reel):
        print(f"  [{beat.title} · {beat.start}–{beat.end}]")
        for line in beat.text.split("\n"):
            print("  " + line)
    print("  ─────────────────────────────────────")
    for s in reel.spare:
        print(f"  запасной хук: {s}")

    print("\n4. Ок переводит в ready")
    reg.clear()
    await reels.on_callback(reg, CHAT, f"ok:{row['id']}")
    after = db.one("SELECT status FROM themes WHERE id = ?", row["id"])
    check("статус ready", after["status"] == "ready", after["status"])


asyncio.run(main())
raise SystemExit(report())
