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

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from config import cfg
from orchestrator import agent, desk, sources
from storage import brand as brand_store

log = logging.getLogger("research")

MAX_TOKENS = 12000
POSTS_LIMIT = 40            # сколько постов читаем с канала
RULE_OF_THREE = 3           # с скольких примеров приём становится выводом
TOP = 3                     # сколько постов показываем сверху и снизу
WATCHLIST = "research/sources.md"

# Недельная сводка называется по неделе. Проверка нужна, потому что в той
# же папке лежит выжимка профиля, а `latest` берёт последний файл по имени:
# без фильтра Стратег получил бы вместо дайджеста выжимку.
WEEK_FILE = re.compile(r"^\d{4}-W\d{2}$")


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

    window: str = ""             # окно, по которому считано; пусто — вся отдача
    outside: int = 0            # постов вне окна
    undated: int = 0            # постов без даты
    covered: bool = True        # лента дотянулась до начала окна

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
    """Заголовок поста одной строкой. Переводы строк съедаются намеренно."""
    line = " ".join(text.split())
    return line[:n] + ("…" if len(line) > n else "")


@dataclass(frozen=True)
class Window:
    """Окно, по которому считается сводка.

    Заведено потому, что без него медиана считалась по всей отдаче ленты.
    На живых источниках это окна до года шириной: у @addmeto лента отдала
    посты с ноября по март, и его медиана 49 100 уехала в сводку 31.08 как
    ориентир недели. Числа были верные, подпись «повестка недели» — нет.
    """
    start: date
    end: date
    name: str                        # ГГГГ-Wnn той недели, что покрываем

    def __str__(self) -> str:
        return f"{self.name} ({self.start} — {self.end})"

    def holds(self, when: datetime | None) -> bool:
        if when is None:
            return False
        d = when.date()
        return self.start <= d <= self.end

    def split(self, posts: list[sources.Post]) -> tuple[list, int, int]:
        """Посты внутри окна, сколько вне и сколько без даты."""
        inside = [p for p in posts if self.holds(p.date)]
        undated = sum(1 for p in posts if p.date is None)
        return inside, len(posts) - len(inside) - undated, undated

    def reaches(self, posts: list[sources.Post]) -> bool:
        """Дотянулась ли лента до начала окна.

        Страница `t.me/s/` отдаёт последние двадцать постов и не больше.
        У активного канала это меньше недели, и тогда срез неполон — про
        это надо сказать, а не молча посчитать по тому, что доехало.
        """
        dated = [p.date.date() for p in posts if p.date]
        return bool(dated) and min(dated) <= self.start


def last_week(today: date | None = None) -> Window:
    """Прошлая ISO-неделя: понедельник по воскресенье.

    Сводка гоняется в понедельник и покрывает неделю, которая кончилась, а
    не ту, что началась вчера. Имя окна — неделя, которую покрываем, чтобы
    файл `research/ГГГГ-Wnn.md` назывался по своему содержимому.
    """
    today = today or date.today()
    monday = today - timedelta(days=today.weekday() + 7)
    sunday = monday + timedelta(days=6)
    return Window(monday, sunday, f"{monday:%G-W%V}")


def measure(src: sources.Source, *, window: Window | None = None) -> Stats:
    """Что видно по числам. Без интерпретаций — их даёт модель."""
    st = Stats(channel=src.url, title=src.title, subscribers=src.subscribers)
    posts = src.posts
    if window is not None:
        posts, st.outside, st.undated = window.split(posts)
        st.window = str(window)
        st.covered = window.reaches(src.posts)
    seen = [(p.views, p.text) for p in posts if p.views is not None]
    st.posts = len(posts)
    st.with_views = len(seen)
    if not seen:
        return st

    st.median = int(median(v for v, _ in seen))
    ranked = sorted(seen, key=lambda x: x[0], reverse=True)
    st.best = [(v, _cut(t)) for v, t in ranked[:TOP]]
    st.worst = [(v, _cut(t)) for v, t in ranked[-TOP:]][::-1]
    return st


