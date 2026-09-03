"""Сток: найти фон по словам, показать, забрать выбранное.

Источник — Pexels: лицензия разрешает коммерческое использование и
правку, атрибуция не обязательна, ключ бесплатный (`PEXELS_API_KEY`).

**Сток не ставится молча.** Дизайнер предлагает его как один из
вариантов фона, а выбирает человек кнопкой; в слепую ротацию
`design._pick_photo` файлы с префиксом `stock-` не берёт. Причина не
вкусовая: фон обложки в личном бренде — сам человек, а безликая
картинка со стока читается как AI-контент быстрее, чем текст.

Скачивается только **выбранное**. На показ идёт превью из ответа API, и
папка бренда не обрастает тем, что человек отверг.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from config import cfg
from orchestrator.imagery import convert, slug

log = logging.getLogger("stock")

API = "https://api.pexels.com/v1/search"
PREFIX = "stock-"
CREDITS = "design/assets/stock-credits.md"
CREDITS_HEAD = """# Сток: откуда какой файл

Заполняет код, когда человек выбирает стоковый фон. Лицензия Pexels
атрибуции не требует, таблица нужна для другого: отличить сток от своей
съёмки через полгода.

| Файл | Автор | Источник | Запрос | Когда |
|---|---|---|---|---|
"""

# User-Agent обязателен: без него перед API стоит Cloudflare и отвечает
# 403 при верном ключе. Ошибка выглядит как «ключ не приняли», и искать
# её начинаешь не там — на это ушёл один заход.
UA = "content-factory/1.0 (photo pull for brand assets)"

# Слова, после которых со стока приезжает чужой человек в кадре. В
# личном бренде это худший сток из возможных: постановку узнают быстрее,
# чем прочитают текст.
PEOPLE = ("woman", "man", "person", "people", "girl", "boy", "team",
          "portrait", "female", "male", "model", "businessman")

CODES = {401: "ключ не принят",
         403: "ключ не принят или не активирован",
         429: "лимит запросов исчерпан"}


class NoStock(RuntimeError):
    """Сток недоступен: нет ключа, нет сети, нет выдачи."""


def ready() -> bool:
    return bool(cfg.pexels_key)


def _get(url: str, headers: dict[str, str], timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise NoStock(f"Pexels ответил {e.code}: "
                      f"{CODES.get(e.code, e.reason)}") from e
    except urllib.error.URLError as e:
        raise NoStock(f"Pexels недоступен: {e.reason}") from e


def search(query: str, n: int = 3, *, landscape: bool = False,
           page: int = 1) -> list[dict[str, Any]]:
    """Кандидаты по запросу. Пусто — не ошибка, а пустая выдача."""
    if not ready():
        raise NoStock("нет PEXELS_API_KEY — ключ берётся на pexels.com/api")
    if re.search(r"[а-яё]", query, re.I):
        # Теги на Pexels английские, а русский запрос он переводит грубо:
        # на «минимализм стол ноутбук» приезжает инжир на тарелке.
        log.warning("запрос кириллицей, выдача будет мимо: %s", query)
    url = f"{API}?" + urllib.parse.urlencode({
        "query": query, "per_page": max(1, min(n, 20)), "page": max(1, page),
        "orientation": "landscape" if landscape else "portrait"})
    return json.loads(_get(url, {"Authorization": cfg.pexels_key}, 30)) \
        .get("photos", [])


def preview(photo: dict[str, Any]) -> bytes:
    """Байты для показа человеку. В папку бренда это не попадает."""
    return _get(photo["src"]["large"], {}, 60)


def take(b, photo: dict[str, Any], query: str) -> str:
    """Забрать выбранное в фотобанк бренда. Возвращает имя файла."""
    images = b.path("design/assets/images")
    images.mkdir(parents=True, exist_ok=True)
    base = f"{PREFIX}{slug(query) or 'photo'}"
    taken = {f.name for f in images.iterdir() if f.is_file()}
    name, n = f"{base}-01.jpg", 1
    while name in taken:
        n += 1
        name = f"{base}-{n:02d}.jpg"

    with tempfile.TemporaryDirectory(prefix="stock-") as tmp:
        raw = Path(tmp) / "raw.jpg"
        raw.write_bytes(_get(photo["src"]["original"], {}, 90))
        convert(raw, Path(tmp) / name)
        (images / name).write_bytes((Path(tmp) / name).read_bytes())

    credit(b, name, photo, query)
    return name


def credit(b, name: str, photo: dict[str, Any], query: str) -> None:
    path = b.path(CREDITS)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CREDITS_HEAD, encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"| `{name}` | {photo.get('photographer') or '—'} | "
                f"{photo.get('url') or '—'} | {query} | "
                f"{dt.date.today().isoformat()} |\n")
