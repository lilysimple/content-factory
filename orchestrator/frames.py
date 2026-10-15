"""Анимированные карточки панели и обложка: HyperFrames рядом с Remotion.

Панель сплита рисует Remotion, и карточку он показывает выездом: она
появляется целиком и дальше стоит. Тому, что нарисовано словами —
список, число, шкала, знак продукта, — выезда мало: на панели три
секунды, и неподвижная карточка эти три секунды просто ждёт.

HyperFrames (`tools/hyperframes`, Apache-2.0) рендерит из HTML и GSAP
готовый mp4. Мы отдаём ему только то, что нарисовано словами: он
собирает клип ровно на длину блока, монтаж кладёт его в панель одним
видео. Резка, субтитры, кроп головы, маскот и материал из альбома
остаются у Remotion — там своя арифметика, и переносить её незачем.

**Клип не обязателен.** Нет тулчейна, нет ffmpeg, упал рендер — функция
отдаёт `None` и строку причины, монтаж рисует карточку как раньше.
Молчаливого отката тут нет: строка уходит человеку в находки, иначе
«почему-то не двигается» выясняется на готовом ролике.

Кэш — папка бренда, `montage/frames/`, имя от отпечатка задания вместе
с отпечатком шаблона. Правка шаблона отпечаток меняет, и пересборка
после правки субтитров в рендер второй раз не ходит.

Устройство и замеры — `NOTES.md`, «Анимация карточек».
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import signal
from pathlib import Path
from typing import Any

log = logging.getLogger("frames")

ROOT = Path(__file__).resolve().parent.parent / "tools" / "hyperframes"
SHIM = Path(__file__).resolve().parent.parent / "tools" / "ffmpeg-shim"
TEMPLATES = ROOT / "templates"
JOBS = ROOT / "jobs"
FRAMES = "montage/frames"

# Сорта блока, которые рисуются словами, а не кадром. `media` и `art`
# сюда не идут: там запись из альбома и картинка по теме, их двигает
# шаблон Remotion своим наездом и подчёркиванием слова.
KINDS = ("card", "icon", "counter", "scale", "art",
         # Сорта, которые рисуют инфографику: поток со стрелками,
         # плитки, окно терминала, столбики, плашки-слова и фраза с
         # подсветкой звучащего слова. Живут только в дорожке — по
         # одиночке их показывать незачем, они и есть поток.
         "flow", "grid", "terminal", "bars", "chips", "mark")

# Карточка со скрином страницы идёт своим шаблоном: окно браузера рисует
# готовый блок каталога HyperFrames, а скрин ложится в его слот. Своё
# окно это чужая работа второй раз и хуже.
SHOT = "shot"
# Фотография едет своим шаблоном: рамка браузера вокруг снимка читается
# как чужой интерфейс. Сюда же уходит `art` — картинка по теме.
PHOTO = "photo"
# Вся панель одним клипом: сцены вместо карточек, переход между ними и
# фон, который не прерывается. Устройство — `NOTES.md`.
TRACK = "track"
STAGE = "compositions/components/browser-device-stage.html"

# Manrope: тот же шрифт, которым Remotion набирает субтитры и обложку.
# Без него карточка приезжала системной гарнитурой, и на одном ролике
# стояли два разных шрифта.
FONTS = "assets/fonts"
# Те же пять гарнитур, что грузит `Cover.tsx`, и те же два начертания:
# ТЗ площадки называет шрифт именем, и шрифт, которого нет, молча
# подменился бы системным. Карточки панели всегда Manrope — там кегли
# считает шаблон, а не ТЗ.
FACES = ("Manrope", "Montserrat", "Unbounded", "GolosText",
         "FiraSansCondensed")
CARD_FONT = "Manrope"

# Потолок на один клип. Панель сплита это 1080×576 и три-четыре секунды;
# замер пробного ролика — десять секунд холста 1920×1080 за 23 с. Минуты
# с запасом хватает, а висящий рендер держит человека в чате.
CLIP_CAP = 120
COVER_CAP = 180

# Чем короче клип, тем дороже он на секунду готового видео: Chrome
# поднимается под каждый рендер заново. Блок короче этого рисует
# Remotion — выезда там всё равно никто не успеет разглядеть.
MIN_SECONDS = 1.0


class NoClip(Exception):
    """Клипа не будет. Сообщение — готовая строка человеку."""


def ready() -> str:
    """Пусто — тулчейн на месте; строка — чего не хватает.

    Проверяется и шим ffmpeg: системного ffmpeg на машине может не быть
    вовсе, а HyperFrames без него доходит до кодирования и падает там —
    то есть после всей отрисовки, самым дорогим способом.
    """
    if not (ROOT / "node_modules" / ".bin" / "hyperframes").exists():
        return ("HyperFrames не установлен. Один раз в Terminal.app: "
                f"cd {ROOT} && npm install")
    if not (TEMPLATES / "panel.html").exists():
        return f"шаблонов карточки нет в {TEMPLATES}"
    if not (SHIM / "ffmpeg").exists():
        return f"ffmpeg для HyperFrames не на месте: {SHIM}"
    missing = [f for f in FACES if not (ROOT / FONTS / f"{f}.css").exists()]
    if missing:
        return (f"шрифтов карточек нет в {ROOT / FONTS}: "
                + ", ".join(missing))
    return ""


def _env() -> dict[str, str]:
    """Окружение рендера.

    Системного ffmpeg на машине нет, и HyperFrames ищет его в `PATH` —
    поэтому впереди идёт шим на бинарь, который привозит Remotion.
    Телеметрия выключается здесь, а не в чужом файле настроек: по панели
    клиента видно и тему, и бренд.
    """
    env = dict(os.environ)
    env["PATH"] = f"{SHIM}:{env.get('PATH', '')}"
    env["HYPERFRAMES_NO_TELEMETRY"] = "1"
    env["DO_NOT_TRACK"] = "1"
    return env


def template_of(spec: dict[str, Any]) -> str:
    """Какой шаблон собирает это задание."""
    kind = spec.get("kind")
    if kind == TRACK:
        return "track.html"
    if kind == SHOT:
        return "shot.html"
    if kind == PHOTO:
        return "photo.html"
    return "panel.html"


def face(name: str) -> str:
    """Файл стилей гарнитуры. Имя не из списка — Manrope: шрифт, которого
    нет, иначе подменится системным молча, уже на готовом кадре."""
    plain = (name or "").replace(" ", "")
    return f"{plain if plain in FACES else CARD_FONT}.css"


def _digest(spec: dict[str, Any], template: str) -> str:
    """Отпечаток задания вместе с шаблоном.

    Шаблон в отпечатке нарочно: правка типографики карточки обязана
    обесценить кэш, иначе старые клипы доедут до ролика вперемешку с
    новыми, и это заметит человек, а не код.
    """
    body = json.dumps(spec, ensure_ascii=False, sort_keys=True)
    mark = (TEMPLATES / template).read_bytes()
    return hashlib.sha256(body.encode() + mark).hexdigest()


# Низ панели отведён под строку субтитра: шаблон Remotion держит
# содержимое блока выше полосы в 0.2 высоты панели, и карточка, собранная
# без этой полосы, ляжет человеку на его же слова.
CAPTION_BAND = 0.2


def panel_spec(block: dict[str, Any], *, size: tuple[int, int],
               fps: int, colors: dict[str, str],
               band: int = 0,
               image: Path | None = None) -> dict[str, Any] | None:
    """Блок панели в задание для HyperFrames. `None` — не его работа.

    На входе ровно тот JSON, который уезжает в Remotion
    (`montage._block_props`): одна точка правды на форму блока. Секунды
    внутри задания — от начала блока, а не от начала ролика: клип живёт
    своей длиной и о ролике не знает.
    """
    kind = str(block.get("kind", "card"))
    if kind not in KINDS:
        return None
    # Запись из альбома остаётся Remotion: там наезд на слово и
    # подчёркивание, своя арифметика. Скрин страницы — наоборот: он едет
    # в окно браузера из каталога, но только если файл дали. Имя в
    # `imagePath` это имя внутри public/ Remotion, по нему файла не найти.
    if block.get("videoPath"):
        return None
    if block.get("imagePath") and image is None:
        return None
    # `art` без картинки это не карточка, а пустое место: сорт заведён
    # ради самой картинки по теме.
    if kind == "art" and image is None:
        return None
    start = float(block["start"])
    dur = round(float(block["end"]) - start, 3)
    if dur < MIN_SECONDS:
        return None
    if block.get("imagePath") and kind in ("card", "art"):
        # Окно браузера — только снимку страницы: он и есть страница, и
        # приходит он от `panelshot` в блоке `card`. Картинка по теме и
        # фотография (`art`) идут кадром во всю карточку: рамка браузера
        # вокруг снимка читается как чужой интерфейс.
        kind = SHOT if kind == "card" else PHOTO
    lines = [str(s) for s in block.get("lines") or []]
    items = [{"text": str(it["text"]), "at": round(float(it["at"]) - start, 3)}
             for it in block.get("items") or []]
    if not lines and not items and block.get("value") is None \
            and kind not in (SHOT, PHOTO):
        return None
    w, h = size
    spec: dict[str, Any] = {
        "kind": kind,
        "kicker": str(block.get("kicker") or ""),
        "lines": lines,
        "items": items,
        "width": w,
        "height": h,
        "fps": fps,
        "duration": dur,
        # Полоса субтитра внизу: клип о ней не знает, её считает питон,
        # который знает высоту самой панели — у блока `full` холст
        # другой, а полоса остаётся прежней.
        "reserve": band,
        "panelColor": colors["panelColor"],
        "accentColor": colors["accentColor"],
        "textColor": colors["textColor"],
        "cardColor": colors["cardColor"],
    }
    if image is not None:
        spec["imageSrc"] = image.name
        # Отпечаток самого файла, а не имени: два разных скрина под одним
        # именем — обычное дело (`panel-0.png`), и кэш отдал бы первый.
        spec["imageMark"] = hashlib.sha256(image.read_bytes()).hexdigest()[:16]
        spec["stageTitle"] = str(block.get("stageTitle") or "")
    if block.get("value") is not None:
        spec["value"] = int(block["value"])
        spec["valueAt"] = round(float(block.get("valueAt", start)) - start, 3)
    return spec


def cover_spec(*, size: tuple[int, int], fps: int, seconds: float,
               lines: list[dict[str, Any]], anchor: str, inset: float,
               focus: tuple[float, float], scrim: float, blur: bool,
               back: str, image: Path | None,
               title: str = "", title_color: str = "#FFFFFF",
               title_size: int = 38, brand_name: str = "",
               font: str = CARD_FONT, weight: int = 800) -> dict[str, Any]:
    """Обложка в задание. Числа те же, что у неподвижной обложки.

    Кегль и цвет строк считает питон по ТЗ площадки
    (`montage.cover_lines`, `title_size`) — HyperFrames их только рисует
    и набирает по одной. Утверждённая человеком картинка остаётся
    картинкой: движение добавляется поверх неё, а не вместо.
    """
    w, h = size
    return {
        "lines": [{"text": str(ln["text"]), "color": str(ln["color"]),
                   "size": int(ln["size"])} for ln in lines],
        "anchor": anchor,
        "inset": round(float(inset), 1),
        "focus": {"x": round(focus[0], 4), "y": round(focus[1], 4)},
        "scrim": round(float(scrim), 3),
        "blur": bool(blur),
        "backColor": back,
        "width": w,
        "height": h,
        "fps": fps,
        "duration": round(float(seconds), 3),
        "imageSrc": image.name if image else "",
        "imageMark": (hashlib.sha256(image.read_bytes()).hexdigest()[:16]
                      if image else ""),
        "title": title,
        "titleColor": title_color,
        "titleSize": int(title_size),
        "brandName": brand_name,
        "font": font,
        "weight": int(weight),
    }


def _write_job(job: Path, template: str, spec: dict[str, Any],
               image: Path | None) -> None:
    """Папка одного рендера: композиция, настройки, gsap и картинка.

    Своя папка на задание, а не правка `index.html` проекта: два монтажа
    в один момент — обычное дело (панель собирается блоками), и общий
    файл они перетирали бы друг другу.
    """
    job.mkdir(parents=True, exist_ok=True)
    body = (TEMPLATES / template).read_text(encoding="utf-8")
    body = (body
            .replace("__FONTCSS__", face(spec.get("font", "")))
            .replace("__FONT__", face(spec.get("font", ""))[:-4])
            .replace("__IMG__", spec.get("imageSrc", ""))
            .replace("__W__", str(spec["width"]))
            .replace("__H__", str(spec["height"]))
            .replace("__FPS__", str(spec["fps"]))
            .replace("__DUR__", str(spec["duration"]))
            .replace("__SPEC__", json.dumps(spec, ensure_ascii=False)))
    if template == "shot.html":
        # Окно браузера — подкомпозиция: загрузчик идёт за файлом по
        # относительному пути, значит он обязан лежать в папке задания.
        stage = job / "components" / "browser-device-stage.html"
        stage.parent.mkdir(exist_ok=True)
        shutil.copy2(ROOT / STAGE, stage)
        shot = (int(spec["width"] * 0.56), int(spec["height"] * 0.72))
        body = (body
                .replace("__SW__", str(shot[0]))
                .replace("__SH__", str(shot[1]))
                .replace("__VARS__", json.dumps(
                    {"chrome": "browser",
                     "title": spec.get("stageTitle") or "",
                     "exit": "none"})))
    (job / "index.html").write_text(body, encoding="utf-8")
    shutil.copy2(ROOT / "hyperframes.json", job / "hyperframes.json")
    # Шрифт едет копией в каждую папку задания: композиция читает его
    # относительным путём, а папка задания живёт и умирает с рендером.
    shutil.copytree(ROOT / FONTS, job / "fonts", dirs_exist_ok=True)
    (job / "vendor").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "vendor" / "gsap.min.js", job / "vendor" / "gsap.min.js")
    if image is not None:
        shutil.copy2(image, job / image.name)


async def _render(job: Path, out: Path, *, cap: int) -> None:
    """Позвать HyperFrames и дождаться файла.

    Подпроцесс идёт своей группой и убивается группой: под `hyperframes`
    работают node, Chrome и ffmpeg, и убитая в одиночку верхушка
    оставляет их держать наши трубы — грабли те же, что у Remotion
    (`montage._kill`).
    """
    cmd = [str(ROOT / "node_modules" / ".bin" / "hyperframes"), "render", ".",
           "-o", str(out), "--quiet", "--format", "mp4"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(job), env=_env(), start_new_session=True,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        raw_out, raw_err = await asyncio.wait_for(proc.communicate(),
                                                 timeout=cap)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()
        out.unlink(missing_ok=True)
        raise NoClip(f"рендер карточки не уложился в {cap} с")

    if proc.returncode != 0 or not out.exists():
        out.unlink(missing_ok=True)
        tail = (raw_err or raw_out or b"").decode(errors="replace").strip()
        raise NoClip("HyperFrames не отдал файл: " + tail[-200:])


async def clip(b, spec: dict[str, Any], *, template: str = "",
               image: Path | None = None,
               images: dict[int, Path] | None = None,
               cap: int = CLIP_CAP) -> Path:
    """Готовый mp4 на это задание. Бросает `NoClip` строкой причины.

    Кэш по отпечатку: тот же блок на пересборке ролика второй раз не
    рендерится. Папка задания убирается всегда — в ней лежит копия
    картинки бренда, и остаться ей в репозитории завода незачем.
    """
    gap = ready()
    if gap:
        raise NoClip(gap)

    template = template or template_of(spec)
    mark = _digest(spec, template)
    folder = b.path(FRAMES)
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{spec.get('kind') or 'cover'}-{mark[:16]}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out

    job = JOBS / mark[:16]
    shutil.rmtree(job, ignore_errors=True)
    try:
        _write_job(job, template, spec, image)
        for src in (images or {}).values():
            shutil.copy2(src, job / src.name)
        await _render(job, out, cap=cap)
    finally:
        shutil.rmtree(job, ignore_errors=True)
    log.info("карточка собрана: %s", out.name)
    return out


async def panel_clips(b, blocks: list[dict[str, Any]], *,
                      size: tuple[int, int], fps: int,
                      colors: dict[str, str], split: float,
                      images: dict[int, Path] | None = None
                      ) -> tuple[dict[int, Path], list[str]]:
    """Клипы под блоки панели. Отдаёт готовые и строки о том, чего нет.

    Холст клипа — та половина кадра, которую блок занимает: обычный
    блок едет в панель (`split` высоты), `full` разворачивается на весь
    кадр. Собрать клип не на том холсте значит получить другой кегль:
    типографика в шаблоне считается от высоты.

    Блоки идут по одному, а не разом: на восьми гигабайтах памяти два
    Chrome рядом — это тот же отказ, из-за которого у Remotion стоит
    `concurrency=2`.
    """
    w, h = size
    out: dict[int, Path] = {}
    notes: list[str] = []
    gap = ready()
    if gap:
        return out, [f"карточки панели без анимации — {gap}"]

    band = round(h * split * CAPTION_BAND)
    shots = images or {}
    for i, block in enumerate(blocks):
        canvas = (w, h) if block.get("full") else (w, round(h * split))
        spec = panel_spec(block, size=canvas, fps=fps, colors=colors,
                          band=band, image=shots.get(i))
        if spec is None:
            continue
        try:
            out[i] = await clip(b, spec, image=shots.get(i))
        except NoClip as e:
            notes.append(f"блок {i + 1} без анимации — {e}")
    return out, notes


class Local:
    """Кэш клипов для входа без бренда.

    `tools/avatar_split.py` собирает ролик из готовой говорящей головы и
    про бренд не знает вовсе: темы в базе такой ролик не заводит. Папку
    бренда ему подменяет эта заглушка — кэш ложится рядом с выходом, и
    перегенерация головы не рендерит карточки заново.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, rel: str) -> Path:
        out = self.root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        return out


