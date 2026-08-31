"""Монтаж: из отснятого человеком видео в готовый рилс.

Вход — тема со статусом `ready` (сценарий Редактора Reels утверждён) и
видеофайл, который человек снял сам и прислал в топик Reels. Выход —
`posts/{id}-reel.mp4`: интро (обложка Дизайнера или карточка бренда),
отснятое видео кроп/паддингом под холст площадки, титул хука первые три
секунды, аутро с CTA.

Как `publisher.py` и рендер PNG в `design.py`, это не AI-роль: модель не
зовём, вся работа детерминированная — ffprobe посчитал длительность,
Remotion собрал кадр. Разница с `design.py` в рендерере: тот правит
HTML headless Chrome, здесь Chrome правит Remotion, а мы только готовим
для него JSON и зовём `npx remotion render` подпроцессом.

Разбор дубля живёт в `footage.py` и модель не зовёт тоже: паузы,
активная зона кадра и пословный транскрипт — арифметика и локальный
whisper. Здесь остаётся сборка: собрать props, позвать Remotion, отдать
человеку файл и честно назвать то, что не сошлось.

Субтитры идут по таймингам самой записи, а не по таймингам из
`-script-notes.md`: те посчитаны от скорости чтения суфлёра (два слова в
секунду), а человек на камере говорит в своём темпе. Дубль без звуковой
дорожки субтитров не получает вовсе — вместо них возвращается титул
хука, и человеку об этом говорится строкой.
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
from orchestrator import desk, design, footage, grab, publisher, reels
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
    pages: list[dict[str, Any]] = field(default_factory=list)
    out: Path | None = None
    findings: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return str(self.theme.get("title") or "")


# ── вход: тема и видео ──────────────────────────────────────────────

def _pick(chat_id: int, ask: str) -> dict[str, Any]:
    return desk.pick(
        chat_id, ask, statuses=("ready",), fresh="ready",
        suits=lambda r: (r["format"] or "").lower() in FORMATS and bool(r["asset"]),
        wrong="у темы {id} нет утверждённого сценария reels",
        none="темы {id} нет среди готовых сценариев",
        empty="нет ни одного утверждённого сценария reels")


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


def _blur(reel: Reel, at: float = 0.0) -> bool:
    if not (reel.still and reel.cover == reel.still):
        return False
    mode = (reel.spec.get("blur") or "да").lower()
    return mode not in ("нет", "no", "false", "0")


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
        if reel.still:
            reel.still.unlink(missing_ok=True)

    if proc.returncode != 0 or not out_path.exists():
        tail = err.decode(errors="replace").strip().splitlines()[-8:]
        raise NoRenderer("Remotion не отдал файл: " + " | ".join(tail))

    return out_path


# ── сборка ────────────────────────────────────────────────────────────

# Бюджет сценария из `roles/reels.md`: дольше пятидесяти секунд ролик
# уже не рилс. Дубль длиннее не режется по смыслу — выбор кусков это
# работа для модели, а монтаж намеренно её не зовёт. Поэтому дубль едет
# целиком, а человек читает строкой, насколько он вышел за бюджет.
BUDGET_SECONDS = 50


async def analyse(reel: Reel, *, say=None) -> None:
    """Три прохода по дублю: паузы, активная зона, слова.

    Ни один из них не обязателен для монтажа: отказ прохода — это строка
    в находках и работа на том, что есть. Ролик без субтитров человеку
    полезнее, чем отказ смонтировать вовсе.
    """
    reel.probe = await footage.probe(reel.video)

    quiet: list[tuple[float, float]] = []
    if reel.probe.has_audio:
        quiet = await footage.silences(reel.video)
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
    reel.pages = footage.pages(footage.cut_words(words, reel.cuts))


async def _intro(reel: Reel, b, size: tuple[int, int]) -> None:
    """Первый кадр: что под текстом и как набран текст.

    Порядок фона ровно такой: обложка Дизайнера, свёрстанная под этот
    холст, — лучшее, что может быть, её и берём. Обложка под соседний
    холст в рилсе теряет треть ширины вместе со своим заголовком, и её
    обрубки спорят с нашим текстом — такую не берём вовсе. Остаётся кадр
    из самого дубля: монтаж модель не зовёт и нарисовать обложку ему
    нечем, зато снятое человеком видео у него есть.
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

    at = footage.calm_at(reel.focus)
    try:
        reel.still = await footage.still(
            reel.video, at, TOOLS / f".still-{tid}.png")
        reel.still_at = at
        reel.cover = reel.still
        if not cover:
            reel.findings.append(
                f"обложки от Дизайнера нет — на первый кадр взят самый "
                f"спокойный кадр дубля ({at:.1f} с)")
    except footage.NoFfmpeg as e:
        reel.findings.append(f"кадр на обложку не снялся: {e}")


async def build(chat_id: int, ask: str, *, say=None) -> Reel:
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиль бренда ещё не собран")

    theme = _pick(chat_id, ask)
    video = _footage(b)
    beats = _beats(b, theme["id"])
    color, accent = _colors(b)

    reel = Reel(theme=theme, video=video, color=color, accent=accent,
                hook=beats.get(HOOK_TITLE, ""), cta=beats.get(CTA_TITLE, ""))

    if not reel.hook and not reel.title:
        reel.findings.append("нет ни хука из сценария, ни заголовка темы — "
                             "первый кадр останется без слов")

    plat = theme.get("plat") or "instagram"
    fmt = theme.get("format") or "reels"
    size = design.CANVAS.get(design._key(plat, fmt)) \
        or design.CANVAS.get((plat, None)) or (1080, 1920)

    if say:
        await say(f"Монтирую <b>{theme.get('title') or theme['id']}</b> "
                  f"из <code>{video.name}</code> ({size[0]}×{size[1]}).\n"
                  "Рендер идёт дольше макета, до нескольких минут.")

    await analyse(reel, say=say)
    await _intro(reel, b, size)

    reel.out = await render(reel, size)
    log.info("%s: смонтировано, %s", theme["id"], reel.out)
    return reel


