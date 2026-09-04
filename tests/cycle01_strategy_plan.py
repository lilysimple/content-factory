"""Цикл 1: жизненный цикл плана без похода в модель.

Модель подменяется заготовленным JSON. Так гоняются все ветки кнопок
столько раз, сколько надо, и видно поведение кода, а не качество текста.
"""
from __future__ import annotations

import asyncio
import json

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from bots.router import resolve                                   # noqa: E402
from config import cfg                                            # noqa: E402
from orchestrator import agent, strategy                          # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

CALLS = {"n": 0, "prompts": []}


def fake_plan(dates: list[str], goals: list[str]) -> str:
    themes = []
    for i, (d, g) in enumerate(zip(dates, goals)):
        themes.append({
            "date": d, "plat": "telegram", "format": "пост",
            "rubric": f"рубрика{i}", "goal": g, "arch": "разбор ошибки",
            "funnel_stage": "tofu", "title": f"тема {i}",
            "hook": f"хук {i}", "why": f"кому и зачем {i}",
            "angle": "разбор ошибки", "charge": "relatable pain",
            "variants": [{"angle": "показ процесса", "charge": "curiosity",
                          "hook": "смотри как"}] if i == 0 else [],
        })
    return json.dumps({
        "context": ["строка 1", "ресёрча нет, экспресс-режим", "строка 3"],
        "themes": themes,
        "unmet": ["метрик нет"],
    }, ensure_ascii=False)


def install(answer):
    """Подменить модель. answer — строка или функция от промпта."""
    async def ask(role, chat_id, prompt, **kw):
        CALLS["n"] += 1
        CALLS["prompts"].append(prompt)
        return answer(prompt) if callable(answer) else answer
    agent.ask = ask


def themes_in_db():
    return db.q("SELECT id, date, status, goal, title FROM themes "
                "WHERE chat_id = ? ORDER BY date", CHAT)


