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
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ROOT, cfg
from orchestrator import agent
from storage import brand as brand_store
from storage import db

log = logging.getLogger("design")

store = brand_store.Store(cfg.brands_path)

PACK = ROOT / "design-pack"
MAX_TOKENS = 16000
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

ABSOLUTE = re.compile(r'(?:href|src)\s*=\s*["\'](?:file://|/|[a-z]+://)', re.I)
LITERAL_COLOR = re.compile(r":\s*#[0-9A-Fa-f]{3,8}\b")
TAGS = re.compile(r"<[^>]+>")


class NoText(RuntimeError):
    """Утверждённого текста нет — верстать нечего."""


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

def _brand(chat_id: int):
    row = db.one("SELECT brand_slug FROM tenants WHERE chat_id = ?", chat_id)
    return store.get(row["brand_slug"]) if row and row["brand_slug"] else None


ID_RX = re.compile(r"\b\d{4}-\d{2}-\d{2}-[a-z]+-\d{2}\b")


def _pick(chat_id: int, ask: str) -> dict[str, Any]:
    """Тема с утверждённым текстом. Верстать черновик смысла нет."""
    rows = db.q("SELECT * FROM themes WHERE chat_id = ? AND status = 'ready' "
                "AND asset IS NOT NULL ORDER BY date", chat_id)
    if not rows:
        raise NoText("нет ни одного утверждённого текста")

    if m := ID_RX.search(ask):
        for r in rows:
            if r["id"] == m.group():
                return dict(r)
        raise NoText(f"у темы {m.group()} нет утверждённого текста")
    return dict(rows[0])


def _copy(b, theme: dict[str, Any]) -> str:
    """Утверждённый текст без служебной шапки Редактора."""
    raw = b.read(theme["asset"])
    if not raw.strip():
        raise NoText(f"файл {theme['asset']} пуст")
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
    profile = html_path.parent / f".chrome-{html_path.stem}"

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

def _brief(theme: dict[str, Any], copy: str, spec: str, photos: list[str],
           size: tuple[int, int], refs: list[tuple[str, str]]) -> str:
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
        f"Карточек нужно: {len(refs)}.",
        "",
        "## Утверждённый текст Редактора", "",
        "Слова на макет берёшь только отсюда.", "",
        copy,
        "",
        "## ТЗ площадки", "", spec,
        "",
        "## Доступные фото", "",
        "Путь вида `../design/assets/images/<файл>`. Чего нет в списке, "
        "того не существует:", "",
    ]
    lines += [f"- {p}" for p in photos] or ["- фото нет"]
    lines += ["", "## Эталон, который клонируешь", ""]
    for name, html in refs:
        lines += [f"### {name}", "", "```html", html.strip(), "```", ""]
    return "\n".join(lines)


async def build(chat_id: int, ask: str, *, say=None) -> Layout:
    b = _brand(chat_id)
    if b is None:
        raise NoText("профиль бренда ещё не собран")

    theme = _pick(chat_id, ask)
    plat = theme.get("plat") or "telegram"
    fmt = theme.get("format") or ""

    spec = _spec(b, plat)
    size = CANVAS.get(_key(plat, fmt)) or CANVAS[(plat, None)]
    refs = _reference(plat, fmt)
    if not refs:
        raise NoSpec(f"{plat}/{fmt}: эталона в шаблон-паке нет")

    copy = _copy(b, theme)
    photos = _photos(b)

    if say:
        n = len(refs)
        await say(f"Верстаю {'макет' if n == 1 else f'{n} карточки'} по теме "
                  f"<b>{theme.get('title') or theme['id']}</b> "
                  f"({plat} · {size[0]}×{size[1]}).\n"
                  "Сборка и рендер займут до минуты.")

    answer = await agent.ask(
        "design", chat_id,
        _brief(theme, copy, spec, photos, size, refs) +
        "\n\nСобери макет. Ответь одним JSON-объектом в формате из твоей "
        "секции «Формат выдачи».",
        brand_name=b.name(), max_tokens=MAX_TOKENS)

    data = agent.parse_json(answer, who="дизайнер")
    cards = [c for c in (data.get("cards") or [])
             if isinstance(c, dict) and str(c.get("html") or "").strip()]
    if not cards:
        raise NoText("Дизайнер не вернул ни одного макета")

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

    log.info("%s: карточек %s, находок %s, заметок %s", theme["id"],
             len(cards), len(lay.findings), len(lay.notes))
    return lay


