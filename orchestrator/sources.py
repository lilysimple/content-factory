"""Чтение публичных источников для распаковки бренда.

Только публичное, без входа в чужие аккаунты.

Telegram отдаётся бесплатно и полно: `t.me/s/<канал>` это публичная
веб-версия, где видны тексты постов и счётчики просмотров, без авторизации
и токенов. Это закрывает и распаковку, и конкурентный анализ.

Instagram публичного доступа не даёт: страница отдаётся пустой оболочкой,
контент подгружается скриптом. Отсюда за ним не ходят вовсе — профили
снимает `tools/instagram_pull.py` через залогиненный Chrome, а читает их
`orchestrator/instagram.py` из кэша папки бренда.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
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

# Дату поста лента отдаёт, и это выяснилось не сразу: сводка 2026-08-31
# написала «дат у постов лента не отдаёт» и построила медиану на постах
# разного возраста. Отдаёт — атрибутом `datetime` в подвале сообщения.
#
# `[^>]*datetime=` обязателен: в чанке с видео есть ещё один `<time>`, это
# длительность ролика, и у него атрибута нет. Берём последнее совпадение —
# подвал идёт в конце сообщения, а у пересланного поста первым стоит время
# оригинала.
TG_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')

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


def _when(chunk: str) -> datetime | None:
    """Время поста из подвала сообщения. Не разобралось — прочерк, не ноль."""
    stamps = TG_TIME.findall(chunk)
    if not stamps:
        return None
    try:
        return datetime.fromisoformat(stamps[-1])
    except ValueError:
        return None


# ── RSS и Atom ────────────────────────────────────────────────────────
#
# До 06.09 внешний источник вне Telegram в сводку не попадал вовсе, хотя
# ссылки на него `sources.md` разрешал. `fetch` снимал со страницы плоский
# текст, постов у такого источника не было ни одного, и `research._brief`
# клал модели «N знаков текста» вместо самих новостей: платили за
# скачивание и не получали ничего. Окно недели к ленте без дат не
# применялось никак, а `measure` отдавал ноль постов и дыру «сравнивать
# нечего» на каждый добавленный сайт.
#
# Фид это тот же список датированных записей, что и лента канала. Разобрав
# его в `Post`, мы отдаём внешний источник тому же коду, что и Telegram:
# `Window.split` режет прошлое, `measure` считает, `_brief` кладёт
# двенадцать записей по двести знаков. Второго дома у арифметики не
# появляется, и цена внешнего источника становится такой же, как у канала.
#
# Фид узнаётся по телу, а не по адресу: `/feed`, `/rss.xml`, `/atom` и
# голый путь раздела встречаются вперемешку, а половина «RSS-адресов»
# отдаёт HTML-страницу с двумя сотнями. Проверка по первым строкам ловит
# и то, и другое.
FEED_MARK = re.compile(r"<(rss|feed|rdf:RDF)[\s>]", re.I)
FEED_ITEM = re.compile(r"<(item|entry)[\s>](.*?)</\1>", re.S | re.I)
FEED_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
FEED_BODY = re.compile(
    r"<(description|summary|content(?::encoded)?)[^>]*>(.*?)</\1>",
    re.S | re.I)
FEED_WHEN = re.compile(
    r"<(pubDate|updated|published|dc:date)[^>]*>([^<]+)</\1>", re.I)
CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)

# Описание режется здесь, а не у читателя. Полнотекстовый фид Substack
# отдаёт статью целиком — двадцать тысяч знаков на запись, — и без этой
# границы один источник съедал бы больше, чем девять каналов вместе.
FEED_CUT = 600


def _feed_plain(raw: str) -> str:
    """Текст записи фида: CDATA снимается, разметка внутри — тоже.

    Порядок важен. Разметка в фиде приезжает экранированной
    (`&lt;p&gt;`), и снять теги до `unescape` значит оставить их в тексте
    видимыми: читатель получит «<p>Сегодня OpenAI</p>» вместо новости.
    """
    text = CDATA.sub(r"\1", raw)
    text = html.unescape(text)
    text = BR.sub("\n", text)
    text = TAGS.sub(" ", text)
    text = html.unescape(text)
    text = SPACES.sub(" ", text)
    return BLANKS.sub("\n\n", text).strip()


def _feed_when(chunk: str) -> datetime | None:
    """Дата записи: RFC 822 у RSS, ISO 8601 у Atom.

    Не разобралось — прочерк, а не сегодня. Дата «на всякий случай»
    затащила бы прошлогоднюю запись в окно недели, а это ровно та
    поломка, ради которой окно и заведено.
    """
    m = FEED_WHEN.search(chunk)
    if not m:
        return None
    stamp = m.group(2).strip()
    try:
        return parsedate_to_datetime(stamp)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Post:
    text: str
    views: int | None = None
    date: datetime | None = None
    # Лайки и комментарии есть у Instagram и нет у ленты Telegram. Поля
    # необязательные: прочерк означает «площадка не отдаёт», а не ноль.
    likes: int | None = None
    comments: int | None = None

    @property
    def length(self) -> int:
        return len(self.text)


@dataclass
class Source:
    url: str
    kind: str                # telegram | feed | website | instagram | youtube
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
        if self.kind == "feed":
            dated = [p.date for p in self.posts if p.date]
            fresh = f", свежая {max(dated):%d.%m}" if dated else ""
            return (f"{self.title or self.url}: "
                    f"{len(self.posts)} записей в отдаче фида{fresh}")
        if self.kind == "telegram":
            lens = [p.length for p in self.posts if p.length]
            avg = sum(lens) // len(lens) if lens else 0
            seen = [p.views for p in self.posts if p.views]
            return (f"{self.title or self.url}: {len(self.posts)} постов, "
                    f"средняя длина {avg} знаков"
                    + (f", медиана просмотров {sorted(seen)[len(seen)//2]}"
                       if seen else "")
                    + (f", {self.subscribers}" if self.subscribers else ""))
        if self.kind == "instagram":
            # Медиана тут по лайкам: просмотры Instagram отдаёт только у
            # видео, и подписать ими сетку значило бы соврать читателю.
            liked = [p.likes for p in self.posts if p.likes]
            dated = [p.date for p in self.posts if p.date]
            return (f"{self.title or self.url}: "
                    f"{len(self.posts)} постов в кэше"
                    + (f", свежий {max(dated):%d.%m}" if dated else "")
                    + (f", медиана лайков {sorted(liked)[len(liked)//2]}"
                       if liked else "")
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
        # Сеть тут не при чём: страница отдаётся пустой оболочкой, а
        # `api/v1` без залогиненной сессии — пустотой или капчей. Профили
        # снимает `tools/instagram_pull.py` в кэш папки бренда, читает
        # `orchestrator/instagram.py`. Отсюда за ними не ходят.
        src.error = ("публичного доступа нет: профиль читается из кэша, "
                     "который снимает `tools/instagram_pull.py`")
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
            src.posts.append(Post(text,
                                  _views(seen.group(1)) if seen else None,
                                  _when(chunk)))
            if len(src.posts) >= limit:
                break
        if not src.posts:
            src.ok = False
            src.error = "канал закрыт или постов не видно"
        return src

    # Фид разбирается постами и уходит дальше тем же кодом, что и лента
    # канала. Проверяем до страницы: тело у фида отдаётся с тем же
    # `text/html` у половины хостов, так что заголовок ответа не улика.
    if FEED_MARK.search(body[:2000]):
        _read_feed(src, body, limit)
        return src

    # website / youtube: снимаем текст страницы
    src.ok = True
    if m := re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I):
        src.title = _plain(m.group(1))
    body = re.sub(r"<(script|style|nav|footer)[^>]*>.*?</\1>", " ", body,
                  flags=re.S | re.I)
    src.text = _plain(body)[:20_000]
    return src


def _read_feed(src: Source, body: str, limit: int) -> None:
    """Записи фида в `Post`. Просмотров тут нет и не будет: их не отдают."""
    src.kind = "feed"
    # Заголовок фида, а не первой записи: `<title>` есть и там, и там, и
    # без выреза записей канал назывался бы своей верхней новостью.
    if m := FEED_TITLE.search(FEED_ITEM.sub(" ", body)):
        src.title = _feed_plain(m.group(1))

    for _, chunk in FEED_ITEM.findall(body):
        head = FEED_TITLE.search(chunk)
        title = _feed_plain(head.group(1)) if head else ""
        told = FEED_BODY.search(chunk)
        text = _feed_plain(told.group(2))[:FEED_CUT] if told else ""
        # Заголовок и описание склеиваются в одну строку: читателю ниже
        # достаётся её начало, и заголовок должен быть в этом начале.
        full = " — ".join(part for part in (title, text) if part)
        if not full:
            continue
        src.posts.append(Post(full, None, _feed_when(chunk)))
        if len(src.posts) >= limit:
            break

    src.ok = bool(src.posts)
    if not src.ok:
        src.error = "фид открылся, но записей в нём нет"


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
