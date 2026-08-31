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
import tempfile as _tmp
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ROOT, cfg
from orchestrator import agent, desk, publisher
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
CANVAS = {
    ("telegram", None):        (1080, 1350),
    ("instagram", "карусель"): (1080, 1350),
    ("instagram", "reels"):    (1080, 1920),
    ("instagram", None):       (1080, 1350),
    ("youtube", None):         (1280, 720),
}

# Эталон под площадку и формат. Клонируется, а не верстается заново.
REFERENCE = {
    ("telegram", None):        ["carousel-01-cover.html"],
    ("instagram", "карусель"): ["carousel-01-cover.html", "carousel-02-context.html",
                                "carousel-03-function.html", "carousel-04-accent.html",
                                "carousel-05-final.html"],
    ("instagram", "reels"):    ["reel-01-photo.html"],
}

# Шаблоны со слотами. Здесь разметку собирает код, а модель заполняет
# дырки — см. `_fill`. Площадка без шаблона идёт старым путём, где модель
# пишет HTML целиком: переезд делается по одной площадке за раз.
TEMPLATES = {
    ("telegram", None): ["telegram-post.html"],
}

# Что модель кладёт в слот. Потолок знаков — не каприз: заголовок длиннее
# вылезет за холст, и поймает это уже человек глазами на PNG.
#
# `photo` особый: значение проверяется по списку файлов бренда, а не по
# длине. `headline_size` в этот словарь не входит вовсе — его считает код
# (`_fit`), и модель о нём не знает. Подбор кегля это арифметика по числу
# знаков, а не суждение: модель может только промахнуться мимо пикселей.
SLOTS: dict[str, tuple[str, int]] = {
    "rubric":          ("рубрика над заголовком, капсом", 28),
    "headline":        ("заголовок; слова только из текста Редактора", 52),
    "headline_accent": ("хвост заголовка акцентным цветом, можно пустым", 24),
    "subtitle":        ("подзаголовок одной фразой", 120),
    "photo":           ("имя файла из списка доступных фото", 0),
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


def _photos(b) -> list[str]:
    folder = b.path("design/assets/images")
    if not folder.is_dir():
        return []
    return sorted(f.name for f in folder.iterdir()
                  if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})


def _spec(b, plat: str) -> str:
    text = b.read(f"design/platforms/{plat}.md")
    if not text.strip():
        raise NoSpec(plat)
    return text


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
        if limit and len(val) > limit:
            raise NoWork(f"слот «{name}» длиннее {limit} знаков: {len(val)}")
        if not val and name not in ("headline_accent",):
            raise NoWork(f"слот «{name}» пустой")

    head = (slots.get("headline") or "") + (slots.get("headline_accent") or "")
    ready = dict(slots, headline_size=str(_fit(head)))

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        val = ready.get(name, "")
        # Кегль это число от кода, экранировать нечего; текст от модели —
        # всегда экранируется: одна кавычка в заголовке иначе рвёт стиль.
        return val if name == "headline_size" else _html.escape(val, quote=True)

    return SLOT_RX.sub(sub, tpl)


