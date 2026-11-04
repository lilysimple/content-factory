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
import base64
import datetime as dt
import hashlib
import ipaddress
import json
import logging
import re
import shutil
import socket
import tempfile as _tmp
import time
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

# Журнал нарисованного. Без него папка — двадцать пять файлов с хешем в
# имени и ничем больше: узнать, что на картинке и к какому ролику её
# рисовали, нельзя ни кодом, ни глазами. Кэш ключуется дословным брифом,
# поэтому Монтажёр, сказавший то же другими словами, платит за новую
# картинку — журнал показывает, за что именно заплачено и что уже есть.
#
# Форма та же, что у `gen-credits.md` Дизайнера: одна таблица, дописываем
# строкой. Чтение — человеку, не коду: разбирать его завод не умеет.
ART_CREDITS = "montage/art/credits.md"
ART_HEAD = """# Картинки панели: что чем нарисовано

Заполняет код, когда картинка действительно нарисована. Взятая из кэша
строки не добавляет — она уже здесь.

| Файл | Тема | Бриф | Когда |
|---|---|---|---|
"""


def art_credit(b, name: str, theme: str, brief: str) -> None:
    """Строка в журнал. Не записалось — картинку это не отменяет."""
    try:
        path = b.path(ART_CREDITS)
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(ART_HEAD, encoding="utf-8")
        line = " ".join(brief.split())[:160]
        with path.open("a", encoding="utf-8") as f:
            f.write(f"| `{name}` | {theme or '—'} | {line} | "
                    f"{dt.date.today().isoformat()} |\n")
    except OSError as e:                                     # noqa: BLE001
        log.warning("журнал картинок не записался: %s", e)

STYLE = ("Single glossy 3D object, centered, floating, soft studio light, "
         "subtle glow in {accent}, on a plain deep {color} background that "
         "fades to the edges. Minimal, premium, calm. Absolutely no text, "
         "no letters, no numbers, no logos, no UI.")

# Холст панели 1080 на 576 (`MOTION_SPLIT`) это 1.875:1, и из того, что
# умеет генератор, ближе всего 16:9; блок на весь кадр — вертикальный.
ASPECT_HALF = "16:9"
ASPECT_FULL = "9:16"


def art_prompt(brief: str, color: str, accent: str) -> str:
    """Бриф роли плюс стиль бренда. Чистая функция — проверяется стендом."""
    return (" ".join(brief.split()).rstrip(".") + ". "
            + STYLE.format(color=color, accent=accent))


async def draw(b, slides: list[Any], *, color: str, accent: str,
               theme: str = "") -> list[str]:
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
                art_credit(b, out.name, theme, s.art)

    await asyncio.gather(*(one(s) for s in todo))
    for s in todo:
        if s.image is None:
            s.kind = "card"
    return lost


# ── снимок страницы под слой доказательства ───────────────────────────
#
# Второй способ снять страницу, рядом с `_page`, и отличается он не
# размером кадра. `_page` снимает окно под карточку панели: кадр
# фиксированный, и всё, что не влезло, обрезано. Слою `proof` нужна
# страница **целиком** и, сверх того, места строк на ней — по ним едет
# маркер, и считать их на глаз нельзя никому.
#
# Поэтому здесь свой Chrome с отладочным портом, а не `design.capture`:
# одним кадром такое не снять, нужен разговор со страницей. Разговор
# один и короткий — снять кадр и спросить прямоугольники строк.
#
# Страница чужая и живая: строки на ней переписывают без нас. Строка,
# которой на странице нет, поэтому не отказ, а пропуск — он называется
# человеку, а остальные подсветки едут как ехали. Отказ тут означал бы,
# что правка одного абзаца в чужой справке роняет монтаж целиком.

PROOF_WIDTH = 1100           # ширина окна: страница верстается под десктоп
PROOF_MAX_HEIGHT = 9000      # потолок высоты снимка, пиксели
PROOF_SETTLE = 4.0           # пауза после навигации: шрифты и картинки
PROOF_START = 25.0           # секунд на старт Chrome и первый кадр

PROOFS = "montage/proofs"

# Поиск строки на странице. Сравниваем по сжатым пробелам и без
# регистра: в вёрстке строка разорвана переносами и тегами, а человек
# цитирует её одной строкой.
FIND_JS = r"""(() => {
  const want = %s;
  const norm = (s) => s.replace(/\s+/g, ' ').trim().toLowerCase();
  const doc = document.documentElement;
  const W = doc.scrollWidth, H = doc.scrollHeight;
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let n;
  while ((n = walk.nextNode())) {
    if (norm(n.nodeValue)) nodes.push(n);
  }
  const out = [];
  for (const phrase of want) {
    const need = norm(phrase);
    let hit = null;
    for (const node of nodes) {
      const text = norm(node.nodeValue);
      const at = text.indexOf(need);
      if (at < 0) continue;
      // Смещение в сжатом тексте не равно смещению в исходном: ищем
      // заново по исходной строке, иначе рамка уедет на пару слов.
      const raw = node.nodeValue.toLowerCase();
      let from = raw.indexOf(need.split(' ')[0]);
      if (from < 0) from = 0;
      const r = document.createRange();
      try {
        r.setStart(node, from);
        r.setEnd(node, Math.min(node.nodeValue.length,
                                from + phrase.length));
      } catch (e) { continue; }
      const box = r.getBoundingClientRect();
      if (box.width < 4 || box.height < 4) continue;
      hit = {
        x: (box.left + window.scrollX) / W,
        y: (box.top + window.scrollY) / H,
        w: box.width / W,
        h: box.height / H,
      };
      break;
    }
    out.push(hit);
  }
  return JSON.stringify({width: W, height: H, spots: out});
})()"""


