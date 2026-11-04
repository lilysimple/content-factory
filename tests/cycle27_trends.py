"""Цикл 27: чужие рилсы — выбросы, метрика партии, старение кэша.

Четыре границы, из-за которых разбор трендов разведён на платный сборщик
и считающего читателя.

Выброс считает код. Роль, которой дали сырой список с числами, называет
залетевшим то, что красивее написано: ровно это случилось с ручным
разбором 18.09, где часть списка попала в него без чисел вовсе. Медиана,
кратность и порог живут в `trends`, и роль их не пересчитывает.

Метрика у партии одна и названа. Instagram отдаёт просмотры только у
видео, лайки — у всего; партия, где просмотры есть у трети роликов,
считается по лайкам и говорит об этом, а не сравнивает шесть роликов из
восемнадцати, называя это медианой источника.

Мало роликов — дыра, а не слабая статистика: при пяти роликах любой
второй оказывается «вдвое выше медианы».

Кэш стареет молча, и тренд стареет быстрее профиля. Приём, собиравший
просмотры месяц назад, сегодня разошёлся по всем — поэтому запас здесь
пять дней, а не восемь.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone

import harness
from harness import check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import trends                                   # noqa: E402
from storage.brand import Brand                                   # noqa: E402


def brand() -> Brand:
    path = cfg.brands_path / "lily-space"
    return Brand("lily-space", path)


def reel(n: int, views=None, likes=None, owner="a") -> dict:
    """Один ролик партии. Дата относительная: вшитая переезжает за порог."""
    when = datetime.now(timezone.utc) - timedelta(days=2)
    return {"code": f"C{n}", "url": f"https://www.instagram.com/reel/C{n}/",
            "owner": owner, "caption": f"Хук номер {n}\nвторая строка",
            "date": when.isoformat(timespec="seconds"),
            "views": views, "likes": likes, "comments": 3,
            "seconds": 42.0, "followers": 12000}


def main() -> None:
    b = brand()

    # Живой кэш трендов из папки бренда в песочницу приезжает: стенд
    # чистит файлы в `research/`, но не в её подпапках. Цикл не должен
    # зависеть от того, что человек снял сборщиком вчера, — иначе он
    # начинает падать от чужой работы, как это уже было у `research`.
    shutil.rmtree(b.path(trends.CACHE), ignore_errors=True)

    # ── адрес цели ───────────────────────────────────────────────────
    check("хэштег разбирается", (t := trends.target("#aiДляРаботы")) is not None
          and t.kind == "tag" and t.value == "aiДляРаботы")
    check("профиль разбирается", (t := trends.target("@tech.unicorn")) is not None
          and t.kind == "user" and t.value == "tech.unicorn")
    check("голое слово целью не считается", trends.target("ai") is None)
    check("имя файла кэша считается одинаково",
          trends.target("#AI").name == "tag-ai")

    # ── список целей ─────────────────────────────────────────────────
    found, gaps = trends.targets(b)
    check("списка целей нет — это дыра, а не отказ",
          not found and any("trends.md" in g for g in gaps), str(gaps))

    (b.path("research")).mkdir(parents=True, exist_ok=True)
    b.path(trends.WATCHLIST).write_text(
        "# Цели\n\n"
        "- #aiдляработы — ядро ниши\n"
        "- @tech.unicorn — ближайший сосед\n"
        "- ai без решётки — так писать нельзя\n"
        "Строка без дефиса игнорируется\n", encoding="utf-8")
    found, gaps = trends.targets(b)
    check("две цели разобрались", len(found) == 2, str([t.human for t in found]))
    check("зачем смотрим сохранилось", found[0].why == "ядро ниши", found[0].why)
    check("кривая строка названа дырой",
          any("не разобралась" in g for g in gaps), str(gaps))

    # ── кэша нет ─────────────────────────────────────────────────────
    batch, gap = trends.read(b, found[0])
    check("без кэша партия не считается", not batch.ok)
    check("дыра называет сборщик", "trends_pull" in gap, gap)

    # ── мало роликов: дыра, а не слабая статистика ───────────────────
    trends.stash(b, {"target": "#aiдляработы",
                     "items": [reel(i, views=100 * i) for i in range(1, 5)]})
    batch, gap = trends.read(b, found[0])
    check("четыре ролика — партия не считается", not batch.ok)
    check("дыра называет порог", str(trends.MIN_ITEMS) in gap, gap)
    check("выбросов у несчитанной партии нет", batch.hot == [])

    # ── партия на пределе считается, но названа ненадёжной ───────────
    #
    # Потолок источника измерен 14.10: актор отдаёт первую страницу сетки
    # профиля, в окне остаётся пять рилсов. При пороге восемь контур не
    # считал ни одной живой партии вовсе.
    six = [reel(i, views=100) for i in range(1, 6)]
    six.append(reel(96, views=400, owner="b"))
    trends.stash(b, {"target": "#aiдляработы", "items": six})
    batch, gap = trends.read(b, found[0])
    check("шесть роликов считаются", batch.ok, batch.error)
    check("но партия помечена ненадёжной", batch.shaky)
    check("выброс на такой партии всё равно найден",
          [r.code for r in batch.hot] == ["C96"], str([r.code for r in batch.hot]))
    out, _ = trends.digest(b)
    check("ненадёжность доезжает до роли",
          "ненадёжная" in "\n".join(out), "\n".join(out)[:300])

    # ── закреплённый ролик мимо фильтра актора ───────────────────────
    #
    # Схема предупреждает прямо, и живой прогон это подтвердил: в партии
    # соседа приехали закреплённые 2024 и 2023 годов, один на 97 млн
    # просмотров. В медиане из пяти такая запись делает выбросом всё
    # остальное — вернее, не делает ничего выбросом.
    pinned = {**reel(95, views=97_000_000, owner="z"),
              "date": "2024-10-01T12:00:00+00:00"}
    wide = [reel(i, views=100) for i in range(1, 7)] + [pinned]
    since = (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
    trends.stash(b, {"target": "#aiдляработы", "since": since, "items": wide})
    batch, gap = trends.read(b, found[0])
    check("закреплённый ролик вне окна отброшен",
          batch.dropped == 1 and len(batch.items) == 6, str(batch.dropped))
    check("медиана считается без него", batch.median == 100, str(batch.median))
    check("окно партии прочитано", batch.since is not None)
    out, _ = trends.digest(b)
    check("отброшенное названо вслух",
          "Вне окна отброшено 1" in "\n".join(out), "\n".join(out)[:400])

    nowin = [reel(i, views=100) for i in range(1, 7)]
    trends.stash(b, {"target": "#aiдляработы", "items": nowin})
    batch, _ = trends.read(b, found[0])
    check("без окна ничего не режется", batch.dropped == 0)
    out, _ = trends.digest(b)
    check("отсутствие окна названо вслух",
          "Окна по дате у партии нет" in "\n".join(out))

    # ── просмотры есть у всех: медиана по просмотрам ─────────────────
    items = [reel(i, views=100, likes=10) for i in range(1, 10)]
    items.append(reel(99, views=1000, likes=11, owner="b"))      # выброс 10×
    items.append(reel(98, views=150, likes=12, owner="c"))       # 1.5×, не выброс
    trends.stash(b, {"target": "#aiдляработы", "items": items})
    batch, gap = trends.read(b, found[0])
    check("партия посчиталась", batch.ok, batch.error)
    check("метрика названа просмотрами", batch.metric == "просмотры", batch.metric)
    check("медиана по просмотрам", batch.median == 100, str(batch.median))
    check("выброс один", len(batch.hot) == 1, str([r.code for r in batch.hot]))
    check("кратность посчитана", batch.hot[0].rate == 10.0, str(batch.hot[0].rate))
    check("полуторный ролик выбросом не считается",
          all(r.code != "C98" for r in batch.hot))
    check("свежий кэш дырой не называется", gap == "", gap)
    check("хук это первая строка подписи",
          batch.hot[0].hook == "Хук номер 99", batch.hot[0].hook)

    # ── просмотры у трети: медиана по лайкам, и это сказано ──────────
    mixed = [reel(i, likes=10) for i in range(1, 10)]
    mixed += [reel(i, views=5000, likes=10) for i in (20, 21)]
    mixed.append(reel(97, likes=100, owner="b"))
    trends.stash(b, {"target": "#aiдляработы", "items": mixed})
    batch, _ = trends.read(b, found[0])
    check("без покрытия просмотрами партия считается по лайкам",
          batch.metric == "лайки", batch.metric)
    check("медиана по лайкам", batch.median == 10, str(batch.median))
    check("выброс по лайкам найден",
          [r.code for r in batch.hot] == ["C97"], str([r.code for r in batch.hot]))

    # ── старение ─────────────────────────────────────────────────────
    t = found[0]
    check("свежий кэш не стар", trends._aged(t, datetime.now(timezone.utc)) == "")
    old = datetime.now(timezone.utc) - timedelta(days=trends.STALE_DAYS + 1)
    aged = trends._aged(t, old)
    check("кэш за порогом назван дырой", "дней назад" in aged, aged)
    check("без даты сбора свежим не считается",
          "нет даты сбора" in trends._aged(t, None))

    # ── выжимка для роли ─────────────────────────────────────────────
    trends.stash(b, {"target": "#aiдляработы", "items": items})
    out, gaps = trends.digest(b)
    text = "\n".join(out)
    check("в выжимке есть кратность", "Кратность" in text)
    check("медиана источника названа", "медиана" in text)
    check("роль предупреждена, что ролика в кэше нет",
          "самого ролика нет" in text.lower(), text[-400:])
    check("ссылка на выброс доехала",
          "https://www.instagram.com/reel/C99/" in text)
    check("ролик ниже порога в выжимку не попал", "/reel/C98/" not in text)
    check("непокрытая цель осталась дырой",
          any("@tech.unicorn" in g for g in gaps), str(gaps))

    # ── партий нет вовсе ─────────────────────────────────────────────
    trends.cache_path(b, found[0].name).unlink()
    out, gaps = trends.digest(b)
    text = "\n".join(out)
    check("без партий роль не придумывает чужие ролики",
          "не придумывай" in text, text)
    check("дыры при этом названы: обе цели и кривая строка",
          len(gaps) == 3 and sum("кэша нет" in g for g in gaps) == 2, str(gaps))

    # ── форму держит читатель, а не сборщик ──────────────────────────
    try:
        trends.stash(b, {"target": "ai", "items": []})
        check("кривая цель не записалась", False, "записал")
    except ValueError as e:
        check("кривая цель не записалась", "не разобралась" in str(e), str(e))
    try:
        trends.stash(b, {"target": "#ai", "items": "не список"})
        check("кривые items не записались", False, "записал")
    except ValueError as e:
        check("кривые items не записались", "списком" in str(e), str(e))

    # ── числа не выдумываются ────────────────────────────────────────
    trends.stash(b, {"target": "#ai", "items": [
        {"code": "X", "caption": "без чисел"}]})
    raw = json.loads(trends.cache_path(b, "tag-ai").read_text(encoding="utf-8"))
    check("не отданное число это прочерк, а не ноль",
          raw["items"][0]["views"] is None and raw["items"][0]["likes"] is None,
          str(raw["items"][0]))

    # ── проводка в заводе ────────────────────────────────────────────
    from bots.handlers import AS_WORKFLOW, TOPIC
    from bots.router import resolve
    from orchestrator import bridge

    check("workflow заведён", "trend" in bridge.WORKFLOWS)
    check("факты кладутся под него", "trend" in bridge.CONTEXT)
    check("команда /trend берётся из того же списка",
          "trend" in "|".join(bridge.WORKFLOWS))
    check("ответ уходит в топик Reels", TOPIC["trend"] == "reels")
    check("просьба словами уходит мостом", AS_WORKFLOW.get("trend") == "trend")
    check("все значения AS_WORKFLOW это настоящие workflow",
          set(AS_WORKFLOW.values()) <= set(bridge.WORKFLOWS))
    check("залетевшие рилсы идут не в сводку",
          resolve("разбери залетающие рилсы", None, {}).role == "trend")
    check("сводка недели осталась у Ресёрчера",
          resolve("собери сводку недели", None, {}).role == "research")
    check("сценарий остался у Редактора Reels",
          resolve("напиши сценарий рилса", None, {}).role == "reels")
    check("имя субагента человеку названо",
          bridge.AGENT_NAMES.get("trendwatcher") == "Trend Watcher")

    # ── запрос к актору: проверен живым прогоном, а не схемой ────────
    #
    # Схема входа снята 14.10 с опубликованного билда
    # `apify/instagram-scraper`, но главное выяснил прогон, а не схема:
    # поиск по тегу у `instagram-scraper` идёт через Google и на
    # `#claudeai` вернул одну запись — огрызок страницы чужого тега, ни
    # одного поста. Страница тега прямым адресом тоже не годится: 21
    # запись, все фото и карусели. Рилсы по тегу отдаёт другой актор,
    # поэтому цели разведены по роду: тег — в хэштег-скрапер с
    # `resultsType: reels`, профиль — в сеточный.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "trends_pull", harness.REPO / "tools" / "trends_pull.py")
    pull = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pull)

    check("тег снимает хэштег-скрапер",
          pull.actor_of(trends.target("#claudeai")) == cfg.apify_actor_tag,
          pull.actor_of(trends.target("#claudeai")))
    check("профиль снимает сеточный актор",
          pull.actor_of(trends.target("@x")) == cfg.apify_actor)

    tag = pull.actor_input(trends.target("#claudeai"), 50)
    check("тег просится полем hashtags, без решётки",
          tag["hashtags"] == ["claudeai"], str(tag.get("hashtags")))
    check("у тега просятся именно рилсы",
          tag["resultsType"] == "reels", tag.get("resultsType"))
    check("поиск актора не используется вовсе",
          "search" not in tag and "searchType" not in tag, str(sorted(tag)))
    check("тегу не посылается поле, которого у актора нет",
          "onlyPostsNewerThan" not in tag and "addParentData" not in tag,
          str(sorted(tag)))
    check("роликов просится по лимиту", tag["resultsLimit"] == 50)

    grid = pull.actor_input(trends.target("@tech.unicorn"), 30)
    check("у профиля тип выдачи posts: рилсы отбираются после",
          grid["resultsType"] == "posts", grid["resultsType"])
    check("у профиля окно по дате стоит явной датой",
          len(grid["onlyPostsNewerThan"]) == 10, grid.get("onlyPostsNewerThan"))

    user = pull.actor_input(trends.target("@tech.unicorn"), 30, days=0)
    check("профиль просится своим адресом",
          user["directUrls"] == ["https://www.instagram.com/tech.unicorn/"])
    check("нулевые дни снимают границу окна", "onlyPostsNewerThan" not in user)

    check("карусель рилсом не считается",
          not pull.is_reel({"type": "Sidecar"}))
    check("рилс узнаётся по productType",
          pull.is_reel({"productType": "clips"}))
    row = pull.convert({"shortCode": "A1", "videoPlayCount": 900,
                        "likesCount": 10, "ownerUsername": "b"})
    check("просмотры берутся из поля схемы", row["views"] == 900, str(row))
    check("ссылка собирается из кода",
          row["url"].endswith("/reel/A1/"), row["url"])
    bare = pull.convert({"shortCode": "A2"})
    check("не отданных чисел сборщик не придумывает",
          bare["views"] is None and bare["likes"] is None, str(bare))

    # ── посадка разбора ─────────────────────────────────────────────
    #
    # Разбор с моста до этого не возвращался в завод: роль писала
    # `tasks/{id}/trends.md`, папка задачи в `.gitignore`, и к следующему
    # прогону от механик не оставалось ничего — при том что адаптер роли
    # велел ей читать прошлые разборы в `research/trends-*.md`.
    #
    # Правило трёх и сверка ссылок с партией здесь же: промпт просил их
    # соблюдать, но не проверял никто.
    for old in b.path("research").glob(trends.REVIEW_GLOB):
        old.unlink()

    wide = [reel(i, views=100, owner="a") for i in range(1, 10)]
    # Код ролика длиной как у Instagram: ссылка сверяется регулярным
    # выражением, и трёхзначная заглушка под настоящий код не похожа.
    wide += [reel("91ab", views=1000, owner="x"),
             reel("92ab", views=1000, owner="y"),
             reel("93ab", views=1000, owner="z"),
             reel("94ab", views=1000, owner="x")]
    trends.stash(b, {"target": "#aiдляработы", "items": wide})

    def mech(name, codes, **kw) -> dict:
        got = {"name": name, "what": "число в заходе", "why": "обещание",
               "examples": [f"https://www.instagram.com/reel/{c}/ · 10× · @{c}"
                            for c in codes],
               "cover": "Первый Skill за вечер", "hooks": ["раз", "два"],
               "save": "открыть вечером", "skip": "чужой темп"}
        got.update(kw)
        return got

    rev, rel = trends.land(b, {
        "window": "#aiдляработы, снято сегодня",
        "metric": "просмотры",
        "mechanics": [
            mech("число в заходе", ["C91ab", "C92ab", "C93ab"]),
            mech("два примера", ["C91ab", "C92ab"]),
            mech("один автор", ["C91ab", "C94ab", "C91ab"]),
            mech("призрачная ссылка", ["C91ab", "C92ab", "ZZZZZ"]),
            mech("через стоп-слово", ["C91ab", "C92ab", "C93ab"],
                 cover="Секретный метод за вечер"),
            mech("стоп-слово у чужого", ["C91ab", "C92ab", "C93ab"],
                 what="держится на обещании «заработай миллион за 3 дня»"),
        ],
        "offers": [{"title": "Первый Skill", "hook": "раз", "seconds": 40}],
        "gaps": ["своей медианы в задаче не было"]})

    names = [m["name"] for m in rev.mechanics]
    check("механика по правилу трёх села",
          "число в заходе" in names, str(names))
    check("механика с двумя примерами уехала в единичные",
          any(s.get("what") == "два примера" for s in rev.singles),
          str(rev.singles))
    check("три ролика одного автора механикой не считаются",
          "один автор" not in names, str(names))
    check("ссылка, которой нет в партии, в счёт не идёт",
          "призрачная ссылка" not in names
          and any("ZZZZZ" in f for f in rev.findings), str(rev.findings))
    check("стоп-слово в переносе это отклонение, а не оговорка",
          "через стоп-слово" not in names
          and any(r.get("what") == "через стоп-слово" for r in rev.rejected),
          str(rev.rejected))
    check("стоп-слово в описании чужого приёма механику не роняет",
          "стоп-слово у чужого" in names, str(names))
    check("подвинутое кодом названо человеку строкой",
          len(rev.findings) >= 3, str(rev.findings))

    path = b.path(rel)
    text = path.read_text(encoding="utf-8")
    check("разбор лёг датированным файлом в папку бренда",
          path.exists() and path.match(trends.REVIEW_GLOB), rel)
    check("в файле есть механика и её перенос",
          "число в заходе" in text and "Первый Skill за вечер" in text)
    check("в файле есть отклонённое с причиной",
          "## Отклонено" in text and "стоп-слово бренда" in text, text[:300])
    check("в файле есть дыры роли", "своей медианы" in text)
    check("в файле есть заготовки тем", "Заготовки тем" in text)

    # Второй прогон в тот же день переписывает разбор, а не плодит второй:
    # два файла одной даты человек читает как два разных разбора.
    rev2, rel2 = trends.land(b, {"mechanics": [], "gaps": ["второй заход"]})
    check("второй разбор за день не плодит второй файл",
          rel2 == rel and len(list(b.path("research").glob(trends.REVIEW_GLOB))) == 1)
    check("пустая выдача садится дырой, а не молчанием",
          "второй заход" in b.path(rel2).read_text(encoding="utf-8"))

    try:
        trends.land(b, {})
        check("пустой контракт не садится", False, "сел")
    except trends.LandRefused as e:
        check("пустой контракт не садится", "пуст" in str(e), str(e))

    check("прошлый разбор находится на диске",
          (last := trends.last_review(b)) is not None and last.name == path.name,
          str(trends.last_review(b)))
    out, _ = trends.digest(b)
    check("задача называет роли прошлый разбор",
          any(path.name in line for line in out), "\n".join(out)[-300:])

    from orchestrator import bridge
    check("мост сажает разбор вместе с планом и текстом",
          "_land_trends" in bridge.harvest.__doc__ or
          hasattr(bridge, "_land_trends"))

    # ── промпт роли и адаптер на месте ───────────────────────────────
    role = (harness.REPO / "roles" / "trend.md").read_text(encoding="utf-8")
    agent = (harness.REPO / ".claude" / "agents" /
             "trendwatcher.md").read_text(encoding="utf-8")
    check("роль требует правило трёх с разными авторами",
          "трёх разных авторов" in role)
    check("роль требует перенос на бренд", "Перенос на бренд" in role)
    check("адаптер не копирует промпт", "адаптер, а не копия" in agent)
    check("адаптер запрещает свою арифметику",
          "арифметики не заводи" in agent)

    report()


if __name__ == "__main__":
    main()
