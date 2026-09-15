"""Картинки панели сплита: знак продукта и скрин его страницы.

Референс держит на панели не слова, а сам продукт: плитку с логотипом,
когда продукт назван, и окно его сайта, когда про него рассказывают. Это
единственные картинки, которые попадают **в тему**, а не рядом с ней:
сток такого не знает, а генерация рисует ломаные буквы.

**Адрес называет Монтажёр, проверяет и снимает код.** Роль знает, какой
продукт прозвучал, и знает его сайт; адрес, которого она не знает, она
оставляет пустым. Код не верит адресу на слово: только `http(s)`, только
публичный хост, страница должна ответить. Не ответила — блок остаётся
словами и это называется человеку строкой, а не приезжает пустым окном.

Снятое лежит в `montage/shots/` бренда под именем от адреса, поэтому
пересборка после правки субтитров в сеть второй раз не ходит.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from orchestrator import design
from orchestrator.imagery import slug

log = logging.getLogger("panelshot")

SHOTS = "montage/shots"

# Кадр страницы под карточку панели. Пропорция ближе к квадрату, чем
# окно браузера: на панели карточка занимает ширину, а в высоту у неё
# меньше половины кадра, и широкий скрин съезжает в узкую полоску.
PAGE_SIZE = (1200, 1400)

# Чужая страница тянет шрифты, картинки и скрипты дольше своего макета:
# бюджет втрое, иначе кадр снимется с пустыми блоками там, где контент
# ещё едет.
PAGE_BUDGET_MS = 25000

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 12
HTML_MAX = 2_000_000
IMAGE_MAX = 5_000_000
# Мельче этого знак на плитке в треть панели поедет мылом: favicon 32×32
# растянутый в десять раз хуже, чем имя словами.
LOGO_MIN_BYTES = 1500
LOGO_MIN_SIDE = 96

GOOGLE_ICONS = "https://www.google.com/s2/favicons?sz=256&domain="

IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
               "image/svg+xml": ".svg"}

# Одновременно снимаем не больше трёх страниц: каждая это свой Chrome.
PARALLEL = 3


class NoShot(RuntimeError):
    """Картинку не взять. Это не поломка монтажа: блок остаётся словами."""


def safe_url(raw: str) -> str | None:
    """Адрес, по которому завод готов сходить, или `None`.

    Роль могла назвать что угодно, включая `file://` и адрес внутри
    сети. Ходим только на публичный хост по `http(s)`.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        u = urllib.parse.urlsplit(raw)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or "." not in host \
            or host == "localhost" or host.endswith(".local"):
        return None
    try:
        ipaddress.ip_address(host)
        return None                  # голый IP роль продуктом не называет
    except ValueError:
        pass
    return urllib.parse.urlunsplit((u.scheme, u.netloc.lower(),
                                    u.path or "/", u.query, ""))


def _public(url: str) -> bool:
    """Хост резолвится только в публичные адреса."""
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return bool(infos)


def _get(url: str, limit: int) -> tuple[bytes, str, str]:
    """Байты, тип и конечный адрес после редиректов."""
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            final = r.geturl()
            if not _public(final):
                raise NoShot(f"{url} уводит во внутреннюю сеть")
            ctype = (r.headers.get_content_type() or "").lower()
            return r.read(limit + 1), ctype, final
    except urllib.error.HTTPError as e:
        raise NoShot(f"{url} ответил {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NoShot(f"{url} не открылся") from e


@dataclass
class Icon:
    href: str
    side: int       # 0 — размер не назван, 10_000 — вектор
    rank: int       # чем меньше, тем лучше при равном размере


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.icons: list[Icon] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Any]]) -> None:
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rel = a.get("rel", "").lower().split()
        href = a.get("href", "").strip()
        if not href or href.startswith("data:"):
            return
        if "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel:
            rank = 0
        elif "icon" in rel or "mask-icon" in rel:
            # mask-icon одноцветный: на белой плитке он чёрный силуэт, это
            # хуже цветного знака, но лучше имени словами.
            rank = 2 if "mask-icon" in rel else 1
        else:
            return
        side = 0
        if href.lower().split("?")[0].endswith(".svg") \
                or "svg" in a.get("type", ""):
            side = 10_000
        for m in re.finditer(r"(\d+)x(\d+)", a.get("sizes", "")):
            side = max(side, int(m.group(1)))
        if href.lower().split("?")[0].endswith(".ico"):
            return                   # .ico Chrome в canvas рисует через раз
        self.icons.append(Icon(href, side, rank))


