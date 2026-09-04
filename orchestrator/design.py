"""Дизайнер: из готового текста в макет.

Вход — тема со статусом `ready` и утверждённый текст Редактора.
Выход — HTML и PNG в `posts/`, оба обязательны.

Отличие от прочих ролей: спека прямо запрещает Дизайнеру собирать на
глаз. Нет ТЗ площадки — нет макета, и это не отговорка, а требование.
Поэтому `platforms/{площадка}.md` в папке бренда проверяется до вызова
модели, и при его отсутствии предлагается завести ТЗ, а не рисуется
что-нибудь похожее.

Рендер — headless Chrome, без новых зависимостей. Макет пишется в папку
бренда и оттуда же рендерится: пути внутри него относительные, поэтому
папку можно отдать клиенту, и всё откроется.
"""
from __future__ import annotations

import asyncio
import logging
import html as _html
import json as _json
import re
import shutil
import subprocess
import tempfile as _tmp
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ROOT, cfg
from orchestrator import agent, desk, imagegen, imagery, publisher, stock
from orchestrator.desk import NoWork
from storage import db

log = logging.getLogger("design")

PACK = ROOT / "design-pack"
MAX_TOKENS = 16000

# Усилие точечной правки. Полная сборка держит усилие роли из `config`,
# а правка по готовому HTML — работа механическая: найти место и заменить.
# Рассуждать тут не над чем, а каждый лишний токен мышления это секунды.
PATCH_EFFORT = "low"
RENDER_TIMEOUT = 45

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)

# Холсты жёсткие, по спеке. Не спрашиваются и не обсуждаются.
#
# Матрица полная: у каждого формата из набора Стратега
# (`strategy.PLATFORMS`) здесь своя строка. Это не педантизм — без своей
# строки формат сваливался на `(площадка, None)` **молча**: сторис
# уезжала в рендер 4:5, а shorts горизонталью. Ловил это человек глазами
# на готовом PNG, то есть в самом конце.
#
# `(площадка, None)` остаётся запасной строкой для темы без формата.
CANVAS = {
    ("telegram", "пост"):      (1080, 1350),
    ("telegram", "анонс"):     (1080, 1350),
    ("telegram", "раздача"):   (1080, 1350),
    ("telegram", "опрос"):     (1080, 1350),
    ("telegram", None):        (1080, 1350),
    ("instagram", "карусель"): (1080, 1350),
    ("instagram", "reels"):    (1080, 1920),
    ("instagram", "сторис"):   (1080, 1920),
    ("instagram", None):       (1080, 1350),
    ("youtube", "видео"):      (1280, 720),
    ("youtube", "shorts"):     (1080, 1920),
    ("youtube", None):         (1280, 720),
}

# Латинское имя формата для путей. Имя формата в базе русское, а файлы
# ТЗ и шаблонов уезжают клиенту в папке бренда и в шаблон-пак продукта,
# где всё остальное латиницей.
SLUG = {
    "пост": "post", "анонс": "announce", "раздача": "giveaway",
    "опрос": "poll", "карусель": "carousel", "reels": "reels",
    "сторис": "stories", "видео": "video", "shorts": "shorts",
}

# Эталон под площадку и формат. Клонируется, а не верстается заново.
#
# Нужен только там, где разметку пишет модель. У формата с шаблоном
# эталона нет и быть не должно: он приглашал бы переизобрести шаблон,
# который модель всё равно не видит.
REFERENCE = {
    ("instagram", "карусель"): ["carousel-01-cover.html", "carousel-02-context.html",
                                "carousel-03-point.html",
                                "carousel-04-point-mirror.html",
                                "carousel-05-final.html"],
}

# Сколько карточек в комплекте. Раньше это число было длиной списка
# эталонов, и карусель собиралась пятью карточками. Считать карточки по
# паттернам — ошибка: паттернов пять, а карточек шесть, потому что пункт
# повторяется трижды, меняя сторону. Число снято с живой карусели бренда
# (`design/assets/reference/ig/`), где в подвале стоит «03 / 06», а не с
# головы: карусель длиннее шести карточек человек не долистывает.
CARDS = {
    ("instagram", "карусель"): 6,
}

# Шаблоны со слотами. Здесь разметку собирает код, а модель заполняет
# дырки — см. `_fill`. Площадка без шаблона идёт старым путём, где модель
# пишет HTML целиком: переезд делается по одной площадке за раз.
TEMPLATES = {
    ("telegram", "пост"):    ["telegram-post.html"],
    ("telegram", "анонс"):   ["telegram-announce.html"],
    ("telegram", "раздача"): ["telegram-giveaway.html"],
    ("telegram", "опрос"):   ["telegram-poll.html"],
    ("telegram", None):      ["telegram-post.html"],
    ("instagram", "reels"):  ["instagram-reels.html"],
    ("instagram", "сторис"): ["instagram-stories.html"],
    ("youtube", "видео"):    ["youtube-video.html"],
    ("youtube", "shorts"):   ["youtube-shorts.html"],
}

# Слоты и потолки знаков. Потолок — не каприз: заголовок длиннее вылезет
# за холст, и поймает это уже человек глазами на PNG.
#
# `photo` особый: значение проверяется по списку файлов бренда, а не по
# длине. `headline_size` в этот словарь не входит вовсе — его считает код
# (`_fit`), и модель о нём не знает. Подбор кегля это арифметика по числу
# знаков, а не суждение: модель может только промахнуться мимо пикселей.
SLOTS: dict[str, tuple[str, int]] = {
    "rubric":          ("рубрика над заголовком, капсом", 28),
    "headline":        ("заголовок; слова только из текста Редактора. Одно "
                        "слово можно выделить акцентом, обернув в звёздочки: "
                        "«Сайт в *Claude*: чат и форма». Звёздочки в потолок "
                        "знаков не считаются", 52),
    "headline_accent": ("хвост заголовка акцентным цветом, можно пустым", 24),
    "subtitle":        ("подзаголовок одной фразой", 120),
    "photo":           ("имя файла из списка доступных фото", 0),
    "handle":          ("подпись бренда на макете, ставит код", 32),
}

# Кто какой слот заполняет. Модели остаётся то, что кодом не решается:
# какие слова из поста попадут на обложку и где акцент.
#
# `rubric` её работой не была никогда: рубрику назначил Стратег, она
# лежит в теме, и модель дважды из двух вернула ровно её же капсом.
# `photo` выбирается правилом бренда (`design/photos.md`) плюс ротацией
# по последним обложкам — а истории прошлых макетов модель не видит и
# повтор фото через день заметить не может в принципе.
MODEL_SLOTS = ("headline", "headline_accent", "subtitle")

# На правке фото возвращается модели: «поставь другое фото» — просьба
# человека, и отвечать на неё ротацией нельзя.
PATCH_SLOTS = MODEL_SLOTS + ("photo",)

# Правило выбора фото. Лежит у бренда, а не в коде продукта: `cover-red`
# и `speaking` — имена файлов одного клиента, у следующего их нет.
PHOTO_RULES = "design/photos.md"

# Подпись бренда на макете. Лежит у бренда, а не в коде продукта, и не в
# ТЗ: ТЗ читает модель, а подпись — работа кода. Ошибка в ней стоит
# дорого: по подписи человек ищет аккаунт, и промах означает, что он его
# не найдёт.
MARK_FILE = "design/mark.md"
MARK_RX = re.compile(r"^Подпись на макете:\s*(.+?)\s*$", re.M)
STOCK = "stock-"      # префикс файлов со стока, см. orchestrator/stock.py
GEN = imagegen.PREFIX  # префикс сгенерированного, см. orchestrator/imagegen.py

# Ни сток, ни генерация не попадают на макет как «своё фото»: код не
# ставит их молча ни ротацией, ни правилом «*». Выбрать их можно только
# кнопкой человека или вписав имя файла в `design/photos.md` руками.
NOT_OWN = (STOCK, GEN)

# Рубрики, где фон генерируется, а не снимается. Список у кода, а не у
# бренда, потому что это правило продукта: «Разбор ошибки» — это чужая
# поломка, «Артефакт в ленте» — чужой промпт, и портрет автора в кадре
# обещает не то, что стоит в посте. Своя съёмка при этом не отменяется:
# генерация идёт первым вариантом из трёх, остальные два свои.
GEN_RUBRICS = ("разбор ошибки", "артефакт в ленте")

# Усилие на бриф для генерации. Это перевод темы в описание кадра, а не
# суждение: какие рубрики генерируются, решено выше и не обсуждается.
GEN_BRIEF_EFFORT = "low"

# Сколько фонов показать человеку до вёрстки. Три — потому что выбор из
# двух это «да/нет», а из пяти уже работа: человек листает вместо того,
# чтобы решить.
BG_CHOICES = 3

# Выбор фона переживает перезапуск бота: кнопка несёт id темы, а сами
# кандидаты лежат рядом с макетом. Стоковый вариант иначе не восстановить
# — выдача Pexels на тот же запрос приходит другой.
BG_FILE = "posts/{id}.bg.json"

# Усилие на ключевые слова для стока. Это перевод темы в три-четыре
# английских слова, а не суждение.
KEYWORDS_EFFORT = "low"
RULE_RX = re.compile(r"^\|([^|]+)\|([^|]+)\|\s*$", re.M)

# Хвост ТЗ, который модели не едет. Ниже этой строки в ТЗ живёт описание
# слоёв и пикселей: на переехавшей площадке разметку собирает код, и
# модели этот раздел только предлагает рассуждать о том, чего она не
# решает. Площадка без шаблона маркер не ставит — там ТЗ нужно целиком.
FOR_HUMAN = "<!-- дальше не для модели -->"