def _proof_name(url: str, marks: list[str]) -> str:
    """Имя снимка. Строки входят в отпечаток вместе с адресом: та же
    страница с другими подсветками это другое задание, и взять её из
    кэша со старыми прямоугольниками значит подсветить не то."""
    key = url + "\n" + "\n".join(marks)
    return "proof-" + hashlib.sha256(key.encode()).hexdigest()[:16]


async def _chrome(port: int, url: str):
    """Свой headless Chrome с отладочным портом. Процесс — на вызвавшем."""
    profile = Path(_tmp.mkdtemp(prefix="chrome-proof-"))
    proc = await asyncio.create_subprocess_exec(
        design.chrome(), "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-background-networking",
        "--disable-sync", f"--window-size={PROOF_WIDTH},1400",
        f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        url,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return proc, profile


async def proof(b, url: str, marks: list[str]
                ) -> tuple[Path, tuple[int, int],
                           list[tuple[str, float, float, float, float]],
                           list[str]]:
    """Снимок страницы целиком и места строк на нём.

    Отдаёт путь к PNG, его размер, найденные строки прямоугольниками в
    долях снимка и строки человеку про то, что не нашлось.
    """
    safe = safe_url(url)
    if safe is None:
        raise NoShot(f"«{url[:40]}» не годится как адрес")
    if not await asyncio.to_thread(_public, safe):
        raise NoShot(f"{safe} не публичный адрес")

    folder = b.path(PROOFS)
    name = _proof_name(safe, marks)
    png = folder / f"{name}.png"
    meta = folder / f"{name}.json"
    if png.is_file() and png.stat().st_size > 0 and meta.is_file():
        saved = json.loads(meta.read_text("utf-8"))
        return (png, tuple(saved["size"]),
                [(s[0], s[1], s[2], s[3], s[4]) for s in saved["spots"]],
                list(saved.get("lost") or []))

    # Страница, которая не отвечает, снимется белым листом — и на видео
    # этот лист не отличить от скрина. Проверяем до Chrome.
    await asyncio.to_thread(_get, safe, HTML_MAX)
    folder.mkdir(parents=True, exist_ok=True)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    proc, profile = await _chrome(port, safe)
    lost: list[str] = []
    try:
        sock = await asyncio.to_thread(_proof_socket, port)
        try:
            await asyncio.sleep(PROOF_SETTLE)
            found = await asyncio.to_thread(
                sock.evaluate, FIND_JS % json.dumps(marks, ensure_ascii=False))
            data = json.loads(found) if isinstance(found, str) else {}
            w = min(int(data.get("width") or PROOF_WIDTH), PROOF_WIDTH * 2)
            h = min(int(data.get("height") or 0), PROOF_MAX_HEIGHT)
            if h < 200:
                raise NoShot(f"{safe}: страница пришла пустой")
            shot = await asyncio.to_thread(
                sock.call, "Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": True,
                 "clip": {"x": 0, "y": 0, "width": w, "height": h,
                          "scale": 1}})
        finally:
            await asyncio.to_thread(sock.close)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        shutil.rmtree(profile, ignore_errors=True)

    raw = base64.b64decode((shot.get("result") or {}).get("data") or "")
    if not raw:
        raise NoShot(f"{safe}: Chrome не отдал кадр")
    png.write_bytes(raw)

    spots: list[tuple[str, float, float, float, float]] = []
    for phrase, hit in zip(marks, list(data.get("spots") or [])):
        if not hit:
            lost.append(f"на странице не нашлось: «{phrase[:40]}» — "
                        "маркер по ней не поедет")
            continue
        spots.append((phrase, float(hit["x"]), float(hit["y"]),
                      float(hit["w"]), float(hit["h"])))
    meta.write_text(json.dumps(
        {"size": [w, h], "spots": [list(s) for s in spots], "lost": lost},
        ensure_ascii=False), "utf-8")
    return png, (w, h), spots, lost


def _proof_socket(port: int):
    """Сокет отладки, когда Chrome успел открыть порт."""
    from orchestrator import cdp
    deadline = time.monotonic() + PROOF_START
    while time.monotonic() < deadline:
        try:
            return cdp.Socket(cdp.page_socket(port, timeout=1.0))
        except Exception:                            # noqa: BLE001
            time.sleep(0.4)
    raise NoShot("Chrome не открыл порт отладки")


async def prove(b, overlays: list[Any]) -> list[str]:
    """Проставить снимки слоям `proof`. Вернуть, что не вышло.

    Меняет слой на месте. Снимка нет — слой выбрасывается вызывающим:
    `proof` без страницы это пустое место в кадре, а не слой словами.
    """
    todo = [o for o in overlays if o.kind == "proof" and o.url]
    if not todo:
        return []
    gate = asyncio.Semaphore(PARALLEL)
    lost: list[str] = []

    async def one(o: Any) -> None:
        async with gate:
            try:
                png, size, spots, gaps = await proof(b, o.url, o.marks)
            except (NoShot, OSError, ValueError) as e:
                lost.append(f"«{o.phrase[:40]}»: {e}")
                return
            o.image, o.image_size, o.spots = png, size, spots
            lost.extend(gaps)

    await asyncio.gather(*(one(o) for o in todo))
    return lost
