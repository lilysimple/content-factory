"""Чужие рилсы: кэш наполняет Apify, завод считает выбросы и читает их.

Та же развязка, что у профилей Instagram, и по той же причине: в сеть за
чужими роликами ходит отдельный сборщик, завод читает то, что осталось
на диске.

    tools/trends_pull.py  →  research/trends/<цель>.json  →  этот модуль

Разница с `orchestrator/instagram.py` одна, и она существенная. Там цель
известна: список профилей, за которыми следим, лежит в `sources.md`, и
снимается последняя дюжина постов каждого. Здесь цель — **ниша**: хэштег
или чужой профиль, и интересны не все посты, а те, что ушли заметно выше
обычного для этого источника. Такой пост здесь зовётся выбросом.

Три границы живут в этом файле, потому что промпт их не держит:

**Выброс считает код.** Медиана источника, во сколько раз выше неё
ролик, порог достоверности — всё здесь. Роль, которой дали сырой список
с числами, называет «залетевшим» то, что красивее написано, а не то, что
собрало просмотры: это уже случилось с ручным разбором 18.09, где два
ролика из десяти попали в список без чисел вовсе.

**Метрика у источника одна, и она названа.** Instagram отдаёт просмотры
только у видео, лайки — у всего. Если просмотры есть не у всех роликов
партии, медиана считается по лайкам, и партия об этом говорит. Складывать
просмотры одного источника с лайками другого нельзя, и подписывать одно
другим — тоже: в ручном разборе «135 тыс.» и «44 взаимодействия» стоят
рядом, и спутать их стоит неверного вывода о механике.

**Кэш стареет молча, а тренд стареет быстро.** Файл трёхнедельной
давности выглядит как снятый утром. У профилей запас восемь дней, здесь
меньше: приём, который собирал просмотры месяц назад, сегодня уже
разошёлся по всем и перестал работать. Возраст называется дырой.

Чего здесь нет намеренно: похода в сеть. Ни одной строки с `urllib` —
иначе завод начнёт добирать чужие рилсы в момент прогона роли, по
платному ключу и без ведома человека.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from validators import check_voice

log = logging.getLogger("trends")

CACHE = "research/trends"
WATCHLIST = "research/trends.md"

# Сколько дней кэшу можно быть. Короче, чем у профилей (там восемь):
# профиль нужен, чтобы закрыть окно прошлой недели, а тренд — чтобы
# понять, что работает сейчас. Пять суток это «сняла в среду, разбираю в
# понедельник».
STALE_DAYS = 5

# Ниже этого числа роликов медиана источника — совпадение, а не база для
# сравнения, и выбросы по такой партии не считаются вовсе.
#
# Пять, а не восемь, и это уступка измеренному. Порог ставился на восьми
# «с запасом», но живой прогон 14.10 показал потолок источника: актор
# отдаёт первую страницу сетки профиля — около двенадцати постов, из них
# рилсов семь, в окне пять, — и `resultsLimit` её не расширяет. При
# пороге восемь не считалась ни одна настоящая партия, то есть контур не
# работал вовсе.
#
# Пять это тот же порог, на котором стоит своя статистика Ресёрчера
# («ниже пяти постов медиана это совпадение»), так что второго числа в
# проекте не появляется. Промежуток до `SHAKY` считается, но называется
# ненадёжным вслух — это дешевле, чем молчащий контур.
MIN_ITEMS = 5

# Партия меньше этого — считается, но помечается ненадёжной. Разница с
# `MIN_ITEMS` в том, что отказ человек видит как дыру, а ненадёжность
# должна доехать до вывода роли: медиана по шести роликам живая, но
# сдвигается одним удачным выходом.
SHAKY = 8

# Во сколько раз выше медианы источника ролик должен уйти, чтобы считаться
# залетевшим. Двойка, а не «топ-3 по просмотрам»: топ есть всегда, в том
# числе у партии, где не залетело ничего, и роль разберёт механику у
# ролика, который просто чуть выше среднего.
HOT = 2.0

# Доля роликов с просмотрами, при которой медиана считается по просмотрам.
# Ниже — по лайкам, и партия об этом говорит.
#
# Замер 23.10 показал, что на деле эта ветка не выбирается никогда:
# прогон по профилю отдаёт `videoPlayCount` и `videoViewCount` пустыми у
# **всех** записей — 453 рилса из 453 по четырнадцати аккаунтам. Покрытие
# ноль, значит медиана всегда считается по лайкам. Порог оставлен: он
# верен, просто сборщик профилей до него не доезжает, а хэштег-скрапер
# меряли не им.
VIEWS_ENOUGH = 2 / 3

# Отклик: лайки, делённые на показы. Нужен затем, что кратность к медиане
# меряет раздачу, а не попадание, и для решения «копировать ли приём»
# этого мало. Замер ниши психосоматики 23.10, 27 роликов от ста тысяч
# показов: ролик с оптической иллюзией собрал 1 249 077 показов при
# отклике 0,28 %, а разбор по теме у того же автора — 113 199 показов при
# 3,54 %. По кратности первый пройдёт в выдачу первым, хотя копировать
# надо второй, и приём там вообще не из ниши.
#
# Две границы, и обе называются в выдаче словами: ниже нижней — раздача
# вхолостую, выше верхней — ролик попал в своего человека. Между ними не
# утверждается ничего.
LIVE_LOW = 0.01
LIVE_HIGH = 0.04

TAG = re.compile(r"^#?([0-9A-Za-z_Ѐ-ӿ]{2,60})$")
HANDLE = re.compile(r"^@?([A-Za-z0-9._]{1,30})$")


# ── цели: за какой нишей следим ───────────────────────────────────────

@dataclass
class Target:
    """Строка из `research/trends.md`: что снимать и зачем."""
    kind: str          # «tag» или «user»
    value: str         # без решётки и без собаки
    why: str = ""

    @property
    def name(self) -> str:
        """Имя файла кэша. Сборщик и читатель считают его одинаково."""
        return f"{self.kind}-{self.value.lower()}"

    @property
    def human(self) -> str:
        return f"#{self.value}" if self.kind == "tag" else f"@{self.value}"


def target(item: str) -> Target | None:
    """`#aiдляработы` → тег, `@tech.unicorn` → профиль, прочее → ничего.

    Решётка и собака обязательны. Голое слово не разбирается намеренно:
    `ai` одинаково похоже на хэштег и на ник, а снятое не по той цели
    приезжает молча — партия непустая, числа есть, источник чужой.
    """
    s = (item or "").strip()
    if s.startswith("#") and (m := TAG.match(s)):
        return Target("tag", m.group(1))
    if s.startswith("@") and (m := HANDLE.match(s)):
        return Target("user", m.group(1))
    return None


def targets(b) -> tuple[list[Target], list[str]]:
    """Список целей из `research/trends.md`. Второе — дыры строками.

    Отдельный файл, а не раздел в `sources.md`, и это не второй список
    того же. В `sources.md` лежат источники, которые читает сводка
    недели, и `research.watchlist` берёт оттуда **каждую** строку с
    дефисом: хэштег, дописанный туда, уехал бы в `sources.fetch`, не
    открылся бы и вернулся дырой «не удалось собрать» в каждую сводку.
    Объекты здесь другие — ниша, а не канал, — и читатель у них другой.
    """
    out: list[Target] = []
    gaps: list[str] = []
    if not b.exists(WATCHLIST):
        return out, [f"списка целей нет — заведи `{WATCHLIST}` в папке "
                     "бренда: строки `- #хэштег — зачем` и `- @профиль — зачем`"]
    for line in b.read(WATCHLIST).splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        item, _, why = line[2:].partition("—")
        t = target(item.strip())
        if t is None:
            gaps.append(f"строка «{line[2:][:40]}» не разобралась — "
                        "цель пишется как `#хэштег` или `@профиль`")
            continue
        t.why = why.strip()
        out.append(t)
    if not out and not gaps:
        gaps.append(f"в `{WATCHLIST}` ни одной цели — снимать нечего")
    return out, gaps


# ── партия: что сняли по одной цели ───────────────────────────────────

@dataclass
class Reel:
    """Один чужой ролик. Чего не отдали — прочерк, а не ноль."""
    code: str = ""
    url: str = ""
    owner: str = ""
    caption: str = ""
    date: datetime | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    seconds: float | None = None
    followers: int | None = None
    rate: float = 0.0            # во сколько раз выше медианы источника
    plays: int | None = None     # показы, добранные поштучно по ссылке

    @property
    def hook(self) -> str:
        """Первая строка подписи. По ней видно заход, а не весь текст."""
        for line in self.caption.splitlines():
            if line.strip():
                return line.strip()
        return ""

    @property
    def live(self) -> float | None:
        """Отклик: лайки на показ. Нет показов или лайков — прочерк.

        Считается от `plays`, а не от `views`, и это не придирка к имени.
        `views` приезжает из общей партии и у профилей всегда пуст;
        `plays` добирается поштучно и есть только у кандидатов. Путать их
        нельзя: отклик, посчитанный от пустого поля, молча станет нулём и
        отправит живой ролик в «раздачу вхолостую».
        """
        if not self.plays or self.likes is None:
            return None
        return self.likes / self.plays

    @property
    def live_word(self) -> str:
        """Отклик словом. Между границами — пусто, а не «средне»."""
        v = self.live
        if v is None:
            return ""
        if v < LIVE_LOW:
            return "вхолостую"
        if v >= LIVE_HIGH:
            return "попал"
        return ""


@dataclass
class Batch:
    """Партия по одной цели: ролики, медиана, метрика, дыра."""
    target: Target
    pulled: datetime | None = None
    since: datetime | None = None    # окно, которое просил сборщик
    items: list[Reel] = field(default_factory=list)
    dropped: int = 0                 # отброшено вне окна: закреплённые и старые
    metric: str = ""             # «просмотры» или «лайки»
    median: float = 0.0
    ok: bool = False
    error: str = ""

    @property
    def shaky(self) -> bool:
        """Медиана живая, но на честном пределе: сказать это обязательно."""
        return self.ok and len(self.items) < SHAKY

    @property
    def hot(self) -> list[Reel]:
        """Выбросы: от крупного к мелкому. Не сошлось — пусто."""
        if not self.ok:
            return []
        return sorted((r for r in self.items if r.rate >= HOT),
                      key=lambda r: r.rate, reverse=True)


def _int(value) -> int | None:
    """Число или прочерк. Ноль означает «реально ноль», а не «не отдали»."""
    if value is None or value is True or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    if value is None or value is True or value is False:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _when(raw) -> datetime | None:
    """Дата. Не разобралась — прочерк, а не сегодня.

    Та же причина, что у фидов и профилей: дата «на всякий случай»
    затащит в разбор прошлогодний ролик, по которому механику признают
    рабочей сегодня.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def cache_path(b, name: str) -> Path:
    return b.path(f"{CACHE}/{name.lower()}.json")


