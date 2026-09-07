"""Цикл 15: Ресёрчер — своя статистика и правило трёх.

Две границы, ради которых роль вообще собрана.

Цифры считает код: медиану и «что зашло» модель не выдумывает, а получает
готовыми. Правило трёх тоже держит код: приём с двумя примерами это
единичный случай, как бы уверенно роль его ни назвала механикой.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import agent, desk, research, sources           # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

CALLS = {"n": 0, "prompts": []}


def install(answer):
    async def ask(role, chat_id, prompt, **kw):
        CALLS["n"] += 1
        CALLS["prompts"].append(prompt)
        return answer(prompt) if callable(answer) else answer
    agent.ask = ask
    CALLS["n"] = 0
    CALLS["prompts"].clear()


def channel(views: list[int | None], title="Свой канал") -> sources.Source:
    src = sources.Source(url="https://t.me/s/own", kind="telegram", ok=True,
                         title=title, subscribers="36 подписчиков")
    for i, v in enumerate(views, 1):
        src.posts.append(sources.Post(f"Пост номер {i} про работу с моделью", v))
    return src


def fake_fetch(src_by_url: dict[str, sources.Source]):
    async def fetch(url, *, limit=40):
        key = sources.classify(url)[1]
        return src_by_url.get(key) or sources.Source(
            url=key, kind="telegram", ok=False, error="не открылось")

    async def fetch_all(urls, *, limit=40):
        return [await fetch(u, limit=limit) for u in urls]
    sources.fetch = fetch
    sources.fetch_all = fetch_all


def answer(mechanics=None, facts=None, gaps=None) -> str:
    return json.dumps({
        "facts": facts if facts is not None else ["Разборы собирают больше «как» постов"],
        "mechanics": mechanics if mechanics is not None else [
            {"name": "Разбор своей работы", "what": "показывать процесс, а не итог",
             "examples": ["пост про роли", "пост про дашборд", "пост про промпт"]},
        ],
        "gaps": gaps or [],
    }, ensure_ascii=False)


async def main() -> None:
    reg = FakeRegistry()
    b = desk.brand(CHAT)
    object.__setattr__(cfg, "publish_channel", "@own")

    # ── 1. цифры считает код ──────────────────────────────────────────
    print("\n1. Медиана и края считает код")
    st = research.measure(channel([10, 20, 30, 40, 50]))
    check("медиана посчитана", st.median == 30, str(st.median))
    check("верх отсортирован", [v for v, _ in st.best] == [50, 40, 30],
          str(st.best))
    check("низ отсортирован", [v for v, _ in st.worst] == [10, 20, 30],
          str(st.worst))
    check("постов с просмотрами посчитано", st.with_views == 5, str(st.with_views))
    check("пяти постов хватает для вывода", st.enough is True)

    thin = research.measure(channel([10, 20, None, None]))
    check("посты без просмотров не в счёт", thin.with_views == 2,
          str(thin.with_views))
    check("на двух постах вывод не делаем", thin.enough is False)

    empty = research.measure(channel([None, None]))
    check("совсем без просмотров не падает", empty.median == 0 and not empty.best,
          str(empty))

    # ── 2. правило трёх ───────────────────────────────────────────────
    print("\n2. Правило трёх держит код")
    good, singles = research.sift([
        {"name": "Три примера", "what": "годится", "examples": ["a", "b", "c"]},
        {"name": "Два примера", "what": "не годится", "examples": ["a", "b"]},
        {"name": "Без примеров", "what": "тем более", "examples": []},
        {"name": "", "what": "безымянная", "examples": ["a", "b", "c"]},
        "мусор вместо объекта",
    ])
    check("прошла только механика с тремя примерами",
          [m["name"] for m in good] == ["Три примера"], str(good))
    check("двое ушли в единичные случаи", len(singles) == 2, str(singles))
    check("названо, чего не хватило",
          all("нужно 3" in s for s in singles), str(singles))
    check("безымянная и мусор отброшены молча",
          not any("безымянная" in str(s) for s in singles), str(singles))

    # ── 3. сводка собирается целиком ──────────────────────────────────
    print("\n3. Сводка собирается")
    fake_fetch({"https://t.me/s/own": channel([10, 20, 30, 40, 50])})
    install(answer())
    reg.clear()
    await research.run(reg, CHAT, "дай сводку")

    week = research.Digest(stats=research.Stats()).week or ""
    files = sorted((harness.TMP / "brands" / "lily-space" / "research").glob("*.md"))
    check("файл сводки записан", len(files) == 1, str(files))
    if files:
        text = files[0].read_text(encoding="utf-8")
        check("в сводке своя статистика", "медиана просмотров: 30" in text,
              text[:200])
        check("в сводке механика", "Разбор своей работы" in text)
        check("в сводке что прочитано", "Что прочитано" in text)

    card = reg.last()
    check("карточка от Ресёрчера", card.role == "research", card.role)
    check("в карточке медиана", "медиана 30" in card.text, card.text[:150])
    check("в карточке путь к файлу", "research/" in card.text, card.text[-80:])

    # ── 4. цифры уехали в промпт готовыми ─────────────────────────────
    print("\n4. Модель получает цифры, а не считает их")
    p = CALLS["prompts"][-1]
    check("медиана в промпте", "медиана просмотров: 30" in p, p[:300])
    check("верх и низ в промпте",
          "Выше медианы" in p and "Ниже медианы" in p)
    check("сказано, что списка чужих нет",
          "Списка источников нет" in p, p[-400:])

    # ── 5. мало данных — роль предупреждена ───────────────────────────
    print("\n5. Мало данных")
    fake_fetch({"https://t.me/s/own": channel([10, 20])})
    install(answer())
    reg.clear()
    await research.run(reg, CHAT, "дай сводку")
    check("предупреждение уехало в промпт",
          "мало для вывода" in CALLS["prompts"][-1], CALLS["prompts"][-1][:400])
    check("и человеку сказано", "для выводов мало" in reg.texts(),
          reg.texts()[:200])

    # ── 6. единичные случаи видны, но не выданы за механики ───────────
    print("\n6. Единичный случай не становится механикой")
    fake_fetch({"https://t.me/s/own": channel([10, 20, 30, 40, 50])})
    install(answer(mechanics=[
        {"name": "Слабый вывод", "what": "один случай", "examples": ["a"]},
    ]))
    reg.clear()
    await research.run(reg, CHAT, "дай сводку")
    check("в карточке нет ложной механики",
          "Механики недели" not in reg.last().text, reg.last().text[:200])
    check("единичный случай посчитан",
          "Единичных случаев: 1" in reg.last().text, reg.last().text[-200:])

    # ── 7. список чужих каналов ───────────────────────────────────────
    print("\n7. Список чужих каналов")
    b.artifact(research.WATCHLIST,
               "# За кем следим\n\n- @peer — сосед по нише\n"
               "- [сюда добавить] — шаблон, не источник\n")
    check("список вычитан", research.watchlist(b) == ["@peer"],
          str(research.watchlist(b)))

    peer = channel([100, 200], title="Сосед")
    fake_fetch({"https://t.me/s/own": channel([10, 20, 30, 40, 50]),
                "https://t.me/s/peer": peer})
    install(answer())
    reg.clear()
    await research.run(reg, CHAT, "дай сводку")
    check("чужой канал прочитан", "Сосед" in CALLS["prompts"][-1],
          CALLS["prompts"][-1][-300:])
    check("жалобы на отсутствие списка больше нет",
          "Списка источников нет" not in CALLS["prompts"][-1])

    # ── 8. канал не открылся ──────────────────────────────────────────
    print("\n8. Канал не открылся")
    fake_fetch({})
    install(answer())
    reg.clear()
    await research.run(reg, CHAT, "дай сводку")
    check("сказано, что не открылось", "не открылось" in reg.texts(),
          reg.texts()[:200])
    check("статистики не выдумано", "медиана" not in reg.last().text.lower(),
          reg.last().text[:200])

    # ── 9. читать нечего вовсе ────────────────────────────────────────
    print("\n9. Читать нечего")
    object.__setattr__(cfg, "publish_channel", "")
    b.path(research.WATCHLIST).unlink()
    install(answer())
    reg.clear()
    await research.run(reg, CHAT, "дай сводку")
    check("честно отказался", "Читать нечего" in reg.texts(), reg.texts()[:150])
    check("объяснил, где взять источники",
          "PUBLISH_CHANNEL" in reg.texts() and research.WATCHLIST in reg.texts(),
          reg.texts()[:300])
    check("модель не звалась", CALLS["n"] == 0, str(CALLS["n"]))

    # ── 9б. модель молчит, цифры остаются ─────────────────────────────
    # Статистика посчитана кодом и от баланса API не зависит. Отдать
    # пустоту, имея верные цифры на руках, — это молчание вместо ответа.
    print("\n9б. Модель не ответила, статистика всё равно есть")
    object.__setattr__(cfg, "publish_channel", "@own")
    fake_fetch({"https://t.me/s/own": channel([10, 20, 30, 40, 50])})

    async def broken(role, chat_id, prompt, **kw):
        raise RuntimeError("на аккаунте API закончились средства")
    agent.ask = broken
    reg.clear()
    await research.run(reg, CHAT, "дай сводку")
    check("сводка всё равно пришла", "медиана 30" in reg.last().text,
          reg.last().text[:200])
    check("причина названа", "средства" in reg.last().text,
          reg.last().text[-200:])
    check("файл записан", (harness.TMP / "brands" / "lily-space" /
                           "research").glob("*.md") is not None)

    # ── 10. Стратег видит сводку ──────────────────────────────────────
    print("\n10. Сводка доезжает до Стратега")
    from orchestrator import strategy
    week, digest = research.latest(b)
    check("последняя сводка находится", bool(digest), "сводки нет")
    layers = strategy._layers(CHAT, {}, [("2026-08-20", "telegram")], "план")
    check("дайджест попал в слои", "Сводка Ресёрчера" in layers,
          layers[:400])
    check("старой фразы про «не подключён» нет",
          "не подключён" not in layers, layers[:400])

    # ── 11. фактура под тему ──────────────────────────────────────────
    # Второй продукт роли: не сводка недели, а чем подпереть один текст.
    # Правило жёстче, чем в сводке: только из показанных постов.
    print("\n11. Фактура под тему")

    b.artifact(research.WATCHLIST, "- @sio — разборы релизов\n")
    fake_fetch({"https://t.me/s/sio": channel([100, 200], title="Сиолошная")})
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                  "status, title) VALUES (?,?,?,?,?,'idea',?)",
                  ("2026-09-10-telegram-01", CHAT, "2026-09-10", "telegram",
                   "пост", "Роль настроена, а текст не мой"))

    facts_answer = json.dumps({
        "facts": [{"claim": "Отчёт про агентов вышел", "source": "@sio",
                   "date": "2026-09-02", "useful": "цифра под первый абзац"}],
        "gaps": ["цен в постах не было"],
    }, ensure_ascii=False)
    install(facts_answer)
    reg.clear()
    await research.run(reg, CHAT,
                       "собери фактуру по теме 2026-09-10-telegram-01")

    check("ушёл в фактуру, а не в сводку",
          "Фактура под тему" in reg.texts(), reg.texts()[:200])
    check("сводку недели не собирал",
          "медиана" not in reg.texts().lower(), reg.texts()[:200])
    rel = research.FACTS_FILE.format(id="2026-09-10-telegram-01")
    saved = b.read(rel)
    check("файл фактуры записан", bool(saved.strip()), rel)
    check("факт в файле", "Отчёт про агентов" in saved, saved[:200])
    check("источник и дата в файле", "@sio" in saved and "2026-09-02" in saved,
          saved[:300])
    check("дыра названа", "цен в постах не было" in saved, saved[:400])
    check("Редактор эту фактуру видит",
          "Отчёт про агентов" in research.facts_for(b, "2026-09-10-telegram-01"))
    check("по другой теме фактуры нет",
          research.facts_for(b, "2026-09-11-telegram-01") == "")

    prompt = CALLS["prompts"][-1]
    check("посты источника уехали в промпт", "Пост номер 1" in prompt,
          prompt[:300])
    check("запрет памяти в промпте", "по памяти" in prompt, prompt[-500:])

    # Пустой ответ модели это результат, а не поломка.
    install(json.dumps({"facts": [], "gaps": []}, ensure_ascii=False))
    reg.clear()
    await research.run(reg, CHAT,
                       "собери фактуру по теме 2026-09-10-telegram-01")
    check("пустая фактура названа словами",
          "нет" in reg.texts().lower(), reg.texts()[:200])
    check("выдуманного факта нет",
          "Отчёт про агентов" not in reg.texts(), reg.texts()[:200])


    # ── внешние источники: фид разбирается постами ────────────────────
    print("\n7. Внешний источник: RSS и Atom")

    rss = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Marketing AI</title>
  <item>
    <title>Six questions for marketing leaders</title>
    <description>&lt;p&gt;Organizations are eager to dive in&lt;/p&gt;</description>
    <pubDate>Wed, 26 Aug 2026 09:00:00 +0000</pubDate>
  </item>
  <item>
    <title><![CDATA[Старая новость]]></title>
    <description>Была давно</description>
    <pubDate>Mon, 03 Mar 2026 09:00:00 +0000</pubDate>
  </item>
</channel></rss>"""

    src = sources.Source(url="https://example.org/rss", kind="website")
    sources._read_feed(src, rss, 20)
    check("фид разобран записями", src.ok and len(src.posts) == 2,
          f"{src.ok} {len(src.posts)}")
    check("вид источника — фид", src.kind == "feed", src.kind)
    check("название фида, а не первой записи", src.title == "Marketing AI",
          src.title)
    check("заголовок записи в тексте",
          src.posts[0].text.startswith("Six questions"), src.posts[0].text[:60])
    check("разметка снята, а не показана",
          "<p>" not in src.posts[0].text and "&lt;" not in src.posts[0].text,
          src.posts[0].text[:80])
    check("CDATA снята", src.posts[1].text.startswith("Старая новость"),
          src.posts[1].text[:40])
    check("дата RFC 822 разобрана",
          src.posts[0].date is not None
          and src.posts[0].date.date().isoformat() == "2026-08-26",
          str(src.posts[0].date))

    atom = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Weblog</title>
  <entry>
    <title>Разбор релиза</title>
    <summary>Коротко о том, что поменялось</summary>
    <updated>2026-08-27T08:42:49Z</updated>
  </entry>
