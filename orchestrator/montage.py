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

Язык словарю не по силам, и его правит автопроверка: дубль слушается
тремя кругами whisper, слово берётся по голосу двух из трёх
(`footage.recheck`). «Остановила» вместо «установила» и съеденное
окончание ловятся так — до рендера, а не на готовом ролике. Модель и
здесь не зовётся: круги локальные, это арифметика.

Что не поймали круги, человек правит **текстом, а не списком замен**:
кнопка отдаёт субтитры одной строкой, присланное обратно сличается с
услышанным, и разница ложится в тот же словарь (`footage.whole_pairs`,
`take_whole`). Строки словаря при этом выводит код, а не человек.
"""
from __future__ import annotations

import asyncio
import contextlib
import html as _html
import json
import logging
import os
import re
import shutil
import signal
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ROOT, cfg
from orchestrator import (album, cut, desk, design, footage, frames, grab,
                          imagegen, imagery, panelshot, publisher, strategy)
from orchestrator.desk import NoWork
from storage import db

log = logging.getLogger("montage")

TOOLS = ROOT / "tools" / "remotion-montage"

# Рендер видео это не PNG: минуты, не секунды. Сторожим здесь **движение,
# а не время**: Remotion печатает «Rendered n/m» на каждый кадр, и пока
# строки идут, рендер живой — даже если машина сегодня вдвое медленнее,
# чем в день замера.
#
# Плоский бюджет «столько-то секунд на кадр» стоил пяти готовых роликов
# в ночь на 05.09. Считался он по замеру 30.08 (0,235 с на кадр), а шли
# те рендеры по 0,29 — и все пять убивались за секунду до выхода
# процесса, уже дорендерив свой mp4 целиком. Человек читал «не уложился»,
# а на диске лежало готовое видео. Скорость машины это не граница: мёртвый
# рендер видно по молчанию, а не по секундомеру.
#
# Потолок остаётся крайним предохранителем — от рендера, который бодро
# печатает кадры, но не кончится и к утру.
RENDER_STALL = 180                # столько молчит только мёртвый рендер
RENDER_CAP_PER_FRAME = 0.6        # втрое к худшему замеренному
RENDER_CAP_MIN = 900

PROGRESS_RX = re.compile(r"Rendered (\d+)/(\d+)")


def _cap(frames: int) -> int:
    """Крайний потолок рендера. Настоящую границу держит `RENDER_STALL`."""
    return max(RENDER_CAP_MIN, int(frames * RENDER_CAP_PER_FRAME) + 120)


def _got(last: str) -> str:
    """«дошёл до 812 кадра из 1406» — по последней строке прогресса."""
    m = PROGRESS_RX.search(last or "")
    return f", дошёл до {m.group(1)} кадра из {m.group(2)}" if m else ""


# Цвета берутся из `design/tokens.css` бренда по имени токена, а не
# «первым попавшимся hex»: первым в файле лежит `--milk`, светлый фон, и
# белый текст на нём исчезает. Имя токена — это договор с Дизайнером,
# порядок строк в файле — совпадение.
DEFAULT_COLOR = "#111111"
DEFAULT_ACCENT = "#C97C5D"
DEFAULT_TEXT = "#F8F5F1"
TOKEN_BG = "graphite"
TOKEN_ACCENT = "terracotta"
TOKEN_TEXT = "milk"

# Тема верхней панели сплита и субтитров — решение человека 23.09, своя,
# не из `design/tokens.css`: тема сайта живёт неоном на графите, а этим
# двум местам заказана светлая пастель. Меняется здесь одной строкой, а
# не через бренд, — так решение осталось только в рилсах.
REEL_PANEL_BG = "#F2EBDA"     # кремовый — фон панели и подложка субтитра
REEL_PANEL_TEXT = "#2B2A26"   # тёмный графит — текст панели на кремовом
REEL_ACCENT = "#E8897F"       # пудровый коралл — кикер и активное слово
# Караоке в сплите живёт на кремовой панели, у шва — решение человека
# 06.10, обратное решению 24.09. Тогда строку увели с панели на снятый
# кадр, чтобы она не читалась частью инфографики; на вертикальном кадре
# она при этом легла человеку на лицо, и это оказалось дороже.
# На кремовом субтитр тёмный, тем же графитом, что и панель. В окнах, где
# панели нет — блок во весь кадр и хвост без панели, — шаблон меняет цвет
# на кремовый сам (`MotionTemplate`, `lowTextColor`): тёмные буквы на
# живом кадре тонут в тенях.
REEL_CAPTION_TEXT = REEL_PANEL_TEXT

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
    text: str = DEFAULT_TEXT
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
    # То же услышанное, но по словарю бренда. Ролям отдаётся оно:
    # Монтажёр, читая сырое, спотыкается о «клот» и «clot» и уносит
    # огрех расшифровки в заголовок темы, откуда он едет на обложку.
    # Сырое при этом никуда не девается — на нём стоит правка словаря.
    said: list[Any] = field(default_factory=list)
    subs: list[dict[str, Any]] = field(default_factory=list)
    # Нарезанное и по словарю — то же, что ушло в кадр и что читала
    # роль. По нему ищется фраза блока: роль цитирует «включить
    # long-running», а в сыром лежит «исключить -running», и блок
    # выпадал с «в записи не нашлось».
    spoken: list[dict[str, Any]] = field(default_factory=list)
    piece: tuple[float, float] | None = None
    face: footage.Face | None = None
    crop: tuple[float, float] = (0.5, 0.5)
    anchor: str = "bottom"
    inset: int = 0
    # Обложка пришла от Дизайнера свёрстанной целиком. Тогда свой текст
    # на первый кадр монтаж не кладёт: слова там уже есть.
    dressed: bool = False
    # Обложка сплита: кадр с лицом, знак продукта и заголовок на плашках.
    # Отдельно от `lines`/`anchor` обычной обложки — вёрстка другая.
    split_cover: dict[str, Any] = field(default_factory=dict)
    pages: list[dict[str, Any]] = field(default_factory=list)
    # Блоки панели до привязки ко времени. Живут на ролике, а не в
    # аргументе рендера: правка субтитров пересобирает ролик заново,
    # и панель обязана приехать та же — иначе человек, поправив имя,
    # получит ролик с другой инфографикой.
    slides: list[Any] = field(default_factory=list)
    # Они же во времени готового ролика. Считаются из `slides`, а не
    # хранятся вместо них: пересборка после правки режет запись заново.
    blocks: list[Block] = field(default_factory=list)
    # Маскот за спиной: картинка бренда и секунда ролика, на которой он
    # уходит. Нет поля — нет и маскота: он украшение, а не часть ролика,
    # и молчаливый дефолт посадил бы знак в кадр каждого монтажа.
    mascot: dict[str, Any] | None = None
    # Фото альбома поверх полного кадра: путь, начало и конец в секундах
    # тела ролика. Второй способ показать присланное — первый это панель
    # сплита, и выбирают между ними словами просьбы (`wants_inserts`).
    inserts: list[dict[str, Any]] = field(default_factory=list)
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


# Что монтаж считает видео, когда человек кладёт файл в папку руками.
# Присланное ботом всегда `pending.*`, но положенное руками называется
# как называлось на камере, и требовать переименования значит завести
# правило, о котором человек узнает только из молчаливого отказа.
VIDEO_SUFFIX = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi")


def _footage(b) -> Path:
    """Свежайшее видео из входящих. Имя любое, лишь бы это было видео."""
    d = incoming_dir(b)
    # Дубль из альбома — такой же кандидат, как присланный одним файлом:
    # свежий побеждает, откуда бы он ни пришёл (`album`).
    files = sorted([*(f for f in d.iterdir()
                      if f.is_file() and f.suffix.lower() in VIDEO_SUFFIX),
                    *album.takes(b)],
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise NoFootage("видео ещё не пришло. Снимите ролик по сценарию и "
                        "пришлите файлом в этот топик — или дайте ссылку "
                        "на запись")
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


def remember(b, pairs: list[tuple[str, str]]
             ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Правка это словарь бренда, а не разовая замена в одном ролике.

    Возвращает записанное и переписанное. Второе нужно потому, что
    правило на то же слово с **другой** правой частью — это не повтор, а
    отказ от прошлого решения: человек только что смотрел ролик и сказал
    иначе. Молча пропустить такую правку значит пересобрать ролик с тем
    же неверным словом и не сказать, почему.
    """
    if not pairs:
        return [], []
    if not b.path(LEXICON).exists():
        b.artifact(LEXICON, LEX_HEAD)

    lines = (b.read(LEXICON) or "").splitlines()
    # Ключ строки → её номер: правило переписывается на месте, а не
    # дописывается второй раз. Файл правят и руками, и порядок строк в
    # нём читается человеком.
    where: dict[str, int] = {}
    for i, line in enumerate(lines):
        bare = line.strip()
        if not bare or bare.startswith("#"):
            continue
        m = ARROW.match(bare)
        if m and footage.norm(m.group(1)):
            where[footage.norm(m.group(1))] = i

    added: list[tuple[str, str]] = []
    swapped: list[tuple[str, str]] = []
    for src, dst in pairs:
        key = footage.norm(src)
        if not key:
            continue
        at = where.get(key)
        if at is None:
            lines.append(f"{src} -> {dst}")
            where[key] = len(lines) - 1
            added.append((src, dst))
            continue
        was = ARROW.match(lines[at].strip())
        if was and was.group(2).strip() == dst.strip():
            continue                    # то же слово с тем же ответом
        lines[at] = f"{src} -> {dst}"
        swapped.append((src, dst))

    if added or swapped:
        b.artifact(LEXICON, "\n".join(lines))
    return added, swapped


def _relex(reel: Reel, rules: list[tuple[str, str]]) -> None:
    """Субтитры ролика из сырых слов и словаря. Считается заново каждый
    раз: словарь пополняется, а `reel.subs` остаются тем, что услышано.
    """
    subs, fixed = footage.relex(reel.subs, rules)
    reel.spoken = subs
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
    "FiraSansCondensed": 0.50,
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


def _refs_note(b, plat: str, fmt: str) -> str:
    """Хвост к строке про дефолты: есть ли с чем сверить получившееся.

    Говорится только когда ТЗ не нашлось. На каждом ролике эта строка
    была бы шумом: при живом ТЗ обложка собрана по числам бренда, и
    сверять её не с чем не приходится.
    """
    # Только точное совпадение площадки и формата: скрин поста обложке
    # рилса не референс, а другой формат под тем же словом.
    refs = design.brand_refs(b, plat, fmt, exact=True)
    if refs:
        return (". Сверить есть с чем: "
                + ", ".join(f"<code>{f.name}</code>" for f in refs[:3]))
    return ". Референсов обложки в папке бренда тоже нет — сверить не с чем"


def _cover_spec(b, plat: str, fmt: str) -> tuple[dict[str, str], str | None]:
    """Настройки обложки из ТЗ бренда. Вторым — что сказать человеку."""
    rel = f"design/platforms/{plat}-{fmt}-cover.md"
    text = b.read(rel)
    if not text:
        return dict(COVER_DEFAULTS), (
            f"ТЗ обложки нет (<code>{rel}</code>) — собрал на дефолтах"
            + _refs_note(b, plat, fmt))

    m = COVER_BLOCK.search(text)
    if not m:
        return dict(COVER_DEFAULTS), (
            f"в <code>{rel}</code> нет блока <code>cover</code> — "
            "собрал на дефолтах" + _refs_note(b, plat, fmt))

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


def _colors(b) -> tuple[str, str, str]:
    """Фон карточек, цвет активного слова караоке и цвет слов на панели.

    Третий нужен сплиту: панель светлая по графиту, и белый «на глаз»
    рядом с молоком бренда читается как другой белый.
    """
    css = b.read("design/tokens.css")
    return (_token(css, TOKEN_BG, DEFAULT_COLOR),
            _token(css, TOKEN_ACCENT, DEFAULT_ACCENT),
            _token(css, TOKEN_TEXT, DEFAULT_TEXT))


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


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Убить рендер целиком, а не одну верхнюю обёртку.

    `npx` это обёртка: работают под ней node и пул chrome-headless-shell,
    а на выходе ещё и ffmpeg. Убитый в одиночку `npx` оставлял их жить —
    они держали открытыми наши же трубы, и `communicate()` после этого не
    возвращался никогда, а освободившиеся ядра доедали у следующего куска
    нарезки. Поэтому подпроцесс идёт своей группой (`start_new_session`),
    и убивается группа.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