# ── символы ─────────────────────────────────────────────────────────

# Значки рисуются линией прямо в композиции: `d` пути лежит здесь, цвет
# берётся от текста. Набор закрытый и маленький нарочно — имя не из
# списка это молчаливо пропавший значок, и ловить такое на готовом
# ролике дороже, чем назвать строкой.
#
# Внешнего источника нет по той же причине, по какой нет шрифта из
# сети: рендер обязан идти без интернета, а знак продукта у нас и так
# снимается фавиконом со страницы (`panelshot`).
ICONS = {
    "папка": "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
    "видео": "M3 6a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z M17 9l4-3v12l-4-3",
    "кадр": "M4 4h16v16H4z M4 9h16 M4 15h16 M9 4v16 M15 4v16",
    "ножницы": "M6 4l12 12 M18 4L6 16 M7 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z M17 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
    "музыка": "M9 18V5l12-2v13 M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0z M21 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
    "часы": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 7v5l3 2",
    "галочка": "M4 13l5 5L20 6",
    "искра": "M12 3l2 6 6 2-6 2-2 6-2-6-6-2 6-2z",
    "терминал": "M4 5h16v14H4z M7 9l3 3-3 3 M13 15h4",
    "картинка": "M4 5h16v14H4z M4 16l5-5 4 4 3-3 4 4",
    "текст": "M5 5h14 M5 10h14 M5 15h9",
    "список": "M8 6h12 M8 12h12 M8 18h12 M4 6h.01 M4 12h.01 M4 18h.01",
    "поиск": "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z M21 21l-4.3-4.3",
    "палочка": "M4 20L17 7 M15 5l4 4 M14 4l1 2 2 1-2 1-1 2-1-2-2-1 2-1z",
    "шестерёнка": "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M19 12l2-1-1-3-2 .4a7 7 0 0 0-2-1.2L15 5h-3l-.9 2.2a7 7 0 0 0-2 1.2L7 8 6 11l2 1-2 1 1 3 2-.4a7 7 0 0 0 2 1.2L12 19h3l.9-2.2a7 7 0 0 0 2-1.2l2 .4 1-3z",
    "речь": "M20 15a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z",
    "стрелка": "M5 12h14 M13 6l6 6-6 6",
    "замок": "M6 11h12v9H6z M9 11V8a3 3 0 0 1 6 0v3",
}


