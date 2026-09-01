"""Цикл 2: живая модель.

Здесь проверяется то, что подменой не поймать: что чат действительно
разговаривает с моделью, что кеш префикса работает, что Ассистент
отвечает данными о состоянии, а не выдумкой.
"""
from __future__ import annotations

import asyncio
import logging

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import agent, refresh, reply, strategy          # noqa: E402
from storage import db                                            # noqa: E402
from validators import check_voice                                # noqa: E402

db.init(cfg.db_path)

# Лог агента ловим, чтобы увидеть кеш и размер входа.
USAGE: list[str] = []


class Catch(logging.Handler):
    def emit(self, record):
        if record.name == "agent":
            USAGE.append(record.getMessage())


logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
logging.getLogger().addHandler(Catch())
logging.getLogger("httpx").setLevel(logging.WARNING)


def cache_read(line: str) -> int:
    """«вход 6647 (из кеша 0, 0%), выход 7911» → 0"""
    try:
        return int(line.split("из кеша")[1].split(",")[0].strip())
    except (IndexError, ValueError):
        return -1


async def main() -> None:
    reg = FakeRegistry()

    # ── 1. Ассистент отвечает моделью ─────────────────────────────────
    print("\n1. Ассистент разговаривает")
    await reply.answer(reg, CHAT, "привет, ты кто и что умеешь?")
    out = reg.texts()
    check("ответ пришёл", len(out) > 80, f"{len(out)} знаков")
    check("не заготовка про заглушку", "не подключён к работе" not in out)
    print(f"    → {out[:220].replace(chr(10), ' ')}…")

    # ── 2. Ассистент отвечает о состоянии данными ─────────────────────
    print("\n2. Состояние из базы, а не из головы")
    reg.clear()
    await reply.answer(reg, CHAT, "какие роли уже подключены, а какие нет?")
    out = reg.texts().lower()
    # Раньше здесь стояли две вшитые фразы: «Стратег подключён», «Редактор
    # ещё нет». Обе описывали август. Сегодня работают все семь ролей, и
    # первая проверка падала на живой формулировке, а вторая проходила по
    # случайности — модель просто не сказала «Редактор подключён», хотя он
    # подключён. Вшитое утверждение о состоянии переживает то состояние,
    # которое описывало: это самый повторяющийся баг проекта, и стенд не
    # должен его воспроизводить.
    #
    # Проверяем то, что действительно является границей: ответ про роли
    # приходит содержательный, а не заготовкой про заглушку.
    check("ответ про роли, а не отписка", len(out) > 120, f"{len(out)} знаков")
    # Границей здесь всегда было «из базы, а не из головы», и держать надо
    # именно её. Требовать, чтобы роли были перечислены, нельзя: статусов
    # «подключена / нет» в данных не хранится вовсе, и честный ответ — это
    # сказать, что такого списка нет. Живой прогон 01.09 ровно так и
    # ответил, а проверка засчитала это за провал.
    #
    # Выдумкой было бы обратное: заявить, что роль не подключена, когда
    # работают все семь.
    invented = [w for w in ("не подключ", "ещё не подключ", "недоступн")
                if w in out]
    check("статус роли не выдуман", not invented, str(invented))
    print(f"    → {reg.texts()[:300].replace(chr(10), ' ')}…")

    # ── 3. Стратег: живой план ────────────────────────────────────────
    print("\n3. Стратег строит живой план")
    reg.clear()
    USAGE.clear()
    await strategy.run(reg, CHAT, "план на неделю")

    rows = db.q("SELECT id, date, goal, title FROM themes WHERE chat_id = ?", CHAT)
    check("темы записаны", len(rows) >= 3, f"{len(rows)}")
    check("у всех есть заголовок", all(r["title"] for r in rows))
    check("цели из словаря", all(r["goal"] in ("warm", "prod", "pers")
                                 for r in rows),
          str({r["goal"] for r in rows}))
    card = reg.last()
    check("карточка с кнопками",
          card.buttons == ["plan:ok", "plan:fix", "plan:redo"], str(card.buttons))
    check("контекст назвал отсутствие ресёрча",
          "ресёрч" in card.text.lower() or "дайджест" in card.text.lower())

    # ── 4. анти-AI правила в выдаче ───────────────────────────────────
    print("\n4. Анти-AI правила")
    body = "\n".join(r["title"] for r in rows)
    check("нет длинного тире в заголовках", "—" not in body,
          [t for t in body.split("\n") if "—" in t])
    # Правило зовём то же самое, которым режет код, — `check_voice`.
    # Здесь стояло приближение: « не » и «, а » искались в **склеенных**
    # заголовках, поэтому «не» из одной строки и «, а» из другой давали
    # находку на ровном месте. Живой прогон 01.09 так и забраковал
    # безобидное «А если я не технарь?». Две правды об одном правиле
    # расходятся молча, и расходится всегда та, что в тесте.
    contrast = [f for f in check_voice.check(body)
                if "не X, а Y" in str(f) or "противопоставл" in str(f)]
    check("нет противопоставления «не X, а Y»", not contrast, str(contrast))

    # ── 5. кеш префикса ───────────────────────────────────────────────
    print("\n5. Кеш системного промпта")
    first = [l for l in USAGE if l.startswith("strategy")]
    check("вызов застался в логе", bool(first), str(USAGE))

    reg.clear()
    USAGE.clear()
    await strategy.on_callback(reg, CHAT, "redo")
    second = [l for l in USAGE if l.startswith("strategy")]
    if second:
        read = cache_read(second[-1])
        check("второй вызов читает из кеша", read > 0,
              f"из кеша {read} — префикс не совпал")
        print(f"    → {second[-1]}")

    # ── 6. правка профиля словами ─────────────────────────────────────
    print("\n6. Правка профиля словами")
    reg.clear()
    await refresh.edit(reg, CHAT, "добавь стоп-слово «прорыв»")
    out = reg.texts()
    check("предложил правку", len(out) > 60, out[:120])
    kb = [s for s in reg.sent if s.buttons]
    check("правка через подтверждение", bool(kb),
          "кнопок нет, профиль правится молча")
    if kb:
        check("кнопки записать/отменить",
              set(kb[-1].buttons) == {"edit:ok", "edit:no"}, str(kb[-1].buttons))

    core_before = (harness.TMP / "brands" / "lily-space" / "core.md").read_text()
    reg.clear()
    await refresh.on_edit_callback(reg, CHAT, "ok")
    core_after = (harness.TMP / "brands" / "lily-space" / "core.md").read_text()
    check("профиль изменился после подтверждения", core_before != core_after)
    check("стоп-слово попало в ядро", "прорыв" in core_after.lower(),
          "модель не добавила стоп-слово")
    check("профиль не обрушился", len(core_after) > len(core_before) * 0.7,
          f"было {len(core_before)}, стало {len(core_after)}")

    print(f"\nживых вызовов модели: {len(USAGE)} (+ прошлые блоки)")


asyncio.run(main())
raise SystemExit(report())
