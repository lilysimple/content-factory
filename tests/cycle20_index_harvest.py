"""Цикл 20: индекс профиля, окно недели, посадка плана с моста.

Три границы, ради которых цикл написан.

**Кэш не должен читать то, ради чего он заведён.** `research._stamp`
считал отпечаток по содержимому файлов профиля: на `lily-space` это 7 800
знаков вычитывались на каждый вызов, только чтобы выяснить, что ничего не
менялось. Теперь отпечаток по `stat()`, и при живом кэше исходники не
открываются вовсе.

**Окно сводки — прошлая ISO-неделя, и посты вне него не считаются.** До
31.08 медиана считалась по всей отдаче ленты, а лента отдаёт последние
двадцать постов независимо от возраста. У одного из девяти каналов это
оказались посты пятимесячной давности, и его медиана уехала в сводку как
ориентир недели.

**План с моста проверяется кодом и садится в базу.** `strategy._fit` жил
внутри старого `build`, куда мост не заходит: дата вне окна доезжала до
человека как рабочий план, а сам план не попадал никуда, кроме папки
задачи в `.gitignore`.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import harness
from harness import CHAT, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import (bridge, design, desk, research,           # noqa: E402
                          sources, strategy)
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

SANDBOX = harness.TMP
bridge.TASKS_DIR = SANDBOX / "tasks"          # боевую tasks/ не трогаем

UTC = timezone.utc


def post(views: int, when: date | None, text: str = "пост") -> sources.Post:
    stamp = datetime(when.year, when.month, when.day, 12, tzinfo=UTC) if when else None
    return sources.Post(text, views, stamp)


def free_bridge() -> None:
    """Мост держит один прогон за раз. В тесте задач много — закрываем."""
    with db.tx() as c:
        c.execute("DELETE FROM bridge_runs")


def src(posts: list[sources.Post]) -> sources.Source:
    s = sources.Source(url="https://t.me/s/x", kind="telegram", ok=True)
    s.posts = posts
    return s


async def main() -> None:
    b = desk.brand(CHAT)

    # ── 1. Отпечаток профиля считается, не читая профиль ──────────────
    print("\n1. Кэш индекса профиля")

    reads: list[str] = []
    real_read = type(b).read

    def spy_read(self, key):                  # noqa: ANN001
        reads.append(key)
        return real_read(self, key)

    type(b).read = spy_read
    try:
        research._stamp(b)
        check("отпечаток не читает файлы профиля", reads == [], reads)
    finally:
        type(b).read = real_read

    stamp = research._stamp(b)
    check("отпечаток устойчив между вызовами", research._stamp(b) == stamp)

    dg = research.profile_digest(b)
    again = research.profile_digest(b)
    check("второй вызов берёт готовое", not again.rebuilt)
    check("отпечаток тот же", again.stamp == dg.stamp, again.stamp)

    path = b.path(research.DIGEST_PATH)
    path.write_text(path.read_text(encoding="utf-8").replace(
        f"stamp: {dg.stamp}", "stamp: сбит"), encoding="utf-8")
    check("сбитый отпечаток ведёт к пересборке",
          research.profile_digest(b).rebuilt)

    # ── 2. Что попало в индекс ────────────────────────────────────────
    print("\n2. Состав индекса")

    dg = research.profile_digest(b)
    for needle in research.CORE_SECTIONS:
        block = b.section("core", needle)
        if not block:
            continue
        head = block.split("\n", 1)[0].lstrip("# ").strip()
        check(f"секция «{needle}» в индексе", head in dg.text, head)

    check("голос в индекс не уехал",
          "Стоп-слова" not in dg.text,
          "стоп-слова читает check_voice из core.md, копия развела бы их")
    check("индекс короче ядра целиком",
          len(dg.text) < len(b.read("core")),
          f"{len(dg.text)} против {len(b.read('core'))}")
    check("нехватка файлов названа", isinstance(dg.missing, tuple))
    check("пропорция воронки даёт сотню",
          sum(dg.funnel.values()) == 100, dg.funnel)

    check("заголовок поста режется коротко, а не по потолку выжимки",
          len(research._cut("слово " * 200)) <= 61,
          "две функции звались _cut, и та, что режет до 1500, перекрывала эту")

    # ── 3. Окно недели ────────────────────────────────────────────────
    print("\n3. Окно прошлой недели")

    w = research.last_week(date(2026, 8, 31))       # понедельник
    check("окно это понедельник — воскресенье",
          (w.start, w.end) == (date(2026, 8, 24), date(2026, 8, 30)), str(w))
    check("окно названо покрытой неделей", w.name == "2026-W35", w.name)

    w2 = research.last_week(date(2026, 9, 3))       # четверг
    check("в середине недели окно то же", w2.name == w.name, w2.name)

    check("пост внутри окна принят", w.holds(datetime(2026, 8, 26, tzinfo=UTC)))
    check("пост до окна отброшен", not w.holds(datetime(2026, 8, 23, tzinfo=UTC)))
    check("пост после окна отброшен", not w.holds(datetime(2026, 8, 31, tzinfo=UTC)))
    check("пост без даты не считается своим", not w.holds(None))

    posts = [post(100, date(2026, 8, 25)), post(200, date(2026, 8, 27)),
             post(9999, date(2026, 3, 19)), post(50, None)]
    inside, outside, undated = w.split(posts)
    check("в окне только свои", len(inside) == 2, len(inside))
    check("вне окна посчитано", outside == 1, outside)
    check("без даты посчитано отдельно", undated == 1, undated)

    check("лента дотянулась до начала окна",
          w.reaches([post(1, date(2026, 8, 20))]))
    check("недотянувшаяся лента названа",
          not w.reaches([post(1, date(2026, 8, 28))]),
          "иначе неполный срез выдаётся за недельный")
    check("пустая отдача это не покрытие", not w.reaches([]))

    # ── 4. Медиана считается по окну ──────────────────────────────────
    print("\n4. Медиана по окну")

    s = src([post(10, date(2026, 8, 25)), post(20, date(2026, 8, 26)),
             post(30, date(2026, 8, 27)), post(50_000, date(2026, 3, 19))])
    plain = research.measure(s)
    windowed = research.measure(s, window=w)
    check("без окна старый пост тянет медиану",
          plain.median > windowed.median, f"{plain.median} против {windowed.median}")
    check("в окне медиана по своим", windowed.median == 20, windowed.median)
    check("постов вне окна названо", windowed.outside == 1, windowed.outside)
    check("окно записано в статистику", windowed.window == str(w), windowed.window)
    check("порог достоверности держится", not windowed.enough,
          "на трёх постах медиана это совпадение")

    # ── 5. Разделы контракта input.md ─────────────────────────────────
    print("\n5. Факты в input.md")

    free_bridge()
    task_id = bridge.create_task(CHAT, "план на неделю", workflow="plan",
                                 today="2026-08-31", brand_slug=b.slug,
                                 brand_path=str(b.root))
    text = (bridge.TASKS_DIR / task_id / "input.md").read_text(encoding="utf-8")

    for head in ("## Окно плана и свободные слоты", "## Индекс профиля",
                 "## Сводка Ресёрчера", "## События недели",
                 "## Архив и невышедшее"):
        check(f"раздел «{head[3:]}» на месте", head in text)

    check("индекс назван путём, а не текстом",
          research.DIGEST_PATH in text and "## 1. Кто это" not in text,
          "текст индекса читает Стратег, Director платил бы за него зря")
    check("отпечаток индекса приехал",
          research.profile_digest(b).stamp in text)
    check("сказано, читались ли файлы профиля",
          "не менялись" in text or "менялись" in text)
    check("свежесть сводки названа",
          research.last_week().name in text,
          "иначе план встанет на сводке позапрошлого месяца молча")
    recent, left = strategy.archive(CHAT)
    if recent:
        check("недавние темы приехали фактом", recent[0] in text, recent[0])
    check("сказано не искать архив в папке",
          "posts/" in text and "не надо" in text,
          "Glob по папке даёт другой ответ: файл переживает снятую тему")

    check("имён субагентов в контракте нет",
          not any(n in text for n in ("researcher", "strategist", "ideator")),
          "кого звать, решает Director")

    # ── 5b. Цифры снимает Python, а не роль ───────────────────────────
    print("\n5b. Цифры фактом в задачу")

    calls: list[str] = []
    real_snap = research.snapshot

    async def fake_snap(brand, **kw):            # noqa: ANN001
        calls.append("снят")
        return ("Окно: тест. Медиана 42.", ["скринов нет"])

    research.snapshot = fake_snap
    try:
        note = await bridge.snapshot(task_id)
        check("снимок сделан для плана", calls == ["снят"], calls)
        stats = bridge.TASKS_DIR / task_id / bridge.STATS_FILE
        check("цифры легли отдельным файлом", stats.exists())
        body = stats.read_text(encoding="utf-8")
        check("в файле сами цифры", "Медиана 42" in body)
        check("сказано, что снял код",
              "research.snapshot" in body,
              "иначе роль решит, что цифры чьё-то мнение")

        text = (bridge.TASKS_DIR / task_id / "input.md").read_text(encoding="utf-8")
        check("контракт называет адрес цифр", bridge.STATS_FILE in text)
        check("цифры в контракт текстом не уехали",
              "Медиана 42" not in text,
              "их читает Ресёрчер, Director платил бы за них зря")
        check("дыра названа в контракте", "скринов нет" in text)
        check("сказано не считать самому",
              "сам не считай" in text or "заново не читай" in text, note[:80])

        calls.clear()
        free_bridge()
        t_post = bridge.create_task(CHAT, "напиши пост", workflow="post",
                                    today="2026-08-31", brand_slug=b.slug,
                                    brand_path=str(b.root))
        await bridge.snapshot(t_post)
        check("для поста цифры не снимаются", calls == [],
              "платить временем за то, что никто не прочтёт, незачем")

        async def boom(brand, **kw):             # noqa: ANN001
            raise RuntimeError("сеть легла")

        research.snapshot = boom
        free_bridge()
        t_bad = bridge.create_task(CHAT, "сводка", workflow="research",
                                   today="2026-08-31", brand_slug=b.slug,
                                   brand_path=str(b.root))
        await bridge.snapshot(t_bad)
        bad = (bridge.TASKS_DIR / t_bad / "input.md").read_text(encoding="utf-8")
        check("упавшая сеть не валит задачу", "сеть легла" in bad, bad[-200:])
        check("файла цифр при отказе нет",
              not (bridge.TASKS_DIR / t_bad / bridge.STATS_FILE).exists())
    finally:
        research.snapshot = real_snap

    # ── 6. Посадка плана с моста ──────────────────────────────────────
    print("\n6. Посадка плана")

    # Своя задача: проверки выше закрывали прогоны, и строки в `bridge_runs`
    # для прежней уже нет — а посадка берёт из неё чат и workflow.
    free_bridge()
    task_id = bridge.create_task(CHAT, "план на неделю", workflow="plan",
                                 today="2026-08-31", brand_slug=b.slug,
                                 brand_path=str(b.root))

    window, free = strategy.free_slots(CHAT)
    good, bad = free[0], ("2020-01-01", "telegram")
    contract = {
        "context": ["пропорция запасная"],
        "themes": [
            {"date": good[0], "plat": good[1], "format": "post",
             "rubric": "разбор", "goal": "warm", "funnel_stage": "tof",
             "title": "тема на свободный слот", "hook": "хук", "why": "зачем",
             "variants": []},
            {"date": bad[0], "plat": bad[1], "format": "post",
             "rubric": "разбор", "goal": "warm", "funnel_stage": "tof",
             "title": "тема в прошлом", "hook": "хук", "why": "зачем",
             "variants": []},
        ],
        "unmet": ["норм площадок нет"],
    }

    d = bridge.TASKS_DIR / task_id
    (d / "strategy.md").write_text(
        "# План недели\n\nпроза для человека\n\n```json\n"
        + json.dumps(contract, ensure_ascii=False) + "\n```\n", encoding="utf-8")

    res = bridge.Result(task_id=task_id)
    note = await bridge.harvest(res)

    check("посадка что-то сказала", bool(note), note)
    rows = db.q("SELECT * FROM themes WHERE chat_id = ? AND date = ?",
                CHAT, good[0])
    check("тема на свободный слот записана", len(rows) == 1, len(rows))
    check("статус idea", rows and rows[0]["status"] == "idea",
          rows and rows[0]["status"])
    check("id собран кодом, а не моделью",
          rows and rows[0]["id"].startswith(f"{good[0]}-{good[1]}"),
          rows and rows[0]["id"])
    check("тема из прошлого не записана",
          not db.q("SELECT 1 FROM themes WHERE chat_id = ? AND date = ?",
                   CHAT, bad[0]),
          "проверку слота на этом пути не делал никто")
    check("отброшенное названо человеку", "не сошлось" in note.lower(), note)
    check("id посаженных тем известны наружу",
          res.landed_ids == [r["id"] for r in rows], str(res.landed_ids))
    check("выгрузка плана легла в бренд",
          any(p.name.startswith(f"{window[0].isocalendar().year}-W")
              for p in b.path("plans").glob("*.md")))

    # ── 7. Посадка не верит артефакту на слово ────────────────────────
    print("\n7. Кривой артефакт")

    free_bridge()
    t2 = bridge.create_task(CHAT, "план", workflow="plan", today="2026-08-31",
                            brand_slug=b.slug, brand_path=str(b.root))
    (bridge.TASKS_DIR / t2 / "strategy.md").write_text(
        "# План\n\nпроза без контракта\n", encoding="utf-8")
    note = await bridge.harvest(bridge.Result(task_id=t2))
    check("контракт не найден — сказано, а не молчание",
          "контракт" in note.lower(), note)

    free_bridge()
    t3 = bridge.create_task(CHAT, "план", workflow="plan", today="2026-08-31",
                            brand_slug=b.slug, brand_path=str(b.root))
    (bridge.TASKS_DIR / t3 / "strategy.md").write_text(
        "```json\n{ это не json }\n```\n", encoding="utf-8")
    note = await bridge.harvest(bridge.Result(task_id=t3))
    check("битый json назван", "разобрал" in note.lower(), note)

    free_bridge()
    t4 = bridge.create_task(CHAT, "сводка", workflow="research",
                            today="2026-08-31", brand_slug=b.slug,
                            brand_path=str(b.root))
    check("не-план не сажается", await bridge.harvest(bridge.Result(task_id=t4)) == "",
          "сажать план из сводки нечего")

    free_bridge()
    t5 = bridge.create_task(CHAT, "план", workflow="plan", today="2026-08-31",
                            brand_slug=b.slug, brand_path=str(b.root))
    check("нет артефакта — нет посадки",
          await bridge.harvest(bridge.Result(task_id=t5)) == "",
          "файла нет значит субагент не отработал")

    # ── 7а. Посадка текста Редактора ──────────────────────────────────
    #
    # Кнопка «Ок» под текстом, которого нет в заводе, это обман: человек
    # соглашается, а соглашаться не с чем. Значит текст должен садиться
    # тем же кодом, что у старого Редактора, и валидатор здесь настоящий
    # гейт, а не самопроверка субагента.
    print("\n7а. Посадка текста")

    tid = "2026-09-09-telegram-77"
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE id = ?", (tid,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, goal, "
                  "status, title, hook, why) VALUES "
                  "(?,?,?,?,?,'warm','idea',?,?,?)",
                  (tid, CHAT, "2026-09-09", "telegram", "пост",
                   "тема под текст", "хук", "кому и зачем"))

    clean = ("Первая строка держит сама.\n\nВторая мысль про конкретный "
             "вечер и открытый файл. Дальше вывод, что делать завтра.")

    def post_task(contract: dict, name: str = "post.md") -> bridge.Result:
        free_bridge()
        t = bridge.create_task(CHAT, "напиши пост", workflow="post",
                               today="2026-08-31", brand_slug=b.slug,
                               brand_path=str(b.root))
        (bridge.TASKS_DIR / t / name).write_text(
            "текст для человека\n\n```json\n"
            + json.dumps(contract, ensure_ascii=False) + "\n```\n",
            encoding="utf-8")
        return bridge.Result(task_id=t)

    res = post_task({"theme_id": tid, "text": clean,
                     "checks": {"voice": 5}, "hold": "", "breaks": "",
                     "notes": ["ссылку на вебинар уточнить"]})
    note = await bridge.harvest(res)
    row = db.one("SELECT * FROM themes WHERE id = ?", tid)

    check("тема переведена в draft", row["status"] == "draft", row["status"])
    check("путь к тексту записан", row["asset"] == f"posts/{tid}.md",
          row["asset"])
    check("текст лёг в папку бренда", clean.split("\n")[0] in b.read(row["asset"]))
    check("id темы известен наружу для кнопок", res.landed_ids == [tid],
          str(res.landed_ids))
    check("пометки Редактора не проглочены", "вебинар" in note, note)

    # ── 7б. Валидатор голоса сильнее самопроверки субагента ───────────
    print("\n7б. Отказ валидатора на пути моста")

    tid2 = "2026-09-10-telegram-77"
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE id = ?", (tid2,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, goal, "
                  "status, title, hook, why) VALUES "
                  "(?,?,?,?,?,'warm','idea',?,?,?)",
                  (tid2, CHAT, "2026-09-10", "telegram", "пост",
                   "тема с длинным тире", "хук", "кому и зачем"))

    res = post_task({"theme_id": tid2,
                     "text": "Первая строка — и сразу мысль.",
                     "checks": {"voice": 5}, "notes": []})
    note = await bridge.harvest(res)
    check("текст с находкой не сел",
          db.one("SELECT status FROM themes WHERE id = ?", tid2)["status"]
          == "idea", "самопроверке субагента поверили на слово")
    check("отказ назван человеку", "не сел" in note.lower(), note)
    check("кнопок под несевшим текстом не будет", res.landed_ids == [],
          str(res.landed_ids))

    res = post_task({"theme_id": "нет-такой-темы", "text": clean,
                     "checks": {"voice": 5}})
    check("чужой id темы отброшен",
          "не сел" in (await bridge.harvest(res)).lower())

    res = post_task({"theme_id": tid, "text": clean}, name="final.md")
    check("final.md артефактом роли не считается",
          await bridge.harvest(res) == "",
          "письмо человеку не контракт: посадить его нельзя")

    # ── 7в. Сажается сделанное, а не объявленное ──────────────────────
    #
    # Director вправе свернуть план до одного текста: тема уже стоит в
    # базе статусом idea, значит Стратег не нужен. Прогон
    # 2026-08-31-plan-04 так и прошёл — и текст не сел, потому что
    # посадка разбирала объявленный workflow и не нашла strategy.md.
    # Восемь минут работы пролежали в tasks/, а он в .gitignore.
    print("\n7в. Шапка говорит plan, отработал Редактор")

    tid3 = "2026-09-11-telegram-77"
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE id = ?", (tid3,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, goal, "
                  "status, title, hook, why) VALUES "
                  "(?,?,?,?,?,'warm','idea',?,?,?)",
                  (tid3, CHAT, "2026-09-11", "telegram", "пост",
                   "тема под свёрнутый план", "хук", "кому и зачем"))

    free_bridge()
    t6 = bridge.create_task(CHAT, "напиши пост по теме", workflow="plan",
                            today="2026-08-31", brand_slug=b.slug,
                            brand_path=str(b.root))
    (bridge.TASKS_DIR / t6 / "post.md").write_text(
        "текст для человека\n\n```json\n"
        + json.dumps({"theme_id": tid3, "text": clean,
                      "checks": {"voice": 5}}, ensure_ascii=False)
        + "\n```\n", encoding="utf-8")

    res = bridge.Result(task_id=t6)
    note = await bridge.harvest(res)
    check("текст сел, хотя задача звалась планом",
          db.one("SELECT status FROM themes WHERE id = ?", tid3)["status"]
          == "draft", "посадка поверила шапке, а не диску")
    check("кнопки получит текст, а не план",
          res.post_ids == [tid3] and res.plan_ids == [],
          f"post_ids={res.post_ids} plan_ids={res.plan_ids}")
    check("посадка сказала об этом", bool(note), note)

    # ── 7г. Тема вне плана ────────────────────────────────────────────
    #
    # Просьба «сделай пост по такой теме» приходит мимо плана регулярно,
    # а посадка требует тему в базе: `editor.land` и `design.land`
    # отказывают словами «темы нет в базе». Придумать `theme_id` нельзя —
    # его проверяют, — поэтому тема заводится по факту просьбы, тем же
    # швом, что и тема под снятый дубль: `desk.adhoc`.
    #
    # Командой, а не полем в контракте: в одном прогоне по одной теме
    # идут Редактор и Дизайнер, и второму нужен тот же id, что и первому.
    # Посадка случается после конца прогона — id, заведённый там, второму
    # уже не достался бы.
    print("\n7г. Тема вне плана")

    free_bridge()
    t7 = bridge.create_task(CHAT, "сделай пост карусель про страх перед AI",
                            workflow="post", today="2026-08-31",
                            brand_slug=b.slug, brand_path=str(b.root))

    def adhoc(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(harness.REPO / "tools" / "theme_adhoc.py"),
             *args], capture_output=True, text=True, env=os.environ,
            cwd=harness.REPO)

    r = adhoc(t7, "--plat", "instagram", "--format", "карусель",
              "--title", "Как перестать бояться AI",
              "--hook", "Страшно не вам одному")
    tid_ad = r.stdout.strip()
    check("тема вне плана заведена", r.returncode == 0, r.stderr)
    check("напечатан один id и ничего больше",
          bool(desk.ID_RX.fullmatch(tid_ad)), repr(r.stdout))

    ad = db.one("SELECT * FROM themes WHERE id = ?", tid_ad)
    check("тема легла в чат задачи, а не в аргумент из головы",
          ad and ad["chat_id"] == CHAT)
    check("помечена как вне плана", ad["src"] == "adhoc", ad["src"])
    check("даты у неё нет: чужой день она не занимает",
          not ad["date"], ad["date"])
    check("статус idea: работа впереди", ad["status"] == "idea", ad["status"])
    check("формат доехал", ad["format"] == "карусель", ad["format"])

    r = adhoc(t7, "--plat", "tiktok", "--title", "чужая площадка")
    check("чужая площадка — отказ, а не тихий дефолт",
          r.returncode == 1 and "не из набора" in r.stderr, r.stderr)
    r = adhoc("2020-01-01-post-99", "--plat", "telegram", "--title", "х")
    check("тема без задачи не заводится", r.returncode == 2, r.stderr)

    # Дальше по цепи всё как у плановой темы: текст садится, статус
    # переходит в draft, у темы появляется файл.
    (bridge.TASKS_DIR / t7 / "post.md").write_text(
        "текст для человека\n\n```json\n"
        + json.dumps({"theme_id": tid_ad, "text": clean,
                      "checks": {"voice": 5}}, ensure_ascii=False)
        + "\n```\n", encoding="utf-8")
    res = bridge.Result(task_id=t7)
    await bridge.harvest(res)
    landed = db.one("SELECT * FROM themes WHERE id = ?", tid_ad)
    check("текст по теме вне плана сел", landed["status"] == "draft",
          landed["status"])
    check("файл текста записан в папку бренда", bool(landed["asset"]),
          "иначе Дизайнеру нечего верстать")

    # Дизайнер до неё доходит: обе проверки посадки макета — тема в базе
    # и утверждённый текст — пройдены, и отказ остаётся только по макету.
    try:
        await design.land(CHAT, {"theme_id": tid_ad})
        why = ""
    except Exception as e:                                    # noqa: BLE001
        why = str(e)
    check("макет упирается в макет, а не в тему",
          "нет в базе" not in why and "утверждённого текста" not in why, why)

    # ── 7б-2. Посадка макета Дизайнера ────────────────────────────────
    #
    # Макет должен оказаться в проекте, а не только в чате: папку бренда
    # отдают клиенту, а `tasks/` лежит в `.gitignore`. Разметку по
    # шаблонной площадке собирает код из слотов — второй копии шаблона в
    # этом проекте быть не должно.
    print("\n7б-2. Посадка макета")

    real_render = design.render

    async def fake_render(path, size):
        png = path.with_suffix(".png")
        png.write_bytes(b"\x89PNG")
        return png

    design.render = fake_render                # Chrome тут не проверяем
    try:
        tid3 = "2026-09-11-telegram-77"
        copy = "Кода не писала ни строчки. Навык нужен тот же самый."
        b.artifact(f"posts/{tid3}.md", f"<!-- {tid3} -->\n\n{copy}")
        with db.tx() as c:
            c.execute("DELETE FROM themes WHERE id = ?", (tid3,))
            c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                      "rubric, goal, status, title, asset) VALUES "
                      "(?,?,'2026-09-11','telegram','пост','Путь','warm',"
                      "'ready','Путь в AI',?)", (tid3, CHAT, f"posts/{tid3}.md"))

        def design_task(contract: dict) -> bridge.Result:
            free_bridge()
            t = bridge.create_task(CHAT, "свёрстай макет", workflow="design",
                                   today="2026-08-31", brand_slug=b.slug,
                                   brand_path=str(b.root))
            (bridge.TASKS_DIR / t / "design.md").write_text(
                "что собрано\n\n```json\n"
                + json.dumps(contract, ensure_ascii=False) + "\n```\n",
                encoding="utf-8")
            return bridge.Result(task_id=t)

        res = design_task({
            "theme_id": tid3,
            "cards": [{"name": "cover", "slots": {
                "rubric": "ПУТЬ В AI", "headline": "Кода не писала",
                "headline_accent": " ни строчки",
                "subtitle": "Навык нужен тот же самый",
                "photo": "author.jpg"}}],
            "accent": "терракота на слове", "notes": []})
        note = await bridge.harvest(res)

        html = b.path(f"posts/{tid3}-cover.html")
        check("макет лёг в папку бренда, а не в задачу", html.exists(), note)
        check("PNG рядом с макетом", html.with_suffix(".png").exists())
        check("слоты положены рядом",
              b.path(f"posts/{tid3}-cover.slots.json").exists(),
              "без них правка снова стала бы разговором про HTML")
        check("разметку собрал код из шаблона",
              "Кода не писала" in html.read_text(encoding="utf-8")
              and "{{headline}}" not in html.read_text(encoding="utf-8"))
        check("макет есть чем показать человеку",
              res.landed_obj is not None and len(res.landed_obj.pngs) == 1,
              str(res.landed_obj))
        check("тема названа в отчёте", tid3 in note, note)

        res = design_task({"theme_id": "нет-такой-темы", "cards": [
            {"name": "cover", "slots": {"headline": "х"}}]})
        check("макет без своей темы не сел",
              "не сел" in (await bridge.harvest(res)).lower())

        with db.tx() as c:
            c.execute("UPDATE themes SET asset = NULL WHERE id = ?", (tid3,))
        res = design_task({"theme_id": tid3, "cards": [
            {"name": "cover", "slots": {"headline": "х"}}]})
        check("верстать без утверждённого текста не станем",
              "текст" in (await bridge.harvest(res)).lower(),
              "верстать черновик значит верстать дважды")
    finally:
        design.render = real_render

    # ── 7в. События недели спрашиваются до запуска ────────────────────
    #
    # Спросить Стратега посреди прогона некому: конец хода Director это
    # конец процесса. Значит вопрос задаётся заранее, а ответ ложится в
    # файл со штампом окна — иначе про ту же неделю спросят второй раз.
    print("\n7в. События недели")

    events = b.path(bridge.EVENTS_PATH)
    events.unlink(missing_ok=True)
    check("файла нет — спрашиваем", not bridge.events_known(CHAT))

    window = bridge.plan_window(CHAT)
    check("в вопросе названо окно датами",
          window[0] in bridge.events_question(CHAT), bridge.events_question(CHAT))

    check("ответ про вебинар считается событием",
          bridge.save_events(CHAT, f"{window[2]} вебинар про контент-завод"))
    check("файл заведён", events.exists())
    check("про это окно больше не спрашиваем", bridge.events_known(CHAT))
    check("событие доехало фактом в input.md",
          "вебинар" in "\n".join(bridge._events(CHAT)))

    check("«нет» это тоже ответ", not bridge.save_events(CHAT, "нет"))
    check("после «нет» не спрашиваем снова", bridge.events_known(CHAT))

    # Прошлая неделя в файле остаётся, но в промпт не едет: чужое событие
    # рядом с нынешним читается как ещё одно событие недели.
    events.write_text("## Неделя 2020-01-01 — 2020-01-07\n\n"
                      "- вебинар пятилетней давности\n", encoding="utf-8")
    check("старая неделя не считается заявленной",
          not bridge.events_known(CHAT))

    events.unlink(missing_ok=True)

    # ── 8. Адаптеры собраны кодом и не разошлись с источником ─────────
    print("\n8. Адаптеры субагентов")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import build_agents                                       # noqa: PLC0415

    check("собранное совпадает с источником", build_agents.main(check=True) == 0,
          "пересобрать: ./.venv/bin/python tools/build_agents.py")

    for name, role_file in build_agents.BUILT.items():
        text = (build_agents.OUT / f"{name}.md").read_text(encoding="utf-8")
        check(f"{name}: каркас вклеен", "# Общий каркас" in text)
        check(f"{name}: роль вклеена целиком",
              f"roles/{role_file}" in text and "Формат выдачи" in text)
        check(f"{name}: сказано не открывать их заново",
              "не надо" in text and "вклеены ниже" in text,
              "иначе роль прочитает то, что уже перед глазами")
        check(f"{name}: подстановок не осталось",
              not re.search(r"\{(role_name|upstream|downstream|output|"
                            r"anti_scope|brand_name|layers|sections)\}", text),
              "frame.md уезжал в модель с литеральными фигурными скобками")
        check(f"{name}: имя роли подставлено",
              "Ты Стратег" in text or "Ты Ресёрчер" in text)
        check(f"{name}: правка руками запрещена вслух",
              "НЕ ПРАВИТЬ РУКАМИ" in text)

    # ── 9. Каркас на пути бота ────────────────────────────────────────
    # Тот же каркас, но собирает его `agent`, а не сборщик адаптеров. До
    # 03.09 он уезжал в модель дословно: роль читала «Ты {role_name}».
    print("\n9. Каркас в системном промпте бота")
    from orchestrator import agent                             # noqa: PLC0415

    sysd = agent.system_text("design", brand_name="Lily Space")
    syse = agent.system_text("editor", brand_name="Lily Space")

    check("подстановок в промпте бота не осталось",
          not agent.leftovers(sysd), str(agent.leftovers(sysd)))
    check("имя роли подставлено", "Ты Дизайнер" in sysd, sysd[:200])
    check("бренд подставлен", "бренда Lily Space" in sysd)
    check("соседи по конвейеру названы", "перед Публикатор" in sysd)

    # Дизайнер не пишет текста: правила голоса, анти-AI и признаков
    # машинного текста ему применять не к чему.
    check("Дизайнеру правила письма не едут",
          "Признаки машинного текста" not in sysd and "Анти-AI" not in sysd,
          "лишние 3674 знака в каждом вызове")
    check("Редактору правила письма едут",
          "Признаки машинного текста" in syse and "Анти-AI" in syse)
    check("каркас у Дизайнера короче",
          len(sysd) < len(syse) - 3000, f"{len(sysd)} против {len(syse)}")

    # Каркас требовал блок «Контекст» и строку передачи, а роли с JSON —
    # «одним объектом и ничем больше». Две инструкции спорили в одном
    # промпте; требование про «Контекст» ушло к пишущим ролям.
    check("каркас не требует «Контекст» у роли с JSON",
          "блоком «Контекст»" not in sysd, sysd)
    check("каркас говорит про чистый JSON", "только JSON" in sysd)

    report()


if __name__ == "__main__":
    asyncio.run(main())
