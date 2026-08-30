"""Цикл 9: Дизайнер.

Модель подменена, рендер настоящий. Главное здесь — что без ТЗ он
отказывается, а не рисует похожее, и что проверки кодом ловят то, чего
промпт только просит.
"""
from __future__ import annotations

import asyncio
import json

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import agent, design, desk                          # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

SENT_FILES = []
CALLS = {"n": 0, "prompts": [], "stable": [], "effort": []}


class Reg(FakeRegistry):
    async def send_file(self, role, chat_id, blob, name, *, caption="",
                        topic="general", kb=None, as_photo=False):
        SENT_FILES.append((name, len(blob), as_photo))


def card(body: str, w=1080, h=1350, photo="author.jpg") -> str:
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<link rel="stylesheet" href="../design/tokens.css"></head><body>'
            f'<div style="width:{w}px;height:{h}px;overflow:hidden;'
            f'position:relative;background:var(--graphite)">'
            f'<img src="../design/assets/images/{photo}" '
            f'style="position:absolute;inset:0;width:100%;height:100%;'
            f'object-fit:cover">'
            f'<div style="position:absolute;left:72px;bottom:80px;right:72px;'
            f'font-family:var(--font-display);font-weight:800;font-size:92px;'
            f'line-height:1;color:var(--milk)">{body}</div></div></body></html>')


def answer(cards, accent="терракота на слове", notes=None) -> str:
    return json.dumps({"cards": cards, "accent": accent,
                       "notes": notes or []}, ensure_ascii=False)


def install(ans):
    async def ask(role, chat_id, prompt, **kw):
        CALLS["n"] += 1
        CALLS["prompts"].append(prompt)
        CALLS["stable"].append(kw.get("stable", ""))
        CALLS["effort"].append(kw.get("effort", ""))
        return ans(prompt) if callable(ans) else ans
    agent.ask = ask
    CALLS["n"] = 0
    for k in ("prompts", "stable", "effort"):
        CALLS[k].clear()


COPY = "Кода не писала ни строчки. Навык нужен тот же самый."


def seed(plat="telegram", fmt="пост", status="ready"):
    """Тема с утверждённым текстом на диске."""
    b = desk.brand(CHAT)
    tid = f"2026-08-15-{plat}-01"
    b.artifact(f"posts/{tid}.md", f"<!-- {tid} -->\n\n{COPY}")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                  "rubric, status, title, asset) VALUES "
                  "(?,?,'2026-08-15',?,?,'Путь',?,'Путь в AI',?)",
                  (tid, CHAT, plat, fmt, status, f"posts/{tid}.md"))
    return tid


