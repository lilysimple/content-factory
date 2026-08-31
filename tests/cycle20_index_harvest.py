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
from datetime import date, datetime, timedelta, timezone

import harness
from harness import CHAT, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import bridge, desk, research, sources, strategy  # noqa: E402
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
                 "## Сводка Ресёрчера", "## События недели"):
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
    note = bridge.harvest(res)

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
    note = bridge.harvest(bridge.Result(task_id=t2))
    check("контракт не найден — сказано, а не молчание",
          "контракт" in note.lower(), note)

    free_bridge()
    t3 = bridge.create_task(CHAT, "план", workflow="plan", today="2026-08-31",
                            brand_slug=b.slug, brand_path=str(b.root))
    (bridge.TASKS_DIR / t3 / "strategy.md").write_text(
        "```json\n{ это не json }\n```\n", encoding="utf-8")
    note = bridge.harvest(bridge.Result(task_id=t3))
    check("битый json назван", "разобрал" in note.lower(), note)

    free_bridge()
    t4 = bridge.create_task(CHAT, "сводка", workflow="research",
                            today="2026-08-31", brand_slug=b.slug,
                            brand_path=str(b.root))
    check("не-план не сажается", bridge.harvest(bridge.Result(task_id=t4)) == "",
          "сажать план из сводки нечего")

    free_bridge()
    t5 = bridge.create_task(CHAT, "план", workflow="plan", today="2026-08-31",
                            brand_slug=b.slug, brand_path=str(b.root))
    check("нет артефакта — нет посадки",
          bridge.harvest(bridge.Result(task_id=t5)) == "",
          "файла нет значит субагент не отработал")

    report()


if __name__ == "__main__":
    asyncio.run(main())