def icon(name: str) -> str:
    """Путь значка по имени. Имени нет в наборе — пусто, и это видно
    вызывающему: он назовёт пропажу строкой, а не нарисует пустоту."""
    return ICONS.get((name or "").strip().lower(), "")


# ── дорожка панели: один клип вместо шести ──────────────────────────

# Потолок на дорожку: это минута видео 1080×576 одним рендером. Замер
# 15.10 — 56 с дорожки собрались за 146 с.
TRACK_CAP = 600


def track_spec(blocks: list[dict[str, Any]], *, size: tuple[int, int],
               fps: int, colors: dict[str, str], band: int,
               images: dict[int, Path] | None = None,
               ) -> dict[str, Any] | None:
    """Все блоки панели одним заданием. `None` — дорожки не будет.

    Карточка перестаёт быть отдельным клипом и становится сценой внутри
    общего времени: отсюда и переход между соседними, и фон, который не
    прерывается на стыке. Шесть клипов встык дают шесть статик подряд —
    это видно на готовом ролике и было первой жалобой на анимацию.

    Секунды сцен — от начала дорожки, а дорожка начинается с первого
    блока: внутри клипа о ролике не знают.

    `full` сюда не идёт: такой блок разворачивает панель на весь кадр, и
    холст у него другой. Есть хоть один — дорожки нет, блоки собираются
    поодиночке, как раньше.
    """
    if not blocks or any(b.get("full") for b in blocks):
        return None
    shots = images or {}
    w, h = size
    start = float(blocks[0]["start"])
    end = max(float(b["end"]) for b in blocks)
    if end - start < MIN_SECONDS:
        return None

    scenes: list[dict[str, Any]] = []
    for i, b in enumerate(blocks):
        kind = str(b.get("kind", "card"))
        if kind == "media" or b.get("videoPath"):
            # Запись из альбома остаётся Remotion: там наезд на слово и
            # своя арифметика. Дорожка её не покрывает — значит и
            # дорожки нет, иначе на её месте будет дыра.
            return None
        at = float(b["start"]) - start
        scene: dict[str, Any] = {
            "kind": kind,
            "kicker": str(b.get("kicker") or ""),
            "lines": [str(s) for s in b.get("lines") or []],
            # Подпись и число у пункта нужны плиткам и столбикам; у
            # списка их просто нет, и пустыми они не едут.
            "items": [{k: v for k, v in (
                ("text", str(it["text"])),
                ("at", round(float(it["at"]) - float(b["start"]), 3)),
                ("note", str(it.get("note") or "")),
                ("value", it.get("value")),
                ("icon", icon(str(it.get("icon") or ""))),
            ) if v not in ("", None)} for it in b.get("items") or []],
            "start": round(at, 3),
            "end": round(float(b["end"]) - start, 3),
        }
        if b.get("icon"):
            scene["icon"] = icon(str(b["icon"]))
        if b.get("value") is not None:
            scene["value"] = int(b["value"])
            scene["valueAt"] = round(
                float(b.get("valueAt", b["start"])) - float(b["start"]), 3)
        if i in shots:
            shot = shots[i]
            scene["imageSrc"] = shot.name
            scene["imageMark"] = hashlib.sha256(
                shot.read_bytes()).hexdigest()[:16]
            # Окно браузера — только снимку страницы (`card`), и рисуется
            # оно в самой дорожке: блок каталога это подкомпозиция с
            # одним слотом на композицию, а окон в дорожке бывает
            # несколько и скрины у них разные.
            scene["frame"] = "browser" if kind == "card" else "photo"
            scene["stageTitle"] = str(b.get("stageTitle") or "")
        scenes.append(scene)

    return {
        "kind": TRACK,
        "scenes": scenes,
        "width": w,
        "height": h,
        "fps": fps,
        "duration": round(end - start, 3),
        "offset": round(start, 3),
        "reserve": band,
        "panelColor": colors["panelColor"],
        "accentColor": colors["accentColor"],
        "textColor": colors["textColor"],
        "cardColor": colors["cardColor"],
        "glowColor": colors.get("glowColor", colors["cardColor"]),
        "font": CARD_FONT,
    }


async def panel_track(b, blocks: list[dict[str, Any]], *,
                      size: tuple[int, int], fps: int,
                      colors: dict[str, str], split: float,
                      images: dict[int, Path] | None = None
                      ) -> tuple[Path | None, float, list[str]]:
    """Дорожка панели одним файлом: путь, её начало в ролике и строки.

    Картинок в дорожке бывает несколько, а папка задания одна — файлы с
    совпадающими именами разъехались бы молча, поэтому каждая картинка
    едет под именем от своего отпечатка.
    """
    gap = ready()
    if gap:
        return None, 0.0, [f"панель без анимации — {gap}"]

    w, h = size
    spec = track_spec(blocks, size=(w, round(h * split)), fps=fps,
                      colors=colors, band=round(h * split * CAPTION_BAND),
                      images=images)
    if spec is None:
        return None, 0.0, []
    try:
        made = await clip(b, spec, template="track.html",
                          images=images, cap=TRACK_CAP)
    except NoClip as e:
        return None, 0.0, [f"панель без анимации — {e}"]
    return made, spec["offset"], []
