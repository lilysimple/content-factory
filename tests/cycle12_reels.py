"""Цикл 12: Редактор Reels без модели.

Главное здесь — что суфлёрный текст собирает код, а не роль, и что
проверка речи действительно даёт отказ: цифра, длинная строка и выход
за бюджет слов уходят на переписывание, а не в чат с оговоркой.
"""
from __future__ import annotations

import asyncio
import json

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import agent, editor, reels                     # noqa: E402
from storage import db                                            # noqa: E402
from validators import check_script                               # noqa: E402

db.init(cfg.db_path)

ROUNDS = {"n": 0, "prompts": []}

# Сорок секунд это 80–95 слов. Блоки ниже дают 86.
BLOCKS = {
    "hook": "Ты открываешь папку с макетами.\nКакой из них финальный?",
    "recognition": ("Вчера вечером я искала\nверсию обложки для канала.\n"
                    "Восемь файлов с похожими\nименами и разными датами."),
    "reason": ("Дело в том, что имя файла\nхранит время, а не решение.\n"
               "Решение живёт в голове\nи стирается за сутки."),
    "shift": ("Папка становится понятной,\nкогда в ней записано,\n"
              "что мы решили и почему."),
    "step": ("Заведи один файл рядом с макетами.\nПиши туда строку после "
             "каждой правки.\nДата, что поменяли, зачем.\n"
             "Через неделю ты перестанешь\nоткрывать восемь файлов подряд."),
    "cta": "Расскажи, как ты ищешь\nсвою финальную версию.",
}

SPARE = ["А какой из них финальный?",
         "Восемь файлов, один нужный."]


def answer(blocks=None, *, voice: int = 5, spare=None, idea: str = "",
           seconds: int = 40, notes=None) -> str:
    return json.dumps({
        "idea": idea or "Папку спасает не порядок в именах, а запись решений.",
        "seconds": seconds,
        "blocks": blocks if blocks is not None else BLOCKS,
        "spare_hooks": SPARE if spare is None else spare,
        "checks": {"hook": 4, "recognition": 4, "step": 4,
                   "voice": voice, "speech": 5},
        "notes": notes or [],
    }, ensure_ascii=False)


def install(*answers):
    seq = list(answers)

    async def ask(role, chat_id, prompt, **kw):
        ROUNDS["n"] += 1
        ROUNDS["prompts"].append(prompt)
        return seq[min(ROUNDS["n"] - 1, len(seq) - 1)]
    agent.ask = ask
    ROUNDS["n"] = 0
    ROUNDS["prompts"].clear()


def seed_themes():
    """Три темы: пост, ролик в Instagram, shorts на YouTube."""
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        rows = [
            ("2026-08-15-telegram-01", "2026-08-15", "telegram", "пост",
             "Файл ЯДРО перестаёт объяснять заново"),
            ("2026-08-16-instagram-01", "2026-08-16", "instagram", "reels",
             "Восемь макетов и ни одного финального"),
            ("2026-08-17-youtube-01", "2026-08-17", "youtube", "shorts",
             "Короткий ответ про инструменты"),
        ]
        for tid, d, p, f, title in rows:
            c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                      "goal, status, title, hook, why) VALUES "
                      "(?,?,?,?,?,'warm','idea',?,?,?)",
                      (tid, CHAT, d, p, f, title, "хук", "кому и зачем"))


def theme(tid):
    return db.one("SELECT * FROM themes WHERE id = ?", tid)


REEL = "2026-08-16-instagram-01"