def _metric(items: list[Reel]) -> str:
    """Чем мерить партию. Решается по покрытию, а не по желанию.

    Просмотры Instagram отдаёт у видео, лайки — у всего. Партия, где
    просмотры есть у трети роликов, посчитанная по просмотрам, сравнила
    бы шесть роликов из восемнадцати и назвала это медианой источника.
    """
    if not items:
        return ""
    seen = sum(1 for r in items if r.views is not None)
    return "просмотры" if seen >= len(items) * VIEWS_ENOUGH else "лайки"


def _value(reel: Reel, metric: str) -> int | None:
    """Число, которым меряется партия.

    `plays` сюда не попадает намеренно, и это главное здесь. Добранные
    показы есть только у кандидатов, то есть у верхушки партии. Медиана,
    посчитанная по ним, была бы медианой выбросов: всё остальное мгновенно
    перестало бы быть выбросом, и контур сказал бы «не залетело ничего»
    ровно там, где залетело. Показы добираются, чтобы их прочитала роль,
    а не чтобы ими мерить.
    """
    return reel.views if metric == "просмотры" else reel.likes


def _window(batch: Batch) -> None:
    """Выбросить из партии то, что старше окна сбора.

    Окно просит сборщик (`onlyPostsNewerThan`), но актор его не держит, и
    схема об этом прямо предупреждает: **закреплённые посты приезжают в
    любом случае**. Живой прогон 14.10 это показал — в партии из семи
    роликов соседа приехали закреплённые 2024 и 2023 годов, и один из них
    на 97 миллионов просмотров. Такая запись не просто лишняя: медиана
    партии из семи с ней считается по витрине аккаунта, а не по его
    обычной отдаче, и всё остальное перестаёт быть выбросом.

    Ролик без даты тоже уходит: положить его в окно нечем, а оставить
    значит пустить мимо проверки именно то, что её не прошло.
    """
    if batch.since is None:
        return
    edge = batch.since.date()
    keep = [r for r in batch.items if r.date and r.date.date() >= edge]
    batch.dropped = len(batch.items) - len(keep)
    batch.items = keep


