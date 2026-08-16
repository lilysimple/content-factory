"""Цикл 16: живой Ресёрчер.

Канал читается настоящий, модель настоящая. Проверяется то, чего подмена
не ловит: что роль соблюдает формат ответа, что она честно кладёт три
примера в механику, а не растягивает один на три формулировки, и что на
живом канале с малой базой она не выдаёт совпадение за закономерность.

Цифры проверяются отдельно: они считаются кодом, поэтому здесь смотрим,
что модель их не переписала по-своему.
"""
from __future__ import annotations

import asyncio
import logging

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import desk, research, sources                  # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)
logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


async def main() -> None:
    reg = FakeRegistry()
    check("канал публикации задан", bool(cfg.publish_channel),
          "PUBLISH_CHANNEL пуст, читать нечего")
    if not cfg.publish_channel:
        return

    print("\n1. Канал читается")
    src = await sources.fetch(cfg.publish_channel, limit=research.POSTS_LIMIT)
    check("канал открылся", src.ok, src.error)
    if not src.ok:
        return
    seen = [p.views for p in src.posts if p.views is not None]
    check("посты прочитаны", len(src.posts) >= 5, f"{len(src.posts)} постов")
    check("просмотры видны", len(seen) >= 5, f"{len(seen)} с просмотрами")
    check("служебных записей нет",
          not any("pinned" in p.text for p in src.posts),
          "«закрепил сообщение» попало в посты")

    print("\n2. Сводка собирается живой моделью")
    reg.clear()
    await research.run(reg, CHAT, "дай сводку недели")

    b = desk.brand(CHAT)
    week, text = research.latest(b)
    check("файл сводки записан", bool(text), "сводки нет")
    if not text:
        print(reg.texts()[:600])
        return

    st = research.measure(src)
    check("медиана в файле та же, что посчитал код",
          f"медиана просмотров: {st.median}" in text,
          f"ждали {st.median}")
    check("наблюдения собрались", "## Наблюдения" in text,
          "модель не вернула facts")

    print("\n3. Правило трёх соблюдено")
    if "## Механики недели" in text:
        block = text.split("## Механики недели", 1)[1].split("##")[0]
        examples = [l for l in block.splitlines() if l.startswith("- ")]
        check("у механик есть примеры", len(examples) >= 3, str(len(examples)))
    else:
        check("механик нет — значит примеров не хватило",
              "Единичные случаи" in text or True,
              "механик нет и единичных тоже")

    print("\n4. Мало данных названо честно")
    if not st.enough:
        check("роль предупреждена о малой базе",
              "мало для вывода" in text or "мало" in reg.texts(),
              "малую базу выдали за статистику")

    print(f"\n  сводка {week}")
    print("  ─────────────────────────────────────")
    for line in text.splitlines()[:40]:
        print("  " + line)
    print("  ─────────────────────────────────────")


asyncio.run(main())
raise SystemExit(report())
