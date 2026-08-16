"""Ресёрчер: недельная сводка и своя статистика.

Две части, и они принципиально разные.

**Своя статистика считается кодом.** Медиана просмотров, что зашло выше
неё, что просело — это арифметика, а не суждение. Она работает при пустом
балансе API и не врёт при пустом кошельке.

**Наблюдения и механики даёт модель**, но границу держит код: правило
трёх (приём становится выводом с трёх независимых примеров, иначе это
«единичный случай») проверяется здесь, а не доверяется промпту.

Источник данных — публичная веб-версия `t.me/s/<канал>`: тексты постов и
счётчики просмотров без токенов и авторизации. Свой канал берётся из
`PUBLISH_CHANNEL`, чужие — из `research/sources.md` папки бренда.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Any

from config import cfg
from orchestrator import agent, desk, sources

log = logging.getLogger("research")

MAX_TOKENS = 12000
POSTS_LIMIT = 40            # сколько постов читаем с канала
RULE_OF_THREE = 3           # с скольких примеров приём становится выводом
TOP = 3                     # сколько постов показываем сверху и снизу
WATCHLIST = "research/sources.md"


class NoData(RuntimeError):
    """Читать нечего: ни своего канала, ни списка источников."""


@dataclass
class Stats:
    """Своя статистика. Считается кодом, модели не доверяется."""
    channel: str = ""
    title: str = ""
    subscribers: str = ""
    posts: int = 0
    with_views: int = 0
    median: int = 0
    best: list[tuple[int, str]] = field(default_factory=list)
    worst: list[tuple[int, str]] = field(default_factory=list)

    @property
    def enough(self) -> bool:
        """Хватает ли данных, чтобы вообще говорить о «зашло — не зашло».

        На трёх постах медиана это не статистика, а совпадение.
        """
        return self.with_views >= 5


@dataclass
class Digest:
    stats: Stats
    watched: list[str] = field(default_factory=list)     # что прочиталось
    failed: list[str] = field(default_factory=list)      # что не открылось
    facts: list[str] = field(default_factory=list)
    mechanics: list[dict[str, Any]] = field(default_factory=list)
    singles: list[str] = field(default_factory=list)     # не прошли правило трёх
    gaps: list[str] = field(default_factory=list)        # чего не удалось собрать
    week: str = ""


# ── своя статистика ───────────────────────────────────────────────────

def _cut(text: str, n: int = 60) -> str:
    line = " ".join(text.split())
    return line[:n] + ("…" if len(line) > n else "")


def measure(src: sources.Source) -> Stats:
    """Что видно по числам. Без интерпретаций — их даёт модель."""
    st = Stats(channel=src.url, title=src.title, subscribers=src.subscribers)
    seen = [(p.views, p.text) for p in src.posts if p.views is not None]
    st.posts = len(src.posts)
    st.with_views = len(seen)
    if not seen:
        return st

    st.median = int(median(v for v, _ in seen))
    ranked = sorted(seen, key=lambda x: x[0], reverse=True)
    st.best = [(v, _cut(t)) for v, t in ranked[:TOP]]
    st.worst = [(v, _cut(t)) for v, t in ranked[-TOP:]][::-1]
    return st


def watchlist(b) -> list[str]:
    """Чужие каналы, за которыми следим. Строки вида `- @имя — зачем`."""
    out = []
    for line in b.read(WATCHLIST).splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        item = line[2:].split("—")[0].split(" — ")[0].strip()
        if item and not item.startswith("["):
            out.append(item)
    return out


# ── правило трёх ──────────────────────────────────────────────────────

def sift(raw: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Развести механики и единичные случаи.

    Промпт просит правило трёх, но промпт это просьба. Модель приносила
    вывод с одним примером и называла его механикой — а по такому выводу
    Стратег строит неделю.
    """
    good: list[dict[str, Any]] = []
    singles: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        examples = [str(e) for e in (item.get("examples") or []) if str(e).strip()]
        if len(examples) >= RULE_OF_THREE:
            good.append({"name": name,
                         "what": str(item.get("what") or "").strip(),
                         "examples": examples})
        else:
            singles.append(f"{name} — примеров {len(examples)}, "
                           f"нужно {RULE_OF_THREE}")
    return good, singles