SNAP_TOP = 3                # сколько постов сверху и снизу на канал
SNAP_CUT = 120              # знаков от текста поста
STATS_DIR = "research/stats"

# Что кабинет площадки отдаёт человеку: картинку или выгрузку таблицей.
SHOT_SUFFIX = (".png", ".jpg", ".jpeg", ".webp")
TABLE_SUFFIX = (".csv", ".tsv", ".xlsx", ".json", ".txt", ".md")


def stats_dir(b) -> Path:
    d = b.path(STATS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def stashed(b) -> tuple[list[str], list[str]]:
    """Что лежит в папке статистики: скрины и выгрузки, по именам."""
    d = b.path(STATS_DIR)
    if not d.is_dir():
        return [], []
    files = sorted(f for f in d.iterdir()
                   if f.is_file() and f.name.lower() != "readme.md")
    shots = [f.name for f in files if f.suffix.lower() in SHOT_SUFFIX]
    tables = [f.name for f in files if f.suffix.lower() in TABLE_SUFFIX]
    return shots, tables


def stash_stats(b, blob: bytes, name: str) -> Path:
    """Положить присланный скрин или выгрузку в папку статистики бренда.

    Имя не затирается: два скрина одного кабинета за разные недели
    приезжают из Telegram под одним и тем же `IMG_1234.PNG`, и молчаливая
    перезапись стоила бы человеку прошлого замера.
    """
    d = stats_dir(b)
    # Имя приходит из Telegram: брать его как путь значит пустить `../`
    # в чужую папку. Нужен только базовый компонент.
    name = Path(name).name or "stats.bin"
    path = d / name
    n = 2
    while path.exists():
        path = d / f"{Path(name).stem}-{n}{Path(name).suffix}"
        n += 1
    path.write_bytes(blob)
    log.info("статистика принята: %s (%s байт)", path, len(blob))
    return path


def _line(p: sources.Post) -> str:
    seen = f"{p.views}" if p.views is not None else "—"
    when = f"{p.date:%d.%m}" if p.date else "дата?"
    return f"- {seen} просм. · {when} · {_cut(p.text, SNAP_CUT)}"


async def snapshot(b, *, window: Window | None = None) -> tuple[str, list[str]]:
    """Цифры за окно: свой канал, соседи, скрины. Текст плюс список дыр.

    Заведено потому, что Ресёрчеру цифры не кладли вовсе, и он добывал их
    сам: искал `sources.fetch` в коде, писал скрипт, гонял, правил. Восемь
    ходов модели на работу, которая занимает три с половиной секунды сети.
    Каждый ход перечитывает накопленный контекст, поэтому платили не за
    сеть, а за разведку.

    Считается тем же кодом, что у старого Ресёрчера: `measure` для чисел,
    `last_week` для окна. Второго дома у арифметики не появляется.

    Сеть падает — это дыра в списке, а не отказ задачи: сводка на части
    источников честнее молчания.
    """
    window = window or last_week()
    gaps: list[str] = []
    out = [f"Окно: {window}. Посты вне него не считаются.", ""]

    own_url = cfg.publish_channel
    if own_url:
        try:
            own = await sources.fetch(own_url, limit=POSTS_LIMIT)
        except Exception as e:                   # noqa: BLE001
            own = None
            gaps.append(f"свой канал {own_url} не открылся: {type(e).__name__}")
        if own is not None and own.ok:
            st = measure(own, window=window)
            out += ["## Свой канал", "",
                    f"- {st.title or own_url}"
                    + (f", {st.subscribers}" if st.subscribers else ""),
                    f"- постов в окне: {st.posts}, с просмотрами: "
                    f"{st.with_views}, вне окна: {st.outside}",
                    f"- медиана просмотров: {st.median}", ""]
            if st.best:
                out += ["Выше медианы:"] + [f"- {v} просм. · {t}"
                                            for v, t in st.best] + [""]
            if st.worst:
                out += ["Ниже медианы:"] + [f"- {v} просм. · {t}"
                                            for v, t in st.worst] + [""]
            if not st.enough:
                gaps.append(f"постов с просмотрами в окне всего {st.with_views}: "
                            "медиана это совпадение, а не статистика")
            if not st.covered:
                gaps.append("лента своего канала не дотянулась до начала окна: "
                            "срез неполон")
        elif own is not None:
            gaps.append(f"свой канал {own_url}: {own.error}")
    else:
        gaps.append("своего канала в настройках нет: PUBLISH_CHANNEL пуст")

    urls = watchlist(b)
    if not urls:
        gaps.append(f"списка чужих источников нет: заведи `{WATCHLIST}` "
                    "в папке бренда")
    else:
        try:
            others = await sources.fetch_all(urls, limit=20)
        except Exception as e:                   # noqa: BLE001
            others = []
            gaps.append(f"чужие источники не открылись: {type(e).__name__}")

        rows, blocks = [], []
        for src in others:
            if not src.ok:
                gaps.append(f"{src.url}: {src.error}")
                continue
            st = measure(src, window=window)
            name = src.title or src.url
            subs = src.subscribers.split()[0] if src.subscribers else "—"
            # Труба в названии канала рвёт таблицу целиком: у «Вайб-кодинг
            # по Чуйкову | Ментор» строка разъезжается на шесть колонок.
            cell = name.replace("|", "/")
            rows.append(f"| {cell} | {subs} | {st.posts} | "
                        f"{st.median or '—'} | {'да' if st.covered else 'нет'} |")
            if not st.covered:
                gaps.append(f"{name}: лента не дотянулась до начала окна, "
                            "срез по каналу неполон")
            inside, _, _ = window.split(src.posts)
            if not inside:
                gaps.append(f"{name}: в окне нет ни одного поста, лента отдала "
                            "только более старые — сравнивать нечего")
                continue
            rank = sorted(inside, key=lambda p: p.views or 0, reverse=True)
            blocks += [f"### {name}", ""]
            blocks += [_line(p) for p in rank[:SNAP_TOP]]
            if len(rank) > SNAP_TOP * 2:
                blocks.append("- …")
            if len(rank) > SNAP_TOP:
                blocks += [_line(p) for p in rank[-SNAP_TOP:]]
            blocks.append("")

        if rows:
            out += ["## Внешний срез", "",
                    "| Канал | Подписчиков | Постов в окне | Медиана | "
                    "Окно покрыто |",
                    "|---|---|---|---|---|"] + rows + [""]
            out += ["Верх и низ каждого канала внутри окна:", ""] + blocks

    shots, tables = stashed(b)
    out += ["## Скрины статистики", ""]
    if shots or tables:
        out += [f"Лежат в `{STATS_DIR}` папки бренда, читай их сам:", ""]
        out += [f"- {n}" for n in shots + tables]
        out += ["", "Они дополняют цифры выше: охват, ER, подписки, досмотры "
                "лента не отдаёт. Разобранное дописывай строками в "
                "`stats.csv`, поле без данных оставляй прочерком."]
    else:
        out.append(f"Папка `{STATS_DIR}` пуста: охвата, ER и досмотров нет, "
                   "в срезе только просмотры.")
        gaps.append("скринов статистики нет: реакций, охвата и досмотров в "
                    "сводке не будет")

    return "\n".join(out), gaps


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


# Просьба про фактуру уходит в другую работу, а не в недельную сводку.
# Роль одна, продукта два, и разводить их топиком нельзя: оба живут в
# 🔍 Ресёрче.
FACTS_ASK = re.compile(r"фактур|чем подкрепить|подперет|подпереть", re.I)


async def run_facts(reg, chat_id: int, ask: str,
                    topic: str = "research") -> None:
    """Собрать фактуру под названную тему и положить рядом с темой."""
    async def say(text: str) -> None:
        await reg.say("research", chat_id, text, topic=topic)

    try:
        theme = desk.pick(
            chat_id, ask, statuses=("idea", "draft"),
            none="темы {id} нет среди начатых и неначатых",
            empty="тем, под которые нужна фактура, сейчас нет")
    except desk.NoWork as e:
        await say(f"Не понял, под какую тему: {e}. Назови id темы.")
        return

    try:
        fx = await facts(chat_id, theme, say=say)
    except NoData as e:
        await say(f"Искать негде: {e}.")
        return
    except agent.BudgetExceeded as e:
        await say(f"Остановился: {e}")
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("фактура не собралась")
        await say(f"Фактура не собралась: {desk.reason(e)}")
        return

    b = desk.brand(chat_id)
    rel = FACTS_FILE.format(id=fx.theme_id)
    b.artifact(rel, facts_markdown(fx))
    await say(facts_card(fx) +
              "\n\nРедактор возьмёт её сам, когда будет писать по этой теме.")


async def run(reg, chat_id: int, ask: str, topic: str = "research") -> None:
    if FACTS_ASK.search(ask or ""):
        await run_facts(reg, chat_id, ask, topic=topic)
        return

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
    files = sorted(f for f in (folder.glob("*.md") if folder.is_dir() else [])
                   if WEEK_FILE.match(f.stem))
    if not files:
        return "", ""
    return files[-1].stem, files[-1].read_text(encoding="utf-8")


# ── фактура под тему ──────────────────────────────────────────────────
#
# Недельная сводка уезжает Стратегу и там кончается: от неё до текста
# доживает одна строка `why` и хук темы. Редактору фактура не доставалась
# вовсе, и это видно в его же `notes` — «цифра взята из хука Стратега,
# по фактическому плану не сверена». Теории в такую щель не пролезает.
#
# Добывать факты сам Редактор не может и не будет: роль идёт одним
# вызовом без инструментов (`--tools ""`, замер в `../CLAUDE.md`), а
# субагенту `writer` поиска не дано. Наружу ходит только Ресёрчер,
# значит фактура — его второй продукт, рядом с недельной сводкой.
#
# Источник тот же, что у сводки: каналы из `research/sources.md`. Список
# заведён под это прямо — «@seeallochnaya, глубокий разбор релизов, чтобы
# сверять факты перед постом». Веб-поиска здесь нет и не нужно: он
# принёс бы то, чего человек не выбирал.
#
# **Ссылки на конкретный пост не будет.** Фетчер держит текст, просмотры
# и дату, id сообщения не хранит. Поэтому источник это «канал плюс дата»,
# и выдумывать permalink нельзя: ложная ссылка хуже её отсутствия.

FACTS_FILE = "research/facts-{id}.md"
FACTS_POSTS = 25            # сколько свежих постов берём с канала под тему
FACTS_MAX = 5               # больше пяти Редактор в один текст не уложит
FACTS_CUT = 400             # знаков поста в промпт: факт живёт в начале

FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim":  {"type": "string"},
                    "source": {"type": "string"},
                    "date":   {"type": "string"},
                    "useful": {"type": "string"},
                },
                "required": ["claim", "source", "date", "useful"],
                "additionalProperties": False,
            },
        },
        "gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["facts", "gaps"],
    "additionalProperties": False,
}


