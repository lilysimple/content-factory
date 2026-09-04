"""Генерация фона моделью: Gemini, он же Nano Banana.

Третий источник фона рядом с фотобанком и стоком (`orchestrator/stock.py`).
Нужен там, где своей съёмки не бывает по природе рубрики: «Разбор
ошибки» и «Артефакт в ленте» — это чужая поломка и чужой промпт, и
поставить туда портрет автора значит соврать кадром.

**Генерация не ставится молча.** Как и сток, она приходит одним из трёх
вариантов и ждёт кнопку человека; в слепую ротацию `design._pick_photo`
файлы с префиксом `gen-` не берёт. Причина та же, что у стока: на
обложке личного бренда безликая картинка читается как AI-контент
быстрее, чем текст, — а здесь она вдобавок им и является.

Ключ отдельный от завода: `GEMINI_API_KEY`. Ключа нет — модуль говорит
об этом и в сеть не ходит, генерация просто не подмешивается к
вариантам. Это не то же самое, что `ANTHROPIC_API_KEY`, которого у
завода нет намеренно: текст идёт через Claude Code по подписке, а
картинки там взять негде.

**Кадр сохраняется только выбранный.** Сгенерированное сначала ложится
в кэш (`design/assets/.gen/`), и в фотобанк переезжает то, на что
человек нажал кнопку. Иначе папка бренда обрастала бы отвергнутым — а
второй раз ту же картинку не получить, у генерации нет номера страницы.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from config import cfg
from orchestrator.imagery import convert, slug

log = logging.getLogger("imagegen")

API = "https://generativelanguage.googleapis.com/v1beta/models"
PREFIX = "gen-"

# Кэш до выбора. Точка в имени папки не случайна: `design._photos`
# смотрит только в `design/assets/images`, и отвергнутый кадр не должен
# попасть ни в фотобанк, ни в правило бренда, ни в git бренда.
CACHE = "design/assets/.gen"

CREDITS = "design/assets/gen-credits.md"
CREDITS_HEAD = """# Генерация: что чем сделано

Заполняет код, когда человек выбирает сгенерированный фон. Таблица
нужна для того же, что и `stock-credits.md`: через полгода отличить
свою съёмку от машинной, не открывая файл.

| Файл | Модель | Тема | Запрос | Когда |
|---|---|---|---|---|
"""

# Холст макета 1080×1350, Chrome снимает его с двойным масштабом. `2K`
# даёт 2048 по длинной стороне: мельче поедет мылом, `4K` стоит дороже и
# всё равно ужимается `imagery.convert` до 4000.
IMAGE_SIZE = "2K"

CODES = {400: "запрос не принят: ключ или соотношение сторон",
         401: "ключ не принят",
         403: "ключ не принят или API не включён в проекте",
         429: "лимит запросов исчерпан"}


class NoGen(RuntimeError):
    """Сгенерировать не вышло. Это не поломка вёрстки: фон найдётся иначе."""


def ready() -> bool:
    return bool(cfg.gemini_key)


def _post(url: str, body: dict[str, Any], timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"x-goog-api-key": cfg.gemini_key,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise NoGen(CODES.get(e.code, f"ответ {e.code}")) from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise NoGen(f"сеть не ответила: {e}") from e


def make(prompt: str, aspect: str = "4:5") -> bytes:
    """Байты картинки по текстовому брифу. Показываются как есть."""
    if not ready():
        raise NoGen("нет GEMINI_API_KEY — ключ берётся на "
                    "aistudio.google.com/apikey")
    data = _post(
        f"{API}/{cfg.gemini_image_model}:generateContent",
        {"contents": [{"parts": [{"text": prompt}]}],
         "generationConfig": {
             "responseModalities": ["IMAGE"],
             "imageConfig": {"aspectRatio": aspect, "imageSize": IMAGE_SIZE}}},
        120)

    # Отказ приходит успешным ответом без картинки: модель считает, что
    # просьба нарушает её правила. Для нас это «фон не вышел», а не сбой
    # сети, и человеку про это лучше сказать словами.
    blocked = (data.get("promptFeedback") or {}).get("blockReason")
    if blocked:
        raise NoGen(f"модель отказалась рисовать: {blocked}")

    for cand in data.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            blob = part.get("inlineData") or part.get("inline_data") or {}
            if blob.get("data"):
                return base64.b64decode(blob["data"])
    raise NoGen("в ответе не оказалось картинки")


def _cache(b, theme_id: str) -> Path:
    return b.path(f"{CACHE}/{theme_id}.jpg")


def stage(b, theme_id: str, blob: bytes) -> str:
    """Положить сгенерированное в кэш до выбора. Возвращает имя файла."""
    path = _cache(b, theme_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path.name


def staged(b, theme_id: str) -> bytes:
    """Показать то же, что уже сгенерировано. Пусто — кэш не пережил."""
    path = _cache(b, theme_id)
    return path.read_bytes() if path.is_file() else b""


def take(b, theme_id: str, title: str, prompt: str) -> str:
    """Перевести выбранный кадр из кэша в фотобанк. Возвращает имя файла."""
    src = _cache(b, theme_id)
    if not src.is_file():
        raise NoGen("кадр не дожил до выбора — попроси макет заново")

    images = b.path("design/assets/images")
    images.mkdir(parents=True, exist_ok=True)
    base = f"{PREFIX}{slug(title) or theme_id}"
    taken = {f.name for f in images.iterdir() if f.is_file()}
    name, n = f"{base}-01.jpg", 1
    while name in taken:
        n += 1
        name = f"{base}-{n:02d}.jpg"

    with tempfile.TemporaryDirectory(prefix="gen-") as tmp:
        convert(src, Path(tmp) / name)
        (images / name).write_bytes((Path(tmp) / name).read_bytes())

    credit(b, name, title, prompt)
    sweep(b, theme_id)
    return name


def sweep(b, theme_id: str) -> None:
    """Убрать кэш темы. Отвергнутое в папке бренда не остаётся."""
    _cache(b, theme_id).unlink(missing_ok=True)


def credit(b, name: str, title: str, prompt: str) -> None:
    path = b.path(CREDITS)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CREDITS_HEAD, encoding="utf-8")
    line = " ".join(prompt.split())[:160]
    with path.open("a", encoding="utf-8") as f:
        f.write(f"| `{name}` | {cfg.gemini_image_model} | {title} | "
                f"{line} | {dt.date.today().isoformat()} |\n")