def measure(batch: Batch) -> None:
    """Посчитать медиану партии и `rate` у каждого ролика.

    Мало роликов — партия не считается вовсе: это дыра, а не слабая
    статистика. Роль, получившая медиану по пяти роликам, построит на
    ней вывод о нише.
    """
    batch.metric = _metric(batch.items)
    nums = [v for r in batch.items if (v := _value(r, batch.metric)) is not None]
    if len(nums) < MIN_ITEMS:
        batch.ok = False
        batch.error = (f"роликов с числами {len(nums)}, нужно {MIN_ITEMS} — "
                       "медиана по такой партии совпадение, а не база")
        return
    batch.median = float(statistics.median(nums))
    if batch.median <= 0:
        batch.ok = False
        batch.error = "медиана источника ноль — сравнивать не с чем"
        return
    for r in batch.items:
        v = _value(r, batch.metric)
        r.rate = round(v / batch.median, 2) if v is not None else 0.0
    batch.ok = True
    batch.error = ""


def read(b, t: Target) -> tuple[Batch, str]:
    """Кэш одной цели в `Batch`. Второе значение — дыра строкой.

    Формы четыре — файла нет, файл битый, роликов мало, кэш стар, — и все
    четыре это дыра, а не отказ: разбор по части ниши честнее молчания.
    """
    batch = Batch(target=t)
    path = cache_path(b, t.name)
    if not path.exists():
        batch.error = "кэша нет"
        return batch, (f"{t.human}: кэша нет — сними "
                       f"`tools/trends_pull.py --target {t.human}`")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        batch.error = f"кэш не читается: {type(e).__name__}"
        return batch, f"{t.human}: кэш не читается ({type(e).__name__}), пересними"

    batch.pulled = _when(data.get("pulled"))
    batch.since = _when(data.get("since"))
    for row in data.get("items") or []:
        batch.items.append(Reel(
            code=str(row.get("code") or ""),
            url=str(row.get("url") or ""),
            owner=str(row.get("owner") or ""),
            caption=str(row.get("caption") or ""),
            date=_when(row.get("date")),
            views=_int(row.get("views")),
            likes=_int(row.get("likes")),
            comments=_int(row.get("comments")),
            seconds=_float(row.get("seconds")),
            followers=_int(row.get("followers")),
            plays=_int(row.get("plays")),
        ))
    _window(batch)
    measure(batch)
    if not batch.ok:
        return batch, f"{t.human}: {batch.error}"
    return batch, _aged(t, batch.pulled)