def _schema(keys: tuple[str, ...]) -> dict[str, Any]:
    """Форма ответа для шаблонного пути.

    Раньше схемы тут не было намеренно: слоты были открытым словарём, и
    закрыть его было нечем. Теперь набор слотов известен коду до вызова,
    поэтому форма гарантируется, а не выпрашивается словами промпта.
    """
    return {
        "type": "object",
        "properties": {
            "theme_id": {"type": "string"},
            "cards": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "slots": {
                        "type": "object",
                        "properties": {k: {"type": "string"} for k in keys},
                        "required": list(keys),
                        "additionalProperties": False,
                    },
                },
                "required": ["name", "slots"],
                "additionalProperties": False,
            }},
            "accent": {"type": "string"},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["theme_id", "cards", "accent", "notes"],
        "additionalProperties": False,
    }


TEMPLATE_DIR = ROOT / "design-pack" / "templates"
SLOT_RX = re.compile(r"\{\{([a-z_]+)\}\}")

ABSOLUTE = re.compile(r'(?:href|src)\s*=\s*["\'](?:file://|/|[a-z]+://)', re.I)
LITERAL_COLOR = re.compile(r":\s*#[0-9A-Fa-f]{3,8}\b")
TAGS = re.compile(r"<[^>]+>")


class NoSpec(RuntimeError):
    """ТЗ площадки нет. Собирать на глаз запрещено."""


class NoRenderer(RuntimeError):
    """Нечем рендерить PNG."""


@dataclass
class Layout:
    theme: dict[str, Any]
    cards: list[dict[str, str]] = field(default_factory=list)
    accent: str = ""
    notes: list[str] = field(default_factory=list)      # словами от роли
    findings: list[str] = field(default_factory=list)   # поймано кодом
    files: list[Path] = field(default_factory=list)     # html и png вперемешку

    @property
    def pngs(self) -> list[Path]:
        return [f for f in self.files if f.suffix == ".png"]

    @property
    def htmls(self) -> list[Path]:
        return [f for f in self.files if f.suffix == ".html"]


def _key(plat: str, fmt: str) -> tuple[str, str | None]:
    fmt = (fmt or "").lower()
    return (plat, fmt) if (plat, fmt) in CANVAS or (plat, fmt) in REFERENCE \
        else (plat, None)


def _known(plat: str, fmt: str) -> None:
    """Формат вне набора площадки — отказ, а не тихий дефолт.

    Стратег формат вне набора не выбрасывает: смысл в теме есть, и он
    ставит её со строкой «проверь». Дизайнеру же сваливаться на холст
    площадки нельзя — он молча отдаст 4:5 там, где нужна вертикаль, и
    увидит это человек уже на PNG. Лучше сказать словами и не верстать.
    """
    if not fmt or (plat, fmt) in CANVAS:
        return
    known = sorted(f for pl, f in CANVAS if pl == plat and f)
    raise NoWork(f"формат «{fmt}» не из набора {plat}, холста под него "
                 f"нет. Набор площадки: {', '.join(known) or 'пуст'}")


def _cards(plat: str, fmt: str, tpls: list, refs: list) -> int:
    """Сколько карточек в комплекте.

    На шаблонном пути это число шаблонов: карточка это шаблон. На
    свободном — `CARDS`, а не длина списка эталонов: эталонов столько,
    сколько паттернов, а карточек больше.
    """
    if tpls:
        return len(tpls)
    return CARDS.get(_key(plat, fmt)) or len(refs)


def chrome() -> str:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    raise NoRenderer("не нашёл Chrome или Chromium для рендера PNG")


# ── вход ──────────────────────────────────────────────────────────────

def _pick(chat_id: int, ask: str) -> dict[str, Any]:
    """Тема с утверждённым текстом. Верстать черновик смысла нет."""
    return desk.pick(
        chat_id, ask, statuses=("ready",), fresh="ready",
        suits=lambda r: bool(r["asset"]),
        wrong="у темы {id} нет утверждённого текста",
        none="темы {id} нет среди утверждённых",
        empty="нет ни одного утверждённого текста")


def _copy(b, theme: dict[str, Any]) -> str:
    """Утверждённый текст без служебной шапки Редактора."""
    raw = b.read(theme["asset"])
    if not raw.strip():
        raise NoWork(f"файл {theme['asset']} пуст")
    return raw.split("-->", 1)[-1].strip() if raw.startswith("<!--") else raw.strip()


def _mark(b) -> str:
    """Подпись бренда для макета. Нет файла — нет подписи, и это не сбой."""
    found = MARK_RX.search(b.read(MARK_FILE) or "")
    return found.group(1).strip() if found else ""


def _photos(b) -> list[str]:
    folder = b.path("design/assets/images")
    if not folder.is_dir():
        return []
    return sorted(f.name for f in folder.iterdir()
                  if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})


# Длинная сторона, ниже которой фон поедет мылом. Холст 1080×1920
# снимается с `--force-device-scale-factor=2`, то есть 2160×3840, и
# телеграмное сжатие фото (1280 по длинной стороне) до него не дотягивает
# вдвое. Файл всё равно берём — но человеку про это говорим, пока он ещё
# у телефона и может переслать файлом.
PHOTO_MIN = 1600

# Сколько знаков подписи берём в имя файла. Имя уезжает в `photos.md` и в
# HTML макета, и «rabochee-mesto-utrom-kogda-eshe-nikto-ne-pishet» там
# читать невозможно.
PHOTO_NAME = 40


@dataclass(frozen=True)
class Stashed:
    """Что стало с присланным кадром. Читает обработчик, чтобы ответить."""
    name: str            # имя файла в папке бренда
    side: int            # длинная сторона исходника, точки
    seen: bool           # это фото уже лежало в банке
    total: int           # сколько фото в банке теперь


def stash_photo(b, blob: bytes, *, name: str = "", key: str = "",
                suffix: str = ".jpg") -> Stashed:
    """Присланное фото в фотобанк бренда.

    Фотобанк пополнялся только из альбома «Фото» (`tools/photos_pull.py`),
    то есть с ноутбука и руками. Топик 📸 Фотобанк при этом висел в
    структуре с первого дня и не делал ничего: снятое на телефон человек
    кидал в чат, а оно уходило перечитывать профиль.

    `key` — id файла у отправителя. По нему присланное дважды не ложится
    дублем: один и тот же кадр приезжает в чат по второму разу чаще, чем
    кажется, а ротация фона на дублях слепнет.
    """
    images = b.path("design/assets/images")
    images.mkdir(parents=True, exist_ok=True)

    index = imagery.index_read(images)
    if key and (images / index.get(key, "")).is_file():
        got = index[key]
        return Stashed(got, max(imagery.measure(images / got)), True,
                       len(_photos(b)))

    # Без подписи имя будет `photo.jpg`, `photo-02.jpg` и так далее.
    # Человек переименует их сам, когда будет раскладывать по целям в
    # `photos.md`: угадывать сюжет по байтам код не умеет.
    base = imagery.slug(name)[:PHOTO_NAME].strip("-") or "photo"
    fname = imagery.free_name(images, base)

    with _tmp.TemporaryDirectory(prefix="photo-") as tmp:
        raw = Path(tmp) / f"raw{suffix or '.jpg'}"
        raw.write_bytes(blob)
        try:
            side = max(imagery.measure(raw))
            imagery.convert(raw, Path(tmp) / fname)
        except subprocess.CalledProcessError as e:
            # Не картинка или битые байты. Это ответ человеку, а не сбой
            # завода: он прислал файл и ждёт строки о том, что с ним.
            raise NoWork("файл не открылся как изображение: "
                         f"{e.stderr.decode()[:120].strip()}") from e
        (images / fname).write_bytes((Path(tmp) / fname).read_bytes())

    if key:
        index[key] = fname
        imagery.index_write(images, index)
    log.info("фотобанк пополнен: %s (%s px)", fname, side)
    return Stashed(fname, side, False, len(_photos(b)))


def _photo_rules(b) -> dict[str, list[str]]:
    """Какое фото под какую цель поста. Таблица бренда, а не константа.

    Файла нет — правила нет, и фото идёт ротацией по всей папке. Это
    хуже подбора по смыслу, но предсказуемо и не выдумывает: у нового
    клиента в папке может лежать что угодно.
    """
    text = b.read(PHOTO_RULES)
    out: dict[str, list[str]] = {}
    for goal, files in RULE_RX.findall(text or ""):
        key = goal.strip().lower()
        if key in ("цель", "---", ":---", "---:"):
            continue
        names = [f.strip() for f in files.split(",") if f.strip()]
        if key and names:
            out[key] = names
    return out