async def main() -> None:
    reg = FakeRegistry()
    seed_themes()

    # ── 1. сценарий пишется и ложится в файлы ─────────────────────────
    print("\n1. Сценарий пишется")
    install(answer())
    await reels.run(reg, CHAT, "сделай сценарий")

    row = theme(REEL)
    check("взял тему под ролик, а не пост", row["status"] == "draft",
          f"{REEL}: {row['status']}")
    check("пост остался нетронутым",
          theme("2026-08-15-telegram-01")["status"] == "idea")
    check("путь к суфлёру записан",
          row["asset"] == f"posts/{REEL}-script.md", str(row["asset"]))

    base = harness.TMP / "brands" / "lily-space"
    script_file = base / f"posts/{REEL}-script.md"
    notes_file = base / f"posts/{REEL}-script-notes.md"
    check("файл суфлёра на диске", script_file.exists(), str(script_file))
    check("файл разбора на диске", notes_file.exists(), str(notes_file))

    if script_file.exists():
        body = script_file.read_text(encoding="utf-8")
        check("в суфлёре служебная шапка", body.startswith("<!--"))
        check("в шапке хронометраж и слова",
              "40 сек" in body.split("\n")[0] and "слов" in body.split("\n")[0],
              body.split("\n")[0])
        clean = body.split("-->", 1)[-1]
        check("в суфлёре нет названий блоков",
              "Хук" not in clean and "hook" not in clean, clean[:120])
        check("блоки разделены пустой строкой", "\n\n" in clean.strip())

    if notes_file.exists():
        n = notes_file.read_text(encoding="utf-8")
        check("в разборе одна мысль", "Одна мысль" in n)
        check("в разборе тайминги", "0:00–" in n, n[:200])
        check("в разборе запасные хуки", SPARE[0] in n)

    # ── 2. суфлёр собирает код ────────────────────────────────────────
    print("\n2. Суфлёр собирает код")
    reel = reels.table.get(CHAT)
    check("текст собран из блоков по порядку",
          reel.script.startswith("Ты открываешь") and
          reel.script.rstrip().endswith("финальную версию."),
          reel.script[:60])
    check("слов посчитано кодом", reel.words == check_script.words(reel.script),
          f"{reel.words}")
    check("слова в бюджете сорока секунд", 80 <= reel.words <= 95,
          f"{reel.words} слов")

    beats = reels.timings(reel)
    check("шесть блоков в разборе", len(beats) == 6, str(len(beats)))
    check("тайминги идут подряд", beats[0].start == "0:00" and
          beats[1].start == beats[0].end, f"{beats[0].end} / {beats[1].start}")

    # ── 3. карточка и кнопки ──────────────────────────────────────────
    print("\n3. Карточка и кнопки")
    card, teleprompter = reg.sent[-2], reg.sent[-1]
    check("разбор пришёл от Reels", card.role == "reels", card.role)
    check("в разборе тайминги, а не текст",
          "Хук" in card.text and "Ты открываешь" not in card.text,
          card.text[:120])
    check("суфлёр отдан отдельным сообщением",
          teleprompter.text.startswith("Ты открываешь"),
          teleprompter.text[:60])
    check("кнопки на суфлёре",
          [":".join(x.split(":")[:2]) for x in teleprompter.buttons] ==
          ["reel:ok", "reel:fix", "reel:design"], str(teleprompter.buttons))
    check("id темы уехал в кнопку",
          all(x.endswith(REEL) for x in teleprompter.buttons),
          str(teleprompter.buttons))
    check("самопроверка в чат не ушла",
          "speech" not in card.text and "voice" not in card.text,
          "самопроверка утекла в чат")

    # ── 4. цифра это отказ ────────────────────────────────────────────
    print("\n4. Цифра вместо слова")
    seed_themes()
    bad = dict(BLOCKS, cta="Расскажи за 5 минут,\nкак ты ищешь версию.")
    install(answer(bad), answer())
    reg.clear()
    await reels.run(reg, CHAT, "сделай сценарий")
    check("переписал вторым кругом", ROUNDS["n"] == 2, f"кругов {ROUNDS['n']}")
    check("причина вернулась в промпт",
          "цифра вместо слова" in ROUNDS["prompts"][-1], "модель не увидела причину")
    check("в чат ушёл чистый текст", "за 5 минут" not in reg.texts(),
          "цифра доехала до человека")

    # ── 5. длинная строка это отказ ───────────────────────────────────
    print("\n5. Строка длиннее сорока знаков")
    seed_themes()
    long_line = dict(BLOCKS, shift="Папка становится понятной ровно тогда, "
                                   "когда в ней записано решение и его причина.")
    install(answer(long_line), answer())
    reg.clear()
    await reels.run(reg, CHAT, "сделай сценарий")
    check("длинная строка поймана", ROUNDS["n"] == 2, f"кругов {ROUNDS['n']}")
    check("правило названо",
          "строка длиннее" in ROUNDS["prompts"][-1], "причина не названа")

    # ── 6. бюджет слов ────────────────────────────────────────────────
    print("\n6. Бюджет слов")
    seed_themes()
    short = {k: "Одна строка тут." for k, _ in reels.BLOCKS}
    install(answer(short), answer())
    reg.clear()
    await reels.run(reg, CHAT, "сделай сценарий")
    check("короткий сценарий отбит", ROUNDS["n"] == 2, f"кругов {ROUNDS['n']}")
    check("названа вилка слов", "бюджет слов" in ROUNDS["prompts"][-1],
          "бюджет не назван")

    # ── 7. структура и запасные хуки ──────────────────────────────────
    print("\n7. Шесть блоков и два хука")
    seed_themes()
    no_step = dict(BLOCKS, step="")
    install(answer(no_step), answer())
    reg.clear()
    await reels.run(reg, CHAT, "сделай сценарий")
    check("пустой блок пойман", ROUNDS["n"] == 2, f"кругов {ROUNDS['n']}")
    check("назван именно «Шаг»", "«Шаг»" in ROUNDS["prompts"][-1],
          ROUNDS["prompts"][-1][-300:])

    seed_themes()
    install(answer(spare=["только один"]), answer())
    reg.clear()
    await reels.run(reg, CHAT, "сделай сценарий")
    check("один запасной хук это отказ", ROUNDS["n"] == 2, f"кругов {ROUNDS['n']}")

    # ── 8. тридцать секунд: сдвиг сливается с причиной ────────────────
    print("\n8. Тридцать секунд")
    seed_themes()
    merged = {
        "hook": "Ты открываешь папку с макетами.\nКакой из них финальный?",
        "recognition": ("Вчера вечером я искала обложку.\nВосемь файлов, "
                        "похожие имена,\nразные даты и ни одной\nпонятной "
                        "пометки."),
        "reason": ("Имя файла хранит время,\nа не решение. Решение живёт\n"
                   "в голове и стирается\nза сутки."),
        "shift": "",
        "step": ("Заведи один файл рядом с макетами.\nПиши строку после "
                 "каждой правки.\nДата, что поменяли, зачем."),
        "cta": "Расскажи, как ищешь свою\nфинальную версию.",
    }
    install(answer(merged, seconds=30))
    reg.clear()
    await reels.run(reg, CHAT, "сценарий на 30 секунд")
    check("хронометраж понят из просьбы",
          reels.seconds_from("сценарий на 30 секунд") == 30)
    check("пустой сдвиг на тридцати секундах разрешён", ROUNDS["n"] == 1,
          f"кругов {ROUNDS['n']}: " + reg.texts()[:200])
    check("бюджет тридцати секунд в промпте", "60–70 слов" in ROUNDS["prompts"][0],
          "бюджет не тот")

    # ── 9. два круга исчерпаны ────────────────────────────────────────
    print("\n9. Два круга исчерпаны")
    seed_themes()
    install(answer(dict(BLOCKS, cta="Ответь за 5 минут,\nкак ты ищешь.")),
            answer(dict(BLOCKS, cta="Ответь за 7 минут,\nкак ты ищешь.")))
    reg.clear()
    await reels.run(reg, CHAT, "сделай сценарий")
    check("больше двух кругов не крутит", ROUNDS["n"] == 2, f"{ROUNDS['n']}")
    check("честно отказался", "не прошёл проверку" in reg.texts(),
          reg.texts()[:150])
    check("плохой текст в чат не ушёл", "за 7 минут" not in reg.texts(),
          "отклонённый сценарий уехал человеку")
    check("тема осталась idea", theme(REEL)["status"] == "idea",
          theme(REEL)["status"])

    # ── 10. низкий балл voice ─────────────────────────────────────────
    print("\n10. Балл voice ниже трёх")
    seed_themes()
    install(answer(voice=2), answer(voice=5))
    reg.clear()
    await reels.run(reg, CHAT, "сделай сценарий")
    check("низкий voice отправил на второй круг", ROUNDS["n"] == 2,
          f"кругов {ROUNDS['n']}")
    check("правило названо в промпте", "voice 2" in ROUNDS["prompts"][-1],
          "модель не узнала причину")

    # ── 11. выбор темы ────────────────────────────────────────────────
    print("\n11. Выбор темы")
    seed_themes()
    install(answer())
    reg.clear()
    await reels.run(reg, CHAT, "сценарий 2026-08-17-youtube-01")
    check("тема выбрана по id",
          theme("2026-08-17-youtube-01")["status"] == "draft",
          "id проигнорирован")

    seed_themes()
    install(answer())
    reg.clear()
    await reels.run(reg, CHAT, "сценарий 2026-08-15-telegram-01")
    check("пост под сценарий не берётся", "сценарий ей не нужен" in reg.texts(),
          reg.texts()[:150])
    check("модель не звалась", ROUNDS["n"] == 0, f"вызовов {ROUNDS['n']}")

    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ? AND format IN "
                  "('reels','shorts')", (CHAT,))
    reg.clear()
    install(answer())
    await reels.run(reg, CHAT, "сделай сценарий")
    check("сказал, что снимать нечего", "Снимать нечего" in reg.texts(),
          reg.texts()[:120])
    check("отправил к Стратегу", "Стратегу" in reg.texts(), reg.texts()[:120])

    # ── 12. Редактор не забирает ролики ───────────────────────────────
    print("\n12. Редактор и ролики")
    seed_themes()
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE id = ?",
                  ("2026-08-15-telegram-01",))
    reg.clear()
    install(json.dumps({"text": "Первая строка держит сама.",
                        "checks": {"voice": 5}, "hold": "", "breaks": "",
                        "notes": []}, ensure_ascii=False))
    await editor.run(reg, CHAT, "напиши пост")
    check("Редактор не взял ролик", theme(REEL)["status"] == "idea",
          theme(REEL)["status"])
    check("Редактор объяснил, чья это работа",
          "Редактору Reels" in reg.texts(), reg.texts()[:160])

    # ── 13. кнопки ────────────────────────────────────────────────────
    print("\n13. Кнопки")
    seed_themes()
    install(answer())
    reg.clear()
    await reels.run(reg, CHAT, "сделай сценарий")
    await reels.on_callback(reg, CHAT, f"ok:{REEL}")
    check("после Ок статус ready", theme(REEL)["status"] == "ready",
          theme(REEL)["status"])
    check("сказал, кто делает обложку и подпись",
          "Дизайнер" in reg.texts() and "Редактор" in reg.texts(),
          reg.texts()[-200:])

    seed_themes()
    install(answer())
    reg.clear()
    await reels.run(reg, CHAT, "сделай сценарий")
    await reels.on_callback(reg, CHAT, "fix:" + REEL)
    check("ждём текст правки", reels.wants_fix(CHAT) is True)

    install(answer())
    reg.clear()
    await reels.revise(reg, CHAT, "меньше вопросов в хуке")
    check("флаг снят", reels.wants_fix(CHAT) is False)
    check("хронометраж пережил правку", "40 сек" in ROUNDS["prompts"][-1] or
          "40 секунд" in ROUNDS["prompts"][-1], ROUNDS["prompts"][-1][:200])

    corr = base / "voice-corrections.md"
    check("правка записана в профиль голоса", corr.exists(), "файла нет")
    if corr.exists():
        text = corr.read_text(encoding="utf-8")
        check("в правке помечено, что это сценарий", "(сценарий)" in text,
              text[-120:])

    # ── 14. кнопка после перезапуска ──────────────────────────────────
    print("\n14. Кнопка переживает перезапуск")
    seed_themes()
    install(answer())
    reg.clear()
    await reels.run(reg, CHAT, "сделай сценарий")
    reels.table.clear()                       # как будто бот перезапустили
    await reels.on_callback(reg, CHAT, f"ok:{REEL}")
    check("сценарий поднят из базы", theme(REEL)["status"] == "ready",
          theme(REEL)["status"])
    check("не сказал «неактуален»", "неактуален" not in reg.texts(),
          reg.texts()[-120:])