def _aged(t: Target, pulled: datetime | None, today: date | None = None) -> str:
    """Кэш старше `STALE_DAYS` — дыра строкой. Возраст не виден на глаз."""
    if pulled is None:
        return f"{t.human}: в кэше нет даты сбора — считать его свежим нельзя"
    age = ((today or date.today()) - pulled.date()).days
    if age > STALE_DAYS:
        return (f"{t.human}: кэш снят {pulled:%d.%m}, {age} дней назад — "
                "приём за это время мог разойтись по всем и перестать работать")
    return ""


def collect(b) -> tuple[list[Batch], list[str]]:
    """Все цели бренда: партии и дыры. Пустой список партий это не поломка."""
    found, gaps = targets(b)
    out: list[Batch] = []
    for t in found:
        batch, gap = read(b, t)
        if gap:
            gaps.append(gap)
        if batch.ok:
            out.append(batch)
    return out, gaps


# ── выжимка для роли ──────────────────────────────────────────────────

# Сколько выбросов на цель уезжает в промпт. Шесть: больше роль всё равно
# не разберёт по правилу трёх, а каждый ролик стоит подписью.
PER_TARGET = 6

# Подпись режется здесь, а не у читателя. Чужие рилсы подписывают
# простынями с тридцатью хэштегами, и без границы одна цель съедала бы
# больше, чем весь профиль бренда.
CAPTION_CUT = 400