def _recent_photos(b, keep: int) -> list[str]:
    """Фото последних обложек, свежие первыми. Против повтора через день."""
    if keep <= 0:
        return []
    folder = b.path("posts")
    if not folder.is_dir():
        return []
    seen: list[str] = []
    for f in sorted(folder.glob("*.slots.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            name = str(_json.loads(f.read_text(encoding="utf-8")).get("photo"))
        except (ValueError, OSError):
            continue
        if name and name not in seen:
            seen.append(name)
        if len(seen) >= keep:
            break
    return seen


def _pick_photo(b, theme: dict[str, Any], photos: list[str]) -> str:
    """Фото под тему: правило бренда плюс ротация."""
    if not photos:
        raise NoWork("в папке бренда нет ни одного фото")
    rules = _photo_rules(b)
    goal = str(theme.get("goal") or "").strip().lower()
    order = [p for p in (rules.get(goal) or rules.get("*") or []) if p in photos]
    if not order:
        # Запасная ротация идёт только по своим фото. Сток
        # (`tools/stock_pull.py`, префикс `stock-`) и сгенерированное
        # (`imagegen`, префикс `gen-`) попадают на макет, только если
        # человек вписал имя файла в правило руками: безликая картинка
        # на обложке личного бренда читается как AI-контент, и выбрать
        # её молча код не должен.
        order = [p for p in photos if not p.startswith(NOT_OWN)] or photos
    recent = _recent_photos(b, len(order) - 1)
    return next((p for p in order if p not in recent), order[0])


def _derive(b, theme: dict[str, Any], photos: list[str],
            photo: str = "") -> dict[str, str]:
    """Слоты, которые заполняет код. Модель о них не знает.

    `photo` — фон, который человек утвердил кнопкой до вёрстки. Пусто —
    выбирает код правилом бренда, как было до 03.09: так работают
    пересборка правки и любой путь, где выбора не показывали.
    """
    rubric = str(theme.get("rubric") or "").strip()
    if not rubric:
        raise NoWork(f"у темы {theme['id']} нет рубрики: её ставит Стратег")
    if photo and photo not in photos:
        raise NoWork(f"фото «{photo}» нет в папке бренда")
    return {"rubric": rubric.upper(),
            "photo": photo or _pick_photo(b, theme, photos),
            "handle": _mark(b)}


def _spec(b, plat: str, fmt: str = "") -> str:
    """ТЗ формата, а нет его — ТЗ площадки.

    Формат сюда приехал 03.09. До этого ТЗ читалось только по площадке,
    и Дизайнер, верставший обложку рилса, получал в промпт рецепт
    карусели: единственным файлом Instagram был `instagram.md`. Ошибка
    тихая — ответ приходил нормальный, просто не по тому рецепту.

    Имя файла собирается латиницей (`SLUG`), как у монтажа
    (`montage._cover_spec`, `{площадка}-{формат}-cover.md`). Файлы
    разные и путать их нельзя: у монтажа ТЗ обложки, которую код
    собирает из кадра дубля, здесь — ТЗ макета, который верстает
    Дизайнер.
    """
    tried = [f"design/platforms/{plat}-{SLUG[fmt]}.md"] if fmt in SLUG else []
    tried.append(f"design/platforms/{plat}.md")
    for rel in tried:
        text = b.read(rel)
        if text.strip():
            return text.split(FOR_HUMAN)[0].strip()
    raise NoSpec(" или ".join(tried))


# ── референсы бренда ──────────────────────────────────────────────────
#
# Две разные вещи носят в этом проекте одно слово, и путать их дорого.
#
# `_reference()` ниже — **эталон разметки**: HTML из `design-pack/slides`,
# он едет в промпт и его клонируют. Общий для всех тенантов.
#
# `brand_refs()` здесь — **живые скрины настоящих постов бренда**, картинки
# в папке тенанта. Их не клонируют, по ним сверяют: так это выглядит в
# ленте на самом деле. До сегодня папка была мёртвой — заведена руками,
# лежала с картинками, и ни одна строка кода её не открывала. Имя
# `design/assets/reference/ig/` встречалось в исходниках ровно один раз, и
# то в комментарии.
#
# Читать картинку умеет не всякий путь. Субагент Дизайнера на пути моста
# открывает файл сам и видит его глазами — ему довольно пути. Прямой путь
# роли идёт одним вызовом без инструментов, и картинку туда не передать:
# там референс может только называться, и врать об этом не надо.

# Формат называется точно там, где точность важна: по этой карте и
# `exact=True` решается, есть ли референс **под этот формат**, а не «хоть
# какой-то у этой площадки».
REF_DIRS: dict[tuple[str, str | None], str] = {
    ("instagram", "карусель"): "ig",
    ("instagram", "reels"):    "reels",
    ("instagram", None):       "ig",
    ("telegram", "пост"):      "tg",
    ("telegram", None):        "tg",
}

REF_SUFFIX = (".png", ".jpg", ".jpeg", ".webp")


def refs_dir(b, plat: str, fmt: str, *, exact: bool = False) -> Path | None:
    """Папка референсов под площадку и формат. `None` — папки под неё нет.

    `exact` запрещает падать на площадку целиком. Разница не
    косметическая: у `telegram` референс это скрин **поста**, и подставить
    его обложке рилса значит сказать «сверить есть с чем» про картинку
    другого формата. Один раз так и вышло на живом бренде.
    """
    # Ключ берётся сырой, а не через `_key`: тот сваливает незнакомый
    # формат на площадку целиком, и `exact` перестал бы что-либо значить —
    # («telegram», «reels») превращалось в («telegram», None) и находило
    # папку со скринами постов.
    name = REF_DIRS.get((plat, (fmt or "").lower()))
    if name is None and not exact:
        name = REF_DIRS.get((plat, None))
    return b.path(f"design/assets/reference/{name}") if name else None


def brand_refs(b, plat: str, fmt: str, *, exact: bool = False) -> list[Path]:
    """Скрины настоящих постов бренда под эту площадку и формат.

    Пусто — это результат, а не поломка: папки может не быть, она может
    быть заведена и пуста. Разницу между «нет папки» и «папка пуста»
    называет тот, кто зовёт: и то и другое значит «сверить не с чем».
    """
    d = refs_dir(b, plat, fmt, exact=exact)
    if d is None or not d.is_dir():
        return []
    return sorted(f for f in d.iterdir()
                  if f.is_file() and f.suffix.lower() in REF_SUFFIX)


def _reference(plat: str, fmt: str) -> list[tuple[str, str]]:
    names = REFERENCE.get(_key(plat, fmt)) or REFERENCE.get((plat, None)) or []
    out = []
    for name in names:
        path = PACK / "slides" / name
        if path.exists():
            out.append((name, path.read_text(encoding="utf-8")))
    return out


# ── проверки кодом ────────────────────────────────────────────────────

def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"\w{4,}", text)}


# ── шаблоны со слотами ────────────────────────────────────────────────

def _templates(plat: str, fmt: str) -> list[tuple[str, str]]:
    """Шаблоны под площадку и формат. Пусто — площадка ещё не переехала."""
    names = TEMPLATES.get(_key(plat, fmt)) or []
    out = []
    for n in names:
        f = TEMPLATE_DIR / n
        if f.exists():
            out.append((f.stem, f.read_text(encoding="utf-8")))
    return out


def _fit(text: str) -> int:
    """Кегль заголовка по числу знаков. Считает код, а не модель.

    Модель не видит холста и не умеет мерить текст — она может только
    назвать число и промахнуться, а вылезший за край заголовок замечает
    уже человек на PNG. Здесь же это простая арифметика: чем длиннее
    строка, тем мельче кегль, ступенями от эталонных 104px.

    Ступени, а не формула: между 104 и 96 разницы на глаз нет, а
    предсказуемость макета дороже точности подгонки.
    """
    n = len(text.strip())
    for limit, size in ((22, 104), (34, 88), (46, 72), (60, 60)):
        if n <= limit:
            return size
    return 52


# Акцент внутри заголовка: `*слово*`. Хвостом (`headline_accent`) он
# ставится только в конце фразы, а на эталоне бренда подсвечено слово в
# середине — «Сайт в *Claude*: чат, форма и аналитика». Разметка тут
# минимальная нарочно: всё остальное в значении экранируется, и `<span>`
# остаётся единственным тегом, который слот может принести.
ACCENT_RX = re.compile(r"\*([^*\n]{1,40})\*")


def _accented(value: str) -> str:
    """Экранированный заголовок со звёздочками → заголовок с акцентом.

    Цвет берётся из `--accent-headline`, который объявляет сам шаблон:
    на тёмном холсте это чистый акцент, на светлом — тёмный его оттенок,
    иначе слово пропадает. Запасной вариант — `--accent`, чтобы шаблон
    без объявления не остался вовсе без цвета.
    """
    return ACCENT_RX.sub(
        r'<span style="color:var(--accent-headline,var(--accent));">\1</span>',
        _html.escape(value, quote=True)).replace("\n", "<br>")


def _fill(tpl: str, slots: dict[str, str], photos: list[str]) -> str:
    """Собрать HTML из шаблона. Значения экранируются, а не доверяются.

    Это то место, ради которого затевался переезд со свободного HTML.
    Четыре проверки из `inspect` здесь становятся невозможными по
    устройству: холст записан в шаблоне, цвета — токенами, путь к фото
    собирает код, а имя файла сверяется со списком бренда.

    Незнакомый слот и пропущенный слот — отказ, а не тихая дырка в
    макете: `{{headline}}`, доехавший до PNG как есть, выглядит рабочим.
    """
    need = set(SLOT_RX.findall(tpl)) - {"headline_size"}
    unknown = set(slots) - need
    if unknown:
        raise NoWork("лишние слоты: " + ", ".join(sorted(unknown)))

    photo = (slots.get("photo") or "").strip()
    if "photo" in need and photo not in photos:
        raise NoWork(f"фото «{photo or '—'}» нет в папке бренда")

    for name in need:
        val = (slots.get(name) or "").strip()
        limit = SLOTS.get(name, ("", 0))[1]
        # Звёздочки акцента в потолок не считаются: они разметка, а
        # человек на макете видит слово без них.
        if name == "headline":
            val_len = len(val.replace("*", ""))
        else:
            val_len = len(val)
        if limit and val_len > limit:
            raise NoWork(f"слот «{name}» длиннее {limit} знаков: {val_len}")
        # Пустыми бывают двое: хвост акцента (его может не быть) и
        # подпись бренда (её может не быть у бренда вовсе).
        if not val and name not in ("headline_accent", "handle"):
            raise NoWork(f"слот «{name}» пустой")

    head = (slots.get("headline") or "") + (slots.get("headline_accent") or "")
    ready = dict(slots, headline_size=str(_fit(head)))

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        val = ready.get(name, "")
        # Кегль это число от кода, экранировать нечего; текст от модели —
        # всегда экранируется: одна кавычка в заголовке иначе рвёт стиль.
        if name == "headline_size":
            return val
        if name == "headline":
            return _accented(val)
        # Перевод строки в значении — это разметка, но единственная,
        # которую слот вправе принести: у анонса подзаголовок это «когда,
        # где, сколько», и в одну строку они не встают. Экранирование
        # идёт до подстановки `<br>`, поэтому тег остаётся единственным.
        return _html.escape(val, quote=True).replace("\n", "<br>")

    return SLOT_RX.sub(sub, tpl)


