"""Обложка дайджеста: Virale в ChatPlace.

Четвёртый источник картинки рядом с фотобанком, стоком и Gemini
(`orchestrator/imagegen.py`). Нужен там, где кадра не бывает по природе
рубрики и фотобанк не помогает: понедельничная сводка это пять чужих
релизов, и поставить туда портрет автора значит соврать кадром.

**У ChatPlace нет REST, есть MCP.** Ключ заводится на странице
«Настройки → MCP» и показывается один раз; разговаривает сервер по
JSON-RPC поверх HTTP, тем же протоколом, которым с ним говорит Claude.
Поэтому здесь свой маленький клиент на сорок строк, а не SDK: завод
зовёт ровно два инструмента, `virale_image_generate` и
`virale_generation_status`, и тащить ради них зависимость дороже, чем
написать два запроса.

Ключа нет — модуль говорит это строкой и в сеть не ходит. Дайджест
уходит человеку без обложки: сводка без картинки это сводка, а молчание
понедельника неотличимо от поломки.

**Логотипы берутся из закрытого набора.** Модель, которой сказано
«логотипы нейросетей по темам недели», рисует несуществующие марки и
путает знаки; поэтому `LOGOS` перечисляет то, что мы умеем называть, а
`tags()` ищет в тексте дайджеста только их. Набор не из промпта, а из
кода — по той же причине, по какой из кода берётся `frames.ICONS`.

**Стиль живёт в папке бренда**, `design/cover-digest.md`: это правило
бренда, а не кода, и меняется оно правкой файла, а не правкой модуля.
Файла нет — модуль называет дыру и работает на запасном описании.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from config import cfg

log = logging.getLogger("virale")

# Сколько ждать картинку. Замер 20.10: две штуки в 2K собрались за 26 с.
# Потолок с запасом втрое: понедельничный прогон никуда не спешит, а
# упасть по таймауту на живой генерации дороже, чем подождать.
POLL_EVERY = 5.0
POLL_CAP = 180.0

# Что умеем называть. Ключ — как пишем в промпте, значения — по чему
# ищем в тексте дайджеста. Марки вне набора в кадр не попадают вовсе:
# выдуманный логотип на обложке читается как ошибка быстрее, чем текст.
LOGOS: dict[str, tuple[str, ...]] = {
    "the Claude starburst mark on a warm terracotta-orange rounded tile":
        ("claude", "anthropic", "клод"),
    "the OpenAI knot mark on a black rounded tile":
        ("openai", "chatgpt", "codex", "gpt", "оупенэй"),
    "the Gemini four-point spark on a blue-violet gradient tile":
        ("gemini", "google", "джемини", "нано банана", "nano banana"),
    "the Perplexity stylised question mark on a teal rounded tile":
        ("perplexity",),
    "the Midjourney sailboat mark on a deep-blue rounded tile":
        ("midjourney",),
    "the Meta infinity mark on a blue rounded tile":
        ("llama", " meta ", "цукерберг"),
    "the Mistral stacked-stripes mark on an orange rounded tile":
        ("mistral",),
    "the Grok slashed mark on a graphite rounded tile":
        ("grok", " xai "),
}

STYLE = "design/cover-digest.md"

# Запасное описание стиля. Нужно ровно на один случай: файла в папке
# бренда нет. Повторять им содержимое файла нельзя — две копии разошлись
# бы молча, — поэтому здесь нейтральный минимум, и модуль говорит, что
# работает на нём.
STYLE_FALLBACK = (
    "A clean minimal 3D product-render scene, soft studio lighting, "
    "pastel palette, glossy spheres and low cylindrical podiums, "
    "a floating glassmorphic UI panel in the centre."
)

MISSING_STYLE = (
    f"Правила обложки нет: {STYLE} в папке бренда не заведён, "
    "кадр собран на запасном описании."
)


class NoCover(RuntimeError):
    """Обложку собрать не вышло. Дайджест это не отменяет."""


def ready() -> bool:
    """Есть ли чем звать Virale. Пусто — обложки не будет, и это строка."""
    return bool(cfg.chatplace_key and cfg.chatplace_mcp)


# ── MCP: ровно столько, сколько нужно на два инструмента ──────────────

_session: str = ""


def _post(payload: dict, *, timeout: int = 120) -> dict:
    """Один запрос JSON-RPC. Ответ приходит либо JSON, либо потоком SSE.

    Поток тут не нужен — нам важен последний кадр, — но Accept без
    `text/event-stream` сервер отвергает: спецификация требует оба типа.
    Поэтому заголовок заявляет оба, а разбор смотрит, что пришло.
    """
    global _session
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {cfg.chatplace_key}",
    }
    if _session:
        headers["Mcp-Session-Id"] = _session

    req = urllib.request.Request(cfg.chatplace_mcp, data=body,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if sid := r.headers.get("Mcp-Session-Id"):
                _session = sid
            raw = r.read().decode("utf-8", "replace")
            kind = (r.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise NoCover(f"ChatPlace ответил {e.code}: {detail}") from e
    except OSError as e:
        raise NoCover(f"ChatPlace недоступен: {e}") from e

    if "text/event-stream" in kind:
        # Нам нужен последний кадр `data:`; промежуточные это прогресс.
        frames = [ln[5:].strip() for ln in raw.splitlines()
                  if ln.startswith("data:")]
        if not frames:
            raise NoCover("ChatPlace вернул пустой поток")
        raw = frames[-1]

    if not raw.strip():
        return {}
    got = json.loads(raw)
    if err := got.get("error"):
        raise NoCover(f"ChatPlace отказал: {err.get('message', err)}")
    return got.get("result") or {}


def _hello() -> None:
    """Рукопожатие. Зовётся один раз на процесс: сессия живёт в модуле."""
    if _session:
        return
    _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2025-06-18",
                      "capabilities": {},
                      "clientInfo": {"name": "content-factory",
                                     "version": "1.0"}}})
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"})


def call(tool: str, args: dict, *, timeout: int = 120) -> dict:
    """Позвать инструмент Virale. Возвращает разобранный JSON ответа.

    MCP отдаёт результат текстом внутри `content`, а не объектом: это
    протокол для модели, и структура в нём везде строкой. Разбираем
    здесь, чтобы вызывающий код не знал про упаковку.
    """
    if not ready():
        raise NoCover("ключа ChatPlace нет: CHATPLACE_KEY и CHATPLACE_MCP "
                      "в окружении пусты")
    _hello()
    res = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": tool, "arguments": args}},
                timeout=timeout)
    if res.get("isError"):
        raise NoCover(f"{tool} отказал: {res}")
    for part in res.get("content") or []:
        if part.get("type") == "text":
            try:
                return json.loads(part["text"])
            except json.JSONDecodeError:
                return {"text": part["text"]}
    return res.get("structuredContent") or {}


# ── Обложка ───────────────────────────────────────────────────────────

def tags(text: str) -> list[str]:
    """Какие марки названы в дайджесте. Порядок — как в `LOGOS`.

    Порядок фиксирован нарочно: та же неделя должна давать ту же
    обложку, а порядок по первому упоминанию зависел бы от того, как
    Ресёрчер расставил абзацы.
    """
    low = f" {text.lower()} "
    return [name for name, words in LOGOS.items()
            if any(w in low for w in words)]


def style(b) -> tuple[str, str]:
    """Правило обложки из папки бренда. Возвращает (описание, дыра)."""
    if b is None:
        return STYLE_FALLBACK, MISSING_STYLE
    path = Path(getattr(b, "root", b)) / STYLE
    if not path.exists():
        return STYLE_FALLBACK, MISSING_STYLE
    return path.read_text("utf-8").strip(), ""


def prompt(look: str, marks: list[str]) -> str:
    """Собрать запрос к модели: стиль бренда плюс знаки недели.

    Знаков больше трёх в кадр не ставим. Четвёртый логотип превращает
    обложку в коллаж из иконок, а подписи под ними всё равно нет: кто
    есть кто, читатель узнаёт из текста поста.

    Слов на обложке нет вовсе, и это сказано дважды. Модель кладёт
    подписи под иконки по привычке, а кириллицу она пишет с ошибками —
    обложка с опечаткой хуже обложки без слов.
    """
    marks = marks[:3] or [next(iter(LOGOS))]
    return (
        f"{look}\n\n"
        "Standing on the podiums around the panel, three glossy 3D "
        "app-icon tiles with rounded squares, slightly tilted, catching "
        "soft reflections: " + "; ".join(marks) + ".\n\n"
        "No text, no words, no letters, no typography, no captions "
        "anywhere in the frame. Wide editorial hero composition with "
        "generous empty space in the upper left. Photoreal 3D render, 4K."
    )


def make(ask: str, *, aspect: str = "16:9") -> bytes:
    """Собрать обложку и вернуть её байтами. Отказ это `NoCover`.

    Генерация у Virale асинхронная: инструмент отдаёт `taskId`, картинка
    появляется позже. Ждём здесь, а не возвращаем номер наружу, по той
    же причине, по какой мост зовёт субагентов синхронно: будить
    некого, тик часов кончится раньше, чем придёт картинка.
    """
    got = call("virale_image_generate", {
        "prompt": ask,
        "aspectRatio": aspect,
        "provider": "gemini_pro",
        "imageSize": "2K",
        "count": 1,
    })
    task = got.get("taskId") or got.get("id")
    if not task:
        raise NoCover(f"Virale не назвал задачу: {got}")

    waited = 0.0
    while waited < POLL_CAP:
        time.sleep(POLL_EVERY)
        waited += POLL_EVERY
        st = call("virale_generation_status", {"taskId": task})
        status = st.get("status") or ""
        if status == "completed":
            return fetch(_first_url(st))
        if status in {"failed", "cancelled"}:
            why = st.get("errorMessage") or status
            raise NoCover(f"Virale не собрал обложку: {why}")
    raise NoCover(f"Virale не ответил за {POLL_CAP:.0f} с, задача {task}")


def _first_url(st: dict) -> str:
    """Адрес картинки из ответа. Пусто — это отказ, а не пустая обложка."""
    images = (st.get("result") or {}).get("images") or st.get("images") or []
    for img in images:
        if url := img.get("imageUrl"):
            return url
    raise NoCover("Virale сказал «готово» и не дал картинку")


def fetch(url: str, *, timeout: int = 120) -> bytes:
    """Скачать картинку. Хост проверяем: адрес приходит из чужого ответа."""
    if not url.startswith("https://"):
        raise NoCover(f"адрес обложки не https: {url[:60]}")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except OSError as e:
        raise NoCover(f"обложка не скачалась: {e}") from e


def cover(b, text: str, *, aspect: str = "16:9") -> tuple[bytes, list[str]]:
    """Обложка к дайджесту: стиль бренда, знаки из текста сводки.

    Возвращает (картинка, что не сошлось). Список строк, а не исключение:
    отсутствие правила стиля это дыра, которую называют человеку, и она
    не должна отменять картинку.
    """
    look, gap = style(b)
    marks = tags(text)
    notes = [gap] if gap else []
    if not marks:
        notes.append("В сводке не названо ни одной марки из набора: "
                     "на обложке знак по умолчанию.")
    return make(prompt(look, marks), aspect=aspect), notes