async def main() -> None:
    reg = FakeRegistry()
    win = [d.isoformat() for d in strategy._window(CHAT)]

    # ── 1. маршрутизация ──────────────────────────────────────────────
    print("\n1. Маршрутизация")
    cases = [("план на неделю", "strategy"), ("что постить", "strategy"),
             ("нужны темы на неделю", "strategy"), ("напиши пост", "editor"),
             ("покажи ядро", "assistant"), ("опубликуй", "publisher"),
             # Работа по названной теме: короткий триггер `тем` ловится
             # внутри слова «теме» и раньше уводил задачу Стратегу —
             # человек просил текст, а завод собирал план недели.
             ("напиши пост по теме 2026-09-01-telegram-01", "editor"),
             ("свёрстай обложку по теме 2026-09-01-telegram-01", "design"),
             ("сценарий рилса по теме 2026-09-02-instagram-01", "reels")]
    for text, want in cases:
        got = resolve(text, None, reg.me).role
        check(f"{text!r} → {want}", got == want, f"получено {got}")

    # ── 2. план строится и ложится в базу ─────────────────────────────
    print("\n2. План строится")
    install(fake_plan(win[:3], ["warm", "prod", "pers"]))
    await strategy.run(reg, CHAT, "план на неделю")

    rows = themes_in_db()
    check("три темы в базе", len(rows) == 3, f"{len(rows)}")
    check("статус idea", all(r["status"] == "idea" for r in rows))
    check("id стабильного вида",
          all(r["id"] == f"{r['date']}-telegram-01" for r in rows),
          str([r["id"] for r in rows]))

    card = reg.last_in(strategy.DRAFT_TOPIC)
    check("карточка от Стратега", card.role == "strategy", card.role)
    check("кнопки на карточке",
          card.buttons == ["plan:ok", "plan:fix", "plan:redo"],
          str(card.buttons))
    check("баланс посчитан", "Баланс:" in card.text)
    check("unmet показан", "Не сошлось" in card.text)
    check("варианты углов в карточке", "показ процесса" in card.text)

    # ── 3. слои попали в промпт ───────────────────────────────────────
    print("\n3. Слои в промпте")
    p = CALLS["prompts"][-1]
    check("свободные даты переданы", win[0] in p)
    check("сказано, что дайджеста нет", "дайджеста нет" in p.lower())
    check("запасная пропорция названа", "60/20/20" in p)
    check("площадка ограничена telegram", "только Telegram" in p)

    # ── 4. выгрузка в папку бренда ────────────────────────────────────
    print("\n4. Выгрузка")
    plans = sorted((harness.TMP / "brands" / "lily-space" / "plans").glob("*.md"))
    check("файл плана записан", len(plans) == 1, str(plans))
    if plans:
        text = plans[0].read_text(encoding="utf-8")
        check("в выгрузке есть таблица", "| id |" in text)
        check("в выгрузке есть контекст", "Контекст недели" in text)

    # ── 5. «другие темы» убирают батч ─────────────────────────────────
    print("\n5. Другие темы")
    reg.clear()
    install(fake_plan(win[3:6], ["warm", "warm", "prod"]))
    await strategy.on_callback(reg, CHAT, "redo")

    rows = themes_in_db()
    check("старый батч удалён, новый на месте", len(rows) == 3, f"{len(rows)}")
    check("даты сменились", {r["date"] for r in rows} == set(win[3:6]),
          str(sorted(r["date"] for r in rows)))

    # ── 6. утверждение ────────────────────────────────────────────────
    print("\n6. Утверждение")
    reg.clear()
    await strategy.on_callback(reg, CHAT, "ok")
    rows = themes_in_db()
    check("темы остались в базе", len(rows) == 3, f"{len(rows)}")
    check("статус не поменялся", all(r["status"] == "idea" for r in rows))
    check("сказано про Редактора", "Редактор" in reg.texts())
    check("не врёт про неподключённого Редактора",
          "не подключён" not in reg.texts().lower(), reg.texts()[-160:])
    check("подсказал следующий шаг",
          "напиши пост" in reg.texts().lower(), reg.texts()[-160:])

    # ── 7. повторное нажатие после утверждения ────────────────────────
    print("\n7. Повторное нажатие")
    reg.clear()
    await strategy.on_callback(reg, CHAT, "ok")
    check("второе утверждение отбито", "неактуален" in reg.texts(),
          reg.texts()[:80])
    check("темы не задвоились", len(themes_in_db()) == 3)

    # ── 8. правки ─────────────────────────────────────────────────────
    print("\n8. Правки")
    reg.clear()
    install(fake_plan(win[:2], ["warm", "pers"]))
    await strategy.run(reg, CHAT, "план на неделю")
    check("новый батч предложен", strategy.wants_fix(CHAT) is False)

    reg.clear()
    await strategy.on_callback(reg, CHAT, "fix")
    check("ждём текст правки", strategy.wants_fix(CHAT) is True)

    reg.clear()
    install(fake_plan(win[:2], ["prod", "prod"]))
    await strategy.revise(reg, CHAT, "больше продуктовых")
    check("флаг правки снят", strategy.wants_fix(CHAT) is False)
    check("правка ушла в промпт", "больше продуктовых" in CALLS["prompts"][-1])
    rows = themes_in_db()
    check("батч пересобран, не задвоен", len(rows) == 5, f"{len(rows)}")

    # ── 9. занятые слоты ──────────────────────────────────────────────
    print("\n9. Занятые слоты")
    busy = strategy._busy(CHAT, strategy._window(CHAT))
    check("утверждённые слоты видны как занятые",
          {(d, "telegram") for d in win[3:6]} <= set(busy),
          str(sorted(busy)))
    w = strategy._window(CHAT)
    busy, free = strategy._free(CHAT, w)
    layers = strategy._layers(CHAT, busy, free, "ещё план")
    check("занятый слот не предлагается свободным",
          all((d, "telegram") not in free for d in win[3:6]),
          "занятый слот попал в свободные")
    check("другие площадки на тот же день свободны",
          all((d, "instagram") in free for d in win[3:6]),
          "instagram потерялся из-за занятого telegram")

    # ── 10. модель вернула мусор ──────────────────────────────────────
    print("\n10. Модель вернула мусор")
    reg.clear()
    before = len(themes_in_db())
    install("извините, не могу")
    await strategy.run(reg, CHAT, "план на неделю")
    check("сказал, что не собралось", "не собрался" in reg.texts().lower(),
          reg.texts()[:120])
    check("в базу ничего не записал", len(themes_in_db()) == before)

    reg.clear()
    install('{"themes": [], "context": [], "unmet": []}')
    await strategy.run(reg, CHAT, "план на неделю")
    check("пустой план это отказ", "не собрался" in reg.texts().lower(),
          reg.texts()[:120])

    print(f"\nвызовов модели (подменённых): {CALLS['n']}")


asyncio.run(main())
raise SystemExit(report())
