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
from orchestrator import agent, design, desk, imagegen                          # noqa: E402
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


def slots(headline="Кода не писала", accent_tail=" ни строчки",
          subtitle="Навык нужен тот же самый", photo=None) -> dict:
    """Слоты вместо HTML: на переехавших площадках разметку собирает код.

    Рубрики и фото здесь нет намеренно: на сборке их ставит код. `photo`
    появляется только на правке, когда человек просит другое фото.
    """
    out = {"headline": headline, "headline_accent": accent_tail,
           "subtitle": subtitle}
    if photo is not None:
        out["photo"] = photo
    return out


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


def seed(plat="telegram", fmt="пост", status="ready", rubric="Путь"):
    """Тема с утверждённым текстом на диске."""
    b = desk.brand(CHAT)
    tid = f"2026-08-15-{plat}-01"
    b.artifact(f"posts/{tid}.md", f"<!-- {tid} -->\n\n{COPY}")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                  "rubric, goal, status, title, asset) VALUES "
                  "(?,?,'2026-08-15',?,?,?,'warm',?,'Путь в AI',?)",
                  (tid, CHAT, plat, fmt, rubric, status, f"posts/{tid}.md"))
    return tid


async def main() -> None:
    reg = Reg()
    b = desk.brand(CHAT)

    # ── 1. макет собирается и рендерится ──────────────────────────────
    print("\n1. Макет собирается")
    tid = seed()
    SENT_FILES.clear()
    install(answer([{"name": "cover", "slots": slots()}]))
    await design.run(reg, CHAT, "сделай обложку", pick_bg=False)

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

    # ── 2. шаблон: разметку собирает код ──────────────────────────────
    print("\n2. Шаблон и слоты")
    p = CALLS["prompts"][-1]
    st = CALLS["stable"][-1]
    check("текст Редактора передан", "Кода не писала" in p)
    check("списка фото в запросе нет: фото выбирает код",
          "author.jpg" not in p, p[-300:])
    check("холста в запросе нет: разметку собирает код",
          "1080×1350" not in p, p[:200])
    check("ТЗ площадки передано в кешируемом блоке",
          "Рецепт" in st or "обложка" in st.lower())
    check("ТЗ не дублируется в теле запроса",
          "Рецепт" not in p and "## ТЗ площадки" not in p)
    check("слоты описаны модели", "`headline`" in st, st[:200])
    check("рубрика и фото модели не предлагаются",
          "`rubric`" not in st and "`photo`" not in st, st[-300:])
    check("хвост ТЗ для человека не поехал",
          "документация шаблона" not in st.lower(), st[-300:])
    check("разметку у модели не просят",
          "html" not in p.lower().split("## текущие")[0], p[:200])
    check("шаблон модели не показывают", "{{" not in st and "{{" not in p)

    made = html.read_text(encoding="utf-8")
    check("холст пришёл из шаблона, а не от модели", "1080px" in made
          and "1350px" in made)
    check("цвета токенами", "var(--milk)" in made, made[:200])
    check("путь к фото собрал код",
          "../design/assets/images/author.jpg" in made)
    check("значение слота попало в макет", "Кода не писала" in made)
    check("рубрику поставил код из темы", ">ПУТЬ<" in made,
          made[made.find('class="rubric"'):][:80])
    check("фото выбрал код по правилу бренда",
          "images/author.jpg" in made, made[:400])
    check("код положил свои слоты рядом с макетом",
          {"rubric", "photo"} <=
          set(json.loads(b.path(f"posts/{tid}-cover.slots.json")
                         .read_text(encoding="utf-8"))))
    check("кегль посчитан кодом",
          f"font-size:{design._fit('Кода не писала ни строчки')}px" in made,
          made[made.find(".head"):made.find(".head") + 200])
    check("слоты сохранены рядом с макетом",
          b.path(f"posts/{tid}-cover.slots.json").exists())

    # Разметка от кода означает, что четыре проверки inspect стали
    # невозможны по устройству. Пятая — чужие слова — осталась: её решает
    # модель, и код её по-прежнему стережёт.
    check("собранный кодом макет чист для inspect",
          design.inspect(made, COPY + " ПУТЬ Навык нужен тот же самый",
                         (1080, 1350), design._photos(b)) == [],
          str(design.inspect(made, COPY, (1080, 1350), design._photos(b))))

    # ── 3. без ТЗ не собирает ─────────────────────────────────────────
    print("\n3. Без ТЗ не собирает")
    seed(plat="youtube", fmt="видео")
    reg.clear()
    install(answer([{"name": "cover", "html": card("что угодно", 1280, 720)}]))
    await design.run(reg, CHAT, "сделай превью", pick_bg=False)
    check("сказал, что ТЗ нет", "ТЗ нет:" in reg.texts(),
          reg.texts()[:150])
    check("предложил завести ТЗ", "design/platforms" in reg.texts())
    check("модель не звалась", CALLS["n"] == 0, f"вызовов {CALLS['n']}")

    # ── 4. без утверждённого текста не верстает ───────────────────────
    print("\n4. Без текста не верстает")
    with db.tx() as c:
        c.execute("UPDATE themes SET status = 'draft' WHERE chat_id = ?", (CHAT,))
    reg.clear()
    install(answer([{"name": "cover", "html": card("х")}]))
    await design.run(reg, CHAT, "сделай обложку", pick_bg=False)
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

    # ── 6. замечания и отказы доезжают до человека ────────────────────
    print("\n6. Замечания доезжают")

    # На шаблоне несуществующее фото это уже не предупреждение, а отказ:
    # путь собирает код, и подставить в него небылицу нечему. Раньше такой
    # макет уходил человеку с ⚠️ и открывался у него пустым прямоугольником.
    tid = seed()
    install(answer([{"name": "cover", "slots": slots()}]))
    reg.clear()
    await design.run(reg, CHAT, "сделай обложку", pick_bg=False)
    await design.on_callback(reg, CHAT, "fix")
    reg.clear()
    install(answer([{"name": "cover", "slots": slots(photo="призрак.jpg")}]))
    await design.revise(reg, CHAT, "поставь другое фото")
    check("несуществующее фото отклонено, а не помечено",
          "Точечно поправить не вышло" in reg.texts(), reg.texts()[:200])
    check("названо, чего не хватает", "призрак.jpg" in reg.texts(),
          reg.texts()[:200])

    # Слишком длинный слот тоже отказ: заголовок вылез бы за холст, а
    # заметил бы это человек глазами на PNG.
    reg.clear()
    install(answer([{"name": "cover", "slots": slots(headline="о" * 80)}]))
    await design.run(reg, CHAT, "сделай обложку", pick_bg=False)
    check("переполненный слот отклонён", "Верстать нечего" in reg.texts(),
          reg.texts()[:160])

    # На непереехавшей площадке путь прежний: замечание, но макет отдан.
    seed(plat="instagram", fmt="карусель")
    SENT_FILES.clear()
    reg.clear()
    install(answer([{"name": f"{i:02d}",
                     "html": card("Кода не писала", 1080, 1350,
                                  photo="призрак.jpg" if i == 1 else "author.jpg")}
                    for i in range(1, 7)]))
    await design.run(reg, CHAT, "собери карусель", pick_bg=False)
    check("на HTML-пути замечание показано", "⚠️" in reg.texts(),
          "замечание проглочено")
    check("макет всё равно отдан",
          any(n.endswith(".png") for n, _, _ in SENT_FILES))

    # ── 7. кнопки ─────────────────────────────────────────────────────
    print("\n7. Кнопки")
    reg.clear()
    await design.on_callback(reg, CHAT, "fix")
    check("ждём правку", design.wants_fix(CHAT) is True)

    tid = seed()
    install(answer([{"name": "cover", "slots": slots()}]))
    reg.clear()
    await design.run(reg, CHAT, "сделай обложку", pick_bg=False)
    await design.on_callback(reg, CHAT, "fix")
    install(answer([{"name": "cover",
                     "slots": slots(subtitle="Навык нужен тот же")}]))
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
    install(answer([{"name": "cover", "slots": slots()}]))
    reg.clear()
    await design.run(reg, CHAT, "сделай обложку", pick_bg=False)
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
    # Паттернов пять, а карточек шесть: пункт повторяется трижды и на
    # третий раз меняет сторону. Считать карточки по эталонам — ошибка,
    # которую видно только у человека в ленте.
    check("карточек карусели шесть",
          design._cards("instagram", "карусель", [], refs) == 6,
          str(design._cards("instagram", "карусель", [], refs)))
    # У Telegram эталона нет и быть не должно: он переехал на шаблон со
    # слотами, а эталон приглашал бы модель переизобрести разметку,
    # которой она не видит.
    check("у telegram эталона нет, есть шаблон",
          not design._reference("telegram", "пост")
          and len(design._templates("telegram", "пост")) == 1)
    check("холст reels вертикальный",
          design.CANVAS[("instagram", "reels")] == (1080, 1920))

    # ── 8b. фон выбирает человек, а не код ────────────────────────────
    # Раньше код выбирал фото сам и сразу верстал: не то фото стоило
    # круга вёрстки и рендера. Теперь три варианта до дизайна.
    print("\n8b. Три фона до вёрстки")
    tid = seed()
    # Макет этой темы уже собирали выше: убираем, иначе «макета ещё нет»
    # проверяет чужой файл.
    for f in b.path("posts").glob(f"{tid}-cover.*"):
        f.unlink()
    reg.clear()
    SENT_FILES.clear()
    install(answer([{"name": "cover", "slots": slots()}]))
    await design.run(reg, CHAT, "сделай обложку")

    check("показаны три фона", len(SENT_FILES) == 3, str(SENT_FILES))
    check("фоны ушли картинками", all(p for _, _, p in SENT_FILES),
          str(SENT_FILES))
    check("кнопки выбора",
          [":".join(x.split(":")[:2]) for x in reg.last().buttons] ==
          ["art:bg", "art:bg", "art:bg", "art:bgmore", "art:bgauto"],
          str(reg.last().buttons))
    check("макета ещё нет", not b.path(f"posts/{tid}-cover.png").exists())
    check("модель не звалась до выбора", CALLS["n"] == 0, f"вызовов {CALLS['n']}")
    check("кандидаты записаны рядом с макетом",
          b.path(f"posts/{tid}.bg.json").exists())

    # Фотобанк бренда полный, поэтому все три варианта свои: сток
    # добирает только нехватку, и слова для него у модели не просят.
    saved = json.loads(b.path(f"posts/{tid}.bg.json").read_text(encoding="utf-8"))
    check("варианты из фотобанка бренда",
          [o["kind"] for o in saved["options"]] == ["own"] * 3,
          str(saved["options"]))
    check("сток не подмешан молча", not saved["query"], saved["query"])

    picked = saved["options"][1]["name"]
    reg.clear()
    SENT_FILES.clear()
    await design.on_callback(reg, CHAT, f"bg:{tid}:2")

    check("после выбора макет собран", b.path(f"posts/{tid}-cover.png").exists())
    check("на макете выбранный фон",
          picked in b.path(f"posts/{tid}-cover.html").read_text(encoding="utf-8"),
          picked)
    check("кнопки макета вернулись",
          [":".join(x.split(":")[:2]) for x in reg.last().buttons] ==
          ["art:ok", "art:fix", "art:queue"], str(reg.last().buttons))

    # «Реши сам» — старый путь: код ставит фото правилом бренда.
    tid = seed()
    reg.clear()
    install(answer([{"name": "cover", "slots": slots()}]))
    await design.run(reg, CHAT, "сделай обложку")
    await design.on_callback(reg, CHAT, f"bgauto:{tid}")
    check("«реши сам» верстает без выбора",
          b.path(f"posts/{tid}-cover.png").exists())

    # Формат без фона не спрашивает о нём вовсе: в шаблоне опроса фото
    # нет, и выбирать там нечего.
    check("у опроса фона не спрашивают",
          not design._needs_bg("telegram", "опрос"))
    check("у поста спрашивают", design._needs_bg("telegram", "пост"))

    # ── 8c. рубрики, где фон генерируется ─────────────────────────────
    # «Разбор ошибки» и «Артефакт в ленте» — чужая поломка и чужой
    # промпт: своей съёмки под них не бывает, и портрет автора в кадре
    # обещал бы не то, что стоит в посте. Генератор подменён, всё
    # остальное настоящее.
    print("\n8c. Сгенерированный фон")
    frame = b.path(f"design/assets/images/{design._photos(b)[0]}").read_bytes()
    GEN = {"n": 0, "prompt": [], "aspect": []}

    def fake_make(prompt, aspect="4:5"):
        GEN["n"] += 1
        GEN["prompt"].append(prompt)
        GEN["aspect"].append(aspect)
        return frame

    def two_ways(p):
        """Бриф кадра и слоты приходят из одного `agent.ask`."""
        return ("wide desk with a single lit lamp and paper"
                if "предметную сцену" in p
                else answer([{"name": "cover", "slots": slots()}]))

    imagegen.ready = lambda: True
    imagegen.make = fake_make

    tid = seed(rubric="Разбор ошибки")
    reg.clear()
    SENT_FILES.clear()
    install(two_ways)
    await design.run(reg, CHAT, "сделай обложку")

    saved = json.loads(b.path(f"posts/{tid}.bg.json").read_text(encoding="utf-8"))
    check("генерация позвана один раз", GEN["n"] == 1, str(GEN["n"]))
    check("соотношение сторон по холсту", GEN["aspect"] == ["4:5"],
          str(GEN["aspect"]))
    check("сюжет от модели уехал в кадр",
          "single lit lamp" in GEN["prompt"][0], GEN["prompt"][0][:120])
    check("рамку кадра держит код, а не модель",
          "no letters" in GEN["prompt"][0] and "No people" in GEN["prompt"][0],
          GEN["prompt"][0][:200])
    check("сгенерированное идёт первым вариантом",
          [o["kind"] for o in saved["options"]][:1] == ["gen"],
          str([o["kind"] for o in saved["options"]]))
    check("рядом стоят свои фото, а не только генерация",
          "own" in [o["kind"] for o in saved["options"]],
          str([o["kind"] for o in saved["options"]]))
    check("показаны три варианта", len(SENT_FILES) == 3, str(SENT_FILES))
    check("кадр ждёт выбора в кэше, а не в фотобанке",
          b.path(f"design/assets/.gen/{tid}.jpg").exists()
          and not any(n.startswith("gen-") for n in design._photos(b)),
          str(design._photos(b)))

    reg.clear()
    await design.on_callback(reg, CHAT, f"bg:{tid}:1")
    made = [n for n in design._photos(b) if n.startswith("gen-")]
    check("выбранный кадр переехал в фотобанк", len(made) == 1, str(made))
    check("кэш убран", not b.path(f"design/assets/.gen/{tid}.jpg").exists())
    check("чем и по чему сделано — записано",
          b.path("design/assets/gen-credits.md").is_file())
    if made:
        check("макет собран на сгенерированном фоне",
              made[0] in b.path(f"posts/{tid}-cover.html").read_text(encoding="utf-8"),
              made[0])

    # Сгенерированное не должно всплыть на чужой обложке само: как и
    # сток, оно берётся только кнопкой или именем файла в `photos.md`.
    check("в слепую ротацию генерация не идёт",
          not design._pick_photo(b, {"goal": "нет такой цели"},
                                 ["gen-x.jpg", "author.jpg"]).startswith("gen-"))

    # Рубрика решает, а не желание сэкономить круг вёрстки.
    GEN["n"] = 0
    tid = seed(rubric="Из жизни")
    reg.clear()
    install(two_ways)
    await design.run(reg, CHAT, "сделай обложку")
    check("на обычной рубрике генерации нет", GEN["n"] == 0, str(GEN["n"]))

    # Ключа нет — фон просто находится иначе, вёрстка не падает.
    imagegen.ready = lambda: False
    GEN["n"] = 0
    tid = seed(rubric="Артефакт в ленте")
    reg.clear()
    install(two_ways)
    await design.run(reg, CHAT, "сделай обложку")
    saved = json.loads(b.path(f"posts/{tid}.bg.json").read_text(encoding="utf-8"))
    check("без ключа генерация не зовётся", GEN["n"] == 0, str(GEN["n"]))
    check("без ключа варианты остаются",
          len(saved["options"]) >= 2 and "gen" not in
          [o["kind"] for o in saved["options"]], str(saved["options"]))
    imagegen.ready = lambda: True

    # У раздачи фон появился 03.09: до этого слота `photo` в шаблоне не
    # было вовсе, и «Артефакт в ленте» этим форматом остался бы без
    # обложки-фона.
    check("у раздачи спрашивают фон", design._needs_bg("telegram", "раздача"))
    tid = seed(fmt="раздача", rubric="Артефакт в ленте")
    reg.clear()
    install(two_ways)
    await design.run(reg, CHAT, "сделай карточку раздачи")
    await design.on_callback(reg, CHAT, f"bg:{tid}:1")
    png = b.path(f"posts/{tid}-cover.png")
    check("раздача рендерится с фоном", png.exists() and png.stat().st_size > 50_000,
          str(png))

    # ── 9. правка это правка, а не пересборка ─────────────────────────
    # Раньше `revise` звал `run`: модель переписывала весь HTML, Chrome
    # перерисовывал все карточки. «Подвинь заголовок» стоило столько же,
    # сколько вёрстка с нуля, при потолке 16000 токенов и усилии high.
    print("\n9. Точечная правка")
    tid = seed(plat="instagram", fmt="карусель")
    install(answer([{"name": f"{i:02d}", "html": card(f"Карточка {i}", 1080, 1350)}
                    for i in range(1, 7)]))
    await design.run(reg, CHAT, "собери карусель", pick_bg=False)
    made = sorted(x.name for x in b.path("posts").glob(f"{tid}-*.png"))
    check("собрано шесть карточек", len(made) == 6, str(made))

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
    check("нетронутые карточки уцелели", len(now) == 6, str(sorted(now)))
    check("человеку сказано, сколько поправлено",
          "1 из 6" in reg.texts(), reg.texts()[:200])
    check("в комплект уехали все шесть",
          len([n for n, _, ph in SENT_FILES if n.endswith(".png") and ph]) == 6,
          str([n for n, _, _ in SENT_FILES]))
    check("кнопки после правки на месте",
          [":".join(x.split(":")[:2]) for x in reg.last().buttons] ==
          ["art:ok", "art:fix", "art:queue"], str(reg.last().buttons))

    # ── 10. когда правка всё-таки пересборка ──────────────────────────
    print("\n10. Пересборка по просьбе и по отказу")
    await design.on_callback(reg, CHAT, "fix")
    reg.clear()
    install(answer([{"name": f"{i:02d}", "html": card(f"Новая {i}", 1080, 1350)}
                    for i in range(1, 7)]))
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

    # ── 11. правка по слотам ──────────────────────────────────────────
    # Самый дешёвый круг: модель получает пять коротких значений и
    # возвращает такие же. Двести байт вместо двух килобайт разметки.
    print("\n11. Правка по слотам")
    tid = seed()
    install(answer([{"name": "cover", "slots": slots()}]))
    reg.clear()
    await design.run(reg, CHAT, "сделай обложку", pick_bg=False)
    png = b.path(f"posts/{tid}-cover.png")
    was = png.stat().st_mtime_ns

    await design.on_callback(reg, CHAT, "fix")
    reg.clear()
    install(answer([{"name": "cover",
                     "slots": slots(subtitle="Тот же навык, другая скорость")}]))
    await design.revise(reg, CHAT, "подзаголовок другой")

    body = CALLS["prompts"][-1]
    check("правка идёт по слотам", "Текущие слоты" in body, body[:200])
    check("разметка в правку не едет", "<!DOCTYPE" not in body)
    check("запрос правки короткий", len(body) < 2000, f"{len(body)} знаков")
    check("правка на низком усилии", CALLS["effort"][-1] == "low",
          str(CALLS["effort"][-1]))

    made = b.path(f"posts/{tid}-cover.html").read_text(encoding="utf-8")
    check("новое значение попало в макет", "другая скорость" in made)
    check("нетронутый слот уцелел", "Кода не писала" in made)
    check("PNG перерисован", png.stat().st_mtime_ns != was)
    check("слоты на диске обновились",
          "другая скорость" in
          b.path(f"posts/{tid}-cover.slots.json").read_text(encoding="utf-8"))

    # Границы те же, что и у HTML-правки: чужая карточка не принимается.
    await design.on_callback(reg, CHAT, "fix")
    reg.clear()
    install(answer([{"name": "самовольная", "slots": slots()}]))
    await design.revise(reg, CHAT, "чуть сдвинь подпись")
    check("чужая карточка не принята и на слотах",
          "Точечно поправить не вышло" in reg.texts(), reg.texts()[:200])

    # Экранирование: кавычка в заголовке рвала бы стиль, если бы значение
    # подставлялось как есть. Это то, чего свободный HTML не гарантировал.
    tid = seed()
    install(answer([{"name": "cover",
                     "slots": slots(headline='Кода "не" писала')}]))
    reg.clear()
    await design.run(reg, CHAT, "сделай обложку", pick_bg=False)
    made = b.path(f"posts/{tid}-cover.html").read_text(encoding="utf-8")
    check("кавычка в слоте экранирована", "&quot;" in made, made[:400])
    check("макет всё равно собрался", b.path(f"posts/{tid}-cover.png").exists())


asyncio.run(main())
raise SystemExit(report())