# ── сборка ────────────────────────────────────────────────────────────

def _brief(st: Stats, others: list[sources.Source]) -> str:
    lines = ["## Свой канал", ""]
    if st.with_views:
        lines += [f"- {st.title or st.channel}, {st.subscribers or 'подписчики неизвестны'}",
                  f"- прочитано постов: {st.posts}, с просмотрами: {st.with_views}",
                  f"- медиана просмотров: {st.median}", "",
                  "Выше медианы:"]
        lines += [f"- {v} просм. · {t}" for v, t in st.best]
        lines += ["", "Ниже медианы:"]
        lines += [f"- {v} просм. · {t}" for v, t in st.worst]
        if not st.enough:
            lines += ["", f"**Постов с просмотрами всего {st.with_views}.** "
                      "Это мало для вывода: скажи об этом и не выдавай "
                      "совпадение за закономерность."]
    else:
        lines.append("- просмотров не видно, статистики нет")

    lines += ["", "## Чужие каналы", ""]
    if not others:
        lines.append("Списка источников нет, внешнего среза на этой неделе "
                     "не будет. Так и скажи в разделе «чего не удалось "
                     "собрать», выводы из ничего не строй.")
    for s in others:
        if not s.ok:
            continue
        lines += ["", f"### {s.title or s.url}", s.summary(), ""]
        for p in s.posts[:12]:
            head = f"[{p.views} просм.] " if p.views is not None else ""
            lines.append(f"- {head}{_cut(p.text, 200)}")
    return "\n".join(lines)


async def build(chat_id: int, ask: str, *, say=None) -> Digest:
    b = desk.brand(chat_id)
    if b is None:
        raise NoData("профиль бренда ещё не собран")

    own = cfg.publish_channel
    others_urls = watchlist(b)
    if not own and not others_urls:
        raise NoData("ни своего канала, ни списка источников")

    if say:
        what = []
        if own:
            what.append("свой канал")
        if others_urls:
            what.append(f"{len(others_urls)} чужих")
        await say(f"Читаю {' и '.join(what)}. Это займёт до минуты.")

    st = Stats()
    dg = Digest(stats=st, week=f"{date.today():%G-W%V}")

    if own:
        src = await sources.fetch(own, limit=POSTS_LIMIT)
        if src.ok:
            st = measure(src)
            dg.stats = st
            dg.watched.append(src.summary())
        else:
            dg.failed.append(f"свой канал {own}: {src.error}")

    others = await sources.fetch_all(others_urls, limit=20) if others_urls else []
    for s in others:
        (dg.watched if s.ok else dg.failed).append(s.summary())
    if not others_urls:
        dg.gaps.append("списка чужих источников нет: "
                       f"заведи `{WATCHLIST}` в папке бренда")

    if say:
        await say("Цифры снял, собираю наблюдения.")

    # Цифры уже есть, и они не зависят от модели. Если вызов не прошёл —
    # кончились средства, упала сеть — отдаём статистику и честно говорим,
    # что наблюдений не будет. Молчание тут хуже неполной сводки.
    try:
        answer = await agent.ask(
            "research", chat_id,
            _brief(st, others) +
            "\n\nСобери недельную сводку. Ответь одним JSON-объектом в "
            "формате из секции «Недельный ресёрч».",
            brand_name=b.name(),
            profile=desk.profile(b, ("Кто это", "Аудитория")),
            max_tokens=MAX_TOKENS)
    except Exception as e:                                   # noqa: BLE001
        log.warning("наблюдения не собрались: %s", desk.reason(e))
        dg.gaps.append(f"наблюдений и механик нет: {desk.reason(e)}. "
                       "Цифры выше сняты кодом и верны")
        return dg

    data = agent.parse_json(answer, who="ресёрчер")
    dg.facts = [str(f) for f in (data.get("facts") or []) if str(f).strip()]
    dg.mechanics, dg.singles = sift(data.get("mechanics") or [])
    dg.gaps += [str(g) for g in (data.get("gaps") or []) if str(g).strip()]

    log.info("сводка %s: фактов %s, механик %s, единичных %s",
             dg.week, len(dg.facts), len(dg.mechanics), len(dg.singles))
    return dg


# ── выгрузка и карточка ───────────────────────────────────────────────