async def main() -> None:
    reg = Reg()
    b = desk.brand(CHAT)

    # ── 1. макет собирается и рендерится ──────────────────────────────
    print("\n1. Макет собирается")
    tid = seed()
    SENT_FILES.clear()
    install(answer([{"name": "cover", "html": card("Кода не писала<br>ни строчки")}]))
    await design.run(reg, CHAT, "сделай обложку")

    html = b.path(f"posts/{tid}-cover.html")
    png = b.path(f"posts/{tid}-cover.png")
    check("HTML записан", html.exists(), str(html))
    check("PNG отрендерен", png.exists(), str(png))
    if png.exists():
        check("PNG непустой", png.stat().st_size > 50_000,
              f"{png.stat().st_size} байт")

    names = [n for n, _, _ in SENT_FILES]
    check("PNG отправлен превью",
          any(n.endswith(".png") and p for n, _, p in SENT_FILES), str(SENT_FILES))
    check("HTML отправлен документом",
          any(n.endswith(".html") and not p for n, _, p in SENT_FILES), str(names))
    check("кнопки на карточке",
          [":".join(x.split(":")[:2]) for x in reg.last().buttons] ==
          ["art:ok", "art:fix", "art:queue"], str(reg.last().buttons))
    check("id темы уехал в кнопку",
          all(x.endswith(tid) for x in reg.last().buttons),
          str(reg.last().buttons))

    # ── 2. что уехало в промпт ────────────────────────────────────────
    print("\n2. Промпт")
    p = CALLS["prompts"][-1]
    st = CALLS["stable"][-1]
    check("текст Редактора передан", "Кода не писала" in p)
    check("список фото передан", "author.jpg" in p)
    check("холст задан", "1080×1350" in p)

    # ТЗ и эталон уехали в кешируемый блок, а не в тело запроса. Это самые
    # объёмные вызовы завода, и в теле за них платили полную цену каждый
    # круг правок. Проверяем обе половины: что доехало и что не в хвосте.
    check("ТЗ площадки передано", "Рецепт" in st or "обложка" in st.lower())
    check("эталон передан", "carousel-01-cover" in st)
    check("ТЗ не дублируется в теле запроса",
          "Рецепт" not in p and "## ТЗ площадки" not in p)
    check("эталон не дублируется в теле запроса",
          "carousel-01-cover" not in p)
    check("кешируемый блок крупный, иначе точка кеша не ставится",
          len(st) >= 2000, f"{len(st)} знаков")

    # ── 3. без ТЗ не собирает ─────────────────────────────────────────
    print("\n3. Без ТЗ не собирает")
    seed(plat="youtube", fmt="видео")
    reg.clear()
    install(answer([{"name": "cover", "html": card("что угодно", 1280, 720)}]))
    await design.run(reg, CHAT, "сделай превью")
    check("сказал, что ТЗ нет", "ТЗ площадки нет" in reg.texts(),
          reg.texts()[:150])
    check("предложил завести ТЗ", "design/platforms" in reg.texts())
    check("модель не звалась", CALLS["n"] == 0, f"вызовов {CALLS['n']}")

    # ── 4. без утверждённого текста не верстает ───────────────────────
    print("\n4. Без текста не верстает")
    with db.tx() as c:
        c.execute("UPDATE themes SET status = 'draft' WHERE chat_id = ?", (CHAT,))
    reg.clear()
    install(answer([{"name": "cover", "html": card("х")}]))
    await design.run(reg, CHAT, "сделай обложку")
    check("отказался верстать черновик", "Верстать нечего" in reg.texts(),
          reg.texts()[:120])
    check("модель не звалась", CALLS["n"] == 0, f"вызовов {CALLS['n']}")

    # ── 5. проверки кодом ─────────────────────────────────────────────
    print("\n5. Проверки кодом")
    photos = design._photos(b)
    size = (1080, 1350)

    bad = card("Кода не писала").replace('src="../design/assets/images/author.jpg"',
                                         'src="/Users/userr/photo.jpg"')
    out = design.inspect(bad, COPY, size, photos)
    check("абсолютный путь пойман",
          any("абсолютный путь" in o for o in out), str(out))

    ghost = card("Кода не писала", photo="несуществующее.jpg")
    out = design.inspect(ghost, COPY, size, photos)
    check("несуществующее фото поймано",
          any("нет в папке бренда" in o for o in out), str(out))

    wrong = card("Кода не писала", 1080, 1920)
    out = design.inspect(wrong, COPY, size, photos)
    check("неверный холст пойман",
          any("холст не заявлен" in o for o in out), str(out))

    extra = card("Купите прямо сейчас невероятное предложение скидка сегодня")
    out = design.inspect(extra, COPY, size, photos)
    check("чужие слова пойманы",
          any("не из текста Редактора" in o for o in out), str(out))

    ok = card("Кода не писала<br>ни строчки")
    check("нормальный макет чист", design.inspect(ok, COPY, size, photos) == [],
          str(design.inspect(ok, COPY, size, photos)))

    # ── 6. замечания доезжают до человека ─────────────────────────────
    print("\n6. Замечания доезжают")
    tid = seed()
    reg.clear()
    install(answer([{"name": "cover",
                     "html": card("Кода не писала", photo="призрак.jpg")}]))
    await design.run(reg, CHAT, "сделай обложку")
    check("замечание показано человеку", "⚠️" in reg.texts(),
          "замечание проглочено")
    check("макет всё равно отдан", any(n.endswith(".png") for n, _, _ in SENT_FILES))

    # ── 7. кнопки ─────────────────────────────────────────────────────
    print("\n7. Кнопки")
    reg.clear()
    await design.on_callback(reg, CHAT, "fix")
    check("ждём правку", design.wants_fix(CHAT) is True)

    tid = seed()
    install(answer([{"name": "cover", "html": card("Кода не писала")}]))
    reg.clear()
    await design.revise(reg, CHAT, "сделай кегль крупнее")
    check("флаг снят", design.wants_fix(CHAT) is False)
    corr = b.path("design/corrections.md")
    check("правка записана в дизайн-профиль", corr.exists(), "файла нет")
    if corr.exists():
        check("в правке есть текст",
              "кегль крупнее" in corr.read_text(encoding="utf-8"))

    reg.clear()
    await design.on_callback(reg, CHAT, "ok")
    check("после Ок сказано, где файлы", "posts/" in reg.texts(),
          reg.texts()[:120])

    # «В очередь» это передача Публикатору, а не рассказ о нём. Кнопка,
    # которая только сообщает о существовании соседа, уже обманывала
    # здесь полторы недели.
    seed()
    install(answer([{"name": "cover", "html": card("Кода не писала")}]))
    reg.clear()
    await design.run(reg, CHAT, "сделай обложку")
    reg.clear()
    await design.on_callback(reg, CHAT, "queue")
    check("очередь передана Публикатору", "Публикатору" in reg.texts(),
          reg.texts()[:120])
    check("Публикатор ответил сам",
          any(s.role == "publisher" for s in reg.sent),
          str([s.role for s in reg.sent]))
    check("не врёт, что сосед не подключён",
          "не подключён" not in reg.texts(), reg.texts()[:160])
    check("превью показано в «Очередь»",
          any(s.topic == "queue" for s in reg.sent),
          str([s.topic for s in reg.sent]))

    # ── 8. карусель это пять карточек ─────────────────────────────────
    print("\n8. Карусель")
    refs = design._reference("instagram", "карусель")
    check("эталонов карусели пять", len(refs) == 5, f"{len(refs)}")
    check("эталон telegram один",
          len(design._reference("telegram", "пост")) == 1)
    check("холст reels вертикальный",
          design.CANVAS[("instagram", "reels")] == (1080, 1920))

    # ── 9. правка это правка, а не пересборка ─────────────────────────
    # Раньше `revise` звал `run`: модель переписывала весь HTML, Chrome
    # перерисовывал все карточки. «Подвинь заголовок» стоило столько же,
    # сколько вёрстка с нуля, при потолке 16000 токенов и усилии high.
    print("\n9. Точечная правка")
    tid = seed(plat="instagram", fmt="карусель")
    install(answer([{"name": f"{i:02d}", "html": card(f"Карточка {i}", 1080, 1350)}
                    for i in range(1, 6)]))
    await design.run(reg, CHAT, "собери карусель")
    made = sorted(x.name for x in b.path("posts").glob(f"{tid}-*.png"))
    check("собрано пять карточек", len(made) == 5, str(made))

    was = {x.name: x.stat().st_mtime_ns
           for x in b.path("posts").glob(f"{tid}-*.png")}
    await design.on_callback(reg, CHAT, "fix")
    SENT_FILES.clear()
    reg.clear()
    # Модель возвращает ОДНУ изменённую карточку — так и просит промпт.
    install(answer([{"name": "03", "html": card("Карточка 3 крупнее", 1080, 1350)}]))
    await design.revise(reg, CHAT, "на третьей карточке кегль крупнее")

    check("вызов модели ровно один", CALLS["n"] == 1, f"вызовов {CALLS['n']}")
    check("правка идёт на низком усилии", CALLS["effort"][-1] == "low",
          f"усилие {CALLS['effort'][-1]!r}")
    body = CALLS["prompts"][-1]
    check("эталон в правку не едет", "carousel-01-cover" not in body)
    check("текущий макет показан модели", "Карточка 1" in body, body[:200])
    check("просьба человека передана", "кегль крупнее" in body)

    now = {x.name: x.stat().st_mtime_ns
           for x in b.path("posts").glob(f"{tid}-*.png")}
    changed = [n for n in was if was[n] != now.get(n)]
    check("перерисована ровно одна карточка", len(changed) == 1, str(changed))
    check("перерисована именно третья", changed and changed[0].endswith("-03.png"),
          str(changed))
    check("нетронутые карточки уцелели", len(now) == 5, str(sorted(now)))
    check("человеку сказано, сколько поправлено",
          "1 из 5" in reg.texts(), reg.texts()[:200])
    check("в комплект уехали все пять",
          len([n for n, _, ph in SENT_FILES if n.endswith(".png") and ph]) == 5,
          str([n for n, _, _ in SENT_FILES]))
    check("кнопки после правки на месте",
          [":".join(x.split(":")[:2]) for x in reg.last().buttons] ==
          ["art:ok", "art:fix", "art:queue"], str(reg.last().buttons))

    # ── 10. когда правка всё-таки пересборка ──────────────────────────
    print("\n10. Пересборка по просьбе и по отказу")
    await design.on_callback(reg, CHAT, "fix")
    reg.clear()
    install(answer([{"name": f"{i:02d}", "html": card(f"Новая {i}", 1080, 1350)}
                    for i in range(1, 6)]))
    await design.revise(reg, CHAT, "пересобери с нуля, другой макет")
    check("«пересобери» ведёт к полной сборке",
          "Верстаю" in reg.texts() or "карточки" in reg.texts(),
          reg.texts()[:200])

    # Незнакомое имя карточки значит, что модель собрала новую вместо
    # правки старой. Принять молча нельзя: в папке заведётся второй макет.
    await design.on_callback(reg, CHAT, "fix")
    reg.clear()
    install(answer([{"name": "самовольная", "html": card("чужая", 1080, 1350)}]))
    await design.revise(reg, CHAT, "чуть сдвинь подпись")
    check("самовольная карточка не принята молча",
          "Точечно поправить не вышло" in reg.texts(), reg.texts()[:200])
    check("после отказа пошла честная пересборка",
          "Пересобираю" in reg.texts(), reg.texts()[:200])


asyncio.run(main())
raise SystemExit(report())
