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
from datetime import date
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
    files = sorted(f for f in (folder.glob("*.md") if folder.is_dir() else [])
                   if WEEK_FILE.match(f.stem))
    if not files:
        return "", ""
    return files[-1].stem, files[-1].read_text(encoding="utf-8")


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
    """Отпечаток файлов, на которых стоит план."""
    return _sha("\n".join(f"{k}:{_sha(b.read(k))}" for k in STAMP_FILES))


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


def _cut(text: str, limit: int = DIGEST_CUT) -> str:
    """Обрезать по границе строки: обрубок на середине слова читается как факт."""
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

    for key in DIGEST_FILES:
        text = b.read(key).strip()
        name = brand_store.PROFILE.get(key, key)
        if not text:
            missing.append(key)
            continue
        body.append(f"### {name}\n\n{_cut(text)}")

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
              "# Выжимка профиля для Стратега\n\n"
              "Собрана кодом из файлов профиля. Пересобирается, когда они "
              "меняются, а не на каждый план.\n")
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
