"""Рабочий стол роли: то общее, что было скопировано в каждую.

Четыре производящие роли устроены одинаково: взять тему, позвать модель,
показать карточку с кнопками, пережить перезапуск, принять правку. Пока
это лежало четырьмя копиями, любая починка чинила одну роль из четырёх —
на этом уже трижды попались вшитые фразы про соседа.

Здесь только каркас. Что именно роль делает с темой, остаётся у неё.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from config import cfg
from storage import brand as brand_store
from storage import db

log = logging.getLogger("desk")

store = brand_store.Store(cfg.brands_path)

# Стабильный id сквозь всю систему: ГГГГ-ММ-ДД-площадка-NN. По нему тема
# связывается с текстом, макетом, публикацией и правками человека.
ID_RX = re.compile(r"\b\d{4}-\d{2}-\d{2}-[a-z]+-\d{2}\b")

# Поля темы, которые роль читает как задание. Порядок от общего к частному.
BRIEF_FIELDS = (("id", "id"), ("дата", "date"), ("площадка", "plat"),
                ("формат", "format"), ("рубрика", "rubric"), ("цель", "goal"),
                ("архетип", "arch"), ("рабочий заголовок", "title"),
                ("хук", "hook"), ("кому и зачем", "why"), ("угол", "angle"),
                ("ведущий заряд", "charge"))


def brand(chat_id: int):
    """Профиль бренда тенанта. None — профиль ещё не собран."""
    row = db.one("SELECT brand_slug FROM tenants WHERE chat_id = ?", chat_id)
    return store.get(row["brand_slug"]) if row and row["brand_slug"] else None


def today(chat_id: int) -> str:
    """Сегодня в часовом поясе тенанта, а не сервера."""
    row = db.one("SELECT tz FROM tenants WHERE chat_id = ?", chat_id)
    tz = (row["tz"] if row else None) or cfg.default_tz
    try:
        return datetime.now(ZoneInfo(tz)).date().isoformat()
    except Exception:                                        # noqa: BLE001
        return datetime.now().date().isoformat()


PROFILE_LIMIT = 8000


def profile(b, sections: tuple[str, ...], limit: int = PROFILE_LIMIT) -> str:
    """Секции профиля для кешируемого блока промпта.

    Роль читает свои секции, а не файл целиком: один запрос — один
    контекст. Секций нет вовсе — отдаём `core.md`, чтобы роль работала
    на том, что есть, а не молчала.
    """
    parts = [s for s in (b.section("core", n) for n in sections) if s]
    return ("\n\n".join(parts) or b.read("core"))[:limit]


# ── живые тексты бренда ───────────────────────────────────────────────
#
# Голос в профиле **описан словами**, а звучит он в текстах. Папка
# `voice-samples/` заводится на онбординге (O4–O5) ровно под это, и
# субагент `writer` открывает её сам: путь назван в его адаптере.
#
# Прямой путь роли не открывал её ни разу. Половина механизма была
# мёртвой ровно так же, как папка референсов у Дизайнера до 04.09: файлы
# лежат, со стороны всё выглядит работающим, а в промпт не уезжает
# ничего. Редактор писал по описанию — а «тёплый разговорный, без
# канцелярита» описывает сотню непохожих текстов, и какой из них ваш,
# видно только по образцу.
#
# Отбор не зависит от темы, и это не лень. Образцы едут в стабильную
# часть промпта, а она кешируется совпадением префикса: подбирать их под
# каждую тему значит платить за префикс заново на каждом посте. Выигрыш
# при этом мелкий — образцом калибруют интонацию, а не фактуру.

SAMPLES_DIR = "voice-samples"
SAMPLES_MAX = 5              # больше пяти голос не уточняют, а раздувают
SAMPLES_CUT = 1200           # знаков на образец: интонация слышна в начале
SAMPLES_MIN = 200            # короче — это подпись, а не текст (spec/07)
SAMPLES_BUDGET = 6000        # потолок раздела целиком

# Служебная шапка образца («прислано человеком 04.09, дообогащение
# голоса») это провенанс для человека, а не часть голоса. Уехав в промпт,
# она приглашает модель писать такие же комментарии в тексте поста.
LEAD_COMMENT = re.compile(r"^\s*<!--.*?-->\s*", re.S)


def voice_samples(b, *, limit: int = SAMPLES_MAX, cut: int = SAMPLES_CUT,
                  budget: int = SAMPLES_BUDGET) -> list[tuple[str, str]]:
    """Живые тексты бренда: имя файла и текст без служебной шапки.

    Пусто — это результат, а не поломка: папка может быть не заведена и
    может быть заведена пустой. И то и другое значит «сверить голос не с
    чем», и назвать это должен тот, кто зовёт.
    """
    d = b.path(SAMPLES_DIR)
    if not d.is_dir():
        return []

    out: list[tuple[str, str]] = []
    spent = 0
    for f in sorted(d.iterdir()):
        if len(out) >= limit:
            break
        if not f.is_file() or f.suffix.lower() != ".md":
            continue
        try:
            text = LEAD_COMMENT.sub("", f.read_text(encoding="utf-8")).strip()
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) < SAMPLES_MIN:
            continue
        text = text[:cut]
        if spent + len(text) > budget:
            break
        out.append((f.stem, text))
        spent += len(text)
    return out


def drafted(chat_id: int, theme_id: str, asset: str) -> None:
    """Черновик написан: путь к файлу и статус в базу."""
    with db.tx() as c:
        c.execute("UPDATE themes SET status = 'draft', asset = ?, "
                  "updated_at = datetime('now') WHERE id = ? AND chat_id = ?",
                  (asset, theme_id, chat_id))


# ── тема вне плана ────────────────────────────────────────────────────
#
# Слот в плане ставит Стратег, но не всякая работа приходит из плана.
# Человек снял дубль из головы или попросил пост по теме, которой в
# плане нет и не будет, — тогда тема заводится **по факту работы**, а не
# работа по теме. Первым так стал монтаж; дом у этого правила один,
# здесь: вторая копия INSERT разъехалась бы с первой на первой починке.
#
# Дата остаётся пустой намеренно: тема не из плана и чужой день занимать
# не должна. `src = 'adhoc'` отличает её от плановой навсегда — по нему
# видно, что слот под неё никто не ставил.

# Площадки, под которые в заводе есть холст, шаблоны и пути к артефактам.
# Список закрыт: `plat` уезжает в id темы и в имена файлов, и опечатка
# здесь заводит тему, которую потом не найдёт ни Дизайнер, ни Публикатор.
PLATS = ("telegram", "instagram", "youtube")

# Статусы, в которых тему заводят по факту. `idea` — работа впереди
# (текст ещё пишется), `ready` — работа уже сделана (ролик смонтирован).
ADHOC_STATUS = ("idea", "ready")


def next_id(chat_id: int, day: str, plat: str) -> str:
    """Свободный id темы на день. Формат тот же, что у плана."""
    n = 1
    while True:
        tid = f"{day}-{plat}-{n:02d}"
        if db.one("SELECT id FROM themes WHERE id = ? AND chat_id = ?",
                  tid, chat_id) is None:
            return tid
        n += 1


def adhoc(chat_id: int, *, plat: str, fmt: str, title: str, hook: str = "",
          why: str = "", rubric: str = "", status: str = "idea") -> dict[str, Any]:
    """Завести тему вне плана. Возвращает тему строкой базы.

    Отказ здесь громкий, а не тихий дефолт: тема с чужой площадкой или
    без заголовка доедет до Дизайнера холстом не того размера и до
    Публикатора комплектом без имени.
    """
    plat = (plat or "").strip().lower()
    if plat not in PLATS:
        raise NoWork(f"площадка «{plat or 'не названа'}» не из набора: "
                     + ", ".join(PLATS))
    if not (title := (title or "").strip()):
        raise NoWork("у темы вне плана нет заголовка: по нему её потом "
                     "ищет человек")
    if status not in ADHOC_STATUS:
        raise NoWork(f"статус «{status}» теме вне плана не ставится: "
                     + ", ".join(ADHOC_STATUS))

    tid = next_id(chat_id, today(chat_id), plat)
    with db.tx() as c:
        c.execute("INSERT INTO themes (id, chat_id, plat, format, rubric, "
                  "title, hook, why, src, status) "
                  "VALUES (?,?,?,?,?,?,?,?,'adhoc',?)",
                  (tid, chat_id, plat, (fmt or "").strip(), rubric.strip(),
                   title, hook.strip(), why.strip(), status))
    log.info("тема вне плана %s: %s / %s", tid, plat, fmt or "без формата")
    row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                 tid, chat_id)
    return dict(row) if row else {"id": tid, "chat_id": chat_id, "plat": plat,
                                  "format": fmt, "title": title, "hook": hook}


def brief(theme: dict[str, Any]) -> list[str]:
    """Тема списком «поле: значение». Пустые поля не показываем вовсе."""
    return [f"- {label}: {theme[key]}"
            for label, key in BRIEF_FIELDS if theme.get(key)]


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w{4,}", (text or "").lower())}


def pick(chat_id: int, ask: str, *, statuses: tuple[str, ...] = ("idea",),
         fresh: str = "idea", suits: Callable[[Any], bool] | None = None,
         wrong: str = "", empty: str = "", none: str = "") -> dict[str, Any]:
    """Какую тему брать.

    По убыванию точности: явный id в сообщении, совпадение по словам
    заголовка, ближайшая по дате.

    Названная по id тема берётся в любом из `statuses`, в том числе
    начатая: правка приходит к уже написанному тексту, и требовать от
    него `idea` значит не находить ровно то, что человек сейчас правит.
    Молча, без id, берётся только `fresh` — иначе просьба «напиши ещё
    один» переписывала бы вчерашнее.

    `suits` отсекает чужие темы: Редактору не нужны ролики, Редактору
    Reels не нужны посты. Отказ формулирует вызывающий, он же знает,
    к кому отправить человека.
    """
    rows = db.q(f"SELECT * FROM themes WHERE chat_id = ? AND status IN "
                f"({','.join('?' * len(statuses))}) ORDER BY date",
                chat_id, *statuses)

    if m := ID_RX.search(ask or ""):
        for r in rows:
            if r["id"] != m.group():
                continue
            if suits and not suits(r):
                raise NoWork(wrong.format(id=m.group(), format=r["format"]
                                          or "без формата"))
            return dict(r)
        raise NoWork(none.format(id=m.group()))

    mine = [r for r in rows if r["status"] == fresh
            and (suits is None or suits(r))]
    if not mine:
        raise NoWork(empty)

    if ask_words := words(ask):
        best, hits = None, 0
        for r in mine:
            n = len(ask_words & words(r["title"]))
            if n > hits:
                best, hits = r, n
        if best is not None and hits >= 2:
            return dict(best)

    return dict(mine[0])


class NoWork(RuntimeError):
    """Подходящей темы нет. Текст сообщения роль пишет сама."""


def ready(chat_id: int, theme_id: str) -> None:
    """Тема принята человеком."""
    with db.tx() as c:
        c.execute("UPDATE themes SET status = 'ready', "
                  "updated_at = datetime('now') WHERE id = ? AND chat_id = ?",
                  (theme_id, chat_id))


def reason(e: Exception) -> str:
    """Человеческая причина отказа вместо имени класса.

    Одна точка на все роли: разбор ошибок API живёт в `agent.reason`,
    иначе «закончились средства» у Редактора и у Ресёрчера выглядели бы
    по-разному, а у одного из них дампом JSON.
    """
    from orchestrator import agent            # локально: agent тяжелее desk
    return agent.reason(e)


CORRECTIONS_TAIL = 12         # сколько последних правок едет в промпт


class Desk:
    """Что роль помнит между сообщениями: последнее сделанное и ждёт ли правку.

    Один стол на роль. Результат живёт в памяти процесса, а процесс
    перезапускается, поэтому у стола есть `recover`: поднять сделанное
    из базы по id из кнопки. Поднятое кладётся обратно на стол — иначе
    кнопка под пережившей рестарт карточкой попросит правку, а следующее
    сообщение человека упрётся в «уже неактуален».
    """

    def __init__(self, role: str, *, corrections: str,
                 recover: Callable[[int, str], Any] | None = None) -> None:
        self.role = role
        self.corrections = corrections
        self._recover = recover
        self._items: dict[int, Any] = {}
        self._fix: set[int] = set()

    def hold(self, chat_id: int, item: Any) -> None:
        self._items[chat_id] = item
        self._fix.discard(chat_id)

    def get(self, chat_id: int, theme_id: str = "") -> Any:
        """Сделанное по этому чату. Кнопка называет тему, память может врать."""
        item = self._items.get(chat_id)
        if item is not None and theme_id and item.theme["id"] != theme_id:
            item = None                    # нажали под старой карточкой
        if item is None and theme_id and self._recover:
            item = self._recover(chat_id, theme_id)
        return item

    def take(self, chat_id: int, theme_id: str = "") -> Any:
        """То же, но со стола убирает: работа закончена."""
        item = self.get(chat_id, theme_id)
        self._items.pop(chat_id, None)
        self._fix.discard(chat_id)
        return item

    def wants_fix(self, chat_id: int) -> bool:
        return chat_id in self._fix

    def await_fix(self, chat_id: int, item: Any) -> None:
        self._items[chat_id] = item
        self._fix.add(chat_id)

    def clear(self, chat_id: int | None = None) -> None:
        if chat_id is None:
            self._items.clear()
            self._fix.clear()
            return
        self._items.pop(chat_id, None)
        self._fix.discard(chat_id)

    def note(self, chat_id: int, theme_id: str, instruction: str) -> None:
        """Правка человека это обучающий сигнал, а не разовая просьба."""
        b = brand(chat_id)
        if b is not None:
            b.append(self.corrections, f"- {theme_id}: {instruction.strip()}")

    def learned(self, b, limit: int = CORRECTIONS_TAIL) -> list[str]:
        """Правки, которые человек уже называл этой роли.

        Обучающим сигналом их зовёт и `note` выше, и адаптер субагента, —
        но на прямом пути их не читал никто: файл рос и оставался
        архивом. Повторить правку, которую человеку уже пришлось
        сказать, хуже, чем не угадать с первого раза.

        Хвост, а не файл целиком. Правки копятся без срока годности, и
        стабильная часть промпта росла бы вместе с ними на каждом вызове;
        свежие при этом говорят о голосе больше старых.
        """
        lines = (b.read(self.corrections) or "").splitlines()
        return [ln.strip()[2:].strip() for ln in lines
                if ln.strip().startswith("- ") and ln.strip()[2:].strip()
                ][-limit:]


# ── карусель: карточки и подпись ──────────────────────────────────────
#
# Карусель это два текста в одном файле: шесть блоков на карточки и
# подпись под пост. Разделяет их одна строка, и она обязана быть
# договорённостью, а не выдумкой круга.
#
# Первый же живой прогон 05.09 показал, чем кончается её отсутствие:
# модель поставила «Подпись:» от себя и написала в `notes`, что в
# публикацию эта строка не идёт. То есть шов существовал, но знала о нём
# только модель — а Дизайнеру уезжал весь текст целиком, и слова подписи
# могли лечь на карточку.
#
# Дом у разбора здесь, а не у Редактора: пишет текст он, а разбирает
# Дизайнер, и импортировать Редактора Дизайнеру нельзя — Редактор сам
# импортирует Дизайнера, чтобы отдать текст кнопкой «В дизайн».

CAPTION_MARK = "## Подпись"


def split_caption(text: str) -> tuple[str, str]:
    """Карточные блоки и подпись под ними. Метки нет — всё это блоки."""
    head, mark, tail = (text or "").partition(CAPTION_MARK)
    return head.strip(), tail.strip() if mark else ""
