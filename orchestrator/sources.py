"""Чтение публичных источников для распаковки бренда.

Только публичное, без входа в чужие аккаунты.

Telegram отдаётся бесплатно и полно: `t.me/s/<канал>` это публичная
веб-версия, где видны тексты постов и счётчики просмотров, без авторизации
и токенов. Это закрывает и распаковку, и конкурентный анализ.

Instagram публичного доступа не даёт: страница отдаётся пустой оболочкой,
контент подгружается скриптом. Честно возвращаем «не открылось» вместо
попыток обойти — правило «отсутствие данных это тоже результат».
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

log = logging.getLogger("sources")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = httpx.Timeout(15.0, connect=8.0)

TAGS = re.compile(r"<[^>]+>")
BR = re.compile(r"<br\s*/?>", re.I)
SPACES = re.compile(r"[ \t]+")
BLANKS = re.compile(r"\n{3,}")

TG_TEXT = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
TG_VIEWS = re.compile(r'<span class="tgme_widget_message_views">([^<]+)</span>')

# Граница одного сообщения в ленте `t.me/s/`. По ней страница режется на
# сообщения, и текст с просмотрами берётся внутри каждого.
#
# Раньше текст и просмотры собирались двумя независимыми списками и
# склеивались по индексу. На живом канале это врало: у поста с одной
# картинкой текста нет, а просмотры есть — и дальше весь список
# сдвигался, приписывая каждому посту чужие цифры.
TG_MSG = "tgme_widget_message_wrap"
SERVICE = "service_message"
TG_TITLE = re.compile(r'<div class="tgme_channel_info_header_title"[^>]*>'
                      r'<span[^>]*>([^<]+)</span>', re.S)
TG_DESC = re.compile(r'<div class="tgme_channel_info_description"[^>]*>(.*?)</div>', re.S)
TG_COUNTER = re.compile(
    r'<span class="counter_value">([^<]+)</span>\s*'
    r'<span class="counter_type">([^<]+)</span>')


def _plain(raw: str) -> str:
    text = BR.sub("\n", raw)
    text = TAGS.sub("", text)
    text = html.unescape(text)
    text = SPACES.sub(" ", text)
    return BLANKS.sub("\n\n", text).strip()


def _views(raw: str) -> int | None:
    """«1.2K» → 1200, «3M» → 3000000."""
    s = raw.strip().upper().replace(",", ".")
    mult = {"K": 1_000, "M": 1_000_000}.get(s[-1:], 1)
    if mult > 1:
        s = s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


@dataclass
class Post:
    text: str
    views: int | None = None

    @property
    def length(self) -> int:
        return len(self.text)


@dataclass
class Source:
    url: str
    kind: str                       # telegram | website | instagram | youtube
    ok: bool = False
    title: str = ""
    description: str = ""
    subscribers: str = ""
    posts: list[Post] = field(default_factory=list)
    text: str = ""
    error: str = ""

    def summary(self) -> str:
        if not self.ok:
            return f"{self.url} — не открылось: {self.error}"
        if self.kind == "telegram":
            lens = [p.length for p in self.posts if p.length]
            avg = sum(lens) // len(lens) if lens else 0
            seen = [p.views for p in self.posts if p.views]
            return (f"{self.title or self.url}: {len(self.posts)} постов, "
                    f"средняя длина {avg} знаков"
                    + (f", медиана просмотров {sorted(seen)[len(seen)//2]}"
                       if seen else "")
                    + (f", {self.subscribers}" if self.subscribers else ""))
        return f"{self.title or self.url}: {len(self.text)} знаков текста"


def classify(url: str) -> tuple[str, str]:
    """Определить тип источника и нормализовать ссылку."""
    u = url.strip()
    # Голое @имя это канал Telegram, а не хост.
    if u.startswith("@"):
        return "telegram", f"https://t.me/s/{u[1:].split('/')[0]}"
    if not u.startswith("http"):
        u = "https://" + u.lstrip("/")
    host = (urlparse(u).netloc or "").lower().removeprefix("www.")
    path = urlparse(u).path.strip("/")

    if host in {"t.me", "telegram.me"}:
        name = path.removeprefix("s/").split("/")[0]
        return "telegram", f"https://t.me/s/{name}"
    if "instagram.com" in host:
        return "instagram", u
    if "youtube.com" in host or host == "youtu.be":
        return "youtube", u
    return "website", u


async def _get(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url, follow_redirects=True)
    r.raise_for_status()
    return r.text


async def fetch(url: str, *, limit: int = 40) -> Source:
    kind, norm = classify(url)
    src = Source(url=norm, kind=kind)

    if kind == "instagram":
        src.error = ("публичного доступа нет, страница отдаётся пустой. "
                     "Попроси прислать посты вручную")
        return src

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT,
                                     headers={"User-Agent": UA}) as c:
            body = await _get(c, norm)
    except Exception as e:                       # сеть, 404, таймаут
        src.error = type(e).__name__
        log.warning("не открылось %s: %s", norm, e)
        return src

    if kind == "telegram":
        src.ok = True
        if m := TG_TITLE.search(body):
            src.title = _plain(m.group(1))
        if m := TG_DESC.search(body):
            src.description = _plain(m.group(1))
        for value, ctype in TG_COUNTER.findall(body):
            if "subscriber" in ctype or "подписчик" in ctype:
                src.subscribers = f"{value.strip()} подписчиков"

        for chunk in body.split(TG_MSG)[1:]:
            # «Закрепил сообщение» и прочая служебная запись это не пост:
            # в статистике она считалась бы постом с нулём просмотров.
            if SERVICE in chunk:
                continue
            # Одно сообщение бывает разбито на несколько блоков текста,
            # поэтому склеиваем их, а не берём первый.
            text = _plain("\n".join(TG_TEXT.findall(chunk)))
            if not text:
                continue                     # картинка без подписи
            seen = TG_VIEWS.search(chunk)
            src.posts.append(Post(text, _views(seen.group(1)) if seen else None))
            if len(src.posts) >= limit:
                break
        if not src.posts:
            src.ok = False
            src.error = "канал закрыт или постов не видно"
        return src

    # website / youtube: снимаем текст страницы
    src.ok = True
    if m := re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I):
        src.title = _plain(m.group(1))
    body = re.sub(r"<(script|style|nav|footer)[^>]*>.*?</\1>", " ", body,
                  flags=re.S | re.I)
    src.text = _plain(body)[:20_000]
    return src


async def fetch_all(urls: list[str], *, limit: int = 40) -> list[Source]:
    return list(await asyncio.gather(*(fetch(u, limit=limit) for u in urls)))


URL_RE = re.compile(
    r"https?://\S+"                                  # обычная ссылка
    r"|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?"          # голый домен
    r"|@[A-Za-z0-9_]{4,}",                           # @канал
    re.I)


def extract_urls(text: str) -> list[str]:
    """Выдрать ссылки из свободного текста. @имя считаем каналом Telegram."""
    out: list[str] = []
    for m in URL_RE.findall(text or ""):
        u = m.rstrip(".,;:)»\"'")
        if u not in out:
            out.append(u)
    return out