def cuts() -> None:
    """Нарезка длинной записи: границы кусков проверяет код, не промпт."""
    print("\n15. Нарезка длинной записи")

    class W:
        def __init__(self, text, start):
            self.text, self.start = text, start

    words = [W(f"с{i}", i * 0.5) for i in range(30)]
    text = reels.transcript(words, line=10)
    check("расшифровка идёт строками с меткой времени",
          text.startswith("[0] ") and "\n[5] " in text, text[:60])

    raw = [
        {"start": 0, "end": 30, "hook": "первый", "title": "т"},
        {"start": 25, "end": 55, "hook": "внахлёст", "title": "т"},
        {"start": 60, "end": 65, "hook": "короткий", "title": "т"},
        {"start": 70, "end": 140, "hook": "длинный", "title": "т"},
        {"start": 150, "end": 175, "hook": "", "title": "без хука"},
        {"start": 100, "end": 300, "hook": "за краем", "title": "т"},
        {"start": "ой", "end": 200, "hook": "не число", "title": "т"},
    ]
    good, lost = reels._fit(raw, 180)
    check("взят только годный кусок", [f.hook for f in good] == ["первый"],
          str([f.hook for f in good]))
    check("наезд отброшен", any("наезжает" in x for x in lost), str(lost))
    check("короткий отброшен", any("короче" in x for x in lost), str(lost))
    check("длинный отброшен", any("длиннее" in x for x in lost), str(lost))
    check("кусок без хука отброшен", any("без хука" in x for x in lost),
          str(lost))
    check("вышедший за длину записи отброшен",
          any("не помещается" in x for x in lost), str(lost))
    check("нечисловое время отброшено",
          any("не число" in x for x in lost), str(lost))
    check("каждое отбрасывание названо", len(lost) == 6, str(len(lost)))

    ok = [{"start": 10, "end": 40, "hook": "х", "title": "", "why": "п"}]
    good, _ = reels._fit(ok, 120)
    check("без title берётся хук", good[0].title == "х", good[0].title)
    check("длительность считается", good[0].seconds == 30, str(good[0].seconds))


asyncio.run(main())
cuts()
raise SystemExit(report())