def logo_candidates(html: str, base: str) -> list[str]:
    """Адреса знака продукта со страницы, лучший первым.

    Чистая функция: стенд проверяет выбор без сети. Крупный и векторный
    раньше мелкого, apple-touch-icon раньше favicon при равном размере —
    он квадратный и цветной по замыслу. Названные мельче `LOGO_MIN_SIDE`
    не берутся вовсе. В конце — `/apple-touch-icon.png`, который сайты
    отдают, не объявляя, и сервис знаков Google.
    """
    p = _Links()
    try:
        p.feed(html)
    except Exception:                                        # noqa: BLE001
        pass
    icons = [i for i in p.icons if i.side == 0 or i.side >= LOGO_MIN_SIDE]
    icons.sort(key=lambda i: (-i.side, i.rank))
    out: list[str] = []
    for i in icons:
        url = urllib.parse.urljoin(base, i.href)
        if url not in out:
            out.append(url)
    host = urllib.parse.urlsplit(base).hostname or ""
    for fallback in (urllib.parse.urljoin(base, "/apple-touch-icon.png"),
                     # Сервис знаков Google: отдаёт крупнейший знак сайта,
                     # в том числе из .ico и из-за Cloudflare, куда нас не
                     # пускают. Мелкий приезжает мелким и отсеивается по
                     # стороне, а не растягивается.
                     GOOGLE_ICONS + urllib.parse.quote(host)):
        if fallback not in out:
            out.append(fallback)
    return out


