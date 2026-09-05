"""Цикл 7: Редактор без модели.

Главное здесь — что скрипт проверки действительно даёт отказ и текст
уходит на переписывание, а не в чат с оговоркой.
"""
from __future__ import annotations

import asyncio
import json

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import agent, desk, editor                          # noqa: E402
from validators import check_voice                                # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

ROUNDS = {"n": 0, "prompts": [], "stable": []}


def answer(text: str, voice: int = 5, notes=None) -> str:
    return json.dumps({
        "text": text,
        "checks": {"hook": 4, "recognition": 4, "pass_on": 4,
                   "voice": voice, "freshness": 4},
        "hold": "держит примером", "breaks": "угол узкий",
        "notes": notes or [],
    }, ensure_ascii=False)


def install(*answers):
    """Ответы по кругам: первый вызов вернёт первый, второй — второй."""
    seq = list(answers)

    async def ask(role, chat_id, prompt, **kw):
        ROUNDS["n"] += 1
        ROUNDS["prompts"].append(prompt)
        ROUNDS["stable"].append(kw.get("stable") or "")
        return seq[min(ROUNDS["n"] - 1, len(seq) - 1)]
    agent.ask = ask
    ROUNDS["n"] = 0
    ROUNDS["prompts"].clear()
    ROUNDS["stable"].clear()


def seed_themes():
    """Три темы в плане: две telegram, одна instagram."""
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        rows = [
            ("2026-08-15-telegram-01", "2026-08-15", "telegram", "пост",
             "Файл ЯДРО перестаёт объяснять заново"),
            ("2026-08-16-telegram-01", "2026-08-16", "telegram", "анонс",
             "Следующая часть воркшопа"),
            ("2026-08-17-instagram-01", "2026-08-17", "instagram", "карусель",
             "Аудитор, проджект, теперь AI"),
        ]
        for tid, d, p, f, title in rows:
            c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                      "goal, status, title, hook, why) VALUES "
                      "(?,?,?,?,?,'warm','idea',?,?,?)",
                      (tid, CHAT, d, p, f, title, "хук", "кому и зачем"))


def theme(tid):
    return db.one("SELECT * FROM themes WHERE id = ?", tid)


CLEAN = ("Первая строка держит сама.\n\nВторая мысль про конкретный вечер "
         "и открытый файл. Дальше вывод, что делать завтра.")