def _row(r: Reel, metric: str) -> str:
    val = _value(r, metric)
    num = f"{val:,}".replace(",", " ") if val is not None else "—"
    plays = f"{r.plays:,}".replace(",", " ") if r.plays else "—"
    live = r.live
    resp = f"{live * 100:.2f} %" if live is not None else "—"
    if word := r.live_word:
        resp += f", {word}"
    secs = f"{r.seconds:.0f} с" if r.seconds else "—"
    subs = f"{r.followers:,}".replace(",", " ") if r.followers else "—"
    when = f"{r.date:%d.%m}" if r.date else "—"
    caption = " ".join(r.caption.split())[:CAPTION_CUT]
    return (f"| {r.rate:g}× | {num} | {plays} | {resp} | {secs} | "
            f"@{r.owner or '—'} | {subs} | {when} | {r.url or '—'} | "
            f"{caption or '—'} |")


def digest(b) -> tuple[list[str], list[str]]:
    """Раздел `input.md` про чужие рилсы и список дыр.

    Числа кладёт код, разбор делает роль. Поэтому здесь таблица выбросов
    с медианой источника рядом, а не «вот последние сто роликов ниши»:
    роль не считает медиану и не выбирает, что считать залетевшим.
    """
    batches, gaps = collect(b)
    out = ["", "## Чужие рилсы: выбросы по нише", ""]
    if not batches:
        out += ["Ни одной партии с числами. Разбирать нечего — скажи об этом "
                "строкой в `final.md` и не придумывай чужие ролики."]
        return out, gaps

    out += ["Выброс это ролик выше медианы своего источника "
            f"минимум в {HOT:g} раза. Медиану и кратность посчитал код: "
            "сам их не пересчитывай и «залетевшим» ничего другого не "
            "называй.", "",
            "Метрика у каждой цели своя и подписана. Просмотры одного "
            "источника с лайками другого не складываются.", "",
            "Столбец «Показы» добран поштучно и только у кандидатов: в "
            "медиану он не входит, сравнивать ролики по нему между собой "
            "нельзя. Прочерк значит «не добирали», а не «ноль».", "",
            "Столбец «Отклик» это лайки на показ, и читать его надо раньше "
            f"кратности. Ниже {LIVE_LOW * 100:g} % помечено «вхолостую»: "
            "ролик разошёлся, но людям не понравился, и копировать там "
            f"нечего. От {LIVE_HIGH * 100:g} % помечено «попал». Миллион "
            "показов при отклике в четверть процента это предупреждение, а "
            "не образец: так выглядят заимствованные вирусные трюки, "
            "которые по кратности проходят первыми, а к нише отношения не "
            "имеют. Между границами ничего не утверждается."]
    for batch in batches:
        med = f"{batch.median:,.0f}".replace(",", " ")
        hot = batch.hot[:PER_TARGET]
        line = (f"Роликов в партии {len(batch.items)}, медиана {med} "
                f"({batch.metric}), снято "
                + (f"{batch.pulled:%d.%m}" if batch.pulled else "неизвестно когда")
                + f". Выбросов {len(batch.hot)}.")
        if batch.since:
            line += f" Окно: с {batch.since:%d.%m}."
        else:
            line += (" Окна по дате у партии нет: в медиану могли попасть "
                     "ролики любой давности.")
        if batch.dropped:
            line += (f" Вне окна отброшено {batch.dropped} — это "
                     "закреплённые и старые ролики, в медиану они не вошли.")
        if batch.shaky:
            line += (f" **Партия меньше {SHAKY} роликов: медиана живая, но "
                     "ненадёжная — один удачный выход её сдвигает. Вывод по "
                     "этой цели помечай как предварительный.**")
        out += ["", f"### {batch.target.human}"
                    + (f" — {batch.target.why}" if batch.target.why else ""), "",
                line]
        if not hot:
            out += ["", "Выбросов нет: в этой партии не залетело ничего. "
                        "Это тоже результат — ровная выдача значит, что "
                        "приёма, который сейчас работает, здесь не видно."]
            continue
        out += ["",
                "| Кратность | " + batch.metric.capitalize() +
                " | Показы | Отклик | Длина | Автор | Подписчиков | Когда "
                "| Ссылка | Подпись |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        out += [_row(r, batch.metric) for r in hot]
    past = last_review(b)
    if past is not None:
        out += ["", f"Прошлый разбор лежит в `research/{past.name}` — прочитай "
                    "его и не выдавай за находку то, что в нём уже названо: "
                    "механику, которая держится с прошлого раза, упомяни "
                    "строкой и место отдай новой."]
    out += ["", "**В кэше только подпись и числа, самого ролика нет.** Хук "
                "на слух, первый кадр, ритм монтажа и титры по этой таблице "
                "не видны: разбор по ней — разбор текста и цифр. Где вывод "
                "держится на том, чего в кэше нет, скажи это строкой, а не "
                "домысли по подписи."]
    return out, gaps


# ── запись: форму держит читатель ─────────────────────────────────────

def stash(b, data: dict) -> Path:
    """Записать снятое сборщиком. Проверка формы здесь, а не в сборщике.

    Та же причина, что у профилей: сборщик ходит в чужой API и меняется
    чаще всех, читатель у кэша один — этот модуль.
    """
    t = target(str(data.get("target") or ""))
    if t is None:
        raise ValueError(f"цель не разобралась: {data.get('target')!r}")
    items = data.get("items") or []
    if not isinstance(items, list):
        raise ValueError("items должен быть списком")

    body = {
        "target": t.human,
        "kind": t.kind,
        "value": t.value,
        "pulled": (data.get("pulled")
                   or datetime.now().astimezone().isoformat(timespec="seconds")),
        "source": str(data.get("source") or ""),
        # Окно, которое просил сборщик. Читатель режет по нему сам:
        # закреплённые ролики приезжают мимо фильтра актора.
        "since": data.get("since"),
        "items": [{
            "code": str(it.get("code") or ""),
            "url": str(it.get("url") or ""),
            "owner": str(it.get("owner") or ""),
            "caption": str(it.get("caption") or ""),
            "date": it.get("date"),
            "views": _int(it.get("views")),
            "likes": _int(it.get("likes")),
            "comments": _int(it.get("comments")),
            "seconds": _float(it.get("seconds")),
            "followers": _int(it.get("followers")),
            # Показы, добранные поштучно. Поле проходит через белый
            # список наравне с остальными: без строки здесь добор доезжал
            # бы до диска и там молча терялся — форму кэша держит этот
            # список, а не сборщик.
            "plays": _int(it.get("plays")),
        } for it in items],
    }
    path = cache_path(b, t.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    log.info("кэш ниши %s: %s роликов", t.human, len(body["items"]))
    return path


# ── посадка разбора: механика доживает до следующего раза ─────────────
#
# До этого разбор с моста не возвращался в завод вовсе. Роль писала
# `tasks/{id}/trends.md`, папка задачи лежит в `.gitignore`, и к
# следующему прогону от разбора не оставалось ничего: ни механик, ни
# отклонённого. Хуже того, адаптер роли велел ей читать прошлые разборы
# в `research/trends-*.md` — файлы, которые не писал никто. Правило «не
# повторяй прошлую находку» было неисполнимым обещанием.
#
# Здесь же стоят две границы, которые промпт только просил соблюдать.

REVIEW = "research/trends-{day}.md"
REVIEW_GLOB = "trends-*.md"

# Правило трёх. Приём становится механикой ниши, только если встретился у
# трёх разных авторов: три ролика одного аккаунта это его формат, он
# держится на его лице и его базе. То же число, что у Ресёрчера
# (`research.sift`), и второго в проекте не появляется.
NEED = 3

# Ссылка на чужой ролик. Короткий код в ней это то, чем ролик назван в
# партии, — по нему пример и сверяется с кэшем.
CODE = re.compile(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]{4,})")


class LandRefused(RuntimeError):
    """Разбор не сел: пустой контракт, класть нечего."""


@dataclass
class Review:
    """Разбор после посадки: что осталось механикой, а что код подвинул."""
    window: str = ""
    metric: str = ""
    mechanics: list[dict] = field(default_factory=list)
    singles: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    offers: list[dict] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    # Что сделала посадка. Человек должен это видеть: механика, уехавшая
    # в единичные, выглядит в чате как механика, если об этом не сказать.
    findings: list[str] = field(default_factory=list)


def _known(b) -> dict[str, str]:
    """Код ролика → автор, по всем партиям на диске.

    Нужен затем, что выдуманная ссылка ловится первым нажатием, и ловить
    её должен код, а не человек в чате. Роль видит только выбросы, но
    сверяем по всей партии: ролик ниже порога она процитировать не могла
    вовсе, а лишняя строгость дала бы отказ на живом примере.

    Партия берётся вся, включая непосчитанную: вопрос здесь «был ли этот
    ролик в снимке», а не «годится ли партия для медианы». Иначе цель,
    просевшая ниже `MIN_ITEMS` после пересъёмки, объявила бы живые
    примеры выдуманными.
    """
    out: dict[str, str] = {}
    for t in targets(b)[0]:
        for r in read(b, t)[0].items:
            code = r.code
            if not code and (m := CODE.search(r.url)):
                code = m.group(1)
            if code:
                out[code] = r.owner
    return out


def _cited(example: str, known: dict[str, str]) -> tuple[str, str, bool]:
    """Пример механики → код ролика, автор, нашёлся ли он в партиях."""
    m = CODE.search(example)
    if m is None:
        return "", "", False
    code = m.group(1)
    if code not in known:
        return code, "", False
    return code, known[code], True


# Строки, которые бренд произносит сам. Стоп-слово здесь это отказ, а в
# `what` и `why` — описание чужого приёма: «одиннадцатикратный выброс на
# „секретном методе“» это наблюдение, и запрещать его нельзя.
SPOKEN = ("cover", "save")


def _stop(b, mech: dict) -> list[str]:
    """Стоп-слова бренда в перенесённых строках. Четвёртое железное правило.

    Приём, который держится на стоп-слове, отклоняется с причиной, а не
    «адаптируется помягче»: кратность стоп-листа не отменяет. Проверяет
    это код, потому что отказ должен быть детерминированным.
    """
    words = b.stopwords()
    if not words:
        return []
    said = [str(mech.get(k) or "") for k in SPOKEN]
    said += [str(h) for h in (mech.get("hooks") or [])]
    out: list[str] = []
    for line in said:
        for f in check_voice.check(line, stopwords=words, allow_em_dash=True):
            if f.kind == "brand":
                out.append(f.fragment)
    return list(dict.fromkeys(out))


def land(b, data: dict) -> tuple[Review, str]:
    """Посадить разбор: проверить механики кодом, записать файл в бренд.

    Публичный шов, как `strategy.land` у плана и `editor.land` у текста.
    Проверок две, и обе раньше были просьбой в промпте:

    **Правило трёх проверяется, а не обещается.** `roles/trend.md` уже
    говорит роли, что короткие списки код отправляет в единичные сам, —
    и до этой функции не отправлял никто. Механика с двумя примерами или
    с тремя примерами одного автора уезжает в `singles` с причиной.

    **Ссылка сверяется с партией.** Пример, которого в снятом кэше нет,
    в счёт правила трёх не идёт, и об этом говорится строкой. Партию
    могли переснять между прогоном и посадкой — тогда живой пример
    окажется ненайденным, поэтому это находка, а не отказ.

    Возвращает разбор и путь выгрузки.
    """
    if b is None:
        raise LandRefused("профиля бренда нет, класть разбор некуда")

    rev = Review(
        window=str(data.get("window") or ""),
        metric=str(data.get("metric") or ""),
        singles=[m for m in (data.get("singles") or []) if isinstance(m, dict)],
        rejected=[m for m in (data.get("rejected") or []) if isinstance(m, dict)],
        offers=[o for o in (data.get("offers") or []) if isinstance(o, dict)],
        gaps=[str(g) for g in (data.get("gaps") or [])])
    incoming = [m for m in (data.get("mechanics") or []) if isinstance(m, dict)]

    if not (incoming or rev.singles or rev.rejected or rev.offers or rev.gaps):
        raise LandRefused("контракт разбора пуст: класть нечего")

    known = _known(b)
    for mech in incoming:
        name = str(mech.get("name") or "без имени")

        stop = _stop(b, mech)
        if stop:
            rev.rejected.append(
                {"what": name,
                 "reason": "стоп-слово бренда в переносе: " + ", ".join(stop)})
            rev.findings.append(
                f"«{name}»: перенос на бренд идёт через стоп-слово "
                f"({', '.join(stop)}) — механика уехала в отклонённое")
            continue

        owners: set[str] = set()
        checked, ghosts, blind = 0, [], 0
        for ex in [str(e) for e in (mech.get("examples") or [])]:
            code, owner, found = _cited(ex, known)
            if not code:
                blind += 1
                continue
            if not found:
                ghosts.append(code)
                continue
            checked += 1
            if owner:
                owners.add(owner)
        if ghosts:
            rev.findings.append(
                f"«{name}»: в снятых партиях нет роликов {', '.join(ghosts)} — "
                "в счёт правила трёх они не пошли")
        if blind:
            rev.findings.append(
                f"«{name}»: примеров без ссылки {blind} — проверить их нечем, "
                "в счёт правила трёх они не пошли")

        if checked < NEED or len(owners) < NEED:
            why = (f"примеров со ссылкой {checked}, разных авторов "
                   f"{len(owners)} — правило трёх не набралось")
            rev.singles.append({"what": name, "why_single": why,
                                "why": str(mech.get("why") or "")})
            rev.findings.append(f"«{name}» уехала в единичные: {why}")
            continue

        rev.mechanics.append(mech)

    rel = REVIEW.format(day=date.today().isoformat())
    b.artifact(rel, _review_md(b, rev))
    log.info("разбор трендов сел: механик %s, единичных %s, отклонено %s",
             len(rev.mechanics), len(rev.singles), len(rev.rejected))
    return rev, rel


def _review_md(b, rev: Review) -> str:
    """Форму выгрузки держит код, как у плана.

    Читателей у файла двое: человек, который вернётся к нему через
    неделю, и следующий прогон роли, которому нельзя повторять старую
    находку. Обе формы собираются здесь, а не в промпте: иначе через три
    разбора файлы перестанут быть похожими друг на друга.
    """
    out = [f"# Чужие рилсы: разбор {date.today():%d.%m.%Y}", ""]
    if rev.window:
        out += [f"Партия: {rev.window}"]
    if rev.metric:
        out += [f"Метрика выбросов: {rev.metric}"]
    out += [""]

    out += ["## Механики", ""]
    if not rev.mechanics:
        out += ["Ни одной механики по правилу трёх не набралось.", ""]
    for m in rev.mechanics:
        out += [f"### {m.get('name') or 'без имени'}", ""]
        for key, title in (("what", "Приём"), ("why", "Почему работает"),
                           ("cover", "Обложка словами бренда"),
                           ("save", "Повод сохранить"),
                           ("skip", "Чего не берём")):
            if m.get(key):
                out += [f"- **{title}.** {m[key]}"]
        hooks = [str(h) for h in (m.get("hooks") or []) if str(h).strip()]
        if hooks:
            out += ["- **Хуки.** " + " / ".join(hooks)]
        ex = [str(e) for e in (m.get("examples") or []) if str(e).strip()]
        if ex:
            out += ["", "Примеры:"] + [f"- {e}" for e in ex]
        out += [""]

    if rev.singles:
        out += ["## Единичные случаи", ""]
        out += [f"- {s.get('what') or '—'} — {s.get('why_single') or '—'}"
                for s in rev.singles] + [""]
    if rev.rejected:
        out += ["## Отклонено", ""]
        out += [f"- {r.get('what') or '—'} — {r.get('reason') or '—'}"
                for r in rev.rejected] + [""]
    if rev.offers:
        out += ["## Заготовки тем", ""]
        for o in rev.offers:
            secs = f", {o['seconds']} с" if o.get("seconds") else ""
            out += [f"- «{o.get('title') or '—'}» — хук: "
                    f"{o.get('hook') or '—'}; механика: "
                    f"{o.get('mechanic') or '—'}{secs}"]
        out += [""]
    if rev.findings:
        out += ["## Что сняла посадка", ""]
        out += [f"- {f}" for f in rev.findings] + [""]
    if rev.gaps:
        out += ["## Чего не хватило", ""]
        out += [f"- {g}" for g in rev.gaps] + [""]
    return "\n".join(out)


def last_review(b) -> Path | None:
    """Последний разбор на диске. Нет ни одного — `None`, и это не поломка.

    Имя файла несёт дату, поэтому сортировки по имени достаточно: время
    правки файла врёт после `git checkout`, а дата сбора — нет.
    """
    folder = b.path("research")
    if not folder.is_dir():
        return None
    found = sorted(folder.glob(REVIEW_GLOB))
    return found[-1] if found else None