# ── карточка и кнопки ─────────────────────────────────────────────────

_pending: dict[int, Layout] = {}
_awaiting_fix: set[int] = set()


def _kb(theme_id: str) -> InlineKeyboardMarkup:
    """id темы в кнопке: макет в памяти не переживает перезапуск бота."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Ок", callback_data=f"art:ok:{theme_id}"),
        InlineKeyboardButton(text="✏️ Правки", callback_data=f"art:fix:{theme_id}"),
        InlineKeyboardButton(text="📤 В очередь",
                             callback_data=f"art:queue:{theme_id}"),
    ]])


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


def wants_fix(chat_id: int) -> bool:
    return chat_id in _awaiting_fix


async def run(reg, chat_id: int, ask: str, topic: str = "design") -> None:
    _awaiting_fix.discard(chat_id)

    async def say(text: str) -> None:
        await reg.say("design", chat_id, text, topic=topic)

    try:
        lay = await build(chat_id, ask, say=say)
    except NoSpec as e:
        await reg.say("design", chat_id,
                      f"ТЗ площадки нет: {e}. Собирать на глаз не буду, "
                      "иначе макет разъедется с брендом.\n\nЗаведи ТЗ в "
                      "<code>design/platforms/</code> папки бренда — по нему "
                      "и соберу.", topic=topic)
        return
    except NoText as e:
        await reg.say("design", chat_id,
                      f"Верстать нечего: {e}. Сначала текст от Редактора.",
                      topic=topic)
        return
    except NoRenderer as e:
        await reg.say("design", chat_id,
                      f"Макет собрал, но PNG не получился: {e}. "
                      "Отдавать один HTML без картинки смысла нет.",
                      topic=topic)
        return
    except agent.BudgetExceeded as e:
        await reg.say("design", chat_id, f"Остановился: {e}", topic=topic)
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("макет не собрался")
        reason = getattr(e, "message", None) or str(e) or type(e).__name__
        await reg.say("design", chat_id, f"Макет не собрался: {reason}",
                      topic=topic)
        return

    plat = lay.theme.get("plat") or "telegram"
    size = CANVAS.get(_key(plat, lay.theme.get("format") or "")) \
        or CANVAS[(plat, None)]
    _pending[chat_id] = lay

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


async def revise(reg, chat_id: int, instruction: str,
                 topic: str = "design") -> None:
    _awaiting_fix.discard(chat_id)
    lay = _pending.get(chat_id)
    if lay is None:
        await reg.say("design", chat_id, "Этот макет уже неактуален.",
                      topic=topic)
        return

    b = _brand(chat_id)
    if b is not None:
        b.append("design/corrections.md",
                 f"- {lay.theme['id']}: {instruction.strip()}")

    await run(reg, chat_id,
              f"Правка к макету темы {lay.theme['id']}: {instruction}",
              topic=topic)


async def on_callback(reg, chat_id: int, action: str,
                      topic: str = "design") -> None:
    action, _, theme_id = action.partition(":")
    lay = _pending.get(chat_id)
    if lay is not None and theme_id and lay.theme["id"] != theme_id:
        lay = None
    if lay is None and theme_id:
        row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                     theme_id, chat_id)
        if row is not None:
            b = _brand(chat_id)
            files = sorted(b.path("posts").glob(f"{theme_id}-*")) if b else []
            lay = Layout(theme=dict(row), files=list(files))

    if action == "ok":
        _pending.pop(chat_id, None)
        _awaiting_fix.discard(chat_id)
        if lay is None:
            await reg.say("design", chat_id, "Этот макет уже неактуален.",
                          topic=topic)
            return
        await reg.say("design", chat_id,
                      f"Принято. Файлы лежат в <code>posts/</code> папки "
                      f"бренда: {len(lay.pngs)} PNG и столько же HTML.",
                      topic=topic)
        return

    if action == "fix":
        if lay is None:
            await reg.say("design", chat_id, "Этот макет уже неактуален.",
                          topic=topic)
            return
        _awaiting_fix.add(chat_id)
        await reg.say("design", chat_id,
                      "Напиши одним сообщением, что поправить. Запишу правку "
                      "в дизайн-профиль и пересоберу.", topic=topic)
        return

    if action == "queue":
        await reg.say("design", chat_id,
                      "Публикатор ещё не подключён, это следующий шаг сборки. "
                      "Комплект готов, выкладываешь пока сама.", topic=topic)