def _cards_from_slots(data: dict[str, Any], tpls: list[tuple[str, str]],
                      photos: list[str],
                      fixed: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Ответ модели со слотами → карточки с готовым HTML.

    Шаблон берётся по порядку, а не по имени из ответа: имён шаблонов
    модель не знает и знать не должна, иначе она сможет выбрать не тот.

    `fixed` — слоты от кода. Они кладутся поверх ответа: если модель по
    старой памяти вернула рубрику или фото, побеждает код, а не она.
    """
    out: list[dict[str, str]] = []
    got = [c for c in (data.get("cards") or []) if isinstance(c, dict)]
    for i, (stem, tpl) in enumerate(tpls):
        if i >= len(got):
            raise NoWork(f"не заполнены слоты карточки {i + 1} из {len(tpls)}")
        slots = got[i].get("slots")
        if not isinstance(slots, dict):
            raise NoWork(f"карточка {i + 1}: слотов нет, а разметку "
                         "здесь пишет код")
        name = re.sub(r"[^a-z0-9-]", "",
                      str(got[i].get("name") or "").lower()) or stem
        # Слоты этого шаблона. Карточка карусели без фото не должна
        # падать от того, что `fixed` принёс фото для обложки, — но
        # выбрасывается только слот, известный коду. Выдуманный доезжает
        # до `_fill` и получает отказ, как и раньше.
        need = set(SLOT_RX.findall(tpl)) - {"headline_size"}
        clean = {k: str(v) for k, v in slots.items()
                 if k not in (fixed or {}) and (k in need or k not in SLOTS)}
        clean.update({k: v for k, v in (fixed or {}).items() if k in need})
        out.append({"name": name, "slots": clean,
                    "html": _fill(tpl, clean, photos)})
    return out


def _slots_stable(spec: str, names: tuple[str, ...]) -> str:
    """Кешируемый блок для шаблонного пути: ТЗ плюс описание слотов.

    Сам шаблон модели не показывается. Она его не пишет и не правит, а
    лишние двадцать строк разметки в контексте только приглашают вернуть
    HTML вместо значений.
    """
    return "\n".join(["## ТЗ площадки", "", spec, "", _slot_brief(names)])


def _slot_brief(names: tuple[str, ...]) -> str:
    """Описание слотов для модели. Едет в кешируемый блок вместе с ТЗ."""
    lines = ["## Слоты, которые ты заполняешь", "",
             "Разметку собирает код. Ты возвращаешь только значения.", ""]
    for name in names:
        what, limit = SLOTS[name]
        cap = f", не длиннее {limit} знаков" if limit else ""
        lines.append(f"- `{name}` — {what}{cap}")
    lines += ["", "Больше в ответе нет ничего. Холст, цвета, кегль, рубрику "
              "и путь к фото ставит код."]
    return "\n".join(lines)


def inspect(html: str, copy: str, size: tuple[int, int],
            photos: list[str]) -> list[str]:
    """Что проверяется кодом, а не доверием к промпту.

    Абсолютный путь и несуществующее фото ломают макет молча: у человека
    он откроется пустым прямоугольником, а у нас отрендерится нормально,
    потому что файл лежит рядом именно здесь.
    """
    out: list[str] = []

    if ABSOLUTE.search(html):
        frag = ABSOLUTE.search(html).group()
        out.append(f"абсолютный путь в макете: {frag}")

    for src in re.findall(r'src\s*=\s*["\']([^"\']+)["\']', html):
        if src.startswith("data:"):
            continue
        name = src.rsplit("/", 1)[-1]
        if name and name not in photos and "assets" in src:
            out.append(f"фото {name} нет в папке бренда")

    w, h = size
    if not re.search(rf"width:\s*{w}px", html) or \
       not re.search(rf"height:\s*{h}px", html):
        out.append(f"холст не заявлен как {w}×{h}")

    if len(LITERAL_COLOR.findall(html)) > 6:
        out.append("много литеральных цветов вместо переменных")

    # Слова с макета, которых нет в утверждённом тексте. Рубрика и подпись
    # бренда приходят из ТЗ, поэтому это предупреждение, а не отказ.
    visible = TAGS.sub(" ", html.split("<body", 1)[-1])
    extra = _words(visible) - _words(copy)
    extra = {w_ for w_ in extra if not w_.isdigit()}
    if len(extra) > 3:
        out.append("слова не из текста Редактора: " +
                   ", ".join(sorted(extra)[:6]))
    return out


# ── рендер ────────────────────────────────────────────────────────────

async def render(html_path: Path, size: tuple[int, int]) -> Path:
    """HTML в PNG через headless Chrome. Масштаб 2×, как в спеке.

    Ждём **файл, а не выход процесса**. Chrome со свежим профилем
    записывает скриншот и остаётся висеть: ожидание `proc.wait()`
    упирается в таймаут при уже готовом PNG. Поэтому опрашиваем файл и
    гасим процесс сами.
    """
    png = html_path.with_suffix(".png")
    png.unlink(missing_ok=True)
    w, h = size
    # Профиль Chrome — во временную папку, а не рядом с макетом.
    #
    # Раньше он заводился прямо в `posts/` бренда, и это стоило дважды:
    # папка клиента обрастала каталогами `.chrome-*`, а уборка после
    # убитого процесса идёт с `ignore_errors`, поэтому недописанное
    # оставалось лежать. Стенд ловил это как «Directory not empty» при
    # чистке песочницы — дважды за один день, каждый раз на другом цикле,
    # то есть выглядело как случайность, а было следствием.
    profile = Path(_tmp.mkdtemp(prefix="chrome-render-"))

    proc = await asyncio.create_subprocess_exec(
        chrome(), "--headless", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--force-device-scale-factor=2",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--disable-background-networking", "--disable-sync",
        f"--window-size={w},{h}",
        # Шрифты приходят с CDN. Без бюджета времени Chrome снимает кадр
        # раньше, чем они доедут, и макет уходит системным шрифтом.
        "--virtual-time-budget=8000",
        f"--user-data-dir={profile}",
        f"--screenshot={png}", html_path.as_uri(),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    try:
        deadline = asyncio.get_running_loop().time() + RENDER_TIMEOUT
        size_seen = -1
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.3)
            if png.exists():
                # Ждём, пока файл перестанет расти: Chrome пишет его не
                # атомарно, и снятый на середине PNG битый.
                now = png.stat().st_size
                if now > 0 and now == size_seen:
                    break
                size_seen = now
            if proc.returncode is not None and png.exists():
                break
        else:
            raise NoRenderer(f"рендер {html_path.name} не уложился в "
                             f"{RENDER_TIMEOUT} секунд")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        shutil.rmtree(profile, ignore_errors=True)

    if not png.exists() or png.stat().st_size == 0:
        raise NoRenderer(f"Chrome не отдал PNG для {html_path.name}")
    return png


# ── сборка ────────────────────────────────────────────────────────────

def _stable(spec: str, refs: list[tuple[str, str]]) -> str:
    """То, что не меняется от круга к кругу: ТЗ площадки и эталон.

    Едет отдельным кешируемым блоком, а не в теле запроса. Раньше лежало
    в `_brief`, то есть в некешируемом хвосте, и это самые объёмные вызовы
    завода: за один и тот же эталон платили полную цену на каждой правке,
    а правок у макета по определению много.

    Внутри одной площадки и формата содержимое совпадает дословно, поэтому
    второй круг читает его из кеша примерно за десятую часть.
    """
    lines = ["## ТЗ площадки", "", spec, "",
             "## Эталон, который клонируешь", ""]
    for name, html in refs:
        lines += [f"### {name}", "", "```html", html.strip(), "```", ""]
    return "\n".join(lines)


def _brief(theme: dict[str, Any], copy: str, photos: list[str],
           size: tuple[int, int], cards: int, *, markup: bool) -> str:
    """Тело запроса. `markup` — модель пишет разметку сама.

    На шаблонном пути холст и список фото из брифа выкинуты: холст она не
    задаёт, фото не выбирает, а лишний раздел в запросе это приглашение
    вернуть то, чего не просили.
    """
    w, h = size
    lines = [
        "## Тема", "",
        f"- id: {theme['id']}",
        f"- площадка: {theme.get('plat')}",
        f"- формат: {theme.get('format')}",
        f"- рубрика: {theme.get('rubric') or '[не задана]'}",
        "",
    ]
    if markup:
        lines += [f"## Холст", "",
                  f"{w}×{h} пикселей. Ровно этот размер, `overflow:hidden`.",
                  f"Карточек нужно: {cards}.", ""]
    else:
        lines += [f"Карточек нужно: {cards}.", ""]
    lines += ["## Утверждённый текст Редактора", "",
              "Слова на макет берёшь только отсюда.", "", copy, ""]
    if markup:
        lines += ["## Доступные фото", "",
                  "Путь вида `../design/assets/images/<файл>`. Чего нет в "
                  "списке, того не существует:", ""]
        lines += [f"- {p}" for p in photos] or ["- фото нет"]
    return "\n".join(lines)


async def build(chat_id: int, ask: str, *, say=None,
                photo: str = "") -> Layout:
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиль бренда ещё не собран")

    theme = _pick(chat_id, ask)
    plat = theme.get("plat") or "telegram"
    fmt = theme.get("format") or ""

    _known(plat, fmt)
    spec = _spec(b, plat, fmt)
    size = size_of(theme)
    tpls = _templates(plat, fmt)
    refs = [] if tpls else _reference(plat, fmt)
    if not tpls and not refs:
        raise NoSpec(f"{plat}/{fmt}: эталона в шаблон-паке нет")
    n = _cards(plat, fmt, tpls, refs)

    copy = _copy(b, theme)
    photos = _photos(b)

    if say:
        await say(f"Верстаю {'макет' if n == 1 else f'{n} карточки'} по теме "
                  f"<b>{theme.get('title') or theme['id']}</b> "
                  f"({plat} · {size[0]}×{size[1]}).\n"
                  "Сборка и рендер займут до минуты.")

    if tpls:
        fixed = _derive(b, theme, photos, photo)
        answer = await agent.ask(
            "design", chat_id,
            _brief(theme, copy, photos, size, n, markup=False) +
            "\n\nЗаполни слоты.",
            brand_name=b.name(),
            stable=_slots_stable(spec, MODEL_SLOTS),
            max_tokens=MAX_TOKENS, schema=_schema(MODEL_SLOTS))
    else:
        answer = await agent.ask(
            "design", chat_id,
            _brief(theme, copy, photos, size, n, markup=True) +
            "\n\nСобери макет. Ответь одним JSON-объектом в формате из твоей "
            "секции «Формат выдачи».",
            brand_name=b.name(), stable=_stable(spec, refs),
            max_tokens=MAX_TOKENS)

    data = agent.parse_json(answer, who="дизайнер")
    if tpls:
        cards = _cards_from_slots(data, tpls, photos, fixed)
    else:
        cards = [c for c in (data.get("cards") or [])
                 if isinstance(c, dict) and str(c.get("html") or "").strip()]
    if not cards:
        raise NoWork("Дизайнер не вернул ни одного макета")

    lay = Layout(theme=theme, cards=cards,
                 accent=str(data.get("accent") or ""),
                 notes=[str(n) for n in (data.get("notes") or [])])

    # Сверять готовый макет не с чем — это находка, а не пустяк. Молчание
    # тут неотличимо от «всё сошлось», а на деле у площадки просто нет ни
    # одного живого скрина, по которому видно, как она выглядит в ленте.
    if not brand_refs(b, plat, fmt):
        d = refs_dir(b, plat, fmt)
        lay.findings.append(
            "референсов бренда под эту площадку нет"
            + (f" (<code>{d.relative_to(b.root)}</code> пуста)" if d
               else " — папка под неё не заведена")
            + ": сверить макет не с чем")

    if say:
        await say(f"Макет собран, рендерю {len(cards)} "
                  f"{'картинку' if len(cards) == 1 else 'картинок'}.")

    await emit(b, lay, size, copy, photos, slots=bool(tpls))
    return lay


async def emit(b, lay: Layout, size: tuple[int, int], copy: str,
               photos: list[str], *, slots: bool) -> Layout:
    """Проверить макеты, положить в папку бренда и отрендерить PNG.

    Общий шов для двух путей: так верстает старый Дизайнер и так же
    садится макет, собранный субагентом через мост. Пока это лежало
    внутри `build`, у моста записи не было вовсе — файлы оставались в
    `tasks/{id}/`, а папка задачи в `.gitignore`. Человек видел картинку
    в чате, а в проекте у него не было ничего.

    `inspect` здесь настоящий гейт: он прогоняется по обоим путям и
    субагенту на слово не верит.
    """
    theme = lay.theme
    for i, c in enumerate(lay.cards, 1):
        html = str(c["html"]).strip()
        # Имя карточки идёт в имя файла, поэтому чистится до латиницы.
        # Вычистилось до пустого — берём порядковый номер.
        name = re.sub(r"[^a-z0-9-]", "", str(c.get("name") or "").lower()) \
            or f"{i:02d}"
        lay.findings += [f"{name}: {p}" for p in
                         inspect(html, copy, size, photos)]

        rel = f"posts/{theme['id']}-{name}.html"
        path = b.artifact(rel, html)
        lay.files.append(path)
        lay.files.append(await render(path, size))

        # Слоты кладутся рядом с макетом. Без них правка снова стала бы
        # разговором про HTML: модели пришлось бы показывать разметку,
        # чтобы она вернула разметку. Со слотами круг правки — это две
        # сотни байт туда и обратно.
        if slots:
            b.artifact(f"posts/{theme['id']}-{name}.slots.json",
                       _json.dumps(c.get("slots") or {}, ensure_ascii=False,
                                   indent=2))

    log.info("%s: карточек %s, находок %s, заметок %s", theme["id"],
             len(lay.cards), len(lay.findings), len(lay.notes))
    return lay


def size_of(theme: dict[str, Any]) -> tuple[int, int]:
    """Холст темы. Считается в одном месте: путей показа теперь два."""
    plat = theme.get("plat") or "telegram"
    return CANVAS.get(_key(plat, theme.get("format") or "")) \
        or CANVAS[(plat, None)]


async def land(chat_id: int, data: dict[str, Any]) -> Layout:
    """Посадить макет, собранный субагентом через мост.

    Публичный шов, как `strategy.land` у плана и `editor.land` у текста.
    Разметку по шаблонным площадкам собирает код (`_fill`), даже если
    субагент прислал слоты: дом у шаблона один, и второй копии разметки
    в этом проекте быть не должно.
    """
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиля бренда нет, класть макет некуда")

    tid = str(data.get("theme_id") or "").strip()
    if not tid:
        raise NoWork("в контракте нет `theme_id`: непонятно, к какой теме "
                     "относится макет")
    row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                 tid, chat_id)
    if row is None:
        raise NoWork(f"темы {tid} нет в базе")
    theme = dict(row)
    if not theme.get("asset"):
        raise NoWork(f"у темы {tid} нет утверждённого текста: верстать "
                     "черновик значит верстать дважды")

    plat = theme.get("plat") or "telegram"
    fmt = theme.get("format") or ""
    _known(plat, fmt)
    tpls = _templates(plat, fmt)
    photos = _photos(b)
    copy = _copy(b, theme)

    if tpls:
        cards = _cards_from_slots(data, tpls, photos, _derive(b, theme, photos))
    else:
        cards = [c for c in (data.get("cards") or [])
                 if isinstance(c, dict) and str(c.get("html") or "").strip()]
    if not cards:
        raise NoWork(f"по теме {tid} не пришло ни одного макета")

    lay = Layout(theme=theme, cards=cards,
                 accent=str(data.get("accent") or ""),
                 notes=[str(n) for n in (data.get("notes") or [])])
    return await emit(b, lay, size_of(theme), copy, photos, slots=bool(tpls))


# ── фон до вёрстки ────────────────────────────────────────────────────
#
# Раньше фон выбирал код и сразу верстал: человек видел готовый макет и,
# если фото не то, шёл в правку — то есть платил вёрсткой и рендером за
# решение, которое принимается взглядом за секунду. Теперь сначала три
# фона на выбор, потом дизайн поверх выбранного.
#
# Код при этом не разжаловали: порядок кандидатов по-прежнему его —
# правило бренда по цели темы плюс ротация против повтора через день.
# Человеку остаётся то, что кодом не решается: какой из трёх кадров
# сегодня про этот текст.


def _bg_path(b, theme_id: str) -> Path:
    return b.path(BG_FILE.format(id=theme_id))


def _needs_bg(plat: str, fmt: str) -> bool:
    """Есть ли у формата фон, который ставит код.

    У раздачи и опроса фото в шаблоне нет вовсе, и предлагать выбор там
    значит спрашивать о том, что никуда не встанет. На свободном пути
    (карусель) фото ставит модель из списка — этот выбор пока не наш.
    """
    return any("photo" in set(SLOT_RX.findall(tpl))
               for _, tpl in _templates(plat, fmt))


def _own(b, theme: dict[str, Any], photos: list[str]) -> list[str]:
    """Свои фото в порядке правила бренда: подходящие и не вчерашние."""
    rules = _photo_rules(b)
    goal = str(theme.get("goal") or "").strip().lower()
    order = [p for p in (rules.get(goal) or rules.get("*") or []) if p in photos]
    order += [p for p in photos
              if p not in order and not p.startswith(NOT_OWN)]
    recent = _recent_photos(b, BG_CHOICES)
    # Недавние не выбрасываются, а уезжают в конец: на маленьком
    # фотобанке выбросить их значило бы остаться без вариантов вовсе.
    return [p for p in order if p not in recent] + \
           [p for p in order if p in recent]


async def _keywords(chat_id: int, theme: dict[str, Any]) -> str:
    """Тема поста → английские слова для стока.

    Русский запрос Pexels переводит грубо: на «минимализм стол ноутбук»
    приезжает инжир на тарелке. Слова просим у модели, потому что тема
    русская, а теги стока английские, и словарём это не закрыть.

    Не вышло — не беда: сток просто не подмешается, свои фото останутся.
    """
    title = str(theme.get("title") or "").strip()
    if not title:
        return ""
    try:
        answer = await agent.ask(
            "design", chat_id,
            "Тема поста: " + title +
            f"\nРубрика: {theme.get('rubric') or '—'}."
            "\n\nДай английский запрос для стокового фото-фона под этот "
            "пост: три-четыре слова через пробел, без кавычек и пояснений. "
            "Фон предметный или фактурный — стол, свет, текстура, "
            "пространство. Людей в кадре не проси.",
            max_tokens=200, effort=KEYWORDS_EFFORT)
    except Exception as e:                                   # noqa: BLE001
        log.warning("ключевые слова для стока не вышли: %s", e)
        return ""
    words = re.findall(r"[a-z]+", answer.lower().splitlines()[0]
                       if answer.strip() else "")
    # Человек в кадре со стока — чужое лицо в узнаваемой постановке.
    # Модель об этом просили словами, но просьба это не гарантия.
    words = [w for w in words if w not in stock.PEOPLE][:4]
    return " ".join(words)


def _wants_gen(theme: dict[str, Any]) -> bool:
    """Генерируется ли фон у этой рубрики. Сравнение по нижнему регистру."""
    return str(theme.get("rubric") or "").strip().lower() in GEN_RUBRICS


def _aspect(size: tuple[int, int]) -> str:
    """Холст → соотношение сторон для генератора.

    Просить квадрат и обрезать его кодом нельзя: модель компонует кадр
    под то соотношение, которое ей назвали, и центр композиции уехал бы
    под обрез вместе со всей задумкой.
    """
    w, h = size
    known = {(1080, 1350): "4:5", (1080, 1920): "9:16",
             (1920, 1080): "16:9", (1080, 1080): "1:1"}
    return known.get((w, h), "4:5")


# Рамка кадра. Держится кодом, а не моделью: это не про тему, а про то,
# куда ляжет текст и чего на обложке бренда не бывает никогда. Буквы
# запрещены отдельной строкой — генераторы любят дописать в кадр
# собственный заголовок, и он приезжает поверх настоящего.
GEN_FRAME = (
    "Editorial photograph used as a post cover background. "
    "Subject: {subject}. "
    "Muted graphite and warm off-white palette, one soft directional "
    "light, shallow depth of field, matte film grain, calm and quiet. "
    "No people, no faces, no hands. No text, no letters, no numbers, "
    "no logos, no user interface screenshots. "
    "Keep the top-left corner and the whole bottom third calm and nearly "
    "empty: the headline is set there. Vertical composition, no borders.")


async def _gen_brief(chat_id: int, theme: dict[str, Any]) -> str:
    """Тема поста → предметное описание кадра для генератора.

    У модели просим только сюжет: остальное держит `GEN_FRAME`. Если
    спросить кадр целиком, она каждый раз переизобретает свет и палитру,
    и обложки одной рубрики перестают быть одной рубрикой.

    Не вышло — не беда: генерация просто не подмешается к вариантам.
    """
    title = str(theme.get("title") or "").strip()
    if not title:
        return ""
    try:
        answer = await agent.ask(
            "design", chat_id,
            "Тема поста: " + title +
            f"\nРубрика: {theme.get('rubric') or '—'}."
            "\n\nОпиши по-английски предметную сцену для фона обложки: "
            "одно предложение, до пятнадцати слов, без кавычек и "
            "пояснений. Предмет, фактура, пространство — стол, бумага, "
            "провод, стекло, тень. Людей, интерфейсов и надписей не "
            "предлагай.",
            max_tokens=200, effort=GEN_BRIEF_EFFORT)
    except Exception as e:                                   # noqa: BLE001
        log.warning("бриф для генерации не вышел: %s", e)
        return ""
    line = " ".join(answer.strip().splitlines()[:1]).strip() if answer.strip() else ""
    return line[:200]


def _own_page(b, theme: dict[str, Any], photos: list[str],
              page: int) -> list[str]:
    start = page * BG_CHOICES
    return _own(b, theme, photos)[start:start + BG_CHOICES]


def _options(own: list[str], *, page: int, query: str,
             gen: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Три кандидата: сгенерированный первым, свои, сток на нехватку.

    Генерация идёт первой, а не единственной: у рубрики она уместнее
    съёмки, но обещать это за человека код не будет — рядом стоят свои
    фото, и кнопка остаётся за ним.
    """
    out: list[dict[str, Any]] = [gen] if gen else []
    out += [{"kind": "own", "name": n} for n in own][:BG_CHOICES - len(out)]
    if len(out) < BG_CHOICES and query and stock.ready():
        try:
            found = stock.search(query, BG_CHOICES - len(out), page=page + 1)
        except stock.NoStock as e:
            log.warning("сток не ответил: %s", e)
            found = []
        out += [{"kind": "stock", "photo": ph} for ph in found]
    return out


def _bg_kb(theme_id: str, n: int) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=str(i),
                                callback_data=f"art:bg:{theme_id}:{i}")
           for i in range(1, n + 1)]
    return InlineKeyboardMarkup(inline_keyboard=[row, [
        InlineKeyboardButton(text="🔄 Ещё три",
                             callback_data=f"art:bgmore:{theme_id}"),
        InlineKeyboardButton(text="🎲 Реши сам",
                             callback_data=f"art:bgauto:{theme_id}"),
    ]])


