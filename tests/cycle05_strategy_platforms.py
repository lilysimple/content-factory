"""Цикл 5: площадки.

Слот стал парой «дата плюс площадка». Проверяется, что пара держится:
чужая площадка не проходит, формат не из набора помечается, а Telegram
и Instagram спокойно стоят в один день.
"""
from __future__ import annotations

import asyncio
import json

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

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
    return db.q("SELECT id, date, plat, format, title FROM themes "
                "WHERE chat_id = ? ORDER BY id", CHAT)


def wipe():
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))


async def main() -> None:
    reg = FakeRegistry()
    win = [d.isoformat() for d in strategy._window(CHAT)]

    # ── 1. три площадки в один день ───────────────────────────────────
    print("\n1. Три площадки в один день")
    wipe()
    install(plan([
        {"date": win[0], "plat": "telegram", "format": "пост", "title": "тг"},
        {"date": win[0], "plat": "instagram", "format": "карусель", "title": "инст"},
        {"date": win[0], "plat": "youtube", "format": "видео", "title": "ютуб"},
    ]))
    await strategy.run(reg, CHAT, "план")
    got = rows()
    check("все три встали в один день", len(got) == 3, f"{len(got)}")
    check("площадки разные", {r["plat"] for r in got} ==
          {"telegram", "instagram", "youtube"}, str({r["plat"] for r in got}))
    check("id содержат площадку",
          {r["id"] for r in got} == {f"{win[0]}-{p}-01"
                                     for p in ("telegram", "instagram", "youtube")},
          str(sorted(r["id"] for r in got)))

    card = reg.last()
    check("в карточке видны площадки",
          all(p in card.text for p in ("telegram", "instagram", "youtube")),
          "площадка не показана")
    check("посчитано по площадкам", "Площадки:" in card.text)
    check("ручные слоты помечены", "✋" in card.text,
          "не сказано, что вне Telegram руками")

    # ── 2. дубль на той же площадке ───────────────────────────────────
    print("\n2. Два поста в один день на одной площадке")
    wipe()
    install(plan([
        {"date": win[1], "plat": "telegram", "title": "первый"},
        {"date": win[1], "plat": "telegram", "title": "второй"},
    ]))
    reg.clear()
    await strategy.run(reg, CHAT, "план")
    tg = [r for r in rows() if r["plat"] == "telegram"]
    check("второй на ту же площадку отброшен", len(tg) == 1, f"{len(tg)}")
    check("про отброшенное сказано", "отброшена" in reg.texts(),
          "потеря темы прошла молча")

    # ── 3. чужая площадка ─────────────────────────────────────────────
    print("\n3. Площадка не подключена")
    wipe()
    install(plan([
        {"date": win[2], "plat": "linkedin", "title": "исключённая"},
        {"date": win[2], "plat": "tiktok", "title": "выдуманная"},
        {"date": win[2], "plat": "telegram", "title": "нормальная"},
    ]))
    reg.clear()
    await strategy.run(reg, CHAT, "план")
    got = {r["plat"] for r in rows()}
    check("linkedin не прошёл", "linkedin" not in got, str(got))
    check("tiktok не прошёл", "tiktok" not in got, str(got))
    check("нормальная осталась", got == {"telegram"}, str(got))
    check("названа причина отказа", "не подключена" in reg.texts(),
          reg.texts()[-200:])

    # ── 4. формат не из набора площадки ───────────────────────────────
    print("\n4. Формат чужой площадки")
    wipe()
    install(plan([
        {"date": win[3], "plat": "telegram", "format": "карусель",
         "title": "карусель в телеге"},
    ]))
    reg.clear()
    await strategy.run(reg, CHAT, "план")
    got = rows()
    check("тема не выброшена из-за формата", len(got) == 1, f"{len(got)}")
    check("формат помечен как спорный", "не из набора" in reg.texts(),
          "формат проехал молча")

    # ── 5. площадка не указана ────────────────────────────────────────
    print("\n5. Площадка не указана")
    wipe()
    install(json.dumps({"context": ["a"], "unmet": [], "themes": [
        {"date": win[4], "title": "без площадки", "goal": "warm"},
    ]}, ensure_ascii=False))
    reg.clear()
    await strategy.run(reg, CHAT, "план")
    check("без площадки не сохраняется", len(rows()) == 0, str(rows()))
    check("сказал, что не собралось", "не собрался" in reg.texts().lower(),
          reg.texts()[:120])

    # ── 6. занят telegram, свободны остальные ─────────────────────────
    print("\n6. Занят Telegram, свободны остальные")
    wipe()
    with db.tx() as c:
        c.execute("INSERT INTO themes (id, chat_id, date, plat, status, title) "
                  "VALUES (?,?,?,?,?,?)",
                  (f"{win[5]}-telegram-01", CHAT, win[5], "telegram",
                   "ready", "занято"))
    busy, free = strategy._free(CHAT, strategy._window(CHAT))
    check("telegram на этот день занят", (win[5], "telegram") not in free)
    check("instagram на этот день свободен", (win[5], "instagram") in free)
    check("youtube на этот день свободен", (win[5], "youtube") in free)

    layers = strategy._layers(CHAT, busy, free, "план")
    check("слоты показаны парой", "Свободные слоты" in layers)
    check("форматы перечислены", "карусель" in layers and "shorts" in layers)
    check("сказано про ручную публикацию", "руками" in layers)


asyncio.run(main())
raise SystemExit(report())
