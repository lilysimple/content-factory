"""Цикл 3: злой тестировщик.

Модель подменена нарочно кривыми ответами: не тем форматом даты, датой
вне окна, занятой датой, чужой целью. Плюс всё, что ломается на границах:
потолок сообщения, столкновение id, пустое окно, исчерпанный бюджет.
"""
from __future__ import annotations

import asyncio
import json

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from bots.registry import _split                                  # noqa: E402
from config import cfg                                            # noqa: E402
from orchestrator import agent, strategy                          # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)


def plan(themes: list[dict]) -> str:
    base = {"plat": "telegram", "format": "пост", "rubric": "р",
            "goal": "warm", "arch": "а", "funnel_stage": "tofu",
            "title": "т", "hook": "х", "why": "з", "angle": "у",
            "charge": "c", "variants": []}
    return json.dumps({"context": ["a", "b"], "unmet": [],
                       "themes": [{**base, **t} for t in themes]},
                      ensure_ascii=False)


def install(answer):
    async def ask(role, chat_id, prompt, **kw):
        return answer(prompt) if callable(answer) else answer
    agent.ask = ask


def rows():
    return db.q("SELECT id, date, goal, status FROM themes WHERE chat_id = ? "
                "ORDER BY id", CHAT)


async def main() -> None:
    reg = FakeRegistry()
    win = [d.isoformat() for d in strategy._window(CHAT)]
    today = strategy._today(CHAT).isoformat()

    # ── 1. дата вне окна ──────────────────────────────────────────────
    print("\n1. Модель придумала дату вне окна")
    install(plan([{"date": "2027-01-01", "title": "из будущего"},
                  {"date": win[0], "title": "нормальная"}]))
    await strategy.run(reg, CHAT, "план")
    got = {r["date"] for r in rows()}
    check("дата вне окна не записана", "2027-01-01" not in got,
          f"в базе {sorted(got)}")

    # ── 2. дата в прошлом ─────────────────────────────────────────────
    print("\n2. Дата в прошлом")
    for r in db.q("SELECT id FROM themes WHERE chat_id = ?", CHAT):
        pass
    db.q("SELECT 1")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
    reg.clear()
    install(plan([{"date": "2020-05-05", "title": "вчерашняя"},
                  {"date": win[1], "title": "нормальная"}]))
    await strategy.run(reg, CHAT, "план")
    got = {r["date"] for r in rows()}
    check("прошлая дата не записана", "2020-05-05" not in got,
          f"в базе {sorted(got)}")

    # ── 3. мусор вместо даты ──────────────────────────────────────────
    print("\n3. Мусор вместо даты")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
    reg.clear()
    install(plan([{"date": "как-нибудь на неделе", "title": "кривая"},
                  {"date": win[2], "title": "нормальная"}]))
    await strategy.run(reg, CHAT, "план")
    got = {r["date"] for r in rows()}
    check("нераспознанная дата не записана",
          "как-нибудь на неделе" not in got, f"в базе {sorted(got)}")
    card = reg.last_in(strategy.DRAFT_TOPIC)
    check("карточка не падает на кривой дате", card is not None
          and "Traceback" not in (card.text or ""))

    # ── 4. занятая дата ───────────────────────────────────────────────
    print("\n4. Модель целится в занятый слот")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, status, title) "
                  "VALUES (?,?,?,?,?,?)",
                  (f"{win[0]}-telegram-01", CHAT, win[0], "telegram",
                   "ready", "уже стоит"))
    reg.clear()
    install(plan([{"date": win[0], "title": "второй за день"}]))
    await strategy.run(reg, CHAT, "план")
    same_slot = [r for r in rows() if r["date"] == win[0]]
    check("второй пост в занятый слот не встал", len(same_slot) == 1,
          f"на {win[0]}/telegram тем: {len(same_slot)}")

    # ── 5. столкновение id ────────────────────────────────────────────
    print("\n5. Столкновение id")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
    install(plan([{"date": win[0], "title": "первая"}]))
    await strategy.run(reg, CHAT, "план")
    await strategy.on_callback(reg, CHAT, "ok")
    ids_before = {r["id"] for r in rows()}
    check("первый id это -01", f"{win[0]}-telegram-01" in ids_before,
          str(ids_before))

    # ── 6. чужая цель в goal ──────────────────────────────────────────
    print("\n6. Цель не из словаря")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
    reg.clear()
    install(plan([{"date": win[1], "goal": "продажа", "title": "чужая цель"}]))
    await strategy.run(reg, CHAT, "план")
    draft = reg.last_in(strategy.DRAFT_TOPIC)
    check("карточка собралась", draft is not None
          and "Баланс" in draft.text, "баланс не посчитался")

    # ── 7. окно занято целиком ────────────────────────────────────────
    print("\n7. Свободных дат нет")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        for i, d in enumerate(win):
            for p_ in strategy.PLATFORMS:
                c.execute("INSERT INTO themes (id, chat_id, date, plat, status,"
                          " title) VALUES (?,?,?,?,?,?)",
                          (f"{d}-{p_}-01", CHAT, d, p_, "ready", f"т{i}{p_}"))
    w = strategy._window(CHAT)
    busy, free = strategy._free(CHAT, w)
    layers = strategy._layers(CHAT, busy, free, "план")
    check("свободных дат действительно нет", free == [], str(free))

    reg.clear()
    install(plan([{"date": win[0], "title": "втискиваюсь"}]))
    await strategy.run(reg, CHAT, "план")
    check("на полном окне сказал, что ставить некуда",
          "ставить некуда" in reg.texts().lower(), reg.texts()[:150])
    check("на полном окне ничего не записал",
          len([r for r in rows() if r["status"] == "idea"]) == 0)

    # ── 8. потолок сообщения ──────────────────────────────────────────
    print("\n8. Потолок 4096")
    long_card = "\n\n".join(f"<b>абзац {i}</b>\nстрока " + "я" * 200
                            for i in range(40))
    chunks = _split(long_card)
    check("режется на части", len(chunks) > 1, f"частей {len(chunks)}")
    check("каждая часть влезает",
          all(len(c) <= 4096 for c in chunks),
          f"максимум {max(len(c) for c in chunks)}")
    check("текст не потерян",
          sum(len(c) for c in chunks) >= len(long_card) * 0.95)

    # ── 9. неразрывный кусок длиннее потолка ──────────────────────────
    print("\n9. Один абзац длиннее потолка")
    monolith = "д" * 9000
    chunks = _split(monolith)
    check("монолит тоже режется", all(len(c) <= 4096 for c in chunks),
          f"максимум {max(len(c) for c in chunks)}")

    # ── 10. бюджет исчерпан ───────────────────────────────────────────
    print("\n10. Бюджет исчерпан")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))

    async def broke(role, chat_id, prompt, **kw):
        raise agent.BudgetExceeded("дневной лимит 200 вызовов исчерпан")
    agent.ask = broke

    reg.clear()
    await strategy.run(reg, CHAT, "план")
    check("про бюджет сказано человеку", "лимит" in reg.texts().lower(),
          reg.texts()[:120])
    check("на бюджете в базу не пишем", len(rows()) == 0)

    # ── 11. профиля нет ───────────────────────────────────────────────
    print("\n11. Профиля нет")
    db.set_tenant(CHAT, brand_slug=None)
    reg.clear()
    await strategy.run(reg, CHAT, "план")
    check("честно сказал, что планировать не по чему",
          "не по чему" in reg.texts().lower(), reg.texts()[:120])


asyncio.run(main())
raise SystemExit(report())