def to_markdown(dg: Digest) -> str:
    st = dg.stats
    out = [f"# Сводка недели {dg.week}", ""]

    out += ["## Своя статистика", ""]
    if st.with_views:
        out += [f"- канал: {st.title or st.channel}",
                f"- подписчиков: {st.subscribers or '[уточнить факт]'}",
                f"- постов прочитано: {st.posts}, с просмотрами: {st.with_views}",
                f"- медиана просмотров: {st.median}", ""]
        if not st.enough:
            out += [f"⚠️ Постов с просмотрами {st.with_views}: мало для вывода.",
                    ""]
        out += ["### Выше медианы", ""]
        out += [f"- **{v}** · {t}" for v, t in st.best]
        out += ["", "### Ниже медианы", ""]
        out += [f"- **{v}** · {t}" for v, t in st.worst]
    else:
        out.append("Просмотров не видно, статистики нет.")

    if dg.facts:
        out += ["", "## Наблюдения", ""] + [f"- {f}" for f in dg.facts]
    if dg.mechanics:
        out += ["", "## Механики недели", ""]
        for m in dg.mechanics:
            out += [f"### {m['name']}", "", m["what"], "",
                    "Примеры:"] + [f"- {e}" for e in m["examples"]] + [""]
    if dg.singles:
        out += ["", "## Единичные случаи", "",
                "Не прошли правило трёх, выводом не считаются.", ""]
        out += [f"- {s}" for s in dg.singles]
    if dg.watched:
        out += ["", "## Что прочитано", ""] + [f"- {w}" for w in dg.watched]
    if dg.gaps or dg.failed:
        out += ["", "## Чего не удалось собрать", ""]
        out += [f"- {g}" for g in dg.gaps + dg.failed]
    return "\n".join(out) + "\n"


def card(dg: Digest) -> str:
    st = dg.stats
    out = [f"🔍 <b>Сводка недели {dg.week}</b>"]
    if st.with_views:
        out.append(f"{st.posts} постов · медиана {st.median} просм."
                   + (f" · {st.subscribers}" if st.subscribers else ""))
        if not st.enough:
            out.append(f"⚠️ с просмотрами всего {st.with_views}, "
                       "для выводов мало")
        out += ["", "<b>Выше медианы</b>"]
        out += [f"· {v} — {t}" for v, t in st.best]
    else:
        out.append("Просмотров не видно, статистики нет.")

    if dg.facts:
        out += ["", "<b>Наблюдения</b>"] + [f"· {f}" for f in dg.facts[:4]]
    if dg.mechanics:
        out += ["", "<b>Механики недели</b>"]
        out += [f"· {m['name']}" for m in dg.mechanics[:4]]
    if dg.singles:
        out += ["", f"Единичных случаев: {len(dg.singles)}, "
                    "в механики не пошли."]
    if dg.gaps or dg.failed:
        out += ["", "⚠️ " + "; ".join((dg.gaps + dg.failed)[:3])]
    return "\n".join(out)


async def run(reg, chat_id: int, ask: str, topic: str = "research") -> None:
    async def say(text: str) -> None:
        await reg.say("research", chat_id, text, topic=topic)

    try:
        dg = await build(chat_id, ask, say=say)
    except NoData as e:
        await say(f"Читать нечего: {e}.\n\nСвой канал беру из "
                  f"<code>PUBLISH_CHANNEL</code>, чужие — из "
                  f"<code>{WATCHLIST}</code> папки бренда: строка вида "
                  "«- @канал — зачем смотрим».")
        return
    except agent.BudgetExceeded as e:
        await say(f"Остановился: {e}")
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("сводка не собралась")
        await say(f"Сводка не собралась: {desk.reason(e)}")
        return

    b = desk.brand(chat_id)
    rel = f"research/{dg.week}.md"
    b.artifact(rel, to_markdown(dg))
    await say(card(dg) + f"\n\nЦеликом: <code>{rel}</code>")


def latest(b) -> tuple[str, str]:
    """Последняя сводка: (неделя, текст). Пусто — сводок ещё нет."""
    folder = b.path("research")
    files = sorted(folder.glob("*.md")) if folder.is_dir() else []
    if not files:
        return "", ""
    return files[-1].stem, files[-1].read_text(encoding="utf-8")
