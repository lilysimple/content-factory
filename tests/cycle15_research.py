"""Цикл 15: Ресёрчер — своя статистика и правило трёх.

Две границы, ради которых роль вообще собрана.

Цифры считает код: медиану и «что зашло» модель не выдумывает, а получает
готовыми. Правило трёх тоже держит код: приём с двумя примерами это
единичный случай, как бы уверенно роль его ни назвала механикой.
"""
from __future__ import annotations

import asyncio
import json

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


asyncio.run(main())
raise SystemExit(report())