# ── нарезка длинной записи на несколько роликов ───────────────────────
#
# Одна запись — несколько рилсов. Где резать по смыслу, решает Редактор
# Reels (`reels.fragments`): у него для этого есть роль и промпт. Монтаж
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

    frags, lost = await reels.fragments(chat_id, words, probe.duration, ask=ask)
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
        reel.pages = footage.pages(footage.cut_words(words, cuts))
        reel.lines = cover_lines(frag.hook, size, spec)
        if spec_gap:
            reel.findings.append(spec_gap)
        if cuts.dropped > 0.5:
            reel.findings.append(f"внутри куска вырезано {cuts.dropped:.0f} с "
                                 "тишины")
        if cuts.total < reels.FRAG_MIN * 0.8:
            reel.findings.append(
                f"после вырезанных пауз кусок стал коротким: "
                f"{cuts.total:.0f} с")

        # Обложка — спокойный кадр внутри самого куска, а не начала записи.
        at = footage.calm_at([f for f in track if frag.start <= f.t <= frag.end]
                             or track, window=frag.end)
        if say:
            await say(f"Собираю {n} из {len(frags)}: <b>{frag.hook}</b> "
                      f"({_clock(frag.start)}–{_clock(frag.end)}, "
                      f"{cuts.total:.0f} с).")

        piece = TOOLS / f".clip-{tid}.mp4"
        try:
            reel.still = await footage.still(
                video, at, TOOLS / f".still-{tid}.png")
            reel.still_at = at
            reel.cover = reel.still

            # Рендерим вырезанный кусок, а не всю запись: перемотка к
            # четырнадцатой минуте на каждый кадр роняет браузер.
            await footage.clip(video, frag.start, frag.end, piece)
            reel.video = piece
            reel.cuts = cuts.shift(frag.start)

            reel.out = await render(reel, size)
        except (NoRenderer, footage.NoFfmpeg) as e:
            log.warning("кусок %s не собрался: %s", tid, e)
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


# ── выгрузка ──────────────────────────────────────────────────────────

def _save(b, reel: Reel, *, drop_source: bool = True) -> Path:
    tid = reel.theme["id"]
    blob = reel.out.read_bytes()
    path = b.artifact(f"posts/{tid}-reel.mp4", blob)
    # Готовое видео не «черновик»: сценарий уже был ready, монтаж его не
    # понижает — desk.drafted() выставил бы status='draft', а это назад.
    with db.tx() as c:
        c.execute("UPDATE themes SET asset = ?, updated_at = datetime('now') "
                  "WHERE id = ? AND chat_id = ?",
                 (f"posts/{tid}-reel.mp4", tid, reel.theme["chat_id"]))
    if drop_source:                             # при нарезке — после всех
        reel.video.unlink(missing_ok=True)      # исходник разобран
    reel.out.unlink(missing_ok=True)            # временный файл в tools/
    return path


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


async def run(reg, chat_id: int, ask: str, topic: str = "reels") -> None:
    table.clear(chat_id)

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
    path = _save(desk.brand(chat_id), reel)
    reel.out = path
    table.hold(chat_id, reel)

    await say(caption(reel))
    await reg.send_file("reels", chat_id, path.read_bytes(), path.name,
                        topic=topic)
    await reg.say("reels", chat_id, "Принимаем?", kb=_kb(reel.theme["id"]),
                  topic=topic)


async def run_split(reg, chat_id: int, ask: str,
                    topic: str = "reels") -> None:
    """Длинная запись → пачка роликов, каждый со своей карточкой."""
    table.clear(chat_id)
    b = desk.brand(chat_id)

    async def say(text: str) -> None:
        await reg.say("reels", chat_id, text, topic=topic)

    async def deliver(reel: Reel, last: bool) -> None:
        # Исходник убираем после последнего: он один на всю пачку.
        path = _save(b, reel, drop_source=last)
        reel.out = path
        table.hold(chat_id, reel)
        await say(caption(reel))
        await reg.send_file("reels", chat_id, path.read_bytes(), path.name,
                            topic=topic)
        await reg.say("reels", chat_id, "Принимаем?",
                      kb=_kb(reel.theme["id"]), topic=topic)

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
    reel = table.take(chat_id)
    if reel is None:
        await reg.say("reels", chat_id, "Этот монтаж уже неактуален.",
                      topic=topic)
        return
    table.note(chat_id, reel.theme["id"], instruction)
    # Правка перерезает то же видео заново — исходник для этого должен
    # быть ещё на месте, поэтому revise не подходит, если человек уже
    # прислал новый дубль поверх: тогда это новый /montage, а не правка.
    await say_unsupported(reg, chat_id, topic)


async def say_unsupported(reg, chat_id: int, topic: str) -> None:
    await reg.say("reels", chat_id,
                  "Текстовые правки монтажа пока не разбираю: пришлите "
                  "новый дубль видео и повторите /montage.", topic=topic)


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

    if action == "ok":
        await say(f"Принято. Файл лежит в <code>posts/{theme_id}-reel.mp4</code> "
                  "папки бренда.")
        return

    await say(f"Передаю комплект <code>{theme_id}</code> Публикатору.")
    await publisher.run(reg, chat_id, theme_id, topic="queue")