def _cards_from_slots(data: dict[str, Any], tpls: list[tuple[str, str]],
                      photos: list[str]) -> list[dict[str, str]]:
    """Ответ модели со слотами → карточки с готовым HTML.

    Шаблон берётся по порядку, а не по имени из ответа: имён шаблонов
    модель не знает и знать не должна, иначе она сможет выбрать не тот.
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
        clean = {k: str(v) for k, v in slots.items()}
        out.append({"name": name, "slots": clean,
                    "html": _fill(tpl, clean, photos)})
    return out


def _slots_stable(spec: str, tpls: list[tuple[str, str]]) -> str:
    """Кешируемый блок для шаблонного пути: ТЗ плюс описание слотов.

    Сам шаблон модели не показывается. Она его не пишет и не правит, а
    лишние двадцать строк разметки в контексте только приглашают вернуть
    HTML вместо значений.
    """
    return "\n".join(["## ТЗ площадки", "", spec, "", _slot_brief()])


def _slot_brief() -> str:
    """Описание слотов для модели. Едет в кешируемый блок вместе с ТЗ."""
    lines = ["## Слоты, которые ты заполняешь", "",
             "Разметку собирает код. Ты возвращаешь только значения.", ""]
    for name, (what, limit) in SLOTS.items():
        cap = f", не длиннее {limit} знаков" if limit else ""
        lines.append(f"- `{name}` — {what}{cap}")
    lines += ["", "Кегль заголовка не твоя забота: его считает код по длине "
              "строки. Размер холста, цвета и путь к фото тоже — их в "
              "ответе быть не должно."]
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
           size: tuple[int, int], cards: int) -> str:
    w, h = size
    lines = [
        "## Тема", "",
        f"- id: {theme['id']}",
        f"- площадка: {theme.get('plat')}",
        f"- формат: {theme.get('format')}",
        f"- рубрика: {theme.get('rubric') or '[не задана]'}",
        "",
        f"## Холст", "",
        f"{w}×{h} пикселей. Ровно этот размер, `overflow:hidden`.",
        f"Карточек нужно: {cards}.",
        "",
        "## Утверждённый текст Редактора", "",
        "Слова на макет берёшь только отсюда.", "",
        copy,
        "",
        "## Доступные фото", "",
        "Путь вида `../design/assets/images/<файл>`. Чего нет в списке, "
        "того не существует:", "",
    ]
    lines += [f"- {p}" for p in photos] or ["- фото нет"]
    return "\n".join(lines)


async def build(chat_id: int, ask: str, *, say=None) -> Layout:
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиль бренда ещё не собран")

    theme = _pick(chat_id, ask)
    plat = theme.get("plat") or "telegram"
    fmt = theme.get("format") or ""

    spec = _spec(b, plat)
    size = CANVAS.get(_key(plat, fmt)) or CANVAS[(plat, None)]
    tpls = _templates(plat, fmt)
    refs = [] if tpls else _reference(plat, fmt)
    if not tpls and not refs:
        raise NoSpec(f"{plat}/{fmt}: эталона в шаблон-паке нет")

    copy = _copy(b, theme)
    photos = _photos(b)

    if say:
        n = len(tpls or refs)
        await say(f"Верстаю {'макет' if n == 1 else f'{n} карточки'} по теме "
                  f"<b>{theme.get('title') or theme['id']}</b> "
                  f"({plat} · {size[0]}×{size[1]}).\n"
                  "Сборка и рендер займут до минуты.")

    if tpls:
        answer = await agent.ask(
            "design", chat_id,
            _brief(theme, copy, photos, size, len(tpls)) +
            "\n\nЗаполни слоты. Ответь одним JSON-объектом: "
            '`{"cards": [{"name": "…", "slots": {…}}], "accent": "…", '
            '"notes": []}`. Разметку не пиши — её соберёт код.',
            brand_name=b.name(), stable=_slots_stable(spec, tpls),
            max_tokens=MAX_TOKENS)
    else:
        answer = await agent.ask(
            "design", chat_id,
            _brief(theme, copy, photos, size, len(refs)) +
            "\n\nСобери макет. Ответь одним JSON-объектом в формате из твоей "
            "секции «Формат выдачи».",
            brand_name=b.name(), stable=_stable(spec, refs),
            max_tokens=MAX_TOKENS)

    data = agent.parse_json(answer, who="дизайнер")
    if tpls:
        cards = _cards_from_slots(data, tpls, photos)
    else:
        cards = [c for c in (data.get("cards") or [])
                 if isinstance(c, dict) and str(c.get("html") or "").strip()]
    if not cards:
        raise NoWork("Дизайнер не вернул ни одного макета")

    lay = Layout(theme=theme, cards=cards,
                 accent=str(data.get("accent") or ""),
                 notes=[str(n) for n in (data.get("notes") or [])])

    if say:
        await say(f"Макет собран, рендерю {len(cards)} "
                  f"{'картинку' if len(cards) == 1 else 'картинок'}.")

    for i, c in enumerate(cards, 1):
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
        if tpls:
            b.artifact(f"posts/{theme['id']}-{name}.slots.json",
                       _json.dumps(c.get("slots") or {}, ensure_ascii=False,
                                   indent=2))

    log.info("%s: карточек %s, находок %s, заметок %s", theme["id"],
             len(cards), len(lay.findings), len(lay.notes))
    return lay


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


async def _show(reg, chat_id: int, lay: Layout, size: tuple[int, int],
                topic: str, head: str = "") -> None:
    """Отдать макет человеку и встать в ожидание кнопки.

    Один код на сборку и на точечную правку. Пока это лежало внутри `run`,
    правка либо не показывала кнопок вовсе, либо обзавелась бы второй
    копией показа — а у этого проекта уже есть история с четырьмя копиями
    одной роли, где каждая починка чинила одну из четырёх.
    """
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


async def run(reg, chat_id: int, ask: str, topic: str = "design") -> None:
    table.clear(chat_id)

    async def say(text: str) -> None:
        await reg.say("design", chat_id, text, topic=topic)

    try:
        lay = await build(chat_id, ask, say=say)
    except NoSpec as e:
        await say(f"ТЗ площадки нет: {e}. Собирать на глаз не буду, "
                  "иначе макет разъедется с брендом.\n\nЗаведи ТЗ в "
                  "<code>design/platforms/</code> папки бренда — по нему "
                  "и соберу.")
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

    plat = lay.theme.get("plat") or "telegram"
    size = CANVAS.get(_key(plat, lay.theme.get("format") or "")) \
        or CANVAS[(plat, None)]
    await _show(reg, chat_id, lay, size, topic)


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

    await run(reg, chat_id,
              f"Правка к макету темы {lay.theme['id']}: {instruction}",
              topic=topic)


def _htmls_on_disk(chat_id: int, theme_id: str) -> list[Path]:
    """Макеты темы, лежащие в папке бренда. Память процесса их не помнит."""
    b = desk.brand(chat_id)
    if b is None:
        return []
    return sorted(b.path("posts").glob(f"{theme_id}-*.html"))


async def _patch_slots(reg, chat_id: int, lay: Layout, htmls: list[Path],
                       instruction: str, topic: str) -> None:
    """Правка шаблонной карточки: меняются слоты, а не разметка.

    Самый дешёвый круг из всех. Модель получает пять коротких значений и
    просьбу человека, возвращает такие же пять — двести байт вместо двух
    килобайт HTML. Разметку заново собирает `_fill`, поэтому промахнуться
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
            "Верни те же карточки с поправленными слотами и **ничего "
            'больше**: `{"cards": [{"name": "…", "slots": {…}}], '
            '"accent": "…", "notes": []}`. Слоты, которых правка не '
            "касается, оставь как есть.",
        ]),
        brand_name=b.name(), stable=_slot_brief(),
        max_tokens=MAX_TOKENS, effort=PATCH_EFFORT)

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
        slots = {k: str(v) for k, v in
                 ((got.get(name) or {}).get("slots") or {}).items()}
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
    await _show(reg, chat_id, out, size, topic,
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
    await _show(reg, chat_id, out, size, topic,
                head=f"Поправлено карточек: {len(touched)} из {len(htmls)}.")


async def on_callback(reg, chat_id: int, action: str,
                      topic: str = "design") -> None:
    action, _, theme_id = action.partition(":")

    async def say(text: str) -> None:
        await reg.say("design", chat_id, text, topic=topic)

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