@dataclass
class Facts:
    """Фактура под одну тему: чем подпереть текст и чего не нашлось."""

    theme_id: str
    items: list[dict[str, str]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    watched: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _facts_brief(theme: dict[str, Any], others: list[sources.Source]) -> str:
    lines = ["## Тема, под которую нужна фактура", ""] + desk.brief(theme)
    lines += ["", "## Что прочитано в чужих каналах", ""]
    if not others:
        lines.append("Ничего: список источников пуст или каналы не открылись.")
    for src in others:
        if not src.ok:
            continue
        lines += ["", f"### {src.title or src.url}", ""]
        for post in src.posts[:FACTS_POSTS]:
            when = f"{post.date:%Y-%m-%d}" if post.date else "дата неизвестна"
            lines.append(f"- [{when}] {_cut(post.text, FACTS_CUT)}")
    return "\n".join(lines)


async def facts(chat_id: int, theme: dict[str, Any], *, say=None) -> Facts:
    """Собрать фактуру под тему из прочитанных чужих каналов.

    Правило здесь жёстче, чем в сводке: факт берётся **только** из
    показанных постов. Модель знает про отрасль много и по памяти, но
    её память это не источник — у неё нет ни даты, ни того, кто это
    сказал, а Редактор поставит такую цифру в живой канал. Нечего
    взять — пустой список и строка в `gaps`; пустая фактура честнее
    правдоподобной.
    """
    b = desk.brand(chat_id)
    if b is None:
        raise NoData("профиль бренда ещё не собран")

    urls = watchlist(b)
    if not urls:
        raise NoData(f"списка источников нет: заведи `{WATCHLIST}` в папке "
                     "бренда")

    if say:
        await say(f"Ищу фактуру под тему <b>{theme.get('title') or theme['id']}</b> "
                  f"в {len(urls)} каналах. Это займёт до минуты.")

    fx = Facts(theme_id=theme["id"])
    others = await sources.fetch_all(urls, limit=FACTS_POSTS)
    for src in others:
        (fx.watched if src.ok else fx.failed).append(src.summary())
    if not any(src.ok for src in others):
        fx.gaps.append("ни один источник не открылся, фактуры под тему нет")
        return fx

    answer = await agent.ask(
        "research", chat_id,
        _facts_brief(theme, others) +
        f"\n\nОтбери до {FACTS_MAX} фактов, которыми можно подпереть эту "
        "тему: исследование, релиз, цифра, разбор. Берёшь только то, что "
        "стоит в показанных постах, — по памяти не добавляешь ничего. "
        "У каждого факта: `claim` — что утверждается, `source` — канал, "
        "`date` — дата поста, `useful` — чем это полезно читателю темы. "
        "Подходящего нет — пустой список и строка в `gaps`. "
        "Ответь одним JSON-объектом.",
        brand_name=b.name(),
        profile=desk.profile(b, ("Кто это", "Аудитория")),
        max_tokens=MAX_TOKENS, schema=FACTS_SCHEMA)

    data = agent.parse_json(answer, who="ресёрчер")
    for raw in (data.get("facts") or [])[:FACTS_MAX]:
        if not isinstance(raw, dict) or not str(raw.get("claim") or "").strip():
            continue
        fx.items.append({k: str(raw.get(k) or "").strip()
                         for k in ("claim", "source", "date", "useful")})
    fx.gaps += [str(g) for g in (data.get("gaps") or []) if str(g).strip()]
    if not fx.items and not fx.gaps:
        fx.gaps.append("в прочитанных постах фактуры под эту тему не нашлось")

    log.info("фактура %s: фактов %s, дыр %s",
             fx.theme_id, len(fx.items), len(fx.gaps))
    return fx


def facts_markdown(fx: Facts) -> str:
    out = [f"# Фактура под тему {fx.theme_id}", ""]
    if fx.items:
        for it in fx.items:
            src = " · ".join(x for x in (it["source"], it["date"]) if x)
            out += [f"- **{it['claim']}**",
                    f"  - источник: {src or '[уточнить факт]'}",
                    f"  - чем полезно: {it['useful']}"]
    else:
        out.append("Фактов под эту тему в прочитанных каналах не нашлось.")
    if fx.gaps or fx.failed:
        out += ["", "## Чего не удалось собрать", ""]
        out += [f"- {g}" for g in fx.gaps + fx.failed]
    if fx.watched:
        out += ["", "## Что прочитано", ""] + [f"- {w}" for w in fx.watched]
    return "\n".join(out) + "\n"


def facts_card(fx: Facts) -> str:
    out = [f"🔍 <b>Фактура под тему</b> <code>{fx.theme_id}</code>"]
    if fx.items:
        out.append("")
        for it in fx.items:
            src = " · ".join(x for x in (it["source"], it["date"]) if x)
            out.append(f"· {it['claim']}" + (f" <i>({src})</i>" if src else ""))
    else:
        out += ["", "В прочитанных каналах фактов под эту тему нет."]
    if fx.gaps or fx.failed:
        out += ["", "⚠️ " + "; ".join((fx.gaps + fx.failed)[:3])]
    return "\n".join(out)


def facts_for(b, theme_id: str) -> str:
    """Собранная фактура под тему. Пусто — её не собирали.

    Читает Редактор перед письмом. Отсутствие файла это не поломка и не
    повод отказываться: тексты писались без фактуры и пишутся дальше,
    просто без слоя чужих цифр.
    """
    text = b.read(FACTS_FILE.format(id=theme_id))
    return text.strip() if text and text.strip() else ""


# ── выжимка профиля ───────────────────────────────────────────────────
#
# Стратег строит план по целям и площадкам, но перечитывать холодную часть
# профиля на каждый план незачем: `goals.md` и `platforms.md` меняются раз
# в недели, а план собирается каждую неделю. Выжимку держит Ресёрчер: он
# один ходит в холодную часть профиля и один отвечает за её свежесть.
#
# Свежесть проверяется хешем исходников, а не памятью процесса. Файл правят
# и кнопкой в чате, и руками в редакторе, и `git pull` на другой машине, и
# после перезапуска бот обязан это увидеть. Хеш сошёлся — отдаём готовое,
# разошёлся — пересобираем и говорим об этом вслух.
#
# Собирается кодом, без модели: это нарезка и склейка, а не суждение.
# Значит, выжимка есть и при пустом балансе API.

DIGEST_PATH = "research/profile-digest.md"
DIGEST_FILES = ("goals", "platforms")        # что попадает в тело выжимки
STAMP_FILES = ("core", "goals", "platforms")  # что сторожим хешем

# Какие секции ядра уезжают в индекс. «Цель» ловит раздел «Цель этапа»,
# который дописывает онбординг на O9: без него Стратег планировал неделю,
# не зная, к чему бренд идёт. «Голос» намеренно не входит: его
# читают роли, которые пишут текст, а стоп-слова из него берёт
# `check_voice` детерминированно и прямо из `core.md`. Копия в индексе
# стала бы вторым домом для входа валидатора, а они расходятся.
CORE_SECTIONS = ("Кто это", "Аудитория", "Цель", "Формат")
DIGEST_CUT = 1500                            # знаков с одного файла

# Пропорция воронки из goals.md: «прогрев 50 / продукт 30 / личное 20».
FUNNEL_WORDS = {"warm": "прогрев", "prod": "продукт", "pers": "личн"}
BACKUP_FUNNEL = {"warm": 60, "prod": 20, "pers": 20}

_STAMP = re.compile(r"^<!--\s*stamp:\s*(\S+)", re.M)
_FUNNEL_LINE = re.compile(r"^<!--\s*funnel:\s*(.+?)\s*-->", re.M)
_MISSING_LINE = re.compile(r"^<!--\s*missing:\s*(.*?)\s*-->", re.M)


@dataclass(frozen=True)
class ProfileDigest:
    """Выжимка целей и площадок для Стратега."""
    text: str
    stamp: str
    rebuilt: bool                    # пересобрана сейчас, файлы менялись
    missing: tuple[str, ...] = ()    # каких файлов профиля ещё нет
    funnel: dict[str, int] = field(default_factory=lambda: dict(BACKUP_FUNNEL))
    backup: bool = True              # пропорция запасная, а не из профиля

    @property
    def ratio(self) -> str:
        return "/".join(str(self.funnel[k]) for k in ("warm", "prod", "pers"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _stamp(b) -> str:
    """Отпечаток файлов, на которых стоит план.

    Считается по `stat()`, а не по содержимому: проверка кэша не должна
    читать то, ради чего кэш и заведён. Прошлая версия вычитывала все
    файлы профиля целиком — на `lily-space` это 7 800 знаков на каждый
    вызов, — только чтобы выяснить, что ничего не менялось.

    Смена `mtime` без смены содержимого (`git checkout`) даёт лишнюю
    пересборку, и это правильный размен: пересборка идёт кодом и модель
    не зовёт. Обратное — содержимое поменялось, а `mtime` и размер те же
    — означает, что файл не писали.
    """
    marks = []
    for key in STAMP_FILES:
        path = b.path(brand_store.PROFILE.get(key, key))
        try:
            st = path.stat()
        except OSError:
            marks.append(f"{key}:-")     # файла нет — это тоже состояние
        else:
            marks.append(f"{key}:{st.st_mtime_ns}:{st.st_size}")
    return _sha("\n".join(marks))


def _funnel(text: str) -> dict[str, int] | None:
    """Пропорция воронки из текста целей. Не сошлась — вернём None.

    Границу держит код: доли, которые не дают сотню, это не пропорция, а
    опечатка, и работать по ней хуже, чем по честной запасной.
    """
    found: dict[str, int] = {}
    for code, word in FUNNEL_WORDS.items():
        m = re.search(rf"{word}\w*\W{{0,12}}?(\d{{1,3}})\s*%?", text, re.I)
        if m:
            found[code] = int(m.group(1))
    if len(found) < len(FUNNEL_WORDS) or sum(found.values()) != 100:
        return None
    return found


def _clip(text: str, limit: int = DIGEST_CUT) -> str:
    """Обрезать по границе строки: обрубок на середине слова читается как факт.

    Называется не `_cut` намеренно. Пока обе функции звались одинаково, эта
    молча перекрывала ту, что режет заголовок поста до шестидесяти знаков:
    определена ниже — значит выигрывает в момент вызова. В срез из-за этого
    уезжали посты целиком, многострочными, и «заголовок» в статистике был
    полным текстом на полторы тысячи знаков.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rfind("\n")
    return (head[:cut] if cut > limit // 2 else head).rstrip() + "\n\n…"


def _build(b) -> ProfileDigest:
    """Собрать выжимку заново."""
    body: list[str] = []
    missing: list[str] = []

    # Ядро идёт секциями, а не файлом: роли читают свою часть, и целиком
    # его не нужно никому. Полный `core.md` это 7 800 знаков против 3 100
    # в трёх секциях.
    core = [_clip(block) for needle in CORE_SECTIONS
            if (block := b.section("core", needle))]
    if core:
        body.append("### core.md\n\n" + "\n\n".join(core))
    else:
        missing.append("core")

    for key in DIGEST_FILES:
        text = b.read(key).strip()
        name = brand_store.PROFILE.get(key, key)
        if not text:
            missing.append(key)
            continue
        body.append(f"### {name}\n\n{_clip(text)}")

    goals = b.read("goals")
    parsed = _funnel(goals) if goals.strip() else None
    funnel = parsed or dict(BACKUP_FUNNEL)

    if "goals" in missing:
        body.append("### goals.md\n\nЦелей этапа и рубрикатора в профиле нет: "
                    "файл собирается на шагах онбординга O4–O12. Пропорция "
                    f"воронки запасная, {BACKUP_FUNNEL['warm']}/"
                    f"{BACKUP_FUNNEL['prod']}/{BACKUP_FUNNEL['pers']}, и "
                    "назвать её запасной в «Контексте» обязательно.")
    elif parsed is None:
        body.append("### Пропорция воронки\n\nВ `goals.md` пропорция не "
                    "читается или доли не дают сотню. Работаешь по запасной, "
                    f"{BACKUP_FUNNEL['warm']}/{BACKUP_FUNNEL['prod']}/"
                    f"{BACKUP_FUNNEL['pers']}, и говоришь об этом.")
    else:
        body.append("### Пропорция воронки\n\nИз `goals.md`: прогрев "
                    f"{funnel['warm']}, продукт {funnel['prod']}, личное "
                    f"{funnel['pers']}. Это правило бренда, а не догадка.")

    if "platforms" in missing:
        body.append("### platforms.md\n\nНорм площадок в профиле нет: файл "
                    "собирается на шагах онбординга O13–O15. Раскладка по "
                    "площадкам — твоё предложение, человек его утверждает, "
                    "и ты говоришь об этом строкой в «Контексте».")

    dg = ProfileDigest(text="\n\n".join(body), stamp=_stamp(b), rebuilt=True,
                       missing=tuple(missing), funnel=funnel,
                       backup=parsed is None)

    header = (f"<!-- stamp: {dg.stamp} -->\n"
              f"<!-- funnel: warm={funnel['warm']} prod={funnel['prod']} "
              f"pers={funnel['pers']} backup={int(dg.backup)} -->\n"
              f"<!-- missing: {' '.join(dg.missing)} -->\n\n"
              "# Индекс профиля\n\n"
              "Собран кодом из файлов профиля. Пересобирается, когда они "
              "меняются, а не на каждый план: отпечаток считается по "
              "`stat()`, файлы при живом кэше не читаются вовсе.\n\n"
              "Это то, что роли читают вместо `core.md`. Исключение — "
              "«Голос бренда» и стоп-слова: их берут из `core.md` те, кто "
              "пишет текст, и `check_voice` читает их оттуда же.\n")
    b.artifact(DIGEST_PATH, header + "\n" + dg.text)
    log.info("выжимка профиля пересобрана: %s, нет файлов: %s",
             dg.stamp, ", ".join(dg.missing) or "все на месте")
    return dg


def _parse(text: str, stamp: str) -> ProfileDigest:
    """Поднять сохранённую выжимку. Ничего не пересчитывая из исходников."""
    funnel = dict(BACKUP_FUNNEL)
    backup = True
    if m := _FUNNEL_LINE.search(text):
        pairs = dict(p.split("=", 1) for p in m.group(1).split() if "=" in p)
        try:
            funnel = {k: int(pairs[k]) for k in ("warm", "prod", "pers")}
            backup = bool(int(pairs.get("backup", 1)))
        except (KeyError, ValueError):
            funnel, backup = dict(BACKUP_FUNNEL), True

    missing: tuple[str, ...] = ()
    if m := _MISSING_LINE.search(text):
        missing = tuple(m.group(1).split())

    body = text.split("\n\n", 1)[-1]
    if "# Выжимка профиля" in body:
        body = body.split("\n", 1)[-1]
    return ProfileDigest(text=body.strip(), stamp=stamp, rebuilt=False,
                         missing=missing, funnel=funnel, backup=backup)


def profile_digest(b) -> ProfileDigest:
    """Выжимка целей и площадок. Пересобирается только при смене файлов."""
    stamp = _stamp(b)
    path = b.path(DIGEST_PATH)
    if path.exists():
        saved = path.read_text(encoding="utf-8")
        m = _STAMP.search(saved)
        if m and m.group(1) == stamp:
            return _parse(saved, stamp)
    return _build(b)


async def notify_profile(reg, chat_id: int, *, topic: str = "strategy") -> bool:
    """Профиль изменился — пересобрать выжимку и сказать об этом Стратегу.

    Возвращает True, если выжимка действительно пересобрана. Если файлы не
    менялись, роль молчит: сообщение «ничего не изменилось» после каждой
    правки быстро перестают читать.
    """
    b = desk.brand(chat_id)
    if b is None:
        return False

    dg = profile_digest(b)
    if not dg.rebuilt:
        return False

    lines = [f"Профиль изменился, пересобрал выжимку для Стратега "
             f"(<code>{dg.stamp}</code>)."]
    lines.append(f"Пропорция воронки: {dg.ratio}"
                 + (" — запасная, в профиле её нет." if dg.backup
                    else " — из <code>goals.md</code>."))
    if dg.missing:
        lines.append("Ещё нет: "
                     + ", ".join(f"<code>{brand_store.PROFILE[k]}</code>"
                                 for k in dg.missing) + ".")
    lines.append("Планы, собранные раньше, строились на прошлой версии.")

    await reg.say("research", chat_id, "\n".join(lines), topic=topic)
    return True