def _name(url: str, kind: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or "site"
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"{slug(host.replace('.', '-'))}-{kind}-{digest}"


def _cached(folder: Path, name: str) -> Path | None:
    for f in folder.glob(f"{name}.*"):
        if f.stat().st_size > 0:
            return f
    return None


def _logo(url: str, folder: Path) -> Path:
    name = _name(url, "logo")
    hit = _cached(folder, name)
    if hit:
        return hit
    if not _public(url):
        raise NoShot(f"{url} не публичный адрес")
    # Страница могла не пустить (claude.ai отвечает 403 без браузера) —
    # знак тогда ищется запасными путями, а не бросается сразу.
    try:
        blob, ctype, final = _get(url, HTML_MAX)
        html = blob.decode("utf-8", "replace") if "html" in ctype else ""
    except NoShot:
        html, final = "", url
    for cand in logo_candidates(html, final):
        try:
            data, itype, _ = _get(cand, IMAGE_MAX)
        except NoShot:
            continue
        suffix = IMAGE_TYPES.get(itype)
        if not suffix or len(data) > IMAGE_MAX:
            continue
        if suffix != ".svg" and len(data) < LOGO_MIN_BYTES:
            continue
        if suffix == ".png" and _png_side(data) < LOGO_MIN_SIDE:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"{name}{suffix}"
        out.write_bytes(data)
        return out
    raise NoShot(f"на {urllib.parse.urlsplit(url).hostname} знака "
                 "крупнее favicon не нашлось")


def _png_side(data: bytes) -> int:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        return 0
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    return min(w, h)


async def _page(url: str, folder: Path) -> Path:
    name = _name(url, "page")
    hit = _cached(folder, name)
    if hit:
        return hit
    if not await asyncio.to_thread(_public, url):
        raise NoShot(f"{url} не публичный адрес")
    # Страницу, которая не отвечает, Chrome снимет белым листом или
    # заглушкой ошибки — и её не отличить от скрина на видео.
    await asyncio.to_thread(_get, url, HTML_MAX)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        return await design.capture(url, folder / f"{name}.png", PAGE_SIZE,
                                    budget_ms=PAGE_BUDGET_MS)
    except design.NoRenderer as e:
        raise NoShot(f"{url} не снялся: {e}") from e


async def take(b, url: str, kind: str) -> Path:
    """Картинка к блоку: знак для `icon`, скрин страницы для `card`."""
    safe = safe_url(url)
    if safe is None:
        raise NoShot(f"«{url[:40]}» не годится как адрес")
    folder = b.path(SHOTS)
    if kind == "icon":
        return await asyncio.to_thread(_logo, safe, folder)
    return await _page(safe, folder)


async def dress(b, slides: list[Any]) -> list[str]:
    """Проставить картинки блокам с адресом. Вернуть, что не вышло.

    Меняет `slide.image` на месте. Блок без картинки остаётся словами:
    роль и так пишет `lines` на каждый блок, и пустым он не приедет.
    """
    todo = [s for s in slides if s.url and s.kind in ("card", "icon")]
    if not todo:
        return []
    gate = asyncio.Semaphore(PARALLEL)
    lost: list[str] = []

    async def one(s: Any) -> None:
        async with gate:
            try:
                s.image = await take(b, s.url, s.kind)
            except NoShot as e:
                what = "знака" if s.kind == "icon" else "скрина"
                lost.append(f"«{s.phrase[:30]}»: {what} не будет — {e}")
            except Exception as e:                           # noqa: BLE001
                log.warning("картинка панели не снялась: %s", e)
                lost.append(f"«{s.phrase[:30]}»: картинки не будет — "
                            f"{s.url} не снялся")

    await asyncio.gather(*(one(s) for s in todo))
    return lost


# ── картинка по теме ──────────────────────────────────────────────────
#
# Записи экрана нет — панель не остаётся одними словами: Монтажёр пишет
# бриф, Nano Banana рисует, шаблон её двигает. Стиль у всех картинок
# ролика один и задан здесь, а не в брифе: роль называет предмет, код —
# как он выглядит. Иначе три картинки одного ролика приезжали бы в трёх
# манерах, и панель читалась бы подборкой из разных мест.

ART = "montage/art"

STYLE = ("Single glossy 3D object, centered, floating, soft studio light, "
         "subtle glow in {accent}, on a plain deep {color} background that "
         "fades to the edges. Minimal, premium, calm. Absolutely no text, "
         "no letters, no numbers, no logos, no UI.")

# Холст панели 1080 на 860 (`MOTION_SPLIT`) ближе всего к 5:4; блок на
# весь кадр — вертикальный.
ASPECT_HALF = "5:4"
ASPECT_FULL = "9:16"


def art_prompt(brief: str, color: str, accent: str) -> str:
    """Бриф роли плюс стиль бренда. Чистая функция — проверяется стендом."""
    return (" ".join(brief.split()).rstrip(".") + ". "
            + STYLE.format(color=color, accent=accent))


async def draw(b, slides: list[Any], *, color: str, accent: str) -> list[str]:
    """Нарисовать картинки блокам `art`. Вернуть, что не вышло.

    Не вышло — блок остаётся карточкой со своими строками, если они есть,
    и выпадает, если нет: пустое место на панели хуже любых слов.
    """
    from orchestrator import imagegen      # сам тянет config, стенду не мешает

    todo = [s for s in slides if s.kind == "art" and s.art]
    if not todo:
        return []
    folder = b.path(ART)
    folder.mkdir(parents=True, exist_ok=True)
    gate = asyncio.Semaphore(PARALLEL)
    lost: list[str] = []

    async def one(s: Any) -> None:
        prompt = art_prompt(s.art, color, accent)
        aspect = ASPECT_FULL if s.full else ASPECT_HALF
        name = hashlib.sha1(f"{aspect}|{prompt}".encode()).hexdigest()[:12]
        hit = _cached(folder, f"art-{name}")
        if hit:
            s.image = hit
            return
        async with gate:
            try:
                blob = await asyncio.to_thread(imagegen.make, prompt, aspect)
            except imagegen.NoGen as e:
                lost.append(f"«{s.phrase[:30]}»: картинки по теме не будет — {e}")
            except Exception as e:                           # noqa: BLE001
                log.warning("картинка по теме не пришла: %s", e)
                lost.append(f"«{s.phrase[:30]}»: картинки по теме не будет — "
                            "генерация не ответила")
            else:
                suffix = ".png" if blob[:4] == b"\x89PNG" else ".jpg"
                out = folder / f"art-{name}{suffix}"
                out.write_bytes(blob)
                s.image = out

    await asyncio.gather(*(one(s) for s in todo))
    for s in todo:
        if s.image is None:
            s.kind = "card"
    return lost