async def offer(reg, chat_id: int, ask: str, *, topic: str = "design",
                page: int = 0, say=None) -> bool:
    """Показать три фона и ждать кнопку. False — выбирать нечего.

    Возвращает False на форматах без фона, на пустом фотобанке и когда
    вариант остался ровно один: спрашивать «выбери из одного» это не
    выбор, а лишний круг.
    """
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиль бренда ещё не собран")

    theme = _pick(chat_id, ask)
    plat = theme.get("plat") or "telegram"
    fmt = theme.get("format") or ""
    _known(plat, fmt)
    if not _needs_bg(plat, fmt):
        return False
    # ТЗ спрашиваем до показа: если его нет, вёрстки не будет всё равно,
    # и человек зря выберет фон.
    _spec(b, plat, fmt)

    photos = _photos(b)
    if not photos:
        raise NoWork("в папке бренда нет ни одного фото")

    own = _own_page(b, theme, photos, page)
    saved = _bg_read(b, theme["id"])
    query = str(saved.get("query") or "") if saved else ""

    # Генерация только на первой тройке. «Ещё три» — это просьба
    # посмотреть, что ещё есть, и второй платный кадр на неё был бы
    # ответом не по адресу: человек уже видел, что предложила модель.
    gen: dict[str, Any] | None = None
    if page == 0 and _wants_gen(theme) and imagegen.ready():
        subject = await _gen_brief(chat_id, theme)
        if subject:
            prompt = GEN_FRAME.format(subject=subject)
            try:
                imagegen.stage(b, theme["id"], imagegen.make(
                    prompt, _aspect(size_of(theme))))
                gen = {"kind": "gen", "subject": subject, "prompt": prompt}
            except (imagegen.NoGen, OSError) as e:
                # Генерация не встала — вариантов просто станет меньше.
                # Ронять вёрстку из-за фона нельзя: свои фото на месте.
                log.warning("фон не сгенерировался: %s", e)
    else:
        imagegen.sweep(b, theme["id"])

    # Слова для стока спрашиваем у модели, только когда своих фото не
    # хватило: у бренда с полным фотобанком это лишний вызов на каждом
    # макете, а он стоит секунд.
    if len(own) + (1 if gen else 0) < BG_CHOICES and not query:
        query = await _keywords(chat_id, theme)

    options = _options(own, page=page, query=query, gen=gen)
    if len(options) < 2:
        return False

    _bg_path(b, theme["id"]).parent.mkdir(parents=True, exist_ok=True)
    _bg_path(b, theme["id"]).write_text(_json.dumps(
        {"theme_id": theme["id"], "ask": ask, "query": query, "page": page,
         "options": options}, ensure_ascii=False, indent=2), encoding="utf-8")

    if say:
        await say(f"Сначала фон для темы <b>{theme.get('title') or theme['id']}</b>. "
                  f"Три варианта, дизайн наложу на выбранный.")
    for i, opt in enumerate(options, 1):
        if opt["kind"] == "own":
            blob = b.path(f"design/assets/images/{opt['name']}").read_bytes()
            caption = f"{i}. {opt['name']}"
        elif opt["kind"] == "gen":
            blob = imagegen.staged(b, theme["id"])
            if not blob:
                continue
            caption = f"{i}. сгенерировано · {opt.get('subject') or '—'}"
        else:
            ph = opt["photo"]
            try:
                blob = stock.preview(ph)
            except stock.NoStock as e:
                log.warning("превью со стока не пришло: %s", e)
                continue
            caption = (f"{i}. сток · {ph.get('photographer') or '—'} · "
                       f"по запросу «{query}»")
        await reg.send_file("design", chat_id, blob, f"bg-{i}.jpg",
                            caption=caption, topic=topic, as_photo=True)
    await reg.say("design", chat_id, "Какой берём?",
                  kb=_bg_kb(theme["id"], len(options)), topic=topic)
    return True