async def _watch(cmd: list[str], cwd: str, *,
                 cap: int, stall: int) -> tuple[int, str, str]:
    """Прогнать рендер, сторожа движение. Отдаёт код, прогресс и stderr.

    Признак жизни — любая строка от Remotion; прогресс он печатает в
    stdout по строке на кадр. Молчание дольше `stall` это смерть, и
    только она обрывает рендер досрочно.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)

    loop = asyncio.get_running_loop()
    beat = loop.time()          # когда рендер последний раз подал голос
    last = ""                   # последняя строка прогресса
    err: deque[str] = deque(maxlen=40)

    async def pump(stream: asyncio.StreamReader, sink: deque[str] | None):
        nonlocal beat, last
        async for raw in stream:
            line = raw.decode(errors="replace").strip()
            beat = loop.time()
            if not line:
                continue
            if sink is None:
                last = line
            else:
                sink.append(line)

    work = asyncio.gather(proc.wait(), pump(proc.stdout, None),
                          pump(proc.stderr, err))
    started = loop.time()
    try:
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(work), timeout=5)
                break
            except asyncio.TimeoutError:
                now = loop.time()
                if now - beat > stall:
                    why = (f"встал: {stall} с без единого кадра"
                           + _got(last))
                elif now - started > cap:
                    why = f"не уложился в {cap} с" + _got(last)
                else:
                    continue
                await _kill(proc)
                raise NoRenderer("рендер " + why)
    finally:
        work.cancel()
        with contextlib.suppress(Exception):
            await work

    return proc.returncode or 0, last, " | ".join(list(err)[-8:])


async def _ready() -> None:
    """Remotion установлен и браузер для него доставлен."""
    if not _installed():
        raise NotInstalled(
            "Remotion не установлен. Один раз в Terminal.app на Mac: "
            f"cd {TOOLS} && npm install")
    await _ensure_browser()


@contextlib.contextmanager
def _staged(files: dict[str, Path]):
    """Отдать рендеру чужие файлы и унести их за собой.

    Remotion в этой версии не отдаёт файлы вне tools/remotion-montage/public
    ни голым абсолютным путём (сервер сборки резолвит его неверно), ни через
    file:// (загрузчик ассетов принимает только http/https) — единственный
    рабочий способ отдать внешний файл, это положить его в public/ и
    передать в композицию только имя файла через staticFile().

    Уборка идёт и на упавшем рендере: в public/ лежит рабочий материал
    репозитория, и дубль на 35 МБ, оставшийся там от сорванного монтажа,
    однажды уедет в коммит.
    """
    public = TOOLS / "public"
    public.mkdir(exist_ok=True)
    done: list[Path] = []
    try:
        for name, src in files.items():
            shutil.copy2(src, public / name)
            done.append(public / name)
        yield
    finally:
        for path in done:
            path.unlink(missing_ok=True)


async def _render(composition: str, props: dict[str, Any], *,
                  key: str, frames: int) -> Path:
    """Позвать Remotion и отдать готовый файл.

    Общее у ролика и сплита: разница между ними целиком в props, а не в
    том, как рендер сторожат и что делают с оборванным файлом.
    """
    props_path = TOOLS / f".props-{key}.json"
    props_path.write_text(json.dumps(props, ensure_ascii=False),
                          encoding="utf-8")

    out_path = TOOLS / f".out-{key}.mp4"
    out_path.unlink(missing_ok=True)

    # Потолок на один кадр: по умолчанию тридцать секунд, и на тяжёлом
    # исходнике рендер падал не потому, что завис, а потому что не успел
    # перемотать. Минуты хватает с запасом.
    cmd = ["npx", "remotion", "render", "src/index.ts", composition,
           str(out_path), f"--props={props_path}", "--timeout=60000"]

    try:
        code, _, err = await _watch(cmd, str(TOOLS), cap=_cap(frames),
                                    stall=RENDER_STALL)
    except NoRenderer:
        # Оборванный рендер оставляет за собой mp4 без последних кадров.
        # Такой файл никому не нужен, а лежать он будет до следующего
        # монтажа этой же темы: имя у него от id, а не от прогона.
        out_path.unlink(missing_ok=True)
        raise
    finally:
        props_path.unlink(missing_ok=True)

    if code != 0 or not out_path.exists():
        out_path.unlink(missing_ok=True)
        raise NoRenderer("Remotion не отдал файл: " + err)

    return out_path


def _video_name(reel: Reel) -> str:
    """Имя дубля внутри public/. Одно на все композиции: имя от id темы,
    а не от прогона, и второй монтаж той же темы кладёт файл поверх."""
    return f"input-{reel.theme['id']}{reel.video.suffix}"


COVER_SPIN = 1.8            # длина первого кадра ролика, секунды


async def _cover_clip(reel: Reel, b, size: tuple[int, int], *,
                      fps: int, seconds: float) -> tuple[Path | None, str]:
    """Обложка движением: медленный наезд на утверждённый кадр.

    Берётся только свёрстанная Дизайнером обложка (`dressed`): слова на
    ней уже стоят, и клип ничего не переписывает — он двигает картинку,
    которую человек утвердил кнопкой. Обложку, которую монтаж набирает
    сам, сюда не отдаём: заголовок там идёт шрифтом бренда, а своего
    Manrope у HyperFrames нет, и человек получил бы два разных шрифта
    на одном ролике.
    """
    if not (reel.dressed and reel.cover):
        return None, ""
    spec = frames.cover_spec(
        size=size, fps=fps, seconds=seconds, lines=[], anchor=reel.anchor,
        inset=reel.inset or round(size[1] * COVER_INSET),
        focus=reel.crop, scrim=0.0, blur=False, back=reel.color,
        image=reel.cover)
    try:
        return await frames.clip(b, spec, template="cover.html",
                                 image=reel.cover, cap=frames.COVER_CAP), ""
    except frames.NoClip as e:
        return None, f"обложка без движения — {e}"


async def render(reel: Reel, b, size: tuple[int, int], *,
                 fps: int = 30) -> Path:
    await _ready()

    assert reel.probe is not None and reel.cuts is not None
    w, h = size

    video_name = _video_name(reel)
    files = {video_name: reel.video}
    cover_name = None
    if reel.cover:
        cover_name = f"cover-{reel.theme['id']}{reel.cover.suffix}"
        files[cover_name] = reel.cover

    # Фото поверх кадра едут тем же способом, что дубль и обложка: через
    # public/ и по имени файла. Имя от id темы и номера — второй монтаж
    # той же темы кладёт файл поверх, а не плодит мусор.
    inserts: list[dict[str, Any]] = []
    for i, ins in enumerate(reel.inserts):
        src = Path(ins["image"])
        name = f"insert-{reel.theme['id']}-{i}{src.suffix.lower()}"
        files[name] = src
        inserts.append({"src": name, "start": ins["start"],
                        "end": ins["end"],
                        "width": ins.get("width", INSERT_MAX_W)})

    # Маскот считается здесь, а не в сборке: маски снимаются с того файла,
    # который едет в рендер, а после обрезки краёв это уже не исходник.
    look = person = None
    masks: Path | None = None
    if reel.mascot:
        look, person, art, masks = await _mascot(reel, size, fps=fps)
        if art and look and look.get("src"):
            files[look["src"]] = art

    intro = COVER_SPIN if (reel.cover or reel.title or reel.hook) else 0.0
    clip_name = None
    if intro:
        made, note = await _cover_clip(reel, b, size, fps=fps, seconds=intro)
        if note:
            reel.findings.append(note)
        if made:
            clip_name = f"cover-clip-{reel.theme['id']}.mp4"
            files[clip_name] = made

    props = {
        "videoPath": video_name,
        "coverPath": cover_name,
        "coverClipPath": clip_name,
        "coverBlur": _blur(reel),
        "coverFocus": {"x": reel.crop[0], "y": reel.crop[1]},
        "coverAnchor": reel.anchor,
        "coverInset": reel.inset or round(h * COVER_INSET),
        "title": (None if reel.dressed
                  else reel.title if 0 < len(reel.title) <= TITLE_LIMIT
                  else None),
        "hook": reel.hook or None,
        "coverLines": reel.lines,
        "coverFont": reel.spec.get("font") or "Manrope",
        "coverWeight": int(reel.spec.get("weight") or 800),
        "titleColor": reel.spec.get("title-color") or "#FFFFFF",
        "titleSize": title_size(reel.title or "", w, reel.spec),
        "scrim": 0.0 if reel.dressed else float(reel.spec.get("scrim") or 0.28),
        "cta": reel.cta or None,
        "brandColor": reel.color,
        # Караоке — пастель рилса, не акцент бренда: см. `REEL_ACCENT`.
        "accentColor": REEL_ACCENT,
        "captionTextColor": REEL_CAPTION_TEXT,
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
        "inserts": inserts,
        "introSeconds": intro,
        "outroSeconds": 1.8 if reel.cta else 0.0,
        "mascot": look,
        "personMask": person,
    }
    props = {k: v for k, v in props.items() if v is not None}
    # «Первые десять секунд» человек считает от начала ролика, а маскот
    # живёт в записи: до него идёт обложка. Вычитаем её здесь, где длина
    # первого кадра уже известна, — иначе маскот уходит позже, чем его
    # просили, и это видно секундомером, а не на глаз.
    if look:
        look["until"] = max(1.0, round(look["until"] - props["introSeconds"], 2))
    frame_count = max(1, round((props["introSeconds"] + reel.cuts.total
                                + props["outroSeconds"]) * fps))

    try:
        with _staged(files):
            return await _render("Reel", props, key=reel.theme["id"],
                                 frames=frame_count)
    finally:
        # Маски уходят вместе с дублём: в public/ лежит рабочий материал
        # репозитория, и полтысячи PNG, оставшихся там от монтажа,
        # однажды уедут в коммит.
        if masks:
            shutil.rmtree(masks, ignore_errors=True)



# ── маскот за спиной ──────────────────────────────────────────────────
#
# «Поставь маскота за плечо» значит матирование: знак ложится поверх
# кадра, а человек — поверх знака. Силуэт даёт системная Vision тем же
# проходом, что считает лица (`footage.person_masks`), и потому чужой
# модели на диске здесь нет.
#
# Место знаку выбирает **замер перекрытия**, а не вкус: на стоп-кадре
# хорошо смотрится почти любой угол, а в движении рука в жесте закрывает
# маскота наполовину, и в кадре остаётся оранжевая полоса без глаз.
# Поэтому кандидаты меряются по тем же маскам заранее, до рендера.
#
# Из прошедших замер берётся не самый свободный угол, а самый тесный:
# знак, отодвинутый в пустоту, читается наклейкой, а стоящий у плеча —
# тем, ради чего его просили. Устройство целиком — `NOTES.md`, «Маскот».

MASCOT_ART = "montage/mascot.png"
MASCOT_UNTIL = 10.0         # секунда ролика, на которой маскот уходит
MASCOT_SIZE = 0.52          # доля ширины холста: половина кадра
# Вдвое против первой посадки. На 0,26 маскот читался наклейкой в
# углу стола — размер выбран глазами по живому ролику 20.09, и
# замер перекрытия считается уже с ним: знак стал больше, а место
# ему ищется тем же способом.
MASCOT_SPOTS = tuple((x, y)
                     for y in (0.34, 0.40, 0.46, 0.52)
                     for x in (0.18, 0.24, 0.30, 0.70, 0.76, 0.82))
MASCOT_SHUT = 0.6           # столько знака под человеком — закрыт
MASCOT_MEAN = 0.35          # среднее перекрытие, выше — не место
MASCOT_OFTEN = 0.15         # доля времени, которую он вправе быть закрыт
MASCOT_EVERY = 3            # каждый третий кадр: голова не дёргается

MASCOT_RX = re.compile(r"маскот|mascot|талисман")
MASCOT_SEC_RX = re.compile(r"(\d+)\s*(?:сек\w*|s\b)")


def wants_mascot(ask: str) -> bool:
    return bool(MASCOT_RX.search((ask or "").lower()))


def mascot_until(ask: str) -> float:
    """«на первые десять секунд» — сколько маскот живёт в ролике."""
    m = MASCOT_SEC_RX.search((ask or "").lower())
    return float(m.group(1)) if m else MASCOT_UNTIL


def _seen(video: tuple[int, int], canvas: tuple[int, int],
          focus: tuple[float, float]) -> tuple[float, float, float, float]:
    """Окно исходника, попавшее в холст, в долях кадра.

    Та же арифметика, что в `Clip.tsx`: коробка растягивается до «кадр
    целиком», нужное окно наезжает на неё сдвигом. Держать её в двух
    местах приходится потому, что кроп решает композицию, а замер
    перекрытия считается до рендера — но обе стороны берут одну формулу,
    и разъехаться им можно только правкой.
    """
    vw, vh = video
    w, h = canvas
    scale = max(w / vw, h / vh)
    box_w, box_h = vw * scale, vh * scale
    left = min(0.0, max(w - box_w, w / 2 - focus[0] * box_w))
    top = min(0.0, max(h - box_h, h / 2 - focus[1] * box_h))
    return left, top, box_w, box_h


def _focus_at(pan: list[dict[str, float]], t: float) -> tuple[float, float]:
    """Куда смотрит кадр на секунде `t` ролика. Трека нет — центр."""
    if not pan:
        return (0.5, 0.5)
    if t <= pan[0]["t"]:
        return (pan[0]["x"], pan[0]["y"])
    for a, b in zip(pan, pan[1:]):
        if t <= b["t"]:
            k = 0.0 if b["t"] == a["t"] else (t - a["t"]) / (b["t"] - a["t"])
            return (a["x"] + (b["x"] - a["x"]) * k,
                    a["y"] + (b["y"] - a["y"]) * k)
    return (pan[-1]["x"], pan[-1]["y"])


def _cover_of(cell: bytes, cols: int, rows: int,
              rect: tuple[float, float, float, float]) -> float:
    """Доля прямоугольника под силуэтом. Прямоугольник — доли кадра."""
    x0, y0, x1, y1 = rect
    c0 = max(0, min(cols - 1, int(x0 * cols)))
    c1 = max(c0 + 1, min(cols, int(x1 * cols + 0.999)))
    r0 = max(0, min(rows - 1, int(y0 * rows)))
    r1 = max(r0 + 1, min(rows, int(y1 * rows + 0.999)))
    total = 0
    for r in range(r0, r1):
        line = cell[r * cols + c0:r * cols + c1]
        total += sum(line)
    return total / (255 * (c1 - c0) * (r1 - r0))


def mascot_spot(grid: list[tuple[int, bytes]], *, cuts: footage.Timeline,
                pan: list[dict[str, float]], video: tuple[int, int],
                canvas: tuple[int, int], until: float,
                fps: int = footage.MASK_FPS, size: float = MASCOT_SIZE,
                spots: tuple[tuple[float, float], ...] = MASCOT_SPOTS,
                cols: int = footage.MASK_COLS, rows: int = footage.MASK_ROWS,
                ) -> tuple[tuple[float, float], dict[str, float]]:
    """Место маскоту по замеру перекрытия. Масок нет — угол по умолчанию.

    Отдаёт точку и замер: среднее перекрытие и долю времени, которую знак
    закрыт больше чем наполовину. Оба числа человек читает строкой — они
    объясняют, почему маскот стоит именно здесь.
    """
    if not grid:
        return spots[len(spots) // 2], {"mean": 0.0, "often": 0.0, "seen": 0}

    w, h = canvas
    side = size * w
    tally: dict[tuple[float, float], list[float]] = {s: [] for s in spots}
    for n, cell in grid:
        if n % MASCOT_EVERY:
            continue
        body = cuts.at(n / fps)
        if body is None or body > until:
            continue                      # вырезано или маскот уже ушёл
        left, top, box_w, box_h = _seen(video, canvas, _focus_at(pan, body))
        for x, y in spots:
            px, py = x * w, y * h
            rect = ((px - side / 2 - left) / box_w,
                    (py - side / 2 - top) / box_h,
                    (px + side / 2 - left) / box_w,
                    (py + side / 2 - top) / box_h)
            tally[(x, y)].append(_cover_of(cell, cols, rows, rect))

    scored = []
    for spot, seen in tally.items():
        if not seen:
            continue
        mean = sum(seen) / len(seen)
        often = sum(1 for v in seen if v > MASCOT_SHUT) / len(seen)
        scored.append((spot, mean, often, len(seen)))
    if not scored:
        return spots[len(spots) // 2], {"mean": 0.0, "often": 0.0, "seen": 0}

    free = [s for s in scored if s[1] <= MASCOT_MEAN and s[2] <= MASCOT_OFTEN]
    # Из прошедших замер берётся самый тесный: знак у плеча, а не в углу.
    # Не прошёл никто — самый свободный, и человек читает об этом строкой.
    spot, mean, often, seen = (max(free, key=lambda s: s[1]) if free
                               else min(scored, key=lambda s: s[1]))
    return spot, {"mean": mean, "often": often, "seen": seen, "free": len(free)}


def _fill_gaps(masks: Path) -> int:
    """Кадры без человека — предыдущей маской. Дыра в ряду хуже маски.

    Пропущенный файл CSS-маской не читается вовсе, слой человека на этом
    кадре ложится целиком, и маскот мигает. Соседняя маска врёт меньше.
    """
    shots = sorted(masks.glob("mask-*.png"))
    if not shots:
        return 0
    last = int(shots[-1].stem.split("-")[1])
    have = {int(p.stem.split("-")[1]) for p in shots}
    prev = shots[0]
    holes = 0
    for n in range(int(shots[0].stem.split("-")[1]), last + 1):
        path = masks / f"mask-{n:05d}.png"
        if n in have:
            prev = path
            continue
        shutil.copy2(prev, path)
        holes += 1
    return holes


async def _mascot(reel: Reel, size: tuple[int, int], *, fps: int,
                  say=None) -> tuple[dict[str, Any] | None,
                                     dict[str, Any] | None,
                                     Path | None, Path | None]:
    """Маскот, силуэт человека, картинка и папка масок в public/.

    Маски не собрались — маскот остаётся, но поверх кадра: знак перед
    человеком хуже знака за спиной, а отказ от маскота хуже обоих.
    """
    assert reel.mascot is not None and reel.cuts is not None
    tid = reel.theme["id"]
    until = min(float(reel.mascot.get("until") or MASCOT_UNTIL),
                reel.cuts.total)
    art = reel.mascot.get("art")

    work = TOOLS / f".masks-{tid}"
    staged: Path | None = None
    person: dict[str, Any] | None = None
    spot, tale = MASCOT_SPOTS[len(MASCOT_SPOTS) // 2], {}
    try:
        if say:
            await say("Снимаю силуэт, чтобы маскот встал за спину, "
                      "а не перед ней.")
        # Маски нужны ровно до ухода маскота: дальше слой человека не
        # рисуется вовсе, и считать его силуэт незачем.
        masks, made, missed, grid = await footage.person_masks(
            reel.video, work, seconds=reel.cuts.back(until) + 1 / fps)
        holes = _fill_gaps(masks)
        spot, tale = mascot_spot(
            grid, cuts=reel.cuts, pan=reel.pan,
            video=(reel.probe.width, reel.probe.height), canvas=size,
            until=until)
        staged = TOOLS / "public" / f"masks-{tid}"
        shutil.rmtree(staged, ignore_errors=True)
        shutil.move(str(masks), str(staged))
        person = {"dir": staged.name, "fps": footage.MASK_FPS}
        if missed or holes:
            reel.findings.append(
                f"человека не нашлось на {missed} кадрах из {made} — "
                "там силуэт взят с соседнего кадра")
        if tale.get("seen") and not tale.get("free"):
            reel.findings.append(
                "свободного места для маскота в кадре не нашлось: он стоит "
                f"там, где закрыт меньше всего — в среднем на "
                f"{tale['mean'] * 100:.0f}%")
    except (footage.NoFfmpeg, footage.NoVision, AssertionError) as e:
        reel.findings.append(
            f"силуэт не снялся ({e}) — маскот стоит поверх кадра, "
            "а не за спиной")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    look: dict[str, Any] = {"x": round(spot[0], 3), "y": round(spot[1], 3),
                            "size": MASCOT_SIZE, "until": round(until, 2),
                            "color": reel.accent, "opacity": 1.0,
                            "glow": 0.0 if art else 0.22,
                            "face": not art}
    name = None
    if art:
        name = f"mascot-{tid}{art.suffix}"
        look["src"] = name
    reel.findings.append(
        f"маскот в кадре первые {until:.0f} с, "
        f"{'слева' if spot[0] < 0.5 else 'справа'} от центра"
        + (f", перекрытие рукой в среднем {tale['mean'] * 100:.0f}%"
           if tale.get("seen") else ""))
    return look, person, (art if art else None), staged


# ── сплит: инфографика и говорящая голова в одном кадре ──────────────
#
# Вторая композиция Remotion (`Motion`), а не флаг у ролика: вход другой.
# Холст делится швом — на одной половине дубль, на другой панель, и
# панель тут не иллюстрация к ролику, а вторая дорожка: пока человек
# говорит «просто опиши», на панели набирается та самая строка.
#
# Арифметику делает питон, ровно как для ролика: куски отобраны, слова
# разложены по страницам, блоки расставлены по секундам **готового**
# ролика — уже после выброшенных пауз. Remotion только рисует.
#
# Откуда берутся сами блоки — чья это работа и в каком файле она лежит —
# ещё не решено. Придумывать формат здесь нельзя: заведённый молча, он
# переживёт то решение, поэтому блоки приходят входом и точка.

# Доля высоты холста под панелью. Панель стоит сверху (`head="bottom"`
# значит «голова снизу»), и число это решение человека, а не замер: с
# 0.448, снятых с кадра примера, окно головы оставалось 1060 пикселей, и
# в вертикальном дубле лицо шло крупно, а панель съедала почти половину
# ленты. 0.40 отдавало человеку 1152; 0.30 — решение человека от 06.10:
# панель это полоса над головой, а не вторая половина кадра, и 1344
# пикселя оставляют в ленте человека, а не инфографику. Ниже падать
# нельзя — кегли панели считаются от её высоты, и на 576 пикселях
# страница в три строки уже ужимается `Fit`.
MOTION_SPLIT = 0.30         # 576 из 1920

# Последние секунды ролика панель уходит, и в кадре остаётся человек.
# Ролик, который кончается записью экрана или швом посередине, не
# кончается, а обрывается: зритель дослушивает вывод, глядя на чужой
# интерфейс. Четыре секунды — это последняя фраза целиком на обычном
# темпе речи.
SOLO_SECONDS = 4.0
MOTION_HEADS = ("top", "bottom")

# Центр лица в окне головы — на этой доле его высоты сверху. Ниже
# середины окна лицо уходит к шву и под плашку субтитра, выше трети —
# макушка упирается в край. Над лицом остаётся воздух, под ним руки.
HEAD_AT = 0.38
# Чем блок занят. `card` — карточка со скрином или строками; остальные
# три двигаются сами: знак прилетает пружиной, число набирается от нуля,
# бегунок едет по ступеням. Картинка себя анимировать не умеет.
MOTION_KINDS = ("card", "icon", "counter", "scale", "media", "art")
GLOW_ALPHA = 0.13           # свечение под карточкой
CARD_ALPHA = 0.06           # подложка карточки: молоко, почти прозрачное


@dataclass
class Block:
    """Один экран панели.

    Живёт от `start` до `end` в секундах готового ролика; дырка между
    блоками — пустая панель, а не ошибка: на вдохе показывать нечего.
    `full` разворачивает панель на весь кадр, и дубль на это время
    уходит целиком — так сделан разгон в начале, где голова не нужна.
    """
    start: float
    end: float
    kicker: str = ""
    image: Path | None = None
    lines: list[str] = field(default_factory=list)
    # Список, который набирается под речь: пункт выезжает в ту секунду,
    # когда человек его называет. Секунду ищет `speak_at`; не нашлась —
    # пункт едет с началом блока, а не пропадает с панели.
    items: list[tuple[str, float | None]] = field(default_factory=list)
    full: bool = False
    kind: str = "card"
    # `counter`: до какого числа считать и на каком слове набирать.
    value: int | None = None
    value_at: float | None = None
    # `media`: запись из альбома, её длина и мерки кадра.
    video: Path | None = None
    video_len: float = 0.0
    media_size: tuple[int, int] = (0, 0)
    # `media`: где на кадре лежит слово, о котором речь. Доли кадра от
    # левого верхнего угла. Есть — шаблон наезжает на него и подчёркивает.
    spot: tuple[float, float, float, float] | None = None
    # Кадр с этим словом. Идёт второй половиной блока, после записи.
    shot: Path | None = None


def _tail(blocks: list[Block], total: float,
          solo: float = SOLO_SECONDS) -> tuple[list[Block], list[str]]:
    """Освободить конец ролика под один дубль.

    Панель заканчивается за `solo` секунд до конца, последний блок
    подрезается. Блок, который целиком попал в хвост, уходит — и это
    называется человеку: там могла лежать запись экрана, которую он
    снимал.
    """
    if not blocks or total <= solo:
        return blocks, []
    edge = total - solo
    out: list[Block] = []
    lost: list[str] = []
    for b in blocks:
        if b.start >= edge:
            what = b.lines[0] if b.lines else b.kicker or b.kind
            lost.append(f"«{str(what)[:40]}»: блок попал в концовку, где "
                        "панель уже ушла, — не показан")
            continue
        out.append(b)
    if out:
        out[-1].end = min(out[-1].end, edge)
    return out, lost


def speak_at(reel: Reel, phrase: str) -> float | None:
    """Секунда готового ролика, в которой человек произносит `phrase`.

    Дубль без расшифровки — это `None` на любую фразу, и панель тогда
    идёт по секундам блоков, без набора под речь.
    """
    return footage.anchor(reel.spoken or reel.subs, phrase)


FULL_KINDS = ("media", "art")

# Находки раскладки узнаются по началу строки: правка пересобирает
# панель, и прошлые строки про неё обязаны уйти вместе с прошлыми
# блоками. Хвосты сообщений тут не годятся — они несут цитату фразы.
LAY_NOTES = ("на панель не встало: ",)
LAY_TAILS = (": блок короче ", ": сказано вне своего блока — ")


def _is_lay_note(text: str) -> bool:
    return (text.startswith(LAY_NOTES)
            or any(tail in text for tail in LAY_TAILS))

# Короче этого блок не читается: полторы секунды на карточку со
# строкой — это мигание, а не вторая дорожка.
BLOCK_MIN = 1.5


def lay(reel: Reel, slides: list[Any]) -> tuple[list[Block], list[str]]:
    """Блоки панели во времени готового ролика.

    Роль назвала фразы, код называет секунды: `speak_at` ищет фразу в
    нарезанной расшифровке. Не нашлась — блок выбрасывается и называется
    человеку. Угадывать нельзя: блок, поставленный наугад, приедет на
    видео под чужими словами и выглядеть будет как ошибка монтажа, а не
    как ненайденная фраза.

    Конец блока это начало следующего: панель меняется, а не гаснет.
    Последний блок живёт до конца ролика.
    """
    assert reel.cuts is not None
    total = reel.cuts.total
    found: list[tuple[float, Any]] = []
    lost: list[str] = []

    for s in slides:
        at = speak_at(reel, s.phrase)
        # Материал из альбома человек снял сам, и потерять его из-за
        # неточной цитаты роли дороже, чем поставить на секунду раньше
        # или позже: ищем его фразу ещё раз, прощая слово с краёв.
        if at is None and s.kind == "media":
            at = footage.loose_anchor(reel.spoken or reel.subs, s.phrase)
            if at is not None:
                lost.append(f"«{s.phrase[:40]}»: точной фразы в записи "
                            "нет, запись экрана встала по её началу")
        if at is None:
            lost.append(f"на панель не встало: «{s.phrase[:40]}» "
                        "в записи не нашлось")
            continue
        found.append((at, s))

    # Роль выдаёт блоки по порядку речи, но порядок — это факт
    # расшифровки, а не обещание промпта.
    found.sort(key=lambda pair: pair[0])

    blocks: list[Block] = []
    for i, (at, s) in enumerate(found):
        end = found[i + 1][0] if i + 1 < len(found) else total
        if end - at < BLOCK_MIN:
            lost.append(f"«{s.phrase[:40]}»: блок короче "
                        f"{BLOCK_MIN:.1f} с — соседний занял его время")
            continue
        # Пункт, чьё слово звучит за пределами блока, на панели не
        # появится вовсе: шаблон отсчитывает его от начала блока. Такой
        # едет с началом, как ненайденный, и это называется человеку.
        items: list[tuple[str, float | None]] = []
        for text in s.items:
            when = speak_at(reel, text)
            if when is not None and not (at <= when < end):
                lost.append(f"«{text[:40]}»: сказано вне своего блока — "
                            "пункт выехал вместе с карточкой")
                when = None
            items.append((text, when))
        blocks.append(Block(
            start=at, end=end, kicker=s.kicker, lines=list(s.lines),
            items=items, kind=s.kind, image=s.image,
            # На весь кадр — только то, что смотрится само: запись и
            # картинка по теме. Слова на весь кадр прятали голову на
            # десяток секунд, и ролик открывался без человека.
            full=s.full and s.kind in FULL_KINDS,
            video=s.video, video_len=s.video_len, media_size=s.media_size,
            spot=s.spot, shot=s.shot, value=s.value,
            value_at=speak_at(reel, s.value_phrase) if s.value_phrase
            else None))

    # Лицо должно быть в кадре с первых секунд: зритель решает, смотреть
    # ли дальше, по человеку, а не по панели. Первый блок на весь кадр
    # остаётся на своей половине. Нашёл живой сплит 15.09: разгон словами
    # на весь кадр держал голову скрытой тринадцать секунд.
    if blocks and blocks[0].full:
        blocks[0].full = False
        lost.append("первый блок показан на половине, а не на весь кадр: "
                    "лицо в кадре с первых секунд")

    # Дырка в начале — пустая панель на первых секундах, и выглядит она
    # поломкой. Тянем первый блок к нулю, а не заводим пустой.
    if blocks and blocks[0].start > 0:
        blocks[0].start = 0.0
    return blocks, lost


def _rgba(color: str, alpha: float) -> str:
    """`#RRGGBB` → `rgba(...)`. Подложки панели задаются прозрачностью от
    цвета бренда, а не вторым списком hex: второй список разъедется."""
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    # Длину проверяем до разбора: `#12345` режется на куски молча и
    # уезжает в рендер третьим цветом, а не отказом.
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        return color
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha})"


def _panel_files(tid: str, blocks: list[Block]) -> dict[int, str]:
    """Имена скринов панели внутри public/. Индекс блока в имени, а не
    имя исходного файла: один и тот же скрин на двух блоках — обычное
    дело, а перетереть чужой файл чужим именем хватит одного раза."""
    return {i: f"panel-{tid}-{i}{(b.video or b.image).suffix.lower()}"
            for i, b in enumerate(blocks) if b.video or b.image}


def _mark_files(tid: str, blocks: list[Block]) -> dict[int, str]:
    """Имена кадров с подчёркнутым словом внутри public/. Отдельно от
    записи: у блока теперь два файла, и второй ложится поверх первого."""
    return {i: f"mark-{tid}-{i}{b.shot.suffix.lower()}"
            for i, b in enumerate(blocks) if b.shot and b.spot}


def _clip_files(tid: str, clips: dict[int, Path]) -> dict[int, str]:
    """Имена анимированных карточек внутри public/. Индекс блока в имени,
    как у скринов: один и тот же клип на двух блоках — обычное дело
    (кэш отдаёт файл по отпечатку, а не по месту в ролике)."""
    return {i: f"clip-{tid}-{i}.mp4" for i in clips}


def _block_props(b: Block, image: str | None,
                 mark: str | None = None,
                 clip: str | None = None) -> dict[str, Any]:
    """Блок в JSON.

    Списки отдаются всегда, даже пустыми: дефолты схемы Remotion
    подставляет в props целиком, а не внутрь элементов массива, и
    пропущенный `items` приезжал не пустым списком, а `undefined` —
    рендер падал на `.map` первого же блока без списка.
    """
    if b.kind not in MOTION_KINDS:
        raise ValueError(f"блок бывает {', '.join(MOTION_KINDS)}, "
                         f"а не «{b.kind}»")
    out: dict[str, Any] = {
        "start": round(b.start, 3),
        "end": round(b.end, 3),
        "lines": list(b.lines),
        "items": [{"text": text, "at": round(b.start if at is None else at, 3)}
                  for text, at in b.items],
        "full": b.full,
        "kind": b.kind,
    }
    if b.kicker:
        out["kicker"] = b.kicker
    if image and b.video:
        out["videoPath"] = image
        out["videoSeconds"] = round(b.video_len, 3)
    elif image:
        out["imagePath"] = image
    if b.media_size[0] and b.media_size[1]:
        out["mediaWidth"], out["mediaHeight"] = b.media_size
    if b.spot and mark:
        x, y, w, h = b.spot
        out["spot"] = {"x": round(x, 4), "y": round(y, 4),
                       "w": round(w, 4), "h": round(h, 4)}
        out["markPath"] = mark
    if clip:
        # Готовая карточка от HyperFrames. Шаблон её только играет, и
        # всё, что он рисовал бы сам, остаётся в props нетронутым: клипа
        # может не оказаться на пересборке, и тогда рисуется старым путём.
        out["clipPath"] = clip
    if b.value is not None:
        out["value"] = b.value
        # Не названа секунда — число набирается с началом блока: счётчик,
        # который так и не тронулся, хуже счётчика не на том слове.
        out["valueAt"] = round(b.start if b.value_at is None else b.value_at, 3)
    return out


def motion_props(reel: Reel, blocks: list[Block], size: tuple[int, int], *,
                 fps: int = 30, head: str = "bottom",
                 split: float = MOTION_SPLIT,
                 clips: dict[int, Path] | None = None) -> dict[str, Any]:
    """JSON для композиции `Motion`.

    Чистая функция, и это нарочно: рендер в стенд не входит, а
    разъехавшийся props приезжает молча — пустой панелью на готовом
    видео. Проверяется здесь то, что можно проверить без Remotion.
    """
    assert reel.probe is not None and reel.cuts is not None
    if head not in MOTION_HEADS:
        raise ValueError(f"голова бывает сверху или снизу, а не «{head}»")

    w, h = size
    names = _panel_files(reel.theme["id"], blocks)
    marks = _mark_files(reel.theme["id"], blocks)
    # Анимированные карточки собраны до props и приходят сюда готовыми:
    # функция остаётся чистой, и стенд проверяет форму props без рендера.
    reels = _clip_files(reel.theme["id"], clips or {})
    props = {
        "videoPath": _video_name(reel),
        "videoWidth": reel.probe.width,
        "videoHeight": reel.probe.height,
        "segments": [{"from": round(a, 3), "to": round(b, 3)}
                     for a, b in reel.cuts.keep],
        "pan": reel.pan,
        "headAnchor": head,
        "split": split,
        # Верх сплита (панель, обложка сплита, CTA-карточка в конце) и
        # субтитр — своя пастельная тема (`REEL_*`), не токены бренда
        # `design/tokens.css`: решение человека 23.09, одной строкой.
        "panelColor": REEL_PANEL_BG,
        "glowColor": _rgba(REEL_ACCENT, GLOW_ALPHA),
        "cardColor": _rgba(REEL_PANEL_TEXT, CARD_ALPHA),
        "accentColor": REEL_ACCENT,
        "textColor": REEL_PANEL_TEXT,
        "captionTextColor": REEL_CAPTION_TEXT,
        "blocks": [_block_props(b, names.get(i), marks.get(i), reels.get(i))
                   for i, b in enumerate(blocks)],
        "pages": reel.pages,
        "cta": reel.cta or None,
        "brandName": reel.theme.get("brand_name") or None,
        # Концовку дубль забирает себе целиком: панель уходит за
        # `SOLO_SECONDS` до конца тела (`_tail` подрезал под это блоки).
        "soloSeconds": SOLO_SECONDS if blocks else 0.0,
        "width": w,
        "height": h,
        "fps": fps,
        # Первый кадр сплита — его обложка (`_split_cover`): кадр с лицом,
        # знак и заголовок. Нет её — панель начинается с первого слова.
        "outroSeconds": 1.8 if reel.cta else 0.0,
        **_cover_props(reel),
    }
    return {k: v for k, v in props.items() if v is not None}


def panel_colors() -> dict[str, str]:
    """Цвета панели одним местом: их читают и props, и анимированная
    карточка. Разъехаться им значит получить ролик, где собранный клип
    лежит на панели другого фона, и видно это уже на готовом видео."""
    return {
        "panelColor": REEL_PANEL_BG,
        "accentColor": REEL_ACCENT,
        "textColor": REEL_PANEL_TEXT,
        "cardColor": _rgba(REEL_PANEL_TEXT, CARD_ALPHA),
    }


async def render_motion(reel: Reel, b, blocks: list[Block],
                        size: tuple[int, int], *, fps: int = 30,
                        head: str = "bottom",
                        split: float = MOTION_SPLIT) -> Path:
    """Сплит от props до файла. Отличается от `render` только props и
    именем композиции — сторож рендера и уборка общие.

    Перед рендером собираются анимированные карточки
    (`frames.panel_clips`): то, что нарисовано словами, набирается под
    речь, а не выезжает целиком. Карточки нет — блок рисует шаблон, как
    рисовал до этого, и причина уходит человеку строкой в находки.
    """
    await _ready()

    names = _panel_files(reel.theme["id"], blocks)
    marks = _mark_files(reel.theme["id"], blocks)
    raw = [_block_props(bl, names.get(i), marks.get(i))
           for i, bl in enumerate(blocks)]
    # Скрин страницы едет в карточку картинкой, а не именем в public/:
    # по имени файла HyperFrames его не найдёт, папка рендера своя.
    shots = {i: bl.image for i, bl in enumerate(blocks)
             if bl.image and not bl.video}
    clips, notes = await frames.panel_clips(
        b, raw, size=size, fps=fps, colors=panel_colors(), split=split,
        images=shots)
    reel.findings.extend(notes)

    props = motion_props(reel, blocks, size, fps=fps, head=head, split=split,
                         clips=clips)
    assert reel.cuts is not None

    files: dict[str, Path] = {_video_name(reel): reel.video}
    for i, name in _clip_files(reel.theme["id"], clips).items():
        files[name] = clips[i]
    for i, name in _panel_files(reel.theme["id"], blocks).items():
        src = blocks[i].video or blocks[i].image
        assert src is not None
        files[name] = src
    for i, name in _mark_files(reel.theme["id"], blocks).items():
        shot = blocks[i].shot
        assert shot is not None
        files[name] = shot
    if props.get("coverPath"):
        files[props["coverPath"]] = reel.split_cover["still"]
    if props.get("coverLogoPath"):
        files[props["coverLogoPath"]] = reel.split_cover["logo"]

    frame_count = max(1, round((reel.cuts.total + props["outroSeconds"]
                                + props["introSeconds"]) * fps))

    with _staged(files):
        return await _render("Motion", props, key=reel.theme["id"],
                             frames=frame_count)

# ── сборка ────────────────────────────────────────────────────────────

# Бюджет сценария из `roles/reels.md`: дольше пятидесяти секунд ролик
# уже не рилс. Дубль длиннее едет целиком: Монтажёр на нём обрезает
# края, а резать середину по смыслу это «нарежь на рилсы» — другое
# задание и другой вход. Человек читает строкой, насколько вышли.
BUDGET_SECONDS = 50


# Слова, которыми просят выбросить куски со взглядом вниз. Человек
# читает свой же текст с ноутбука: он при этом говорит, значит ни тишина,
# ни громкость такой кусок не поймают — ловит его наклон головы
# (`footage.looking_down`).
#
# Просьба своя, а не всегда: проход по кадрам стоит отдельной минуты на
# каждом монтаже, а дубль, снятый по памяти, вырезать нечем — там взгляд
# вниз это пауза на мысль, а не чтение.
GAZE_WORDS = ("взгляд вниз", "взгляды вниз", "взглядом вниз",
              "глаза вниз", "смотрю вниз", "смотришь вниз", "смотрит вниз",
              "смотрю в экран", "смотрю в ноутбук", "в суфлер", "суфлера")


def wants_eyes(ask: str) -> bool:
    """Просьба выбросить куски, где человек смотрит вниз."""
    low = (ask or "").lower().replace("ё", "е")
    return any(w in low for w in GAZE_WORDS)


# Меньше этого от дубля не оставляем: порог наклона снят с одной съёмки
# (`footage.GAZE_DOWN`), и на другой камере он способен признать опущенным
# всё подряд. Ролик в три секунды человек получил бы молча — а это
# поломка, которая выглядит готовой работой.
GAZE_LEAVE = 8.0


async def _gaze(reel: Reel, *, say=None) -> None:
    """Выбросить куски, где лицо опущено в экран."""
    assert reel.cuts is not None
    work = TOOLS / f".gaze-{reel.theme.get('id') or 'dub'}"
    try:
        if say:
            await say("Смотрю, где взгляд уходит вниз, — это отдельный "
                      "проход по кадрам.")
        points = await footage.head_angles(reel.video, work)
    except (footage.NoFfmpeg, footage.NoVision) as e:
        reel.findings.append(f"взгляд вниз не искала: {e}")
        return
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if not points:
        reel.findings.append("взгляд вниз не искала: лица в кадре не нашлось")
        return

    spans = footage.looking_down(points)
    if not spans:
        reel.findings.append("взглядов вниз в дубле не нашлось — "
                             "вырезать нечего")
        return

    before = reel.cuts.total
    cut_to = footage.prune(reel.cuts, spans)
    if cut_to.total < GAZE_LEAVE:
        reel.findings.append(
            f"взгляд вниз не вырезала: от дубля осталось бы "
            f"{cut_to.total:.0f} с из {before:.0f}. Порог наклона снят с "
            "другой съёмки — скорее он, чем запись")
        return

    reel.cuts = cut_to
    if reel.focus:
        reel.pan = footage.cut_track(reel.focus, reel.cuts)
    reel.findings.append(
        f"вырезано {before - reel.cuts.total:.0f} с взгляда вниз "
        f"({len(spans)} кусков); осталось {reel.cuts.total:.0f} с")


async def analyse(reel: Reel, *, say=None, ask: str = "",
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

    # Взгляд вниз ищется до расшифровки и режет ту же нарезку: слова
    # раскладываются по кускам в самом конце разбора, и вырезанное сюда
    # уже не попадёт.
    if wants_eyes(ask):
        await _gaze(reel, say=say)

    if not reel.probe.has_audio:
        return

    if say:
        await say("Слушаю дубль тремя кругами и сверяю слова между ними — "
                  "это самая долгая часть, дальше рендер.")
    # Три круга вместо одного: слово берётся по голосу двух из трёх, и
    # подмена вроде «остановила» вместо «установила» ловится до рендера, а
    # не после него (`footage.recheck`). Отказ отдельного круга это строка
    # в находках, отказ всех — прежний путь без субтитров.
    try:
        words, checked = await footage.recheck(reel.video,
                                               seconds=reel.probe.duration)
    except footage.NoWhisper as e:
        reel.findings.append(f"субтитров не будет, транскрипт не собрался: {e}")
        return
    reel.findings += checked

    if not words:
        reel.findings.append("в дубле не нашлось речи — субтитров не будет")
        return
    # Кашель и повторный заход тишиной не ловятся: первый громкий, второй
    # это нормальная речь. Оба находятся по расшифровке, и потому здесь, а
    # не в `footage.timeline` — до слов их искать нечем (`footage.junk`).
    # Кашель ищется по громкости, а не по списку тишины: он громкий, и
    # `silencedetect` рвёт тишину вокруг него надвое (`footage.coughs`).
    track = await footage.loudness_track(reel.video)
    spans, back, noise = footage.junk(words, track, quiet=quiet)

    # Кусок, названный словами в просьбе. Правило про повтор ловит заход,
    # начатый теми же словами; оборванную мысль, продолженную другими
    # словами, не ловит никто — но человек слышит её сразу и называет
    # цитатой (`footage.asked_cuts`).
    heard = [{"text": w.text, "start": w.start, "end": w.end} for w in words]
    asked, missed = footage.asked_cuts(heard, ask)
    reel.findings += missed[:2]
    if asked:
        spans = sorted(spans + asked)
        reel.findings.append(f"вырезано по просьбе кусков: {len(asked)}")

    if spans:
        before = reel.cuts.total
        reel.cuts = footage.prune(reel.cuts, spans)
        if reel.focus:
            reel.pan = footage.cut_track(reel.focus, reel.cuts)
        what = [n for n in (f"повторных заходов {back}" if back else "",
                            f"кашля и вздохов {noise}" if noise else "") if n]
        if not track:
            reel.findings.append("громкость дубля не снялась — кашель и "
                                 "вздохи остались в ролике")
        reel.findings.append(
            f"вырезано из середины {before - reel.cuts.total:.0f} с — "
            + ", ".join(what) + f"; осталось {reel.cuts.total:.0f} с")

    # На столе остаётся услышанное, а не исправленное: словарь может
    # пополниться правкой человека, и тогда он применяется к тому же
    # транскрипту заново, без второй расшифровки.
    reel.heard = words
    reel.said = footage.relex_heard(words, rules or [])
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

    Обложка Дизайнера приходит **свёрстанной целиком**: фото из
    фотобанка, рубрика, заголовок, подзаголовок. Своих слов монтаж на
    неё не кладёт — они легли бы поверх чужих, — и затемнение тоже не
    рисует: свой скрим у неё уже есть. Текст монтажа остаётся для
    запасного пути, где под ним голый кадр дубля.
    """
    tid = reel.theme["id"]
    plat = reel.theme.get("plat") or "instagram"
    fmt = reel.theme.get("format") or "reels"

    cover = _cover(b, tid)
    if cover and _cover_fits(cover, size):
        # ТЗ обложки тут не спрашиваем и строк не считаем: всё, что оно
        # решает, — как монтаж набирает свой текст, а его не будет.
        reel.cover = cover
        reel.dressed = True
        reel.findings.append(
            f"первый кадр — обложка Дизайнера <code>{cover.name}</code>")
        return

    reel.spec, gap = _cover_spec(b, plat, fmt)
    if gap:
        reel.findings.append(gap)
    reel.lines = cover_lines(reel.hook, size, reel.spec)
    if reel.title and len(reel.title) > TITLE_LIMIT:
        reel.findings.append(
            f"заголовок темы длиннее {TITLE_LIMIT} знаков — на обложку "
            "не поставлен, там остался хук")

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
        frags, lost = await cut.fragments(chat_id, reel.said or reel.heard,
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


async def _panel(chat_id: int, reel: Reel, *, ask: str = "",
                 say=None) -> None:
    """Блоки панели от Монтажёра. Не вышло — говорим и монтируем обычным.

    Мягко по делу, ровно как `bounds`: сплит это способ показать
    сказанное, а не условие монтажа. Молча свалиться в обычный ролик
    нельзя — человек просил панель и обязан узнать, что её не будет.
    """
    if not reel.subs:
        reel.findings.append("панели не будет: речи в дубле не разобрано")
        return
    if say:
        await say("Собираю панель: что показать рядом с речью.")
    b = desk.brand(chat_id)
    items = album.materials(b, reel.video) if b is not None else []
    shown = await _describe(items)
    art = imagegen.ready()
    try:
        slides, lost, cover = await cut.panel(
            chat_id, reel.said or reel.heard, ask=ask,
            materials=[d for d, _ in shown], art=art)
    except Exception as e:                                   # noqa: BLE001
        log.warning("панель не пришла: %s", e)
        reel.findings.append(f"панели не будет: блоки не пришли "
                             f"({desk.reason(e)}) — смонтирован обычным")
        return

    # Материал альбома встаёт на слайд файлом: картинкой или записью.
    for s in slides:
        if s.kind != "media":
            continue
        item, meta = items[s.media - 1], shown[s.media - 1][1]
        s.media_size = meta["size"]
        if item.kind == "video":
            s.video, s.video_len = item.path, meta["seconds"]
        else:
            s.image = item.path
    # Слово, о котором речь, ищется на записи до посадки на секунды:
    # найденное превращает запись в скриншот с наездом (`_mark`).
    if any(s.kind == "media" and s.mark for s in slides):
        if say:
            await say("Ищу на записях слова, о которых идёт речь.")
        reel.findings += (await _mark(reel, slides))[:3]


    if not items and not art:
        reel.findings.append("записей экрана нет, картинок по теме тоже: "
                             "нет GEMINI_API_KEY — панель словами")

    # Картинки до посадки на слова: `lay` переносит их со слайда на блок.
    # Недоставшееся — своей строкой, а не хвостом `lost`: там его срезал
    # бы потолок в три строки, и человек не узнал бы, почему окно пустое.
    if b is not None and any(s.url for s in slides):
        if say:
            await say("Снимаю знаки и страницы продуктов для панели.")
        reel.findings += (await panelshot.dress(b, slides))[:3]
    if b is not None and any(s.kind == "art" for s in slides):
        if say:
            await say("Рисую картинки по теме для панели.")
        reel.findings += (await panelshot.draw(
            b, slides, color=reel.color, accent=reel.accent,
            theme=str(reel.theme.get("id") or "")))[:3]
    # Картинка не нарисовалась и слов у блока нет — ставить нечего.
    slides = [s for s in slides
              if s.kind != "card" or s.lines or s.items or s.image]

    blocks, dropped = lay(reel, slides)
    blocks, cut_off = _tail(blocks, reel.cuts.total if reel.cuts else 0.0)
    reel.findings += [str(n) for n in lost[:3]] + dropped[:3] + cut_off

    # Что из альбома не доехало до панели, считаем по блокам, а не по
    # слайдам: слайд с материалом мог не встать на слова и выпасть в
    # `lay`, и тогда человек снимал экран впустую, не узнав об этом
    # (дубль 22.09: запись про проекты пропала молча).
    alive = {id(b.video) for b in blocks if b.video}
    idle = [str(s.media) for s in slides
            if s.kind == "media" and s.video is not None
            and id(s.video) not in alive]
    idle += [str(s.media) for s in slides
             if s.kind == "media" and s.video is None and s.image is None]
    if idle:
        reel.findings.append(
            "из альбома на панель не встали: "
            + ", ".join(sorted(set(idle)))
            + " — под них не нашлось фразы в речи")
    if not blocks:
        reel.findings.append("панели не будет: ни один блок не встал на "
                             "слова — смонтирован обычным")
        return
    reel.slides = slides
    reel.blocks = blocks
    reel.split_cover = dict(cover)
    if say:
        await say(f"Панель: {len(blocks)} блока по речи.")


async def _inserts(chat_id: int, reel: Reel, size: tuple[int, int], *,
                   ask: str = "", say=None) -> None:
    """Фото альбома поверх полного кадра. Не вышло — говорим и монтируем без.

    Фразу для каждого фото называет тот же Монтажёр, что собирает панель
    (`cut.panel`): работа одна и та же — что показать рядом со сказанным.
    Берём из его ответа только блоки `media`, всё остальное — карточки,
    счётчики, нарисованные картинки — здесь не нужно: человек просил
    показать своё, а не собрать инфографику.

    Конец блока тут считается не так, как у панели. У панели блок живёт
    до следующего, потому что делить холст на пустую половину нельзя.
    Накладка гаснет через `INSERT_HOLD`, и в кадре остаётся человек.
    """
    b = desk.brand(chat_id)
    items = album.materials(b, reel.video) if b is not None else []
    if not items:
        return
    if not reel.subs:
        reel.findings.append("фото поверх кадра не будет: речи в дубле не "
                             "разобрано — не на что их ставить")
        return
    if say:
        await say("Слушаю, на каких словах показать присланные фото.")

    shown = await _describe(items)
    try:
        slides, lost, _cover = await cut.panel(
            chat_id, reel.said or reel.heard, ask=ask,
            materials=[d for d, _ in shown], art=False)
    except Exception as e:                                   # noqa: BLE001
        log.warning("фразы для накладок не пришли: %s", e)
        reel.findings.append(f"фото поверх кадра не будет: фразы не пришли "
                             f"({desk.reason(e)})")
        return

    # Из всего ответа берём только материалы человека, и только картинки:
    # запись экрана накладкой поверх лица не читается — её место на
    # панели, где под неё отдана половина холста.
    found: list[tuple[float, Path, int, float]] = []
    idle: list[str] = []
    for s in slides:
        if s.kind != "media" or not (1 <= s.media <= len(items)):
            continue
        item = items[s.media - 1]
        shot = shown[s.media - 1][1]["size"]
        if item.kind != "image":
            idle.append(f"{s.media} — это запись, а не фото: "
                        "накладкой не ставлю")
            continue
        at = speak_at(reel, s.phrase)
        if at is None:
            idle.append(f"{s.media} — фразы «{s.phrase[:40]}» в записи "
                        "не нашлось")
            continue
        found.append((at, item.path, s.media, insert_width(shot, size)))

    total = reel.cuts.total if reel.cuts else 0.0
    found.sort()
    out: list[dict[str, Any]] = []
    for i, (at, path, num, wide) in enumerate(found):
        # Соседнее фото важнее полной выдержки: две накладки внахлёст
        # читаются сбоем рендера, а не решением.
        stop = found[i + 1][0] if i + 1 < len(found) else total
        end = min(at + INSERT_HOLD, stop, total)
        if end - at < 0.8:
            idle.append(f"{num} — на него осталось меньше секунды")
            continue
        out.append({"image": path, "start": round(at, 3),
                    "end": round(end, 3), "media": num, "width": wide})

    reel.inserts = out
    reel.findings += [str(n) for n in lost[:2]]
    if idle:
        reel.findings.append("поверх кадра не встали: " + "; ".join(idle[:3]))
    if not out:
        reel.findings.append("фото поверх кадра не будет: ни одно не встало "
                             "на слова — смонтирован без них")
        return
    if say:
        await say(f"Фото поверх кадра: {len(out)} на своих фразах.")


# Кадров на пробу с одной записи. Слово из меню видно почти всё время,
# а результат печатается к концу: шесть отсчётов ловят и то и другое, и
# стоят одного запуска распознавателя на всю пачку.
MARK_SHOTS = 6


async def _mark(reel: Reel, slides: list[Any]) -> list[str]:
    """Снять с записи кадр, где видно названное слово, и запомнить рамку.

    Материал с `mark` перестаёт быть записью и становится скриншотом: по
    записи глаз ищет, где происходит названное, и не находит за три
    секунды блока. Кадр с подчёркнутым словом читается сразу.

    Слово не нашлось — материал остаётся записью, и это строка человеку,
    а не тихая подмена: наезд, которого он просил, не состоялся.
    """
    lost: list[str] = []
    tid = str(reel.theme.get("id") or "mark")
    for n, s in enumerate(slides):
        if s.kind != "media" or not s.mark or s.video is None:
            continue
        shots: list[Path] = []
        try:
            length = max(0.1, float(s.video_len or 0.0))
            for i in range(MARK_SHOTS):
                at = length * (i + 0.5) / MARK_SHOTS
                shots.append(await footage.still(
                    s.video, at, TOOLS / f".mark-{tid}-{n}-{i}.png"))
            found = await footage.words_on(shots)
        except (footage.NoFfmpeg, footage.NoOcr) as e:
            for shot in shots:
                shot.unlink(missing_ok=True)
            lost.append(f"«{s.mark[:30]}» на записи не искали: "
                        f"{desk.reason(e)} — материал идёт записью")
            continue

        best: tuple[Path, footage.Spot] | None = None
        for shot, spots in zip(shots, found):
            spot = footage.find_spot(spots, s.mark)
            if spot is not None and (best is None or spot.conf > best[1].conf):
                best = (shot, spot)
        if best is None:
            for shot in shots:
                shot.unlink(missing_ok=True)
            lost.append(f"«{s.mark[:30]}» на записи не нашлось — "
                        "материал идёт записью, без наезда")
            continue

        shot, spot = best
        for other in shots:
            if other != shot:
                other.unlink(missing_ok=True)
        # Запись остаётся записью: кадр ложится поверх неё второй
        # половиной блока. Подмена выбрасывала бы то, ради чего человек
        # снимал экран, — поймано на первом сплите 22.09.
        s.shot = shot
        s.spot = (spot.x, spot.y, spot.w, spot.h)
    return lost


async def _describe(items: list[album.Item]) -> list[tuple[str, dict[str, Any]]]:
    """Материалы альбома строкой для Монтажёра и мерками для шаблона.

    Длительность записи роли нужна, чтобы не ставить минутный скринкаст
    на трёхсекундную фразу; мерки — шаблону, чтобы вписать кадр, не
    растягивая его.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for item in items:
        meta: dict[str, Any] = {"size": (0, 0), "seconds": 0.0}
        try:
            if item.kind == "video":
                pr = await footage.probe(item.path)
                meta.update(size=(pr.width, pr.height), seconds=pr.duration)
                what = f"запись экрана или видео, {pr.duration:.0f} с"
            else:
                meta["size"] = await asyncio.to_thread(imagery.measure,
                                                       item.path)
                what = "картинка"
        except Exception as e:                               # noqa: BLE001
            log.warning("материал альбома не прочитался: %s", e)
            what = "видео" if item.kind == "video" else "картинка"
        w, h = meta["size"]
        if w and h:
            what += ", вертикальная" if h > w else ", горизонтальная"
        if item.caption:
            what += f" — подпись: «{item.caption[:120]}»"
        seen = await _read_screen(item)
        if seen:
            what += "; на экране видно — " + "; ".join(seen)
        out.append((what, meta))
    return out


# Строк с экрана в описание материала. Меню целиком роли не нужно — ей
# нужно узнать, про что этот материал и какое слово на нём можно
# подчеркнуть; десяток самых заметных строк это даёт.
SCREEN_LINES = 8         # строк с одного кадра
SCREEN_SHOTS = 5         # кадров с одной записи
SCREEN_MIN = 3           # огрызки в одну-две буквы это иконки, не слова


async def _read_screen(item: album.Item) -> list[str]:
    """Что написано на материале. Не прочиталось — пусто, и это не поломка.

    Роль пишет `mark` — слово, которое монтаж подчеркнёт на скриншоте, — и
    писать его наугад нельзя: «Память» на английском интерфейсе не
    найдётся никогда, а человек увидит материал без наезда и без
    объяснения. Поэтому список слов роль получает с самого экрана.
    """
    try:
        shots: list[Path] = []
        marks: list[float] = []
        if item.kind == "video":
            pr = await footage.probe(item.path)
            for i in range(SCREEN_SHOTS):
                at = pr.duration * (i + 0.5) / SCREEN_SHOTS
                marks.append(at)
                shots.append(await footage.still(
                    item.path, at, TOOLS / f".seen-{item.path.stem}-{i}.png"))
        else:
            shots = [item.path]
            marks = [0.0]
        found = await footage.words_on(shots)
    except (footage.NoFfmpeg, footage.NoOcr, OSError) as e:
        log.warning("текст с материала не прочитался: %s", e)
        return []
    finally:
        if item.kind == "video":
            for shot in TOOLS.glob(f".seen-{item.path.stem}-*.png"):
                shot.unlink(missing_ok=True)

    # Запись экрана листают: в начале одно окно, в середине другое. Один
    # общий список слов смешивал их в кашу, и роль ставила запись про
    # проекты на разговор о памяти (дубль 22.09). Поэтому каждый кадр
    # отдаётся своей строкой, со своей секундой.
    out: list[str] = []
    for at, spots in zip(marks, found):
        lines: list[str] = []
        for s in sorted((s for s in spots if s.whole), key=lambda s: -s.h):
            text = " ".join(s.text.split())
            if len(text) < SCREEN_MIN or text in lines:
                continue
            lines.append(text)
            if len(lines) >= SCREEN_LINES:
                break
        if lines:
            out.append(f"на {int(at // 60)}:{int(at % 60):02d} — "
                       + ", ".join(lines))
    return out


def head_track(points: list[tuple[float, "footage.Face"]],
               video: tuple[int, int], size: tuple[int, int], *,
               split: float = MOTION_SPLIT) -> list["footage.Focus"]:
    """Трек окна головы в сплите: лицо на `HEAD_AT` высоты окна.

    Чистая функция: стенд проверяет арифметику без детектора. Окно головы
    — нижняя часть холста под панелью; видео вписано в него по большей
    стороне, как в `Clip`, и видна только доля кадра. Фокус это центр
    окна в долях исходного кадра, поэтому лицо поднимается в окне
    сдвигом фокуса вниз на (0.5 − HEAD_AT) видимой высоты.
    """
    vw, vh = video
    bw, bh = size[0], size[1] * (1 - split)
    if not points or not vw or not vh:
        return []
    k = max(bw / vw, bh / vh)
    seen_h = bh / (vh * k)
    track = [footage.Focus(t, round(f.cx, 4),
                           round(f.cy + (0.5 - HEAD_AT) * seen_h, 4), 1.0)
             for t, f in points]
    return footage._smooth(track, span=1)


async def _head(reel: Reel, size: tuple[int, int]) -> None:
    """Окно головы сплита — по лицу, а не по движению.

    Не нашлось лица или детектора — остаётся трек активности, и это
    называется: сплит с головой, срезанной по глаза, человек иначе
    примет за поломку шаблона.
    """
    if reel.probe is None or reel.cuts is None:
        return
    try:
        points = await footage.face_points(reel.video, reel.probe.duration,
                                           TOOLS)
    except (footage.NoVision, footage.NoFfmpeg) as e:
        reel.findings.append(f"лицо для сплита не искали ({e}) — окно "
                             "головы едет за движением")
        return
    track = head_track(points, (reel.probe.width, reel.probe.height), size)
    if not track:
        reel.findings.append("лица в дубле не нашлось — окно головы сплита "
                             "едет за движением")
        return
    reel.focus = track
    reel.pan = footage.cut_track(track, reel.cuts)


# ── обложка сплита ────────────────────────────────────────────────────
#
# Первым кадром — полноразмерный кадр дубля с лицом, сверху белая плашка
# со знаком продукта, под ней заголовок на белых плашках, слова цветом
# бренда. Так сделаны обложки референса, и в сетке профиля ролик
# узнаётся по человеку и по знаку, а не по панели, которой там не видно.

COVER_SECONDS = 1.2
COVER_FILE = "montage/cover.md"
PLATE_CHARS = 22          # строка плашки: длиннее — кегль падает, не читается
PLATE_LINES = 3

# Геометрия обложки сплита повторяет `SplitCover.tsx` в долях ширины
# холста: там те же числа, только в CSS. Считаем здесь по той же
# причине, что и у обычной обложки, — блок заголовка стоит сверху, а на
# портретном дубле сверху лицо, и текст ложится человеку на лоб молча.
# Композиция про лицо не знает вовсе, рамку лица знает питон.
PLATE_TOP = 0.10          # отступ знака от верха холста
PLATE_GAP = 0.022         # воздух между плашками
PLATE_BADGE = 0.136       # высота плашки со знаком: 0.1 кадра + 2 × 0.018 поля
PLATE_TAIL = 0.01         # отбивка под знаком, `marginBottom` в композиции
PLATE_SIZE = 0.066        # кегль строки заголовка
PLATE_BOX = 1.4           # высота плашки: 1.12 интерлиньяж + 2 × 0.14 поля


def plate_block(count: int, width: int, *, badge: bool) -> tuple[float, float]:
    """Заголовок сплита сверху: где он начинается и какой он высоты, в px.

    Повторяет колонку `SplitCover.tsx`: знак, отбивка, плашки через
    `gap`. Обе цифры нужны одному — `place_plates`.
    """
    top = width * PLATE_TOP
    if badge:
        top += width * (PLATE_BADGE + PLATE_TAIL + PLATE_GAP)
    block = (count * width * PLATE_SIZE * PLATE_BOX
             + max(0, count - 1) * width * PLATE_GAP)
    return top, block


def place_plates(count: int, canvas: tuple[int, int],
                 band: tuple[float, float] | None, *,
                 badge: bool = False) -> dict[str, Any]:
    """Куда лечь заголовку сплита, чтобы он не перекрыл лицо.

    `band` — полоса лица на холсте в долях высоты, та же, что считает
    `cover_crop`. Нет лица — заголовок стоит под знаком, как стоял.
    Отдаёт якорь, отступ от его края и строку «не сошлось», если увести
    текст оказалось некуда.

    Знак остаётся наверху всегда: он маленький, и в сетке профиля ролик
    узнаётся по нему. Уезжает только заголовок.
    """
    w, h = canvas
    top, block = plate_block(count, w, badge=badge)
    out: dict[str, Any] = {"anchor": "top", "inset": round(top), "note": None}
    if not band or count <= 0:
        return out

    # Кадр въезжает с 1.06 до 1, и полоса лица в первые кадры чуть выше
    # и крупнее. Разница — доли процента высоты, её накрывает FACE_PAD.
    face_top, face_bottom = band[0] * h, band[1] * h
    if top + block <= face_top - h * FACE_PAD:
        return out                            # под знаком и выше лица

    edge = h * COVER_EDGE
    below = (h - edge) - (face_bottom + h * FACE_PAD)
    if block <= below:
        # Под лицом на портретном дубле обычно вся нижняя треть.
        room = h - (face_bottom + h * FACE_PAD) - block
        out["anchor"] = "bottom"
        out["inset"] = round(min(h * COVER_INSET, max(edge, room)))
        return out

    out["note"] = ("заголовок обложки лёг у лица: свободной полосы под ним "
                   "не хватило. Стоит снять дубль, где лицо не по центру, "
                   "или задать обложку у Дизайнера")
    return out


def cover_default(b) -> tuple[str, str]:
    """Знак бренда по умолчанию: `logo: адрес` и `name: имя` в файле бренда.

    Роль называет продукт, когда он прозвучал. Не прозвучал — у бренда
    свой постоянный знак на обложках: у lily-space это Claude. Файла нет —
    обложка без знака, и это не поломка.
    """
    url = name = ""
    for line in (b.read(COVER_FILE) or "").splitlines() if b else []:
        key, _, value = line.partition(":")
        if key.strip().lower() == "logo":
            url = value.strip()
        elif key.strip().lower() == "name":
            name = value.strip()
    return url, name


def plate_lines(title: str, width: int = PLATE_CHARS,
                most: int = PLATE_LINES) -> list[str]:
    """Заголовок на плашки: по словам, не длиннее `width`, не больше `most`.

    Не влезло в `most` — хвост дописывается в последнюю строку, а не
    пропадает: обложка с оборванной мыслью хуже длинной плашки.
    """
    out: list[str] = []
    for word in title.split():
        if out and len(out[-1]) + 1 + len(word) <= width:
            out[-1] += " " + word
        else:
            out.append(word)
    if len(out) > most:
        out = out[:most - 1] + [" ".join(out[most - 1:])]
    return out


async def _split_cover(reel: Reel, b, size: tuple[int, int]) -> None:
    """Собрать обложку сплита. Не вышло — ролик идёт без неё, и это названо."""
    if b is None or reel.probe is None:
        return
    info = reel.split_cover
    title = str(info.get("title") or "").strip() or reel.hook or reel.title
    if not title:
        reel.findings.append("обложки нет: ни заголовка, ни хука")
        reel.split_cover = {}
        return

    tid = reel.theme["id"]
    try:
        shot = await footage.cover_shot(
            reel.video, reel.focus, reel.probe.duration,
            TOOLS / f".still-{tid}.png", window=reel.piece, quiet=reel.quiet)
    except footage.NoFfmpeg as e:
        reel.findings.append(f"обложки нет: кадр не снялся ({e})")
        reel.split_cover = {}
        return
    if shot.note:
        reel.findings.append(shot.note)
    reel.still = shot.path
    focus = (0.5, 0.35)
    band = None
    if shot.face:
        fx, fy, top, bottom = cover_crop(
            shot.face, (reel.probe.width, reel.probe.height), size)
        focus = (round(fx, 4), round(fy, 4))
        band = (top, bottom)

    url = str(info.get("url") or "").strip()
    name = str(info.get("name") or "").strip()
    if not url:
        url, name = cover_default(b)
    logo = None
    if url:
        try:
            logo = await panelshot.take(b, url, "icon")
        except panelshot.NoShot as e:
            reel.findings.append(f"знака на обложке не будет — {e}")

    accent = [str(w).strip() for w in info.get("accent") or [] if str(w).strip()]
    lines = plate_lines(title)
    spot = place_plates(len(lines), size, band, badge=bool(logo or name))
    if spot["note"]:
        reel.findings.append(spot["note"])
    reel.split_cover = {
        "still": shot.path, "focus": focus, "logo": logo, "name": name,
        "lines": lines, "accent": accent,
        "anchor": spot["anchor"], "inset": spot["inset"]}


def _cover_props(reel: Reel) -> dict[str, Any]:
    """Обложка сплита в props. Пусто — первого кадра нет."""
    c = reel.split_cover
    if not c or not c.get("still"):
        return {"introSeconds": 0.0}
    tid = reel.theme["id"]
    out: dict[str, Any] = {
        "introSeconds": COVER_SECONDS,
        "coverPath": f"cover-{tid}{Path(c['still']).suffix}",
        "coverFocus": {"x": c["focus"][0], "y": c["focus"][1]},
        "coverLines": list(c["lines"]),
        "coverAccent": list(c["accent"]),
        # Куда лёг заголовок, считает `place_plates` по рамке лица.
        # Композиция только рисует — как и у обычной обложки.
        "coverTitleAnchor": c.get("anchor", "top"),
        "coverTitleInset": c.get("inset", 0),
    }
    if c.get("logo"):
        out["coverLogoPath"] = f"cover-logo-{tid}{Path(c['logo']).suffix}"
    if c.get("name"):
        out["coverName"] = c["name"]
    return out


async def _draw(reel: Reel, b, size: tuple[int, int]) -> Path:
    """Рендер тем шаблоном, который этому ролику положен.

    Одно место, где выбирается композиция: и монтаж, и пересборка после
    правки субтитров ходят сюда. Разойдись они — правка имени в сплите
    вернула бы обычный ролик, и человек увидел бы это уже на видео.
    """
    if reel.blocks:
        return await render_motion(reel, b, reel.blocks, size)
    return await render(reel, b, size)


async def build(chat_id: int, ask: str, *, say=None,
                panel: bool = False) -> Reel:
    """Смонтировать ролик из снятого.

    `panel` — сплит: холст делится швом, на второй половине панель,
    которая идёт за речью. Блоки панели пишет Монтажёр по той же
    расшифровке, фразами, а не секундами (`cut.panel`).
    """
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиля бренда ещё нет")

    theme = _pick(chat_id, ask)

    # Запись приходит двумя путями — файлом в топик или ссылкой на эфир, —
    # и дальше монтаж их не различает. Раньше ссылку понимала только
    # нарезка, а «смонтируй по ссылке» отвечало «видео ещё не пришло»:
    # ссылка была прямо в сообщении и молча игнорировалась.
    url = grab.link(ask)
    if url:
        if say:
            await say(f"Скачиваю запись по ссылке <code>{url}</code>.")
        video = (await grab.fetch(url, incoming_dir(b))).video
    else:
        video = _footage(b)

    # Материалы в альбоме нужны только панели: прислал человек запись
    # экрана к дублю — значит, ролик сплитом, даже если слово не сказано.
    extra = album.materials(b, video)
    # Способов показать присланное два, и выбор между ними делается до
    # разбора: сплит делит холст на весь ролик, накладка выезжает поверх
    # полного кадра и уходит. Молчаливый выбор прежний — сплит; накладку
    # просят словами (`wants_inserts`).
    overlay = bool(extra) and not panel and wants_inserts(ask)
    if extra and not panel and not overlay:
        panel = True
        if say:
            await say(f"К дублю в альбоме {len(extra)} материала — "
                      "монтирую сплитом, они пойдут на панель.")
    elif overlay and say:
        await say(f"К дублю в альбоме {len(extra)} фото — положу их "
                  "накладками поверх кадра, холст делить не буду.")

    beats = _beats(b, theme["id"]) if theme else {}
    color, accent, text = _colors(b)

    plat = (theme or {}).get("plat") or "instagram"
    fmt = (theme or {}).get("format") or "reels"
    size = design.CANVAS.get(design._key(plat, fmt)) \
        or design.CANVAS.get((plat, None)) or (1080, 1920)

    # Тема заводится после Монтажёра, а не до: заголовок и хук у дубля из
    # головы берутся из сказанного, а id темы попадает в имена всех
    # файлов рендера. Заведённая заранее пустышка пережила бы неудачный
    # монтаж строкой в базе без единого артефакта.
    reel = Reel(theme=theme or {}, video=video, color=color, accent=accent,
                text=text,
                hook=beats.get(HOOK_TITLE, ""), cta=beats.get(CTA_TITLE, ""))

    if say:
        name = (theme or {}).get("title") or (theme or {}).get("id") \
            or "снятый дубль"
        await say(f"Монтирую <b>{name}</b> из <code>{video.name}</code> "
                  f"({size[0]}×{size[1]}).\n"
                  "Рендер идёт дольше макета, до нескольких минут.")

    await analyse(reel, say=say, ask=ask, rules=lexicon(b))

    # Сценарий есть — хук и CTA уже написаны, границы ставил суфлёр, и
    # звать модель незачем. Сценария нет — дубль сняли из головы, и
    # единственный, кто может сказать, где он начинается, это Монтажёр.
    frag = None
    if not beats:
        if theme:
            reel.findings.append("сценария нет, монтирую по записи: хук и "
                                 "границы взяты из сказанного")
        frag = await bounds(chat_id, reel, ask=ask, say=say)
        # Границу, попавшую в середину слова, отодвигаем на его край:
        # роль ставит секунды на слух, а край слова знает расшифровка
        # (`footage.snap`).
        if frag and reel.heard:
            frag.start, frag.end = footage.snap(reel.heard, frag.start,
                                                frag.end)

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

    # Маскота просят словами, и картинку берём брендовую: спорить с
    # маскотом бренда своим нарисованным значком значит подменять его
    # молча. Нет картинки — рисуем знак и говорим об этом строкой.
    if wants_mascot(ask):
        art = b.path(MASCOT_ART)
        reel.mascot = {"art": art if art.exists() else None,
                       "until": mascot_until(ask)}
        if not art.exists():
            reel.findings.append(
                f"картинки маскота нет (<code>{MASCOT_ART}</code> в папке "
                "бренда) — в кадре нарисованный знак")

    narrows = bool(frag and reel.probe
                   and (frag.start > TRIM_MIN
                        or frag.end < reel.probe.duration - TRIM_MIN))
    if narrows:
        _trim(reel, frag, b)

    if panel:
        await _panel(chat_id, reel, ask=ask, say=say)
        if reel.blocks:
            await _head(reel, size)
            await _split_cover(reel, b, size)
    elif overlay:
        await _inserts(chat_id, reel, size, ask=ask, say=say)

    piece = None
    try:
        # Первого кадра у сплита нет: панель начинается с первого слова,
        # а обложка это отдельная работа Дизайнера.
        if not panel:
            await _intro(reel, b, size)
        if narrows:
            # Кусок рендерится отдельным файлом: перемотка к середине
            # записи на каждый кадр роняет браузер, и время внутри куска
            # идёт с нуля.
            piece = TOOLS / f".clip-{reel.theme['id']}.mp4"
            await footage.clip(video, frag.start, frag.end, piece)
            reel.video = piece
            reel.cuts = reel.cuts.shift(frag.start)
        reel.out = await _draw(reel, b, size)
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

# Слова, после которых из одной записи делают несколько роликов. Список
# был уже фразы, которой это просят: «сделай из этого видео несколько
# рилс» не попадало сюда ни одним словом и уходило монтировать запись
# целиком одним роликом.
SPLIT_WORDS = ("нареж", "нареза", "разрежь", "разбей", "на рилсы",
               "на куски", "на ролики", "по кускам",
               "несколько рилс", "несколько ролик", "пару рилс",
               "нарезку", "клипы")


def _clock(sec: float) -> str:
    """Секунды в «м:сс». Минуты делятся нацело, а не округляются: на
    `:.0f` запись 2:16 показывалась как 1:16, и человек искал кусок не там.
    """
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


# Слова, которыми просят сплит. С нарезкой их путать нельзя: «нарежь»
# делает из одной записи несколько роликов, «смонтируй сплитом» — один
# ролик с панелью. Обе просьбы живут в одном топике, и пересечься их
# спискам значит отдать человеку не ту работу после минут рендера.
MOTION_WORDS = ("сплит", "с панелью", "панелью", "инфографик",
                "с графикой", "пополам")


def wants_motion(ask: str) -> bool:
    low = (ask or "").lower()
    return any(w in low for w in MOTION_WORDS)


# Слова, которыми просят показать присланное фото поверх полного кадра,
# а не панелью сплита. Со сплитом их путать нельзя по той же причине,
# по какой сплит нельзя путать с нарезкой: обе просьбы живут в одном
# топике, и разойтись они должны до минут рендера, а не после.
#
# Молчаливый выбор остаётся прежним — сплит: так работает путь записи
# экрана, ради которого альбом и заведён. Накладка это просьба словами.
INSERT_WORDS = ("накладк", "фото сверху", "фотки сверху", "картинки сверху",
                "поверх кадра", "не сплитом", "без сплита", "без панели")

# Сколько фото висит в кадре. Три с половиной секунды это одна фраза:
# меньше — человек не успевает прочитать надпись на картинке, больше —
# она начинает спорить с лицом, ради которого дубль и снят.
INSERT_HOLD = 3.5

# Сколько холста вправе занять накладка: по ширине и по высоте. Высота
# тут главнее, и оплачена она первым же живым роликом: вертикальный кадр
# 9:16, вписанный по одной ширине в 0,66, занял две трети высоты и
# закрыл лицо — то есть ровно то, ради чего дубль снят. Вписываем в
# рамку по меньшей из двух долей, как `object-fit: contain`.
INSERT_MAX_W = 0.66
INSERT_MAX_H = 0.42


def insert_width(shot: tuple[int, int], canvas: tuple[int, int]) -> float:
    """Ширина накладки долей холста. Мерок нет — вписываем по ширине."""
    w, h = shot
    cw, ch = canvas
    if w <= 0 or h <= 0:
        return INSERT_MAX_W
    # Доля ширины, при которой высота картинки ровно `INSERT_MAX_H`.
    by_height = INSERT_MAX_H * ch * w / (h * cw)
    return round(min(INSERT_MAX_W, by_height), 4)


def wants_inserts(ask: str) -> bool:
    low = (ask or "").lower()
    return any(w in low for w in INSERT_WORDS)


def wants_split(ask: str) -> bool:
    low = (ask or "").lower()
    return any(w in low for w in SPLIT_WORDS)


def _theme(chat_id: int, frag, plat: str, fmt: str) -> dict[str, Any]:
    """Завести тему под кусок записи.

    Тема ложится в базу готовой к публикации: работа уже сделана, резать
    и рендерить осталось коду. Всё остальное — свободный id, пустая дата,
    `src = 'adhoc'` — общее правило темы вне плана, и живёт оно в
    `desk.adhoc`: тем же швом заводится тема под пост, которого не было
    в плане.
    """
    return desk.adhoc(chat_id, plat=plat, fmt=fmt,
                      title=frag.title or frag.hook, hook=frag.hook,
                      why=frag.why, status="ready")


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
    color, accent, text = _colors(b)
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
        # Тот же край слова, что и у одного дубля: граница куска,
        # названная ролью на слух, не рвёт слово пополам (`footage.snap`).
        frag.start, frag.end = footage.snap(words, frag.start, frag.end)
        theme = _theme(chat_id, frag, plat, fmt)
        tid = theme["id"]
        cuts = footage.window(whole, frag.start, frag.end)

        reel = Reel(theme=theme, video=video, hook=frag.hook, color=color,
                    accent=accent, text=text, probe=probe, cuts=cuts,
                    spec=spec)
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

            reel.out = await render(reel, b, size)
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
        if reel.dressed:
            # Обложку Дизайнера унесли между рендерами: свой текст на
            # первый кадр монтаж не клал, и без него кадр остался бы
            # пустым. Набираем его заново, как на запасном пути.
            reel.dressed = False
            reel.lines = cover_lines(reel.hook, size, reel.spec)
        if lost not in reel.findings:       # правок может быть несколько
            reel.findings.append(lost)

    # Панель считается от фраз заново: слова те же, но секунды ролика
    # после пересборки свои, и старые блоки встали бы мимо речи.
    if reel.slides:
        reel.blocks, lost = lay(reel, reel.slides)
        # Находки раскладки — тоже заново. Правка словаря меняет текст,
        # в котором ищется фраза, и выпавший блок после неё встаёт. Пока
        # старые строки доживали до карточки, человек читал «на панель
        # не встало» на ролике, где встало всё.
        reel.findings = [f for f in reel.findings if not _is_lay_note(f)]
        reel.findings.extend(lost)

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
        reel.out = await _draw(reel, b, size)
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
    # `asset` тут НЕ трогаем, и это важнее, чем кажется. Поле читается как
    # текст в трёх местах — `publisher.collect`, `editor.revise`,
    # `design.build`, — и все три делают `b.read()`, то есть
    # `read_text(utf-8)`. Путь к mp4 в этом поле роняет их
    # `UnicodeDecodeError` на первом же нетекстовом байте, а колбэки в
    # `bots/handlers.py` ничем не обёрнуты: человек нажимает «В очередь» и
    # не получает вообще ничего. Молчание неотличимо от поломки.
    #
    # Вторая потеря тише и хуже: у темы из плана в `asset` лежал сценарий
    # (`posts/{id}-script.md`), и перезапись уносила ссылку на него.
    #
    # Ролик ищется по имени файла — `posts/{id}-reel.mp4`, — тем же швом,
    # каким Публикатор ищет макеты. Готовое видео при этом не «черновик»:
    # сценарий уже был ready, и монтаж его не понижает.
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
        album.drop(b, reel.video)
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
    """Карточка, пережившая рестарт: тема из базы, ролик с диска.

    Ролик ищется **файлом**, а не полем `asset`. Раньше искался полем — и
    не находился никогда: путь к mp4 в `asset` роняет чтение как текст в
    трёх местах, поэтому `_save` его туда намеренно не пишет. С того дня
    любая кнопка под пережившей рестарт карточкой отвечала «этот монтаж
    уже неактуален», включая «Правки»: словарь субтитров после
    перезапуска завода поправить было нельзя вовсе.
    """
    row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                 theme_id, chat_id)
    b = desk.brand(chat_id)
    if row is None or b is None:
        return None
    out = publisher.reel_path(b, theme_id)
    return Reel(theme=dict(row), video=Path("/dev/null"), out=out) \
        if out else None


table = desk.Desk("montage", corrections="montage-corrections.md",
                  recover=_recover)


def wants_fix(chat_id: int) -> bool:
    return table.wants_fix(chat_id)


# Слова, которыми просят монтаж заново. Нужны, чтобы выйти из режима
# правки: пока он взведён, сообщение в топике разбирается как замена
# слов, а неразобранная замена взводит его снова. Человек, попросивший
# смонтировать, получал справку про формат правки — и получал её на
# каждую следующую попытку, потому что выхода из режима не было вовсе.
NEW_WORDS = ("смонтируй", "смонтировать", "собери ролик", "собери видео",
             "сделай монтаж", "нареж", "нареза", "разрежь", "разбей",
             "сплит", "с панелью", "инфографик")


def wants_new(ask: str) -> bool:
    """Просьба смонтировать заново, а не поправить слова."""
    low = (ask or "").lower()
    return any(w in low for w in NEW_WORDS)


def leave_fix(chat_id: int) -> None:
    """Выйти из режима правки, не трогая стол.

    Ролик со стола не убираем: человек мог попросить новый монтаж и
    вернуться к старой карточке кнопкой.
    """
    table.done_fix(chat_id)
    leave_whole(chat_id)


def wants_relex(chat_id: int, ask: str) -> bool:
    """Замена словами под готовой карточкой — правка субтитров.

    Кнопку «Правки» человек нажимает не всегда: «субтитры: клод ->
    Claude» приходит просто сообщением. Собирать на него ролик заново
    значит потерять минуты рендера на работу, которой не просили.
    """
    return bool(fixes(ask)) and table.get(chat_id) is not None


def _kb(theme_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Ок",
                                 callback_data=f"mont:ok:{theme_id}"),
            InlineKeyboardButton(text="✏️ Правки",
                                 callback_data=f"mont:fix:{theme_id}"),
            InlineKeyboardButton(text="📤 В очередь",
                                 callback_data=f"mont:queue:{theme_id}"),
        ],
        # Второй ряд, а не четвёртая кнопка в первом: три кнопки это уже
        # вся ширина экрана телефона, и подпись четвёртой обрезалась бы.
        [
            InlineKeyboardButton(text="📅 В свой день",
                                 callback_data=f"mont:slot:{theme_id}"),
            InlineKeyboardButton(text="📝 Текст субтитров",
                                 callback_data=f"mont:text:{theme_id}"),
        ],
    ])




def _esc(text: str) -> str:
    """Голая скобка в тексте роняет отправку целиком — а вместе с ней и
    удавшуюся работу. Субтитры идут через экранирование, как проза моста.
    """
    return _html.escape(str(text), quote=False)


# ── правка субтитров целиком ──────────────────────────────────────────
#
# Замена по одному слову хороша для опечатки. Человек, посмотревший
# готовый ролик, видит сразу всё — и правит текстом, а не списком замен.
# Поэтому кнопка отдаёт субтитры одной строкой, а присланное обратно
# разбирается сличением (`footage.whole_pairs`): что изменилось, то и
# становится строками словаря.
#
# Режим держится своим множеством, а не полем стола: стол один на роль, а
# ждать от человека можно двух разных вещей — список замен и целый текст.
# Разбирать целый текст как список замен значит не найти ни одной строки
# «как слышно -> как правильно» и ответить справкой про формат на
# аккуратно перепечатанный абзац.

_WHOLE: set[int] = set()


def wants_whole(chat_id: int) -> bool:
    """Ждём от человека субтитры целиком, а не список замен."""
    return chat_id in _WHOLE


def leave_whole(chat_id: int) -> None:
    _WHOLE.discard(chat_id)


def whole_text(reel: Reel) -> str:
    """Субтитры этого ролика одной строкой — то, что человек видит в кадре."""
    return footage.subtitle_text(reel.spoken or reel.subs)


async def say_whole(reg, chat_id: int, reel: Reel, topic: str) -> None:
    """Отдать субтитры на правку целиком и ждать их обратно.

    Текст уходит в `<code>`: на телефоне такой блок копируется нажатием,
    а человеку нужно именно скопировать, поправить и прислать обратно.
    """
    async def say(text: str) -> None:
        await reg.say("reels", chat_id, text, topic=topic)

    text = whole_text(reel)
    if not text:
        # Карточка, поднятая из базы после рестарта, слов не помнит:
        # услышанное живёт в памяти процесса. Обещать правку нечем.
        await say("Субтитров этого ролика у меня на руках нет — он собран "
                  "в другом запуске завода. Пришлите дубль и повторите "
                  "«смонтируй», тогда поправлю текстом.")
        return
    if len(text) > footage.WHOLE_MAX:
        await say(f"Субтитры этого ролика длиной {len(text)} знаков — "
                  "одним сообщением они не ходят, а склеивать правку из "
                  "нескольких я не берусь: непонятно, где она кончилась.\n\n"
                  "Поправьте словами: строка «как слышно -&gt; как "
                  "правильно», можно несколько сразу.")
        table.await_fix(chat_id, reel)
        return

    table.await_fix(chat_id, reel)
    _WHOLE.add(chat_id)
    await say("Вот субтитры ролика. Скопируйте, поправьте что не так и "
              "пришлите обратно целиком — я сличу с услышанным, занесу "
              "разницу в словарь и пересоберу ролик.")
    await say(f"<code>{_esc(text)}</code>")


async def take_whole(reg, chat_id: int, text: str, topic: str = "reels") -> None:
    """Присланные субтитры целиком: разница → словарь → пересборка."""
    reel = table.get(chat_id)

    async def say(msg: str) -> None:
        await reg.say("reels", chat_id, msg, topic=topic)

    leave_whole(chat_id)
    if reel is None:
        await say("Этот монтаж уже неактуален.")
        return

    pairs, notes = footage.whole_pairs(reel.spoken or reel.subs,
                                       reel.subs, text)
    for n in notes:
        await say(n)
    if not pairs:
        table.hold(chat_id, reel)       # кнопки под карточкой живые
        return

    table.note(chat_id, reel.theme["id"], text)
    b = desk.brand(chat_id)
    added, swapped = remember(b, pairs)
    if added:
        await say("Записала в словарь субтитров: "
                  + ", ".join(f"<b>{_esc(s)}</b> → {_esc(d) or '—'}"
                              for s, d in added[:8]) + ".")
    if swapped:
        await say("Переписала прошлые правила: "
                  + ", ".join(f"<b>{_esc(s)}</b> → {_esc(d) or '—'}"
                              for s, d in swapped[:8]) + ".")

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
    # Под новой карточкой ждать текст прошлой нельзя: субтитры уже другие.
    leave_whole(chat_id)
    await reg.say("reels", chat_id, caption(reel), topic=topic)
    await reg.send_file("reels", chat_id, path.read_bytes(), path.name,
                        topic=topic, as_video=True)
    await reg.say("reels", chat_id, "Принимаем?", kb=_kb(reel.theme["id"]),
                  topic=topic)


async def run(reg, chat_id: int, ask: str, topic: str = "reels",
              panel: bool = False) -> None:
    table.clear(chat_id)
    leave_whole(chat_id)
    _sweep()

    async def say(text: str) -> None:
        await reg.say("reels", chat_id, text, topic=topic)

    try:
        reel = await build(chat_id, ask, say=say, panel=panel)
    except NoWork as e:
        await say(f"Монтировать нечего: {e}.")
        return
    except NoFootage as e:
        await say(str(e))
        return
    except grab.NoVideo as e:
        await say(f"Запись по ссылке не забралась: {e}")
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
    leave_whole(chat_id)
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
    except grab.NoVideo as e:
        await say(f"Запись по ссылке не забралась: {e}")
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

    # Целый текст субтитров ждём отдельно: список замен в нём не найдётся
    # ни одной строкой, и человек получил бы справку про формат в ответ на
    # аккуратно перепечатанный абзац.
    if wants_whole(chat_id):
        await take_whole(reg, chat_id, instruction, topic)
        return

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
        "Правок много — нажмите «Текст субтитров»: отдам их одной строкой, "
        "перепишете как надо и пришлёте обратно, разницу в словарь занесу "
        "сама.\n\n"
        "Всё остальное — кадр, паузы, обложку — правкой словами не соберу: "
        "пришлите новый дубль и повторите «смонтируй».", topic=topic)


async def _take_slot(reg, chat_id: int, theme_id: str, topic: str,
                     say) -> None:
    """Занять смонтированному ролику день в плане.

    Без этого автомат ролик не публикует никогда, и «автопостинг
    Instagram» остаётся кнопкой. Причина в дате: тема по факту съёмки
    заводится пустым днём (`desk.adhoc`) — слот в плане ставит Стратег, а
    съёмка приходит от человека с камерой, — а автомат отбирает темы по
    `date = сегодня`. Пустой день для него просто не наступает.

    Поэтому день занимается тем же швом, которым его занимает тема рукой:
    `strategy.hand_slot` смотрит окно и занятость, и второй ролик в один
    день на Instagram не встанет. Придумать дату своим счётом нельзя —
    два счёта свободных дней разошлись бы молча.

    Ролик при этом остаётся `ready`: работа сделана, ждёт он только часов.
    """
    row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                 theme_id, chat_id)
    if row is None:
        await say("Этой темы уже нет.")
        return
    t = dict(row)
    if publisher.reel_path(desk.brand(chat_id), theme_id) is None:
        await say("Ролика на диске нет: сначала монтаж, потом день.")
        return

    try:
        day = strategy.hand_slot(chat_id, t.get("plat") or "instagram")
    except strategy.NoSlots as e:
        await say(f"День не занять: {e}. Опубликовать сейчас — «В очередь».")
        return

    with db.tx() as c:
        c.execute("UPDATE themes SET date = ?, updated_at = datetime('now') "
                  "WHERE id = ? AND chat_id = ?", (day, theme_id, chat_id))

    on, at = publisher.settings(chat_id)
    if on:
        tail = f"Уйдёт сам {day} в {at}."
    else:
        tail = (f"Покажу превью {day} и буду ждать кнопку: автопубликация "
                "выключена, включается командой <code>/auto</code>.")
    await say(f"Ролик <code>{theme_id}</code> встал на {day}. {tail}")


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
        leave_whole(chat_id)
        await say_unsupported(reg, chat_id, topic)
        return

    if action == "text":
        reel = table.get(chat_id, theme_id)
        if reel is None:
            await say("Этот монтаж уже неактуален.")
            return
        await say_whole(reg, chat_id, reel, topic)
        return

    if action == "slot":
        await _take_slot(reg, chat_id, theme_id, topic, say)
        return

    if action not in {"ok", "queue"}:
        return

    reel = table.take(chat_id, theme_id)
    leave_whole(chat_id)
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