async def main() -> None:
    reg = FakeRegistry()
    seed_themes()

    # ── 1. текст пишется и ложится в файл ─────────────────────────────
    print("\n1. Текст пишется")
    install(answer(CLEAN))
    await editor.run(reg, CHAT, "напиши пост")

    row = theme("2026-08-15-telegram-01")
    check("взял ближайшую тему", row["status"] == "draft", row["status"])
    check("путь к тексту записан", row["asset"] == "posts/2026-08-15-telegram-01.md",
          str(row["asset"]))
    path = harness.TMP / "brands" / "lily-space" / row["asset"]
    check("файл на диске", path.exists(), str(path))
    if path.exists():
        body = path.read_text(encoding="utf-8")
        check("в файле служебная шапка", body.startswith("<!--"))
        check("в файле сам текст", CLEAN.split("\n")[0] in body)

    card = reg.last()
    check("карточка от Редактора", card.role == "editor", card.role)
    check("кнопки на карточке",
          [":".join(x.split(":")[:2]) for x in card.buttons] ==
          ["post:ok", "post:fix", "post:design"], str(card.buttons))
    check("id темы уехал в кнопку",
          all(x.endswith("2026-08-15-telegram-01") for x in card.buttons),
          str(card.buttons))
    check("в шапке id и площадка",
          "2026-08-15-telegram-01" in card.text and "telegram" in card.text)
    check("самопроверка в чат не ушла",
          "hook" not in card.text and "breaks" not in card.text,
          "самопроверка утекла в чат")

    # ── 2. спецификация площадки в промпте ────────────────────────────
    print("\n2. Спецификация площадки")
    p = ROUNDS["prompts"][-1]
    check("telegram-спека передана", "первая строка" in p.lower())
    check("чужие площадки не переданы",
          "125 знаков" not in p and "поисковик" not in p,
          "в промпт уехали чужие площадки")

    # ── 3. отказ скрипта: длинное тире ────────────────────────────────
    print("\n3. Длинное тире это отказ")
    seed_themes()
    install(answer("Первая строка — и сразу мысль."), answer(CLEAN))
    reg.clear()
    await editor.run(reg, CHAT, "напиши пост")
    check("переписал вторым кругом", ROUNDS["n"] == 2, f"кругов {ROUNDS['n']}")
    check("находка вернулась в промпт",
          "длинное тире" in ROUNDS["prompts"][-1], "модель не увидела причину")
    check("в чат ушёл чистый текст", "—" not in reg.last().text,
          "тире доехало до человека")

    # ── 4. отказ скрипта: стоп-слово бренда ───────────────────────────
    print("\n4. Стоп-слово бренда")
    seed_themes()
    b = desk.brand(CHAT)
    stop = b.stopwords()
    check("стоп-слова вычитаны из профиля", len(stop) >= 3, str(stop))
    if stop:
        install(answer(f"Это настоящий {stop[0]} в работе."), answer(CLEAN))
        reg.clear()
        await editor.run(reg, CHAT, "напиши пост")
        check("стоп-слово поймано скриптом", ROUNDS["n"] == 2,
              f"кругов {ROUNDS['n']}")
        check("названо стоп-словом бренда",
              "стоп-слово бренда" in ROUNDS["prompts"][-1],
              "причина не названа")

    # ── 5. два круга исчерпаны ────────────────────────────────────────
    print("\n5. Два круга исчерпаны")
    seed_themes()
    install(answer("Опять — тире."), answer("И снова — тире."))
    reg.clear()
    await editor.run(reg, CHAT, "напиши пост")
    check("больше двух кругов не крутит", ROUNDS["n"] == 2, f"{ROUNDS['n']}")
    check("честно отказался", "не прошёл проверку" in reg.texts(),
          reg.texts()[:150])
    check("не выдал с оговоркой", "Опять" not in reg.texts(),
          "плохой текст всё равно уехал в чат")
    check("тема осталась idea", theme("2026-08-15-telegram-01")["status"] == "idea")

    # ── 6. низкий балл voice ──────────────────────────────────────────
    print("\n6. Балл voice ниже трёх")
    seed_themes()
    install(answer(CLEAN, voice=2), answer(CLEAN, voice=5))
    reg.clear()
    await editor.run(reg, CHAT, "напиши пост")
    check("низкий voice отправил на второй круг", ROUNDS["n"] == 2,
          f"кругов {ROUNDS['n']}")
    check("правило названо в промпте", "voice 2" in ROUNDS["prompts"][-1],
          "модель не узнала причину")

    # ── 7. выбор темы по id и по словам ───────────────────────────────
    print("\n7. Выбор темы")
    seed_themes()
    install(answer(CLEAN))
    reg.clear()
    await editor.run(reg, CHAT, "напиши пост 2026-08-17-instagram-01")
    check("тема выбрана по id",
          theme("2026-08-17-instagram-01")["status"] == "draft",
          "id проигнорирован")
    check("instagram-спека подставлена",
          "125 знаков" in ROUNDS["prompts"][-1], "площадка не учтена")

    seed_themes()
    install(answer(CLEAN))
    reg.clear()
    await editor.run(reg, CHAT, "напиши пост про следующую часть воркшопа")
    check("тема выбрана по словам заголовка",
          theme("2026-08-16-telegram-01")["status"] == "draft",
          "совпадение по заголовку не сработало")

    # ── 8. кнопки ─────────────────────────────────────────────────────
    print("\n8. Кнопки")
    seed_themes()
    install(answer(CLEAN))
    reg.clear()
    await editor.run(reg, CHAT, "напиши пост")
    await editor.on_callback(reg, CHAT, "ok")
    check("после Ок статус ready",
          theme("2026-08-15-telegram-01")["status"] == "ready",
          theme("2026-08-15-telegram-01")["status"])

    # Состояние соседа берётся из данных: Публикатор подключён, но канал
    # может быть не задан. Вшитая фраза тут уже врала полторы недели.
    check("про Публикатора сказано по состоянию, а не заглушкой",
          "Публикатор" in reg.texts() and
          "Публикатор ещё не подключён" not in reg.texts(),
          reg.texts()[-200:])
    check("названа настоящая причина молчания",
          ("PUBLISH_CHANNEL" in reg.texts()) == (not cfg.publish_channel),
          reg.texts()[-200:])

    reg.clear()
    await editor.on_callback(reg, CHAT, "ok")
    check("повторное Ок отбито", "неактуален" in reg.texts(), reg.texts()[:80])

    seed_themes()
    install(answer(CLEAN))
    reg.clear()
    await editor.run(reg, CHAT, "напиши пост")
    await editor.on_callback(reg, CHAT, "design")
    check("текст принят перед вёрсткой",
          theme("2026-08-15-telegram-01")["status"] == "ready",
          theme("2026-08-15-telegram-01")["status"])
    check("передан Дизайнеру, а не заглушке",
          "передаю Дизайнеру" in reg.texts() and
          "Дизайнер ещё не подключён" not in reg.texts(),
          reg.texts()[:140])

    # ── 9. правка пишется в профиль голоса ────────────────────────────
    print("\n9. Правка это обучающий сигнал")
    seed_themes()
    install(answer(CLEAN))
    reg.clear()
    await editor.run(reg, CHAT, "напиши пост")
    await editor.on_callback(reg, CHAT, "fix")
    check("ждём текст правки", editor.wants_fix(CHAT) is True)

    install(answer(CLEAN))
    reg.clear()
    await editor.revise(reg, CHAT, "меньше вопросов к читателю")
    check("флаг снят", editor.wants_fix(CHAT) is False)
    # Правка приходит к уже написанному тексту, а он в статусе draft.
    # Пока _pick смотрел только на idea, каждая правка отвечала
    # «темы нет среди неначатых» — и переписать текст было нельзя.
    check("правка нашла свою тему, а не соседнюю",
          "2026-08-15-telegram-01" in ROUNDS["prompts"][-1] and
          "нет среди" not in reg.texts(), reg.texts()[:160])
    check("текст переписан", theme("2026-08-15-telegram-01")["status"] == "draft",
          theme("2026-08-15-telegram-01")["status"])

    corr = harness.TMP / "brands" / "lily-space" / "voice-corrections.md"
    check("правка записана в профиль", corr.exists(), "файла нет")
    if corr.exists():
        check("в правке есть id темы и текст",
              "меньше вопросов" in corr.read_text(encoding="utf-8"))

    # ── 10. плана нет ─────────────────────────────────────────────────
    print("\n10. Плана нет")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
    reg.clear()
    install(answer(CLEAN))
    await editor.run(reg, CHAT, "напиши пост")
    check("сказал, что писать не о чем", "не о чем" in reg.texts(),
          reg.texts()[:120])
    check("модель не звалась", ROUNDS["n"] == 0, f"вызовов {ROUNDS['n']}")

    # ── капслок против собственных понятий завода ─────────────────────
    # Про файл ЯДРО написать пост было нельзя вовсе: скрипт считал слово
    # капслоком и давал отказ, Редактор переписывал два круга, тема
    # оставалась `idea`. Тема на 18.08 в плане была ровно такая.
    print("\n8. ЯДРО это понятие бренда, а не капслок")

    caps = lambda t: [f.fragment for f in check_voice.check(t)
                      if f.rule == "капслок"]

    check("ЯДРО не отказ", caps("Файл ЯДРО помогает не объяснять заново.") == [],
          str(caps("Файл ЯДРО помогает не объяснять заново.")))
    check("падежи ЯДРА тоже",
          caps("В ЯДРЕ записан голос, из ЯДРА берётся тон.") == [],
          str(caps("В ЯДРЕ записан голос, из ЯДРА берётся тон.")))
    check("настоящий капслок по-прежнему отказ",
          caps("Это ОЧЕНЬ важно.") == ["ОЧЕНЬ"],
          str(caps("Это ОЧЕНЬ важно.")))
    check("имена продуктов не отказ", caps("Открыл CLAUDE и TELEGRAM.") == [])
    check("короткие аббревиатуры не трогаем", caps("Наш AI и HR.") == [])

    # ── 11. спецификация формата ──────────────────────────────────────
    # До 05.09 формат темы до модели не доезжал вовсе: спека приходила
    # только по площадке, и «опрос» с «каруселью» модель достраивала
    # сама. Матрица полная, чужой формат в промпт не уезжает.
    print("\n11. Спецификация формата")

    from orchestrator.strategy import PLATFORMS, SPOKEN
    missing = [(pl, f) for pl, fs in PLATFORMS.items() for f in fs
               if f not in SPOKEN and (pl, f) not in editor.FORMAT_SPEC]
    check("матрица форматов полная", not missing, str(missing))

    check("незнакомый формат идёт на запасную строку",
          "не проставлен" in editor.spec("telegram", "манифест")[1])
    check("незнакомая площадка сваливается на telegram",
          editor.spec("mastodon", "пост") == editor.spec("telegram", "пост"))

    seed_themes()
    reg.clear()
    install(answer(CLEAN))
    await editor.run(reg, CHAT, "напиши текст по теме 2026-08-16-telegram-01")
    p_anons = ROUNDS["prompts"][-1]
    check("спека формата в промпте", "## Спецификация формата" in p_anons)
    check("это спека анонса", "Повестка" in p_anons, p_anons[-600:])
    check("чужие форматы не уехали",
          "карточку" not in p_anons.lower() or "Шесть блоков" not in p_anons,
          "в промпт уехала чужая спека")

    reg.clear()
    install(answer(CLEAN))
    await editor.run(reg, CHAT, "напиши текст по теме 2026-08-17-instagram-01")
    p_car = ROUNDS["prompts"][-1]
    check("карусель просит шесть блоков", "Шесть блоков" in p_car)
    check("подпись под карусель заказана", "600–900" in p_car, p_car[-400:])
    check("спека анонса сюда не уехала", "Повестка" not in p_car)
    check("разделитель подписи назван", desk.CAPTION_MARK in p_car)

    # Подпись под карусель на карточки не идёт: Дизайнер верстает её
    # словами, а живёт она в ленте.
    blocks, cap = desk.split_caption(
        "обложка\n\nпункт\n\n## Подпись\n\nдлинный текст #тег")
    check("блоки отрезаны от подписи", blocks == "обложка\n\nпункт", blocks)
    check("подпись поднялась целиком", cap == "длинный текст #тег", cap)
    check("без метки всё идёт блоками",
          desk.split_caption("просто текст") == ("просто текст", ""))

    # ── 12. фактура Ресёрчера доезжает до Редактора ────────────────────
    # Файла нет — это норма, а не поломка: так завод работал до 05.09.
    print("\n12. Фактура под тему")
    from orchestrator import research

    seed_themes()
    reg.clear()
    install(answer(CLEAN))
    await editor.run(reg, CHAT, "напиши пост")
    check("без фактуры раздела нет",
          "## Фактура под тему" not in ROUNDS["prompts"][-1])

    b = desk.brand(CHAT)
    b.artifact(research.FACTS_FILE.format(id="2026-08-15-telegram-01"),
               "# Фактура\n\n- **Отчёт про агентов вышел 2 сентября**\n")
    seed_themes()
    reg.clear()
    install(answer(CLEAN))
    await editor.run(reg, CHAT, "напиши пост")
    p_facts = ROUNDS["prompts"][-1]
    check("фактура уехала в промпт", "## Фактура под тему" in p_facts)
    check("сам факт на месте", "Отчёт про агентов" in p_facts)
    check("правило про память названо", "по памяти" in p_facts)
    check("человеку сказано, что фактура есть",
          "фактура" in reg.texts().lower(), reg.texts()[:200])

    # ── 13. живые тексты бренда доезжают до Редактора ──────────────────
    # Папка `voice-samples/` заводится на онбординге и до 05.09 была
    # мёртвой на прямом пути: субагент её открывал, бот — ни разу.
    print("\n13. Живые тексты бренда")

    b = desk.brand(CHAT)
    samples = desk.voice_samples(b)
    check("образцы бренда прочитаны", len(samples) >= 2, str(len(samples)))
    check("служебная шапка снята",
          all(not t.startswith("<!--") for _, t in samples))
    check("коротких обрывков нет",
          all(len(t) >= desk.SAMPLES_MIN for _, t in samples))
    check("бюджет раздела соблюдён",
          sum(len(t) for _, t in samples) <= desk.SAMPLES_BUDGET)

    seed_themes()
    reg.clear()
    install(answer(CLEAN))
    await editor.run(reg, CHAT, "напиши пост")
    st = ROUNDS["stable"][-1]
    check("образцы уехали в стабильную часть", "## Как звучит бренд" in st)
    check("текст образца на месте", samples[0][1][:60] in st, st[:200])
    check("образцы не в теле запроса",
          "## Как звучит бренд" not in ROUNDS["prompts"][-1])
    check("сказано, что это не источник тем", "не темы" in st)
    check("про пустую папку человеку не сказано",
          "voice-samples" not in reg.texts())

    # Пустая папка — находка, а не молчание.
    import shutil
    folder = b.path(desk.SAMPLES_DIR)
    keep = folder.parent / "voice-samples.keep"
    shutil.move(str(folder), str(keep))
    folder.mkdir()
    try:
        seed_themes()
        reg.clear()
        install(answer(CLEAN))
        await editor.run(reg, CHAT, "напиши пост")
        check("без образцов раздела нет",
              "## Как звучит бренд" not in ROUNDS["stable"][-1])
        check("пустая папка названа человеку",
              "voice-samples" in reg.texts(), reg.texts()[-300:])
    finally:
        shutil.rmtree(folder)
        shutil.move(str(keep), str(folder))

    # ── 14. правки человека возвращаются в промпт ──────────────────────
    # `note` писал их с первого дня, читать их не читал никто.
    print("\n14. Накопленные правки")

    # Правку из круга 6 записал `note` — теперь её можно и прочитать.
    check("правка из кнопки «Правки» читается",
          any("меньше вопросов" in ln for ln in editor.table.learned(b)),
          str(editor.table.learned(b)))
    editor.table.note(CHAT, "2026-08-15-telegram-01", "не начинай с вопроса")
    check("свежая правка идёт последней",
          editor.table.learned(b)[-1] ==
          "2026-08-15-telegram-01: не начинай с вопроса",
          str(editor.table.learned(b)[-1]))

    for i in range(desk.CORRECTIONS_TAIL + 3):
        editor.table.note(CHAT, "2026-08-15-telegram-01", f"правка {i}")
    got = editor.table.learned(b)
    check("в промпт едет хвост, а не весь файл",
          len(got) == desk.CORRECTIONS_TAIL, str(len(got)))
    check("хвост это самые свежие", got[-1].endswith("правка 14"), got[-1])

    seed_themes()
    reg.clear()
    install(answer(CLEAN))
    await editor.run(reg, CHAT, "напиши пост")
    st = ROUNDS["stable"][-1]
    check("правки уехали в стабильную часть",
          "## Правки, которые человек уже называл" in st)
    check("сама правка на месте", "правка 14" in st)
    check("сказано, что правка сильнее дефолта", "сильнее" in st)

    # Образцы стоят перед правками: они меняются реже, а кеш это
    # совпадение префикса.
    check("образцы стоят до правок",
          st.index("## Как звучит бренд") < st.index("## Правки"))


asyncio.run(main())
raise SystemExit(report())