def _bg_read(b, theme_id: str) -> dict[str, Any]:
    path = _bg_path(b, theme_id)
    if not path.is_file():
        return {}
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _current_photo(b, theme_id: str) -> str:
    """Фон, который уже стоит на макете темы. Для пересборки на правке."""
    for f in sorted(b.path("posts").glob(f"{theme_id}-*.slots.json")):
        try:
            name = _json.loads(f.read_text(encoding="utf-8")).get("photo")
        except (ValueError, OSError):
            continue
        if name:
            return str(name)
    return ""


# ── карточка и кнопки ─────────────────────────────────────────────────

def _recover(chat_id: int, theme_id: str) -> Layout | None:
    """Поднять макет из базы и с диска: память процесса его не помнит."""
    row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                 theme_id, chat_id)
    if row is None:
        return None
    b = desk.brand(chat_id)
    files = sorted(b.path("posts").glob(f"{theme_id}-*")) if b else []
    return Layout(theme=dict(row), files=list(files))


table = desk.Desk("design", corrections="design/corrections.md",
                  recover=_recover)


def wants_fix(chat_id: int) -> bool:
    return table.wants_fix(chat_id)


def _kb(theme_id: str) -> InlineKeyboardMarkup:
    """id темы в кнопке: макет в памяти не переживает перезапуск бота."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Ок", callback_data=f"art:ok:{theme_id}"),
        InlineKeyboardButton(text="✏️ Правки", callback_data=f"art:fix:{theme_id}"),
        InlineKeyboardButton(text="📤 В очередь",
                             callback_data=f"art:queue:{theme_id}"),
    ]])


async def show(reg, chat_id: int, lay: Layout, size: tuple[int, int] | None = None,
               topic: str = "design", head: str = "") -> None:
    """Отдать макет человеку и встать в ожидание кнопки.

    Один код на сборку и на точечную правку. Пока это лежало внутри `run`,
    правка либо не показывала кнопок вовсе, либо обзавелась бы второй
    копией показа — а у этого проекта уже есть история с четырьмя копиями
    одной роли, где каждая починка чинила одну из четырёх.
    """
    size = size or size_of(lay.theme)
    table.hold(chat_id, lay)
    if head:
        await reg.say("design", chat_id, head, topic=topic)
    await reg.say("design", chat_id, caption(lay, size), topic=topic)
    for png in lay.pngs:
        await reg.send_file("design", chat_id, png.read_bytes(), png.name,
                            topic=topic, as_photo=True)
    # HTML отдаём документом: по нему правят, а из PNG правку не сделаешь.
    for html in lay.htmls:
        await reg.send_file("design", chat_id, html.read_bytes(), html.name,
                            topic=topic)
    await reg.say("design", chat_id, "Принимаем?",
                  kb=_kb(lay.theme["id"]), topic=topic)


def caption(lay: Layout, size: tuple[int, int]) -> str:
    t = lay.theme
    out = [f"🎨 <b>{t.get('plat')} · {t.get('format')}</b> · "
           f"{size[0]}×{size[1]} · {len(lay.pngs)} шт",
           f"<code>{t['id']}</code>"]
    if lay.accent:
        out.append(f"акцент: {lay.accent}")
    # Под ⚠️ идёт только пойманное кодом. Рассуждения роли туда мешать
    # нельзя: четыре объяснения подряд превращают значок в фон, и
    # настоящая поломка в нём теряется.
    if lay.findings:
        out.append("⚠️ " + "; ".join(lay.findings[:4]))
    if lay.notes:
        out.append("от дизайнера: " + "; ".join(lay.notes[:3]))
    return "\n".join(out)


async def run(reg, chat_id: int, ask: str, topic: str = "design", *,
              photo: str = "", pick_bg: bool = True) -> None:
    """Собрать макет. По умолчанию сначала спрашивает фон.

    `pick_bg=False` — верстать сразу: так идёт пересборка на правке (фон
    там уже утверждён) и кнопка «Реши сам». `photo` — утверждённый
    человеком фон, он же отменяет выбор правилом.
    """
    table.clear(chat_id)

    async def say(text: str) -> None:
        await reg.say("design", chat_id, text, topic=topic)

    try:
        if pick_bg and not photo and await offer(reg, chat_id, ask,
                                                 topic=topic, say=say):
            return
        lay = await build(chat_id, ask, say=say, photo=photo)
    except NoSpec as e:
        await say(f"ТЗ нет: {e}. Собирать на глаз не буду, иначе макет "
                  "разъедется с брендом.\n\nЗаведи любой из этих файлов "
                  "в папке бренда — сначала смотрю ТЗ формата, потом "
                  "площадки целиком.")
        return
    except NoWork as e:
        await say(f"Верстать нечего: {e}. Сначала текст от Редактора.")
        return
    except NoRenderer as e:
        await say(f"Макет собрал, но PNG не получился: {e}. "
                  "Отдавать один HTML без картинки смысла нет.")
        return
    except agent.BudgetExceeded as e:
        await say(f"Остановился: {e}")
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("макет не собрался")
        await say(f"Макет не собрался: {desk.reason(e)}")
        return

    await show(reg, chat_id, lay, size_of(lay.theme), topic)


# Слова, после которых правка это всё-таки пересборка. Точечная правка
# работает по существующему HTML, и «переделай целиком» ей не по адресу:
# она бы аккуратно поправила то, что человек просил выбросить.
REDO = ("пересобери", "с нуля", "заново", "переделай целиком",
        "другой макет", "другую верстку", "другую вёрстку")


async def revise(reg, chat_id: int, instruction: str,
                 topic: str = "design") -> None:
    lay = table.take(chat_id)
    if lay is None:
        await reg.say("design", chat_id, "Этот макет уже неактуален.",
                      topic=topic)
        return

    table.note(chat_id, lay.theme["id"], instruction)

    low = instruction.lower()
    htmls = lay.htmls or _htmls_on_disk(chat_id, lay.theme["id"])
    if htmls and not any(w in low for w in REDO):
        try:
            plat = lay.theme.get("plat") or "telegram"
            if _templates(plat, lay.theme.get("format") or ""):
                await _patch_slots(reg, chat_id, lay, htmls, instruction, topic)
            else:
                await _patch(reg, chat_id, lay, htmls, instruction, topic)
            return
        except NoWork as e:
            # Не молчим и не делаем вид, что правка прошла: говорим, что
            # точечно не вышло, и честно идём длинным путём.
            log.warning("%s: точечная правка не удалась (%s), пересобираю",
                        lay.theme["id"], e)
            await reg.say("design", chat_id,
                          f"Точечно поправить не вышло ({e}). "
                          "Пересобираю макет целиком, это дольше.",
                          topic=topic)

    # Пересборка на правке идёт с тем же фоном и без выбора: человек
    # просил поправить макет, а не начать сначала. Другое фото — это
    # `_patch_slots`, у него `photo` в наборе слотов.
    b = desk.brand(chat_id)
    await run(reg, chat_id,
              f"Правка к макету темы {lay.theme['id']}: {instruction}",
              topic=topic, pick_bg=False,
              photo=_current_photo(b, lay.theme["id"]) if b else "")


def _htmls_on_disk(chat_id: int, theme_id: str) -> list[Path]:
    """Макеты темы, лежащие в папке бренда. Память процесса их не помнит."""
    b = desk.brand(chat_id)
    if b is None:
        return []
    return sorted(b.path("posts").glob(f"{theme_id}-*.html"))


async def _patch_slots(reg, chat_id: int, lay: Layout, htmls: list[Path],
                       instruction: str, topic: str) -> None:
    """Правка шаблонной карточки: меняются слоты, а не разметка.

    Самый дешёвый круг из всех. Модель получает короткие значения и
    просьбу человека, возвращает такие же — двести байт вместо двух
    килобайт HTML.

    Фото здесь модели возвращается, в отличие от сборки: «поставь другое
    фото» это просьба человека, и отвечать на неё ротацией нельзя.
    Рубрика не возвращается — её ставит Стратег, и код её вернёт на
    место, что бы ни пришло в ответе. Разметку заново собирает `_fill`, поэтому промахнуться
    мимо холста, токенов или пути к фото она по-прежнему не может.

    Слоты лежат рядом с макетом (`*.slots.json`). Их нет — значит макет
    собран до переезда на шаблоны, и правим его старым способом.
    """
    b = desk.brand(chat_id)
    theme = lay.theme
    plat = theme.get("plat") or "telegram"
    fmt = theme.get("format") or ""
    size = CANVAS.get(_key(plat, fmt)) or CANVAS[(plat, None)]
    tpls = _templates(plat, fmt)
    photos = _photos(b)
    copy = _copy(b, theme)

    current: dict[str, dict[str, str]] = {}
    for path in htmls:
        name = path.stem[len(theme["id"]) + 1:] or path.stem
        f = path.with_name(f"{path.stem}.slots.json")
        if not f.exists():
            raise NoWork("слотов на диске нет, макет собран старым способом")
        current[name] = _json.loads(f.read_text(encoding="utf-8"))

    await reg.say("design", chat_id, f"Правлю по слотам: {instruction}",
                  topic=topic)

    answer = await agent.ask(
        "design", chat_id,
        "\n".join([
            "## Что просит человек", "", instruction, "",
            "## Утверждённый текст Редактора", "",
            "Слова на макет берёшь только отсюда.", "", copy, "",
            "## Доступные фото", "",
            *([f"- {p}" for p in photos] or ["- фото нет"]), "",
            "## Текущие слоты", "",
            "```json", _json.dumps(current, ensure_ascii=False, indent=2),
            "```", "",
            "Верни те же карточки с поправленными слотами. Слоты, "
            "которых правка не касается, оставь как есть. Рубрику здесь "
            "не поменять — её ставит Стратег; просят её — скажи строкой "
            "в `notes`.",
        ]),
        brand_name=b.name(), stable=_slot_brief(PATCH_SLOTS),
        max_tokens=MAX_TOKENS, effort=PATCH_EFFORT,
        schema=_schema(PATCH_SLOTS))

    data = agent.parse_json(answer, who="дизайнер")
    got = {re.sub(r"[^a-z0-9-]", "", str(c.get("name") or "").lower()): c
           for c in (data.get("cards") or []) if isinstance(c, dict)}
    unknown = set(got) - set(current)
    if unknown:
        raise NoWork("вернулась незнакомая карточка «"
                     + ", ".join(sorted(unknown)) + "»")

    out = Layout(theme=theme, accent=str(data.get("accent") or ""),
                 notes=[str(n) for n in (data.get("notes") or [])])
    touched: set[str] = set()
    by_name = {stem: tpl for stem, tpl in tpls}

    for path in htmls:
        name = path.stem[len(theme["id"]) + 1:] or path.stem
        # Пришедшее кладётся поверх того, что было: модель возвращает
        # то, что правила, и не обязана переписывать остальное. Рубрика
        # из ответа выбрасывается — её ставит Стратег.
        got_slots = {k: str(v) for k, v in
                     ((got.get(name) or {}).get("slots") or {}).items()
                     if k != "rubric"}
        slots = dict(current[name], **got_slots) if got_slots else {}
        if not slots or slots == current[name]:
            # Не изменилась — не перерисовываем. PNG на диске валиден.
            out.files.append(path)
            png = path.with_suffix(".png")
            if png.exists():
                out.files.append(png)
            continue

        tpl = by_name.get(name) or tpls[0][1]
        html = _fill(tpl, slots, photos)
        out.findings += [f"{name}: {p}" for p in
                         inspect(html, copy, size, photos)]
        new_path = b.artifact(f"posts/{theme['id']}-{name}.html", html)
        b.artifact(f"posts/{theme['id']}-{name}.slots.json",
                   _json.dumps(slots, ensure_ascii=False, indent=2))
        out.files.append(new_path)
        out.files.append(await render(new_path, size))
        touched.add(name)

    if not touched:
        raise NoWork("дизайнер не изменил ни одного слота")

    log.info("%s: правка по слотам, изменено %s из %s",
             theme["id"], len(touched), len(htmls))
    await show(reg, chat_id, out, size, topic,
                head=f"Поправлено карточек: {len(touched)} из {len(htmls)}.")


async def _patch(reg, chat_id: int, lay: Layout, htmls: list[Path],
                 instruction: str, topic: str) -> None:
    """Поправить существующие макеты, а не собирать заново.

    Раньше `revise` звал `run`, то есть полную пересборку: модель заново
    писала весь HTML, Chrome заново рендерил все карточки. «Подвинь
    заголовок на две строки ниже» стоило ровно столько же, сколько вёрстка
    с нуля, — при потолке в 16000 токенов и усилии `high`.

    Здесь модели даётся то, что уже есть, и возвращает она **только**
    изменённые карточки. Нетронутые не перерисовываются вовсе: их PNG уже
    лежит на диске и остаётся валидным.

    Эталон и ТЗ в теле запроса не нужны — макет уже собран по ним, и они
    к тому же лежат в кешируемом блоке.
    """
    b = desk.brand(chat_id)
    theme = lay.theme
    plat = theme.get("plat") or "telegram"
    fmt = theme.get("format") or ""
    size = CANVAS.get(_key(plat, fmt)) or CANVAS[(plat, None)]
    copy = _copy(b, theme)
    photos = _photos(b)

    current = []
    for path in htmls:
        name = path.stem[len(theme["id"]) + 1:] or path.stem
        current += [f"### {name}", "", "```html",
                    path.read_text(encoding="utf-8").strip(), "```", ""]

    await reg.say("design", chat_id,
                  f"Правлю по месту: {instruction}", topic=topic)

    answer = await agent.ask(
        "design", chat_id,
        "\n".join([
            "## Что просит человек", "", instruction, "",
            f"## Холст", "",
            f"{size[0]}×{size[1]} пикселей, `overflow:hidden`. Размер не "
            "меняется.", "",
            "## Утверждённый текст Редактора", "",
            "Слова на макет берёшь только отсюда.", "", copy, "",
            "## Доступные фото", "",
            "Путь вида `../design/assets/images/<файл>`. Чего нет в "
            "списке, того не существует:", "",
            *([f"- {p}" for p in photos] or ["- фото нет"]), "",
            "## Текущие макеты", "", *current,
            "Поправь то, о чём просит человек, и **ничего больше**. "
            "Ответь одним JSON-объектом в формате из твоей секции "
            "«Формат выдачи», но верни в `cards` **только те карточки, "
            "которые изменились**. Имя карточки сохрани прежним.",
        ]),
        brand_name=b.name(),
        max_tokens=MAX_TOKENS, effort=PATCH_EFFORT)

    data = agent.parse_json(answer, who="дизайнер")
    cards = [c for c in (data.get("cards") or [])
             if isinstance(c, dict) and str(c.get("html") or "").strip()]
    if not cards:
        raise NoWork("дизайнер не вернул ни одной изменённой карточки")

    known = {p.stem[len(theme["id"]) + 1:] or p.stem for p in htmls}
    out = Layout(theme=theme, accent=str(data.get("accent") or ""),
                 notes=[str(n) for n in (data.get("notes") or [])])
    # Нетронутое остаётся на диске как есть — и попадает в комплект.
    touched: set[str] = set()

    for c in cards:
        name = re.sub(r"[^a-z0-9-]", "", str(c.get("name") or "").lower())
        if name not in known:
            # Придуманное имя означает, что модель собрала новую карточку
            # вместо правки старой. Молча принять это нельзя: в папке
            # заведётся второй макет, и в комплект уедут оба.
            raise NoWork(f"вернулась незнакомая карточка «{name or '?'}»")
        html = str(c["html"]).strip()
        out.findings += [f"{name}: {p}" for p in
                         inspect(html, copy, size, photos)]
        path = b.artifact(f"posts/{theme['id']}-{name}.html", html)
        out.files.append(path)
        out.files.append(await render(path, size))
        touched.add(name)

    for path in htmls:
        name = path.stem[len(theme["id"]) + 1:] or path.stem
        if name not in touched:
            out.files.append(path)
            png = path.with_suffix(".png")
            if png.exists():
                out.files.append(png)

    log.info("%s: точечная правка, изменено %s из %s",
             theme["id"], len(touched), len(htmls))
    await show(reg, chat_id, out, size, topic,
                head=f"Поправлено карточек: {len(touched)} из {len(htmls)}.")


async def on_callback(reg, chat_id: int, action: str,
                      topic: str = "design") -> None:
    action, _, theme_id = action.partition(":")

    async def say(text: str) -> None:
        await reg.say("design", chat_id, text, topic=topic)

    if action in ("bg", "bgmore", "bgauto"):
        await _on_bg(reg, chat_id, action, theme_id, topic)
        return

    if action == "fix":
        lay = table.get(chat_id, theme_id)
        if lay is None:
            await say("Этот макет уже неактуален.")
            return
        table.await_fix(chat_id, lay)
        await say("Напиши одним сообщением, что поправить. Запишу правку "
                  "в дизайн-профиль и пересоберу.")
        return

    if action not in {"ok", "queue"}:
        return

    lay = table.take(chat_id, theme_id)
    if lay is None:
        await say("Этот макет уже неактуален.")
        return

    if action == "ok":
        await say(f"Принято. Файлы лежат в <code>posts/</code> папки "
                  f"бренда: {len(lay.pngs)} PNG и столько же HTML.")
        return

    # «В очередь» это передача Публикатору, а не рассказ о нём: комплект
    # собирает он, и он же скажет, чего в комплекте не хватает.
    tid = lay.theme["id"]
    await say(f"Передаю комплект <code>{tid}</code> Публикатору.")
    await publisher.run(reg, chat_id, tid, topic="queue")


async def _on_bg(reg, chat_id: int, action: str, arg: str,
                 topic: str) -> None:
    """Кнопки выбора фона: номер, «ещё три», «реши сам».

    Кандидаты читаются с диска, а не из памяти процесса: бот
    перезапускается, а стоковую выдачу на тот же запрос второй раз не
    получить — Pexels отдаёт другое.
    """
    theme_id, _, index = arg.rpartition(":") if action == "bg" else (arg, "", "")

    async def say(text: str) -> None:
        await reg.say("design", chat_id, text, topic=topic)

    b = desk.brand(chat_id)
    if b is None:
        await say("Профиль бренда ещё не собран.")
        return

    saved = _bg_read(b, theme_id)
    if not saved:
        await say("Этот выбор фона уже неактуален — попроси макет заново.")
        return
    ask = str(saved.get("ask") or f"свёрстай макет по теме {theme_id}")

    if action == "bgauto":
        imagegen.sweep(b, theme_id)
        await run(reg, chat_id, ask, topic, pick_bg=False)
        return

    if action == "bgmore":
        try:
            shown = await offer(reg, chat_id, ask, topic=topic,
                                page=int(saved.get("page") or 0) + 1, say=say)
        except (NoWork, NoSpec) as e:
            await say(f"Больше вариантов нет: {e}")
            return
        if not shown:
            await say("Больше вариантов нет — верстаю на том, что выбрал код.")
            await run(reg, chat_id, ask, topic, pick_bg=False)
        return

    options = saved.get("options") or []
    try:
        opt = options[int(index) - 1]
    except (ValueError, IndexError):
        await say("Не понял, какой из вариантов.")
        return

    if opt.get("kind") == "own":
        photo = str(opt.get("name") or "")
        imagegen.sweep(b, theme_id)
    elif opt.get("kind") == "gen":
        row = db.one("SELECT title FROM themes WHERE id = ? AND chat_id = ?",
                     theme_id, chat_id)
        try:
            photo = imagegen.take(b, theme_id, str(row["title"] if row else ""),
                                  str(opt.get("prompt") or ""))
        except (imagegen.NoGen, OSError) as e:
            await say(f"Сгенерированный фон не забрался: {e}. Выбери другой.")
            return
        await say(f"Забрал в фотобанк: <code>{photo}</code>. "
                  "Чем и по какому запросу — в "
                  "<code>design/assets/gen-credits.md</code>.")
    else:
        # Скачивается только выбранное: папка бренда не должна обрастать
        # тем, что человек отверг.
        try:
            photo = stock.take(b, opt["photo"], str(saved.get("query") or "фон"))
        except (stock.NoStock, KeyError, OSError) as e:
            await say(f"Стоковый фон не забрался: {e}. Выбери другой.")
            return
        imagegen.sweep(b, theme_id)
        await say(f"Забрал в фотобанк: <code>{photo}</code>. "
                  "Автор записан в <code>design/assets/stock-credits.md</code>.")

    await run(reg, chat_id, ask, topic, photo=photo, pick_bg=False)
