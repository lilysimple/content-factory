"""Монтаж: из отснятого человеком видео в готовый рилс.

Вход — видеофайл, который человек снял сам и прислал в топик Reels, или
ссылка на свой эфир. Тема при этом может быть, а может и не быть: под
дубль из головы её нет и в плане не было, и она заводится по факту
съёмки. Выход — `posts/{id}-reel.mp4`: интро (обложка Дизайнера или
карточка бренда), отснятое видео кроп/паддингом под холст площадки,
титул хука первые три секунды, аутро с CTA.

Сборка детерминирована, как `publisher.py` и рендер PNG в `design.py`:
ffprobe посчитал длительность, Remotion собрал кадр. Разница с
`design.py` в рендерере: тот правит HTML headless Chrome, здесь Chrome
правит Remotion, а мы только готовим для него JSON и зовём
`npx remotion render` подпроцессом.

**Модель зовётся ровно один раз и не отсюда.** Границы кусков ставит
Монтажёр (`orchestrator/cut.py`): где в длинной записи ролики и где
начинается снятый дубль — это рассуждение, и оно вынесено в роль. Всё
остальное здесь считается арифметикой и решать «где мысль закончилась»
не умеет. Отсюда же мягкость: не пришли границы — монтируем целиком,
а не отказываем в монтаже уже снятого.

Разбор дубля живёт в `footage.py` и модель не зовёт тоже: паузы,
активная зона кадра и пословный транскрипт — арифметика и локальный
whisper. Здесь остаётся сборка: собрать props, позвать Remotion, отдать
человеку файл и честно назвать то, что не сошлось.

Субтитры идут по таймингам самой записи, а не по таймингам из
`-script-notes.md`: те посчитаны от скорости чтения суфлёра (два слова в
секунду), а человек на камере говорит в своём темпе. Дубль без звуковой
дорожки субтитров не получает вовсе — вместо них возвращается титул
хука, и человеку об этом говорится строкой.

Расшифровка слышит речь, а не имена: «клод», «ремоушен», «антропик»
приезжают в караоке ровно так, как звучат. Правит это словарь бренда
(`montage/subtitles.md`), а не второй проход модели: ошибка на каждом
дубле одна и та же. Словарь применяется сам, пополняется правкой
человека под карточкой и переписывает субтитры без новой расшифровки.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ROOT, cfg
from orchestrator import cut, desk, design, footage, grab, publisher
from orchestrator.desk import NoWork
from storage import db

log = logging.getLogger("montage")

TOOLS = ROOT / "tools" / "remotion-montage"

# Рендер видео это не PNG: минуты, не секунды. Потолок считается от
# длины ролика, а не константой. Замер 30.08 (Remotion 4.0.518,
# chrome-headless-shell, macOS arm64, штатная параллельность): 778
# кадров за 183 с, то есть примерно 4,2 кадра в секунду. Плоские 240 с
# хватало только коротким: сценарии Редактора Reels идут до 50 секунд
# (`roles/reels.md`, таблица бюджета), а это ~1600 кадров и ~370 с —
# такой ролик упирался бы в потолок всегда, и человек читал бы «не
# уложился» вместо готового видео.
RENDER_SECONDS_PER_FRAME = 0.25   # с запасом к замеренным 0,235
RENDER_TIMEOUT_MIN = 240          # короткому ролику меньше не даём


def _timeout(frames: int) -> int:
    """Потолок рендера от числа кадров, не короче минимального."""
    return max(RENDER_TIMEOUT_MIN, int(frames * RENDER_SECONDS_PER_FRAME) + 60)
# Цвета берутся из `design/tokens.css` бренда по имени токена, а не
# «первым попавшимся hex»: первым в файле лежит `--milk`, светлый фон, и
# белый текст на нём исчезает. Имя токена — это договор с Дизайнером,
# порядок строк в файле — совпадение.
DEFAULT_COLOR = "#111111"
DEFAULT_ACCENT = "#C97C5D"
TOKEN_BG = "graphite"
TOKEN_ACCENT = "terracotta"

HOOK_TITLE = "Хук"
CTA_TITLE = "CTA"

# Тот же холст, что у Дизайнера: рилс без своего размера площадки не
# заводим, берём у design.CANVAS, чтобы два места не разъехались.
FORMATS = ("reels", "shorts")


class NoFootage(RuntimeError):
    """Видео от человека ещё не пришло."""


class NoRenderer(RuntimeError):
    """Remotion не смог отрендерить видео."""


class NotInstalled(RuntimeError):
    """npm install в tools/remotion-montage ещё не запускали."""


@dataclass
class Reel:
    theme: dict[str, Any]
    video: Path
    cover: Path | None = None
    hook: str = ""
    cta: str = ""
    color: str = DEFAULT_COLOR
    accent: str = DEFAULT_ACCENT
    probe: footage.Probe | None = None
    cuts: footage.Timeline | None = None
    focus: list[footage.Focus] = field(default_factory=list)
    spec: dict[str, str] = field(default_factory=lambda: dict(COVER_DEFAULTS))
    lines: list[dict[str, Any]] = field(default_factory=list)
    pan: list[dict[str, float]] = field(default_factory=list)
    still: Path | None = None
    still_at: float = 0.0
    quiet: list[tuple[float, float]] = field(default_factory=list)
    # Услышанное до словаря и до нарезки. Нужно дважды: словарь
    # применяется к нему заново на правке, а границы Монтажёра
    # приходят уже после расшифровки и режут те же слова.
    heard: list[Any] = field(default_factory=list)
    subs: list[dict[str, Any]] = field(default_factory=list)
    piece: tuple[float, float] | None = None
    face: footage.Face | None = None
    crop: tuple[float, float] = (0.5, 0.5)
    anchor: str = "bottom"
    inset: int = 0
    pages: list[dict[str, Any]] = field(default_factory=list)
    out: Path | None = None
    findings: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return str(self.theme.get("title") or "")


# ── вход: тема и видео ──────────────────────────────────────────────

def _pick(chat_id: int, ask: str) -> dict[str, Any] | None:
    """Тема под монтаж. `None` — темы нет, и это нормальный вход.

    Названная по id берётся любая reels-тема, в том числе без
    утверждённого сценария: человек снял дубль и хочет ролик, а хук и CTA
    это украшение первого и последнего кадра, которого может не быть.
    Караоке, нарезка пауз и панорама считаются по записи, а не по
    сценарию, и без него работают целиком.

    Молча, без id, берём по-прежнему только тему с принятым сценарием.
    Угадывать, какой из черновиков человек держал в голове, монтажу
    нельзя: рендер стоит минут, и ошибка выясняется в конце.

    Не нашлось ничего — это не отказ, а второй нормальный вход: человек
    снял дубль из головы, и темы под него в плане нет и не было. Границы
    и заголовок такому дублю даёт Монтажёр, тема заводится по факту
    съёмки (`src = 'adhoc'`). Названный id, которого нет в базе, отказом
    остаётся: молчать про опечатку в id значит смонтировать не то.
    """
    named = bool(desk.ID_RX.search(ask or ""))
    try:
        return desk.pick(
            chat_id, ask,
            statuses=("idea", "draft", "ready") if named else ("ready",),
            fresh="ready",
            suits=lambda r: ((r["format"] or "").lower() in FORMATS
                             and (named or bool(r["asset"]))),
            wrong="у темы {id} формат «{format}», а не ролик",
            none="темы {id} нет",
            empty="нет ни одного утверждённого сценария reels")
    except NoWork:
        if named:
            raise
        return None


def incoming_dir(b) -> Path:
    d = b.path("montage/incoming")
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage_video(b, blob: bytes, suffix: str = ".mp4") -> Path:
    """Сохранить присланный человеком файл. Одно видео на бренд ждёт разбора.

    Как `materials.stage_dir` — файл лежит рядом с профилем, а не во
    временной папке процесса, только здесь ждать нечего: следующий
    /montage сразу заберёт этот файл под ближайшую готовую тему.
    """
    d = incoming_dir(b)
    path = d / f"pending{suffix}"
    path.write_bytes(blob)
    log.info("видео принято: %s (%s байт)", path, len(blob))
    return path


def _footage(b) -> Path:
    d = incoming_dir(b)
    files = sorted(d.glob("pending.*"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    if not files:
        raise NoFootage("видео ещё не пришло. Снимите ролик по сценарию и "
                        "пришлите файлом в этот топик")
    return files[0]


# ── словарь субтитров ─────────────────────────────────────────────────
#
# Файл бренда, а не настройка кода: «клод» вместо Claude это ошибка про
# этот бренд, у соседнего в кадре другие имена. Формат человеческий —
# строка «как слышно -> как правильно», потому что править его будут
# руками не реже, чем кнопкой.
#
# Пустая правая часть значит «выбросить»: слова-паразиты и «ээ» из
# расшифровки в караоке не нужны.

LEXICON = "montage/subtitles.md"
LEX_NOTE = "по словарю субтитров поправлено слов: "

# Стрелка в любом виде, который человек напишет с телефона. Знак «=»
# сюда не берём: в прозе он встречается чаще, чем в замене.
ARROW = re.compile(r"^\s*[-*]?\s*(.{1,80}?)\s*(?:->|-->|=>|→|—>)\s*(.{0,80})\s*$")
# «клод» на «Claude» — та же замена словами. Кавычки обязательны: без них
# фраза «замени клод на claude код» разбирается двумя способами.
QUOTED = re.compile(r"[«\"\']([^«»\"\']{1,80})[»\"\']\s*(?:на|→|->)\s*"
                    r"[«\"\']([^«»\"\']{0,80})[»\"\']")

# Пояснение в файле идёт комментариями, а не прозой: строка «как слышно
# -> как правильно» посреди объяснения сама разбирается как замена.
LEX_HEAD = ("# Словарь субтитров\n"
            "#\n"
            "# Строка на замену: как слышно -> как правильно.\n"
            "# Пустая правая часть выбрасывает слово из субтитров.\n"
            "# Строки, начинающиеся с #, — комментарии.\n")


def lexicon(b) -> list[tuple[str, str]]:
    """Замены бренда. Нет файла — нет замен, и это не поломка."""
    out: list[tuple[str, str]] = []
    for line in (b.read(LEXICON) or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = ARROW.match(line)
        if m and footage.norm(m.group(1)):
            out.append((m.group(1).strip(), m.group(2).strip()))
    return out


def fixes(text: str) -> list[tuple[str, str]]:
    """Правка человека → пары замен. Не разобрали — пустой список."""
    out: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = QUOTED.search(line) or ARROW.match(line)
        if m and footage.norm(m.group(1)):
            out.append((m.group(1).strip(), m.group(2).strip()))
    return out


def remember(b, pairs: list[tuple[str, str]]) -> None:
    """Правка это словарь бренда, а не разовая замена в одном ролике."""
    if not pairs:
        return
    known = {footage.norm(src) for src, _ in lexicon(b)}
    if not b.path(LEXICON).exists():
        b.artifact(LEXICON, LEX_HEAD)
    for src, dst in pairs:
        if footage.norm(src) in known:
            continue                    # то же слово во второй раз
        known.add(footage.norm(src))
        b.append(LEXICON, f"{src} -> {dst}")


def _relex(reel: Reel, rules: list[tuple[str, str]]) -> None:
    """Субтитры ролика из сырых слов и словаря. Считается заново каждый
    раз: словарь пополняется, а `reel.subs` остаются тем, что услышано.
    """
    subs, fixed = footage.relex(reel.subs, rules)
    reel.pages = footage.pages(subs)
    reel.findings = [f for f in reel.findings if not f.startswith(LEX_NOTE)]
    if fixed:
        reel.findings.append(f"{LEX_NOTE}{fixed}")


def _cover(b, theme_id: str) -> Path | None:
    """Обложка от Дизайнера, если уже собрана. Нет — не беда, будет карточка."""
    matches = sorted(b.path("posts").glob(f"{theme_id}-*.png"))
    return matches[0] if matches else None


def _png_size(path: Path) -> tuple[int, int] | None:
    """Размер PNG из заголовка IHDR. Ради этого не нужна библиотека картинок."""
    try:
        head = path.read_bytes()[:24]
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))


def _cover_fits(cover: Path, size: tuple[int, int]) -> bool:
    """Обложка того же холста, что ролик, или соседнего.

    Дизайнер верстает под площадку и формат, и обложка поста 1080×1350
    ложится в рилс 9:16 с потерей трети ширины — вместе с заголовком.
    Показывать такую как есть нельзя: её обрубки спорят с нашим титулом.
    """
    got = _png_size(cover)
    if not got:
        return False
    return abs(got[0] / got[1] - size[0] / size[1]) < 0.05


# ── ТЗ обложки ────────────────────────────────────────────────────────
#
# У Дизайнера ТЗ площадки читает модель, поэтому оно проза. Монтаж модель
# не зовёт вовсе, значит ему нужно то же самое числами — блок ```cover в
# том же файле. Две формы одного ТЗ, и расходиться им нельзя: в файле про
# это написано прямо.
#
# Нет файла — работаем на дефолтах и говорим об этом строкой. Дизайнер в
# такой ситуации отказывается собирать, и это правильно для макета: он
# весь состоит из ТЗ. У монтажа ТЗ решает только обложку, а ролик человек
# ждёт целиком — отказать в видео из-за цвета заголовка было бы обменом
# не в его пользу.

COVER_BLOCK = re.compile(r"```cover\n(.*?)```", re.S)

# Шрифты, которые подключены в `src/Cover.tsx`. Список живёт в двух
# местах, и это осознанно: сборщик Remotion требует статических импортов,
# а питон должен уметь сказать человеку «такого нет» до рендера, а не
# показать ему обложку, молча собранную не тем шрифтом.
# Средняя ширина знака в долях кегля — у каждого шрифта своя, и кегль
# без неё промахивается: Unbounded шире Manrope почти на треть, и одна и
# та же строка либо вылезет за край, либо повиснет в воздухе. Замерено
# рендером, а не взято из метрик: считаем с трекингом -0.035em.
COVER_FONTS = {
    "Manrope": 0.63,
    "Montserrat": 0.68,
    "Unbounded": 0.86,
    "GolosText": 0.62,
}

COVER_DEFAULTS = {
    "font": "Montserrat",
    "weight": "900",
    "colors": "#F2A8A0, #E8705A, #F1C453",
    "title-color": "#FFFFFF",
    "blur": "да",
    "scrim": "0.28",
    "max-lines": "4",
}


def _cover_spec(b, plat: str, fmt: str) -> tuple[dict[str, str], str | None]:
    """Настройки обложки из ТЗ бренда. Вторым — что сказать человеку."""
    rel = f"design/platforms/{plat}-{fmt}-cover.md"
    text = b.read(rel)
    if not text:
        return dict(COVER_DEFAULTS), (
            f"ТЗ обложки нет (<code>{rel}</code>) — собрал на дефолтах")

    m = COVER_BLOCK.search(text)
    if not m:
        return dict(COVER_DEFAULTS), (
            f"в <code>{rel}</code> нет блока <code>cover</code> — "
            "собрал на дефолтах")

    spec = dict(COVER_DEFAULTS)
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        if value.strip():
            spec[key.strip()] = value.strip()

    if spec["font"] not in COVER_FONTS:
        wrong = spec["font"]
        spec["font"] = COVER_DEFAULTS["font"]
        return spec, (f"шрифт <code>{wrong}</code> из ТЗ не подключён — "
                      f"обложка набрана {spec['font']}. Подключённые: "
                      + ", ".join(COVER_FONTS))
    return spec, None


# Размытие спрашивается у ТЗ и работает только под кадром из дубля:
# обложку Дизайнера портить нечем.
#
# Определять «это запись экрана» автоматически мы пробовали дважды и оба
# раза отказались, померив на настоящем материале:
#
# - пропорция кадра: съёмка с камеры тоже бывает 16:9;
# - доля светлых точек: ваша камерная запись дала 0.69, интерфейс
#   Claude — 0.29, то есть признак работает наоборот;
# - плотность резких перепадов (текст): 0.023 у документа против 0.049 у
#   камеры, то есть не разделяет вовсе.
#
# Поэтому решает человек в ТЗ, а не догадка кода. По умолчанию размываем:
# размытый фон читается всегда, а резкий кадр с мелким чужим текстом под
# заголовком — нет. Тихая догадка, ошибающаяся в половине случаев, хуже
# честной настройки: макет с нечитаемым заголовком выглядит рабочим.
#
# `blur: авто` в ТЗ стоял с самого начала, а кода за ним не было — он
# молча означал «размывать». Теперь означает ровно то, что написано:
# лицо в кадре найдено — не размываем (ради лица кадр и выбирался),
# не найдено — размываем, потому что под текстом остался интерфейс.
# Это уже не догадка по светлоте: детектор лиц либо нашёл лицо, либо нет.

AUTO_BLUR = ("авто", "auto")
NO_BLUR = ("нет", "no", "false", "0")


def _blur(reel: Reel, at: float = 0.0) -> bool:
    if not (reel.still and reel.cover == reel.still):
        return False
    mode = (reel.spec.get("blur") or "да").lower()
    if mode in AUTO_BLUR:
        return reel.face is None
    return mode not in NO_BLUR


# ── раскладка обложки ─────────────────────────────────────────────────
#
# Кегль считает код, как у Дизайнера в `design._fit`, и по той же
# причине: модели здесь нет вовсе, а угадывать пиксели по длине строки
# всё равно нельзя — вылезший за край заголовок замечает уже человек.
#
# Правило одно: каждая строка тянется примерно на всю ширину кадра.
# Отсюда и разнобой кегля на обложках бренда — короткое слово выходит
# огромным, длинная строка мельче.

SIDE_MARGIN = 0.07        # поля по бокам, доля ширины холста
ADVANCE = 0.63            # запасная, если шрифт незнакомый
COVER_MIN, COVER_MAX = 54, 300
LINE_HEIGHT = 1.04
BLOCK_SHARE = 0.56        # сколько высоты холста отдаём блоку хука

# Предлоги и союзы не заканчивают строку: «Claude у» и «всё о» на обложке
# читаются как обрыв. Правило типографское, а не наше: короткое служебное
# слово уезжает к следующему.
GLUE = {"у", "в", "о", "к", "с", "и", "а", "на", "по", "за", "до", "из",
        "от", "для", "про", "как", "не", "но", "же", "ли", "то"}
TITLE_LIMIT = 44          # длиннее — заголовок на обложке не показываем
TITLE_MIN, TITLE_MAX = 26, 40


def _tokens(text: str) -> list[str]:
    """Слова с приклеенными к ним предлогами."""
    out: list[str] = []
    glued: list[str] = []
    for w in text.split():
        glued.append(w)
        if w.lower().strip(",.—:;") in GLUE:
            continue
        out.append(" ".join(glued))
        glued = []
    if glued:                       # фраза кончилась предлогом — так и оставим
        out.append(" ".join(glued))
    return out


def _split(text: str, lines: int) -> list[str]:
    """Разбить фразу на строки примерно равной длины, не рвя слова."""
    words = _tokens(text)
    if not words or lines <= 1:
        return [" ".join(words)] if words else []

    target = max(1, len(text) / lines)
    out: list[str] = []
    cur: list[str] = []
    for w in words:
        left = len(words) - words.index(w)
        if cur and (len(" ".join(cur)) + 1 + len(w) > target * 1.3
                    and len(out) < lines - 1
                    and left >= lines - len(out) - 1):
            out.append(" ".join(cur))
            cur = []
        cur.append(w)
    if cur:
        out.append(" ".join(cur))
    return out[:lines]


def _size(line: str, width: int, advance: float = ADVANCE) -> int:
    """Кегль, при котором строка занимает ширину кадра."""
    usable = width * (1 - 2 * SIDE_MARGIN)
    raw = usable / max(1, len(line)) / advance
    return int(max(COVER_MIN, min(COVER_MAX, raw)))


def title_size(title: str, width: int, spec: dict[str, str]) -> int:
    """Кегль мелкой строки заголовка: одна строка, а не две.

    Ширина знака у шрифтов разная, и на широком (Unbounded) заголовок
    при фиксированном кегле переносился и ломал обложку. Считаем так же,
    как крупные строки, только с потолком помельче.
    """
    advance = COVER_FONTS.get(spec.get("font", ""), ADVANCE)
    raw = width * (1 - 2 * SIDE_MARGIN) / max(1, len(title)) / advance
    return int(max(TITLE_MIN, min(TITLE_MAX, raw)))


def cover_lines(hook: str, size: tuple[int, int],
                spec: dict[str, str]) -> list[dict[str, Any]]:
    """Строки обложки: текст, цвет, кегль. Цвета идут по кругу."""
    hook = " ".join(hook.split())
    if not hook:
        return []

    colors = [c.strip() for c in spec["colors"].split(",") if c.strip()]
    limit = int(spec.get("max-lines") or 4)
    # Считаем по словам, а не по знакам: обложка бренда набрана короткими
    # строками в одно-два слова, и именно из этого берётся её лесенка
    # кеглей. Одна строка во всю ширину — это уже не обложка, а титр.
    count = max(2, min(limit, round(len(hook.split()) / 1.7)))

    advance = COVER_FONTS.get(spec.get("font", ""), ADVANCE)
    lines = [{"text": line, "color": colors[i % len(colors)],
              "size": _size(line, size[0], advance)}
             for i, line in enumerate(_split(hook, count))]

    # Блок не должен вылезти за отведённую ему высоту: строки крупные, и
    # три таких уже спорят с кадром. Ужимаем весь блок целиком, чтобы
    # лесенка кеглей осталась лесенкой.
    total = sum(l["size"] * LINE_HEIGHT for l in lines)
    room = size[1] * BLOCK_SHARE
    if total > room:
        k = room / total
        for l in lines:
            l["size"] = int(max(COVER_MIN, l["size"] * k))
    return lines


# ── лицо на обложке ───────────────────────────────────────────────────
#
# Требование бренда, а не украшение: обложка рилса это плитка в сетке
# профиля, её листают по лицу, и заголовок поверх лица портит её молча.
# Отсюда две работы, обе арифметические.
#
# Первая — кроп. Кадр из дубля ложится на холст рилса через `object-fit:
# cover`, и запись экрана 2940×1912 теряет по бокам две трети ширины. При
# кропе по центру человек, сидящий не по центру, уезжает за край: кадр «с
# лицом» превращается в кадр со стеной. Поэтому кроп ведётся за лицом —
# `coverFocus`, та же точка, что `object-position` в CSS.
#
# Вторая — куда лечь тексту. Блок хука по умолчанию стоит внизу, и на
# портретном кадре это ровно то место, где лицо. Считаем высоту блока и
# кладём его в свободную полосу: под лицом, если она больше, над лицом,
# если под ним не помещается. Не помещается нигде — ужимаем кегль тем же
# коэффициентом, что и `cover_lines`, и если и это не спасает, говорим
# человеку строкой, а не отдаём обложку с текстом по лицу.

FACE_PAD = 0.035          # доля высоты холста: воздух между лицом и текстом
COVER_INSET = 0.11        # отступ блока от низа, как в ТЗ обложки
COVER_EDGE = 0.05         # ближе к краю холста блок не подводим
TITLE_GAP = 18            # отступ под заголовком темы, как в Cover.tsx
TITLE_LINE = 1.25
BRAND_BLOCK = 52          # строка бренда с отступом, как в Cover.tsx


def cover_crop(face: footage.Face, src: tuple[int, int],
               canvas: tuple[int, int]) -> tuple[float, float, float, float]:
    """Точка кропа за лицом и полоса лица на холсте.

    Отдаёт `(focus_x, focus_y, top, bottom)`: первые два — доли для
    `object-position`, вторые — где лицо оказалось на холсте, в долях его
    высоты. Считается ровно то, что потом сделает браузер, поэтому
    правило живёт здесь, а не в композиции.
    """
    sw, sh = src
    cw, ch = canvas
    if sw <= 0 or sh <= 0:
        return 0.5, 0.5, face.y, face.y + face.h
    scale = max(cw / sw, ch / sh)
    dw, dh = sw * scale, sh * scale

    def follow(size: float, box: float, centre: float) -> float:
        """Доля object-position, при которой центр лица в центре холста."""
        room = box - size                      # ≤ 0, если кадр обрезается
        if room >= 0:
            return 0.5
        return min(1.0, max(0.0, (box / 2 - centre * size) / room))

    fx = follow(dw, cw, face.cx)
    fy = follow(dh, ch, face.cy)
    top = ((ch - dh) * fy + face.y * dh) / ch
    return fx, fy, top, top + face.h * dh / ch


def _block_height(lines: list[dict[str, Any]], title_px: int,
                  brand: bool) -> float:
    """Высота блока обложки в пикселях: заголовок, строки хука, подпись."""
    total = sum(l["size"] * LINE_HEIGHT for l in lines)
    if title_px:
        total += title_px * TITLE_LINE + TITLE_GAP
    if brand:
        total += BRAND_BLOCK
    return total


def place_cover(lines: list[dict[str, Any]], canvas: tuple[int, int],
                band: tuple[float, float] | None, *, title_px: int = 0,
                brand: bool = False) -> dict[str, Any]:
    """Куда поставить блок обложки, чтобы он не лёг на лицо.

    `band` — полоса лица на холсте в долях высоты. Нет лица — блок стоит
    внизу, как стоял. Отдаёт якорь, отступ от его края, возможно ужатые
    строки и строку «не сошлось», если места не хватило нигде.
    """
    h = canvas[1]
    out: dict[str, Any] = {"anchor": "bottom", "inset": round(h * COVER_INSET),
                           "lines": lines, "note": None}
    if not band or not lines:
        return out

    edge = h * COVER_EDGE
    top, bottom = band[0] * h, band[1] * h
    below = (h - edge) - (bottom + h * FACE_PAD)
    above = (top - h * FACE_PAD) - edge
    block = _block_height(lines, title_px, brand)

    if block <= below:
        # Низ остаётся низом: отступ по ТЗ, если он не наезжает на лицо.
        room = h - (bottom + h * FACE_PAD) - block
        out["inset"] = round(min(h * COVER_INSET, max(edge, room)))
        return out
    if block <= above:
        out["anchor"] = "top"
        room = top - h * FACE_PAD - block
        out["inset"] = round(min(h * COVER_INSET, max(edge, room)))
        return out

    # Не помещается целиком — ужимаем блок в ту полосу, что больше.
    room = max(below, above)
    if room > 0:
        k = room / block
        small = [dict(l, size=int(max(COVER_MIN, l["size"] * k)))
                 for l in lines]
        if _block_height(small, title_px, brand) <= room:
            out["lines"] = small
            out["anchor"] = "bottom" if below >= above else "top"
            out["inset"] = round(edge)
            return out

    out["note"] = ("лицо занимает почти весь кадр — текст обложки ужать "
                   "некуда, он лёг поверх. Стоит снять дубль, где лицо не "
                   "по центру, или задать обложку у Дизайнера")
    return out



# ── разбор сценария: хук и CTA из notes-файла Редактора Reels ────────

BEAT_RX = re.compile(
    r"^### (?P<title>[^·\n]+)·[^\n]+\n\n(?P<text>.+?)(?=\n### |\n## |\Z)",
    re.M | re.S)


def _beats(b, theme_id: str) -> dict[str, str]:
    """Текст блоков сценария по заголовку. Файл пишет `reels._save`,

    формат его — не наша выдумка, поэтому парсим жёстко, а не гадаем.
    """
    text = b.read(f"posts/{theme_id}-script-notes.md")
    out: dict[str, str] = {}
    for m in BEAT_RX.finditer(text):
        out[m.group("title").strip()] = m.group("text").strip()
    return out


def _token(css: str, name: str, fallback: str) -> str:
    m = re.search(rf"--{name}\s*:\s*(#[0-9A-Fa-f]{{3,8}})\b", css)
    return m.group(1) if m else fallback


def _colors(b) -> tuple[str, str]:
    """Фон карточек и цвет активного слова караоке."""
    css = b.read("design/tokens.css")
    return (_token(css, TOKEN_BG, DEFAULT_COLOR),
            _token(css, TOKEN_ACCENT, DEFAULT_ACCENT))


# ── рендер: npx remotion render подпроцессом ─────────────────────────

def _installed() -> bool:
    return (TOOLS / "node_modules").is_dir()


async def _ensure_browser() -> None:
    """Доставить chrome-headless-shell, если его ещё нет.

    Remotion рендерит своим браузером, а не системным Chrome: полный
    Chrome 151 не держит больше двух страниц из пула и роняет рендер
    строкой «Visited http://localhost:3000/index.html but got no
    response». Разбор и замер — в шапке `tools/remotion-montage/remotion.config.ts`.

    `npm install` его не тянет, качается он отдельной командой и один
    раз (~93 МБ). Зовём здесь, а не оставляем первому рендеру: молча
    съеденные скачиванием секунды ушли бы в потолок рендера, и первый
    в жизни монтаж падал бы по времени без объяснения.
    """
    proc = await asyncio.create_subprocess_exec(
        "npx", "remotion", "browser", "ensure", cwd=str(TOOLS),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise NoRenderer(
            "не поставил браузер для рендера: "
            + err.decode(errors="replace").strip()[-200:])


async def render(reel: Reel, size: tuple[int, int], *, fps: int = 30) -> Path:
    if not _installed():
        raise NotInstalled(
            "Remotion не установлен. Один раз в Terminal.app на Mac: "
            f"cd {TOOLS} && npm install")

    await _ensure_browser()

    assert reel.probe is not None and reel.cuts is not None
    w, h = size

    # Remotion в этой версии не отдаёт файлы вне tools/remotion-montage/public
    # ни голым абсолютным путём (сервер сборки резолвит его неверно), ни через
    # file:// (загрузчик ассетов принимает только http/https) — единственный
    # рабочий способ отдать внешний файл, это положить его в public/ и
    # передать в композицию только имя файла через staticFile().
    public = TOOLS / "public"
    public.mkdir(exist_ok=True)
    video_name = f"input-{reel.theme['id']}{reel.video.suffix}"
    shutil.copy2(reel.video, public / video_name)
    cover_name = None
    if reel.cover:
        cover_name = f"cover-{reel.theme['id']}{reel.cover.suffix}"
        shutil.copy2(reel.cover, public / cover_name)

    props = {
        "videoPath": video_name,
        "coverPath": cover_name,
        "coverBlur": _blur(reel),
        "coverFocus": {"x": reel.crop[0], "y": reel.crop[1]},
        "coverAnchor": reel.anchor,
        "coverInset": reel.inset or round(h * COVER_INSET),
        "title": (reel.title if 0 < len(reel.title) <= TITLE_LIMIT else None),
        "hook": reel.hook or None,
        "coverLines": reel.lines,
        "coverFont": reel.spec.get("font") or "Manrope",
        "coverWeight": int(reel.spec.get("weight") or 800),
        "titleColor": reel.spec.get("title-color") or "#FFFFFF",
        "titleSize": title_size(reel.title or "", w, reel.spec),
        "scrim": float(reel.spec.get("scrim") or 0.28),
        "cta": reel.cta or None,
        "brandColor": reel.color,
        "accentColor": reel.accent,
        "brandName": reel.theme.get("brand_name") or None,
        "width": w,
        "height": h,
        "fps": fps,
        "videoWidth": reel.probe.width,
        "videoHeight": reel.probe.height,
        "segments": [{"from": round(a, 3), "to": round(b, 3)}
                     for a, b in reel.cuts.keep],
        "pan": reel.pan,
        "pages": reel.pages,
        "introSeconds": 1.8 if (reel.cover or reel.title or reel.hook) else 0.0,
        "outroSeconds": 1.8 if reel.cta else 0.0,
    }
    props = {k: v for k, v in props.items() if v is not None}

    props_path = TOOLS / f".props-{reel.theme['id']}.json"
    props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")

    out_path = TOOLS / f".out-{reel.theme['id']}.mp4"
    out_path.unlink(missing_ok=True)

    # Потолок на один кадр: по умолчанию тридцать секунд, и на тяжёлом
    # исходнике рендер падал не потому, что завис, а потому что не успел
    # перемотать. Минуты хватает с запасом.
    cmd = ["npx", "remotion", "render", "src/index.ts", "Reel", str(out_path),
          f"--props={props_path}", "--timeout=60000"]
    frames = max(1, round((props["introSeconds"] + reel.cuts.total
                           + props["outroSeconds"]) * fps))
    limit = _timeout(frames)

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(TOOLS),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=limit)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise NoRenderer(f"рендер не уложился в {limit} секунд "
                         f"({frames} кадров)") from e
    finally:
        props_path.unlink(missing_ok=True)
        (public / video_name).unlink(missing_ok=True)
        if cover_name:
            (public / cover_name).unlink(missing_ok=True)

    if proc.returncode != 0 or not out_path.exists():
        tail = err.decode(errors="replace").strip().splitlines()[-8:]
        raise NoRenderer("Remotion не отдал файл: " + " | ".join(tail))

    return out_path


# ── сборка ────────────────────────────────────────────────────────────

# Бюджет сценария из `roles/reels.md`: дольше пятидесяти секунд ролик
# уже не рилс. Дубль длиннее едет целиком: Монтажёр на нём обрезает
# края, а резать середину по смыслу это «нарежь на рилсы» — другое
# задание и другой вход. Человек читает строкой, насколько вышли.
BUDGET_SECONDS = 50


async def analyse(reel: Reel, *, say=None,
                  rules: list[tuple[str, str]] | None = None) -> None:
    """Три прохода по дублю: паузы, активная зона, слова.

    Ни один из них не обязателен для монтажа: отказ прохода — это строка
    в находках и работа на том, что есть. Ролик без субтитров человеку
    полезнее, чем отказ смонтировать вовсе.
    """
    reel.probe = await footage.probe(reel.video)

    quiet: list[tuple[float, float]] = []
    if reel.probe.has_audio:
        quiet = await footage.silences(reel.video)
        reel.quiet = quiet
    else:
        reel.findings.append("в дубле нет звуковой дорожки: ни субтитров, "
                             "ни нарезки пауз — в кадре останется титул хука")
    reel.cuts = footage.timeline(reel.probe.duration, quiet)

    if reel.cuts.dropped > 0.5:
        reel.findings.append(
            f"вырезано {reel.cuts.dropped:.0f} с тишины "
            f"({len(quiet)} пауз), осталось {reel.cuts.total:.0f} с")

    if reel.cuts.total > BUDGET_SECONDS:
        reel.findings.append(
            f"дубль длиннее бюджета рилса: {reel.cuts.total:.0f} с против "
            f"{BUDGET_SECONDS} — смонтирован целиком. Напишите «нарежь на "
            "рилсы», и я разберу запись на отдельные ролики по смыслу")

    try:
        reel.focus = await footage.pan(reel.video)
        reel.pan = footage.cut_track(reel.focus, reel.cuts)
    except footage.NoFfmpeg as e:
        reel.findings.append(f"кадр не поедет за активной зоной: {e}")

    if not reel.probe.has_audio:
        return

    if say:
        await say("Слушаю дубль и расшифровываю речь — это самая долгая "
                  "часть, дальше рендер.")
    try:
        words = await footage.captions(reel.video)
    except footage.NoWhisper as e:
        reel.findings.append(f"субтитров не будет, транскрипт не собрался: {e}")
        return

    if not words:
        reel.findings.append("в дубле не нашлось речи — субтитров не будет")
        return
    # На столе остаётся услышанное, а не исправленное: словарь может
    # пополниться правкой человека, и тогда он применяется к тому же
    # транскрипту заново, без второй расшифровки.
    reel.heard = words
    reel.subs = footage.cut_words(words, reel.cuts)
    _relex(reel, rules or [])


async def _intro(reel: Reel, b, size: tuple[int, int]) -> None:
    """Первый кадр: что под текстом и как набран текст.

    Порядок фона ровно такой: обложка Дизайнера, свёрстанная под этот
    холст, — лучшее, что может быть, её и берём. Обложка под соседний
    холст в рилсе теряет треть ширины вместе со своим заголовком, и её
    обрубки спорят с нашим текстом — такую не берём вовсе. Остаётся кадр
    из самого дубля: нарисовать обложку монтажу нечем, зато снятое
    человеком видео у него есть.
    """
    tid = reel.theme["id"]
    plat = reel.theme.get("plat") or "instagram"
    fmt = reel.theme.get("format") or "reels"

    reel.spec, gap = _cover_spec(b, plat, fmt)
    if gap:
        reel.findings.append(gap)
    reel.lines = cover_lines(reel.hook, size, reel.spec)
    if reel.title and len(reel.title) > TITLE_LIMIT:
        reel.findings.append(
            f"заголовок темы длиннее {TITLE_LIMIT} знаков — на обложку "
            "не поставлен, там остался хук")

    cover = _cover(b, tid)
    if cover and _cover_fits(cover, size):
        reel.cover = cover
        return

    if cover:
        reel.findings.append(
            f"обложка <code>{cover.name}</code> свёрстана под другой холст — "
            "на первый кадр взят кадр из дубля")

    dur = reel.probe.duration if reel.probe else 0.0
    try:
        # Границы куска уже поставлены — кадр ищем внутри них. Обложка
        # из отрезанного хвоста показывает то, чего в ролике нет.
        shot = await footage.cover_shot(
            reel.video, reel.focus, dur, TOOLS / f".still-{tid}.png",
            window=reel.piece, quiet=reel.quiet)
    except footage.NoFfmpeg as e:
        reel.findings.append(f"кадр на обложку не снялся: {e}")
        return

    reel.still = shot.path
    reel.still_at = shot.at
    reel.cover = shot.path
    reel.face = shot.face
    if shot.note:
        reel.findings.append(shot.note)
    elif not cover:
        reel.findings.append(
            f"обложки от Дизайнера нет — на первый кадр взят кадр дубля с "
            f"лицом в кадре ({shot.at:.1f} с)")

    _place(reel, size)


def _place(reel: Reel, size: tuple[int, int]) -> None:
    """Кроп за лицом и блок текста мимо лица.

    Требование бренда к обложке рилса: кадр берётся с человеком, и слова
    не ложатся ему на лицо. Обе работы считает код — модели здесь нет
    вовсе, а промахнувшийся мимо лица заголовок человек замечает уже на
    готовом ролике.
    """
    band = None
    if reel.face and reel.probe:
        fx, fy, top, bottom = cover_crop(
            reel.face, (reel.probe.width, reel.probe.height), size)
        reel.crop = (round(fx, 4), round(fy, 4))
        band = (top, bottom)

    shown = reel.title if 0 < len(reel.title) <= TITLE_LIMIT else ""
    spot = place_cover(
        reel.lines, size, band,
        title_px=title_size(shown, size[0], reel.spec) if shown else 0,
        brand=bool(reel.theme.get("brand_name")))
    reel.lines = spot["lines"]
    reel.anchor = spot["anchor"]
    reel.inset = spot["inset"]
    if spot["note"]:
        reel.findings.append(spot["note"])
    elif band and reel.anchor == "top":
        reel.findings.append(
            "лицо в нижней половине кадра — текст обложки поднят наверх, "
            "чтобы не лечь на него")


# Меньше этого резать нечего: перекодирование куска стоит времени, а
# четыре десятых секунды в начале дубля человек не заметит.
TRIM_MIN = 0.4


async def bounds(chat_id: int, reel: Reel, *, ask: str = "",
                 say=None) -> "cut.Fragment | None":
    """Границы дубля от Монтажёра. Не вышло — работаем на целом.

    Мягко по делу: без сценария границы это украшение, а не условие. До
    03.09 дубль без сценария монтировался целиком и человек читал строку
    «первый кадр останется без слов» — если модель сейчас недоступна или
    ответила мимо, мы возвращаемся ровно к этому, а не отказываем в
    монтаже уже снятого.
    """
    if not reel.heard or reel.probe is None:
        return None
    if say:
        await say("Слушаю, где дубль начинается и где кончается.")
    try:
        frags, lost = await cut.fragments(chat_id, reel.heard,
                                          reel.probe.duration,
                                          whole=True, ask=ask)
    except Exception as e:                                   # noqa: BLE001
        log.warning("границы дубля не пришли: %s", e)
        reel.findings.append(f"границы дубля не пришли ({desk.reason(e)}) — "
                             "смонтирован целиком")
        return None
    if not frags:
        reel.findings.append("границы дубля не сошлись"
                             + (": " + "; ".join(lost[:2]) if lost else "")
                             + " — смонтирован целиком")
        return None
    return frags[0]


def _trim(reel: Reel, frag: "cut.Fragment", b) -> None:
    """Сузить дубль до границ куска: паузы, панорама, субтитры, обложка.

    Тишина по всей записи уже посчитана, и пересчитывать её внутри куска
    нельзя: порог, снятый с двадцати секунд, разойдётся с порогом всей
    записи на соседних секундах. Куску достаётся своя часть найденного,
    ровно как в нарезке.
    """
    assert reel.cuts is not None and reel.probe is not None
    cuts = footage.window(reel.cuts, frag.start, frag.end)
    reel.cuts = cuts
    reel.pan = footage.cut_track(reel.focus, cuts)
    if reel.heard:
        reel.subs = footage.cut_words(reel.heard, cuts)
        _relex(reel, lexicon(b))
    reel.piece = (frag.start, frag.end)
    tail = reel.probe.duration - frag.end
    reel.findings.append(
        f"дубль обрезан по краям: {frag.start:.1f} с в начале, "
        f"{max(tail, 0.0):.1f} с в конце, осталось {cuts.total:.0f} с")


async def build(chat_id: int, ask: str, *, say=None) -> Reel:
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиля бренда ещё нет")

    theme = _pick(chat_id, ask)
    video = _footage(b)
    beats = _beats(b, theme["id"]) if theme else {}
    color, accent = _colors(b)

    plat = (theme or {}).get("plat") or "instagram"
    fmt = (theme or {}).get("format") or "reels"
    size = design.CANVAS.get(design._key(plat, fmt)) \
        or design.CANVAS.get((plat, None)) or (1080, 1920)

    # Тема заводится после Монтажёра, а не до: заголовок и хук у дубля из
    # головы берутся из сказанного, а id темы попадает в имена всех
    # файлов рендера. Заведённая заранее пустышка пережила бы неудачный
    # монтаж строкой в базе без единого артефакта.
    reel = Reel(theme=theme or {}, video=video, color=color, accent=accent,
                hook=beats.get(HOOK_TITLE, ""), cta=beats.get(CTA_TITLE, ""))

    if say:
        name = (theme or {}).get("title") or (theme or {}).get("id") \
            or "снятый дубль"
        await say(f"Монтирую <b>{name}</b> из <code>{video.name}</code> "
                  f"({size[0]}×{size[1]}).\n"
                  "Рендер идёт дольше макета, до нескольких минут.")

    await analyse(reel, say=say, rules=lexicon(b))

    # Сценарий есть — хук и CTA уже написаны, границы ставил суфлёр, и
    # звать модель незачем. Сценария нет — дубль сняли из головы, и
    # единственный, кто может сказать, где он начинается, это Монтажёр.
    frag = None
    if not beats:
        if theme:
            reel.findings.append("сценария нет, монтирую по записи: хук и "
                                 "границы взяты из сказанного")
        frag = await bounds(chat_id, reel, ask=ask, say=say)

    if frag and not reel.hook:
        reel.hook = frag.hook

    if theme is None:
        # Дубль из головы: темы в плане нет и не будет. День остаётся
        # пустым — слот в плане ставит Стратег, а съёмка приходит от
        # человека с камерой.
        stub = frag or cut.Fragment(0.0, reel.probe.duration if reel.probe
                                    else 0.0, "",
                                    f"Дубль {desk.today(chat_id)}", "")
        reel.theme = _theme(chat_id, stub, plat, fmt)
        if say:
            await say(f"Темы под этот дубль не было, завела "
                      f"<code>{reel.theme['id']}</code>: "
                      f"<b>{reel.theme.get('title')}</b>.")

    if not reel.hook and not reel.title:
        reel.findings.append("нет ни хука, ни заголовка темы — первый кадр "
                             "останется без слов")

    narrows = bool(frag and reel.probe
                   and (frag.start > TRIM_MIN
                        or frag.end < reel.probe.duration - TRIM_MIN))
    if narrows:
        _trim(reel, frag, b)

    piece = None
    try:
        await _intro(reel, b, size)
        if narrows:
            # Кусок рендерится отдельным файлом: перемотка к середине
            # записи на каждый кадр роняет браузер, и время внутри куска
            # идёт с нуля.
            piece = TOOLS / f".clip-{reel.theme['id']}.mp4"
            await footage.clip(video, frag.start, frag.end, piece)
            reel.video = piece
            reel.cuts = reel.cuts.shift(frag.start)
        reel.out = await render(reel, size)
    except Exception as e:                                   # noqa: BLE001
        # Тему завели мы — не оставлять её в `ready` без файла: такую
        # Публикатор возьмёт в очередь как готовую к публикации.
        if theme is None:
            with db.tx() as c:
                c.execute("UPDATE themes SET status = 'failed', "
                          "skip_reason = ? WHERE id = ? AND chat_id = ?",
                          (str(e)[:200], reel.theme["id"], chat_id))
        raise
    finally:
        reel.video = video          # исходник для правки, не временный кусок
        if piece:
            piece.unlink(missing_ok=True)

    log.info("%s: смонтировано, %s", reel.theme["id"], reel.out)
    return reel


# ── нарезка длинной записи на несколько роликов ───────────────────────
#
# Одна запись — несколько рилсов. Где резать по смыслу, решает
# Монтажёр (`cut.fragments`): у него для этого есть роль и промпт. Монтаж
# получает готовый список кусков и делает свою детерминированную работу —
# режет, ищет обложку, рендерит, заводит тему в базе.
#
# Паузы ищутся по всей записи один раз, а каждому куску достаётся своя
# часть найденного (`footage.window`). Считать тишину заново на каждом
# куске значит получить разные пороги на соседних секундах одной записи.

SPLIT_WORDS = ("нареж", "нареза", "разрежь", "разбей", "на рилсы",
               "на куски", "на ролики", "по кускам")


def _clock(sec: float) -> str:
    """Секунды в «м:сс». Минуты делятся нацело, а не округляются: на
    `:.0f` запись 2:16 показывалась как 1:16, и человек искал кусок не там.
    """
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def wants_split(ask: str) -> bool:
    low = (ask or "").lower()
    return any(w in low for w in SPLIT_WORDS)


def _next_id(chat_id: int, day: str, plat: str) -> str:
    """Свободный id темы на сегодня. Формат тот же, что у плана."""
    n = 1
    while True:
        tid = f"{day}-{plat}-{n:02d}"
        if db.one("SELECT id FROM themes WHERE id = ? AND chat_id = ?",
                  tid, chat_id) is None:
            return tid
        n += 1


def _theme(chat_id: int, frag, plat: str, fmt: str) -> dict[str, Any]:
    """Завести тему под кусок записи.

    Дата остаётся пустой намеренно: слот в плане ставит Стратег, а
    нарезка приходит от человека с камерой, а не из плана. Тема ложится
    в базу готовой к публикации, но не занимает чужой день.
    """
    tid = _next_id(chat_id, desk.today(chat_id), plat)
    with db.tx() as c:
        c.execute(
            "INSERT INTO themes (id, chat_id, plat, format, title, hook, "
            "why, src, status) VALUES (?,?,?,?,?,?,?,'adhoc','ready')",
            (tid, chat_id, plat, fmt, frag.title or frag.hook, frag.hook,
             frag.why))
    row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                 tid, chat_id)
    return dict(row) if row else {"id": tid, "chat_id": chat_id,
                                  "plat": plat, "format": fmt,
                                  "title": frag.title, "hook": frag.hook}


async def split(chat_id: int, ask: str, *, say=None, deliver=None) -> list[Reel]:
    """Длинная запись → несколько готовых роликов.

    Готовый ролик отдаётся человеку сразу (`deliver`), а не в конце
    пачкой: рендер третьего куска падал, и вместе с ним пропадали два
    уже собранных. Темы в базе при этом оставались — то есть человек
    получал отказ и две записи без файлов.
    """
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиль бренда ещё не собран")

    # Запись приходит двумя путями: человек прислал файл в топик или дал
    # ссылку на свой эфир. Дальше монтаж их не различает.
    subs: list[footage.Word] = []
    url = grab.link(ask)
    if url:
        if say:
            await say(f"Скачиваю запись по ссылке <code>{url}</code> вместе "
                      "с субтитрами.")
        got = await grab.fetch(url, incoming_dir(b))
        video, subs = got.video, got.words
        if say and got.title:
            await say(f"Скачано: <b>{got.title}</b>"
                      + (f", субтитров {len(subs)} слов." if subs
                         else ", субтитров у видео нет — расшифрую сама."))
    else:
        video = _footage(b)

    probe = await footage.probe(video)
    if not probe.has_audio:
        raise NoWork("в записи нет звука — резать по смыслу нечего. "
                     "Смонтировать её целиком можно словом «смонтируй»")

    plat, fmt = "instagram", "reels"
    size = design.CANVAS.get(design._key(plat, fmt)) or (1080, 1920)
    color, accent = _colors(b)
    spec, spec_gap = _cover_spec(b, plat, fmt)

    if say:
        await say(f"Разбираю запись <code>{video.name}</code> "
                  f"({probe.duration / 60:.0f} мин): слушаю, ищу паузы, "
                  "потом выберу куски на ролики.")

    rules = lexicon(b)
    quiet = await footage.silences(video)
    whole = footage.timeline(probe.duration, quiet)
    track = await footage.pan(video)

    # Субтитры YouTube приходят с пословными таймингами, и это ровно то,
    # что нужно и для выбора кусков, и для караоке. Гонять поверх них
    # whisper значит потратить пятнадцать минут на час эфира ради того,
    # что уже приехало вместе с видео.
    words = subs or await footage.captions(video)
    if not words:
        raise NoWork("в записи не нашлось речи — резать нечего")

    if say:
        await say(f"{'Субтитры' if subs else 'Расшифровала'}: {len(words)} "
                  "слов. Выбираю куски.")

    frags, lost = await cut.fragments(chat_id, words, probe.duration,
                                      ask=ask)
    if not frags:
        raise NoWork("подходящих кусков не нашлось"
                     + (": " + "; ".join(lost[:3]) if lost else ""))

    out: list[Reel] = []
    for n, frag in enumerate(frags, 1):
        theme = _theme(chat_id, frag, plat, fmt)
        tid = theme["id"]
        cuts = footage.window(whole, frag.start, frag.end)

        reel = Reel(theme=theme, video=video, hook=frag.hook, color=color,
                    accent=accent, probe=probe, cuts=cuts, spec=spec)
        reel.focus = track
        reel.pan = footage.cut_track(track, cuts)
        reel.subs = footage.cut_words(words, cuts)
        _relex(reel, rules)
        reel.lines = cover_lines(frag.hook, size, spec)
        if spec_gap:
            reel.findings.append(spec_gap)
        if cuts.dropped > 0.5:
            reel.findings.append(f"внутри куска вырезано {cuts.dropped:.0f} с "
                                 "тишины")
        if cuts.total < cut.FRAG_MIN * 0.8:
            reel.findings.append(
                f"после вырезанных пауз кусок стал коротким: "
                f"{cuts.total:.0f} с")

        if say:
            await say(f"Собираю {n} из {len(frags)}: <b>{frag.hook}</b> "
                      f"({_clock(frag.start)}–{_clock(frag.end)}, "
                      f"{cuts.total:.0f} с).")

        piece = TOOLS / f".clip-{tid}.mp4"
        try:
            # Обложка — кадр внутри самого куска, а не начала записи, и
            # по тому же правилу, что у целого дубля: сначала кадр с
            # лицом, иначе самый спокойный.
            shot = await footage.cover_shot(
                video, track, probe.duration, TOOLS / f".still-{tid}.png",
                window=(frag.start, frag.end), quiet=quiet)
            reel.still, reel.still_at = shot.path, shot.at
            reel.cover, reel.face = shot.path, shot.face
            if shot.note:
                reel.findings.append(shot.note)
            _place(reel, size)

            # Рендерим вырезанный кусок, а не всю запись: перемотка к
            # четырнадцатой минуте на каждый кадр роняет браузер.
            await footage.clip(video, frag.start, frag.end, piece)
            reel.video = piece
            reel.cuts = cuts.shift(frag.start)
            reel.piece = (frag.start, frag.end)

            reel.out = await render(reel, size)
        except (NoRenderer, footage.NoFfmpeg) as e:
            log.warning("кусок %s не собрался: %s", tid, e)
            if reel.still:
                reel.still.unlink(missing_ok=True)
            with db.tx() as c:
                c.execute("UPDATE themes SET status = 'failed', "
                          "skip_reason = ? WHERE id = ? AND chat_id = ?",
                          (str(e)[:200], tid, chat_id))
            if say:
                await say(f"Кусок «{frag.hook}» не собрался: {e}")
            continue
        finally:
            piece.unlink(missing_ok=True)

        reel.video = video          # исходник для выгрузки, не временный кусок
        out.append(reel)
        if deliver:
            await deliver(reel, n == len(frags))

    if lost and out:
        out[0].findings.append("не взято в нарезку: " + "; ".join(lost[:4]))
    return out


# ── правка субтитров ──────────────────────────────────────────────────
#
# Правка это не новый монтаж: дубль тот же, паузы те же, кадр тот же.
# Меняются только слова, и второй раз слушать запись незачем — на столе
# лежат услышанные слова с таймингами, а словарь применяется к ним
# заново. Дорогим остаётся один рендер, и его не обойти: субтитры вшиты
# в кадр.


class NoRedo(RuntimeError):
    """Пересобрать нечем: нет исходника или нет расшифровки."""


async def refit(reel: Reel, b, *, say=None) -> Reel:
    """Тот же ролик с субтитрами по обновлённому словарю."""
    if not reel.subs:
        raise NoRedo("субтитров у этого ролика нет — править нечего")
    if reel.cuts is None or reel.probe is None:
        raise NoRedo("этот ролик собран в другом запуске завода — "
                     "пришлите дубль и повторите «смонтируй»")
    source = reel.video
    if not source.exists():
        raise NoRedo("исходный дубль уже убран — пришлите его снова "
                     "и повторите «смонтируй»")

    _relex(reel, lexicon(b))

    plat = reel.theme.get("plat") or "instagram"
    fmt = reel.theme.get("format") or "reels"
    size = design.CANVAS.get(design._key(plat, fmt)) \
        or design.CANVAS.get((plat, None)) or (1080, 1920)

    lost = "кадр обложки не сохранился — первый кадр собран без него"
    if reel.cover and not reel.cover.exists():
        reel.cover = None
        if lost not in reel.findings:       # правок может быть несколько
            reel.findings.append(lost)

    # Кусок нарезки рендерится сам по себе: `reel.cuts` у него считаются
    # от начала куска, а не от начала записи.
    clip = None
    try:
        if reel.piece:
            clip = TOOLS / f".clip-{reel.theme['id']}.mp4"
            if say:
                await say("Вырезаю кусок заново и пересобираю субтитры.")
            await footage.clip(source, reel.piece[0], reel.piece[1], clip)
            reel.video = clip
        reel.out = await render(reel, size)
    finally:
        reel.video = source
        if clip:
            clip.unlink(missing_ok=True)
    return reel


# ── выгрузка ──────────────────────────────────────────────────────────

def _save(b, reel: Reel) -> Path:
    """Готовый ролик в папку бренда.

    Исходник тут не трогаем: пока карточка на столе, человек может
    поправить слово в субтитрах, и ролик пересобирается из того же
    дубля. Исходник и кадр-обложка уходят приёмкой (`_drop`).
    """
    tid = reel.theme["id"]
    blob = reel.out.read_bytes()
    path = b.artifact(f"posts/{tid}-reel.mp4", blob)
    # Готовое видео не «черновик»: сценарий уже был ready, монтаж его не
    # понижает — desk.drafted() выставил бы status='draft', а это назад.
    with db.tx() as c:
        c.execute("UPDATE themes SET asset = ?, updated_at = datetime('now') "
                  "WHERE id = ? AND chat_id = ?",
                 (f"posts/{tid}-reel.mp4", tid, reel.theme["chat_id"]))
    reel.out.unlink(missing_ok=True)            # временный файл в tools/
    return path


def _drop(b, reel: Reel) -> None:
    """Приёмка: разобранный дубль и кадр под обложку больше не нужны.

    Удаляем только то, что лежит в папке входящих этого бренда: карточка
    могла пережить рестарт, и поднятый из базы ролик несёт `/dev/null`
    вместо пути к дублю.
    """
    if reel.still:
        reel.still.unlink(missing_ok=True)
    if b is None:
        return
    try:
        if reel.video.parent == incoming_dir(b) and reel.video.exists():
            reel.video.unlink()
    except OSError as e:                                     # noqa: BLE001
        log.warning("исходник не убрался: %s", e)


def _sweep() -> None:
    """Кадры-обложки прошлых карточек. Новый монтаж стирает стол —
    значит и пересобирать по ним уже нечего."""
    for old in TOOLS.glob(".still-*.png"):
        old.unlink(missing_ok=True)


# ── карточка и кнопки ─────────────────────────────────────────────────

def _recover(chat_id: int, theme_id: str) -> Reel | None:
    row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                 theme_id, chat_id)
    if row is None or not row["asset"] or not str(row["asset"]).endswith(".mp4"):
        return None
    b = desk.brand(chat_id)
    out = b.path(row["asset"]) if b else None
    return Reel(theme=dict(row), video=Path("/dev/null"), out=out) \
        if out and out.exists() else None


table = desk.Desk("montage", corrections="montage-corrections.md",
                  recover=_recover)


def wants_fix(chat_id: int) -> bool:
    return table.wants_fix(chat_id)


def wants_relex(chat_id: int, ask: str) -> bool:
    """Замена словами под готовой карточкой — правка субтитров.

    Кнопку «Правки» человек нажимает не всегда: «субтитры: клод ->
    Claude» приходит просто сообщением. Собирать на него ролик заново
    значит потерять минуты рендера на работу, которой не просили.
    """
    return bool(fixes(ask)) and table.get(chat_id) is not None


def _kb(theme_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Ок", callback_data=f"mont:ok:{theme_id}"),
        InlineKeyboardButton(text="✏️ Правки", callback_data=f"mont:fix:{theme_id}"),
        InlineKeyboardButton(text="📤 В очередь", callback_data=f"mont:queue:{theme_id}"),
    ]])


def caption(reel: Reel) -> str:
    t = reel.theme
    out = [f"🎬 <b>{t.get('plat')} · {t.get('format')}</b>",
           f"<code>{t['id']}</code>"]
    if reel.cuts:
        out.append(f"хронометраж {reel.cuts.total:.0f} с")
    if reel.pages:
        words = sum(len(p["words"]) for p in reel.pages)
        out.append(f"субтитры: {words} слов, {len(reel.pages)} страниц")
    if reel.pan:
        out.append("кадр едет за активной зоной")
    if reel.hook:
        out.append(f"хук: {reel.hook}")
    # Находки не режутся до трёх: строка «не сошлось» ради которой всё и
    # заведено, не должна теряться за более свежей.
    for f in reel.findings:
        out.append(f"⚠️ {f}")
    return "\n".join(out)


async def show(reg, chat_id: int, reel: Reel, topic: str = "reels") -> None:
    """Карточка, файл и кнопки. Один показ на монтаж, нарезку и правку:
    пока их было два, у правки не было кнопок вовсе."""
    path = _save(desk.brand(chat_id), reel)
    reel.out = path
    table.hold(chat_id, reel)
    await reg.say("reels", chat_id, caption(reel), topic=topic)
    await reg.send_file("reels", chat_id, path.read_bytes(), path.name,
                        topic=topic)
    await reg.say("reels", chat_id, "Принимаем?", kb=_kb(reel.theme["id"]),
                  topic=topic)


async def run(reg, chat_id: int, ask: str, topic: str = "reels") -> None:
    table.clear(chat_id)
    _sweep()

    async def say(text: str) -> None:
        await reg.say("reels", chat_id, text, topic=topic)

    try:
        reel = await build(chat_id, ask, say=say)
    except NoWork as e:
        await say(f"Монтировать нечего: {e}.")
        return
    except NoFootage as e:
        await say(str(e))
        return
    except NotInstalled as e:
        await say(f"{e}")
        return
    except (NoRenderer, footage.NoFfmpeg) as e:
        await say(f"Не смонтировалось: {e}")
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("монтаж не собрался")
        await say(f"Монтаж не собрался: {desk.reason(e)}")
        return

    reel.theme.setdefault("chat_id", chat_id)
    await show(reg, chat_id, reel, topic)


async def run_split(reg, chat_id: int, ask: str,
                    topic: str = "reels") -> None:
    """Длинная запись → пачка роликов, каждый со своей карточкой."""
    table.clear(chat_id)
    _sweep()

    async def say(text: str) -> None:
        await reg.say("reels", chat_id, text, topic=topic)

    async def deliver(reel: Reel, last: bool) -> None:
        # Исходник один на всю пачку и живёт до приёмки: по нему идёт
        # пересборка субтитров.
        await show(reg, chat_id, reel, topic)

    try:
        made = await split(chat_id, ask, say=say, deliver=deliver)
    except NoWork as e:
        await say(f"Нарезать нечего: {e}.")
        return
    except NoFootage as e:
        await say(str(e))
        return
    except NotInstalled as e:
        await say(f"{e}")
        return
    except (NoRenderer, footage.NoFfmpeg, footage.NoWhisper) as e:
        await say(f"Не нарезалось: {e}")
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("нарезка не собралась")
        await say(f"Нарезка не собралась: {desk.reason(e)}")
        return

    if not made:
        await say("Ни один кусок не собрался. Что помешало — строками выше.")
        return

    await say(f"Готово: {len(made)} ролика из одной записи. Темы заведены "
              "в базе без даты — слот в плане ставит Стратег, а не нарезка.")


async def revise(reg, chat_id: int, instruction: str,
                 topic: str = "reels") -> None:
    """Правка под карточкой. Разбираем ровно одно: слова субтитров.

    Всё остальное в монтаже это съёмка, а не текст: подвинуть кадр или
    перерезать паузы правкой словами нельзя, и обещать этого не будем.
    А неверно расслышанное имя — самая частая претензия к готовому
    ролику, и стоит она одного рендера.
    """
    reel = table.get(chat_id)

    async def say(text: str) -> None:
        await reg.say("reels", chat_id, text, topic=topic)

    if reel is None:
        await say("Этот монтаж уже неактуален.")
        return

    pairs = fixes(instruction)
    if not pairs:
        table.await_fix(chat_id, reel)          # остаёмся в правках
        await say_unsupported(reg, chat_id, topic)
        return

    table.note(chat_id, reel.theme["id"], instruction)
    b = desk.brand(chat_id)
    # Словарь пополняется до всякой пересборки: даже если этот ролик
    # пересобрать нечем, следующий дубль приедет уже с исправлением.
    remember(b, pairs)
    listed = ", ".join(f"<b>{src}</b> → {dst or '—'}" for src, dst in pairs[:5])
    await say(f"Записала в словарь субтитров: {listed}.")

    try:
        reel = await refit(reel, b, say=say)
    except NoRedo as e:
        table.hold(chat_id, reel)
        await say(f"Этот ролик пересобрать нечем: {e}. Словарь я запомнила — "
                  "в следующем ролике эти слова приедут исправленными.")
        return
    except (NoRenderer, NotInstalled, footage.NoFfmpeg) as e:
        table.hold(chat_id, reel)
        await say(f"Не пересобралось: {e}")
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("субтитры не пересобрались")
        table.hold(chat_id, reel)
        await say(f"Не пересобралось: {desk.reason(e)}")
        return

    await show(reg, chat_id, reel, topic)


async def say_unsupported(reg, chat_id: int, topic: str) -> None:
    await reg.say(
        "reels", chat_id,
        "Из правок монтажа разбираю слова субтитров: напишите строкой "
        "«как слышно -&gt; как правильно», например <code>клод -&gt; Claude</code> "
        "или «клод» на «Claude». Можно несколько строк сразу, пустая правая "
        "часть выбрасывает слово.\n\n"
        "Всё остальное — кадр, паузы, обложку — правкой словами не соберу: "
        "пришлите новый дубль и повторите «смонтируй».", topic=topic)


async def on_callback(reg, chat_id: int, action: str,
                      topic: str = "reels") -> None:
    action, _, theme_id = action.partition(":")

    async def say(text: str) -> None:
        await reg.say("reels", chat_id, text, topic=topic)

    if action == "fix":
        reel = table.get(chat_id, theme_id)
        if reel is None:
            await say("Этот монтаж уже неактуален.")
            return
        table.await_fix(chat_id, reel)
        await say_unsupported(reg, chat_id, topic)
        return

    if action not in {"ok", "queue"}:
        return

    reel = table.take(chat_id, theme_id)
    if reel is None:
        await say("Этот монтаж уже неактуален.")
        return

    # Работа принята: дубль и кадр-обложку держали ради правки субтитров,
    # больше они не нужны.
    _drop(desk.brand(chat_id), reel)

    if action == "ok":
        await say(f"Принято. Файл лежит в <code>posts/{theme_id}-reel.mp4</code> "
                  "папки бренда.")
        return

    await say(f"Передаю комплект <code>{theme_id}</code> Публикатору.")
    await publisher.run(reg, chat_id, theme_id, topic="queue")