</feed>"""
    a = sources.Source(url="https://example.org/atom", kind="website")
    sources._read_feed(a, atom, 20)
    check("Atom разобран", a.ok and len(a.posts) == 1, f"{a.ok} {len(a.posts)}")
    check("дата ISO 8601 разобрана",
          a.posts[0].date is not None
          and a.posts[0].date.date().isoformat() == "2026-08-27",
          str(a.posts[0].date))

    # Окно режет фид тем же кодом, что и ленту канала.
    window = research.Window(date(2026, 8, 24), date(2026, 8, 30), "2026-W35")
    fst = research.measure(src, window=window)
    check("окно оставило только запись недели", fst.posts == 1, str(fst.posts))
    check("старое посчитано вне окна", fst.outside == 1, str(fst.outside))
    check("просмотров у фида нет", fst.with_views == 0, str(fst.with_views))

    # Дата не разобралась — прочерк, а не сегодня: иначе прошлогодняя
    # запись заедет в окно недели.
    broken = sources.Source(url="https://example.org/bad", kind="website")
    sources._read_feed(broken, rss.replace("Wed, 26 Aug 2026 09:00:00 +0000",
                                           "позавчера"), 20)
    check("кривая дата — прочерк", broken.posts[0].date is None,
          str(broken.posts[0].date))

    # HTML под видом фида это не фид: cookie-стена отдаёт двести и страницу.
    wall = sources.Source(url="https://example.org/wall", kind="website")
    sources._read_feed(wall, "<html><body>Skip to content</body></html>", 20)
    check("страница фидом не притворится", not wall.ok, str(wall.ok))
    check("причина названа", "записей" in wall.error, wall.error)

    check("фид узнаётся по телу", bool(sources.FEED_MARK.search(rss)))
    check("страница фидом не считается",
          not sources.FEED_MARK.search("<html><head><title>x"))


asyncio.run(main())
raise SystemExit(report())
