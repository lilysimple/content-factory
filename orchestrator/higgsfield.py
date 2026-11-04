"""Клип панели генерацией: Higgsfield.

Четвёртый источник кадра рядом с фотобанком, стоком и генерацией фона
(`orchestrator/imagegen.py`) — и единственный, который отдаёт **движение**,
а не картинку. Нужен там, где в панели сплита должно идти то, чего не
снять и не найти: «поток», абстрактная фактура под слово, кадр из того,
о чём говорит голова.

Зачем он лёг именно сюда. У карточки панели поле `video` есть с самого
начала (`tools/avatar_split.py`): готовый клип делает сорт `media`, файл
уезжает в `public/` рядом с головой, ретушь мини-роликов ложится на него
по `graded`. То есть дырка под генерацию была прорезана раньше, чем
появился поставщик, и модуль закрывает ровно её — ничего в монтаже
менять не нужно.

**Ключ платный, и считает Higgsfield генерации, а не минуты.** Поэтому
модуль зовёт человек руками через `tools/higgsfield_pull.py`, как
`heygen_pull.py` и `trends_pull.py`: автоматического вызова из роли у
него нет. Пусто в ключе — модуль говорит это строкой и в сеть не идёт.

Ключ двойной: Higgsfield авторизует парой «id и секрет» одним
заголовком — `Authorization: Key ${ID}:${SECRET}`. Берётся в консоли
платформы, показывается один раз.

**Запрос идёт с `Idempotency-Key`.** Генерация стоит денег, а оборванный
ответ выглядит как неудача: повтор без ключа идемпотентности оплатил бы
тот же клип дважды. Ключ считается от модели, промпта и длины — тот же
запрос второй раз денег не стоит.

Модели держатся таблицей, а не строкой в вызове: у каждой свой адрес,
свой набор полей и свои потолки длины. Kling 2.5 Turbo на Higgsfield
живёт **только** image-to-video, поэтому текстовый путь по умолчанию
идёт на Kling 3.0 Standard: 3–15 секунд, вертикаль 9:16, звук
выключается полем.

**Звук у клипа выключен нарочно.** В сплите говорит голова, и вторая
дорожка под ней — это не атмосфера, а каша; Kling по умолчанию отдаёт
озвученный кадр.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from config import cfg

log = logging.getLogger("higgsfield")

API = "https://api.higgsfield.ai"

# Рендер идёт минутами, спрашивать чаще нечего. Потолок — та же граница,
# что у HeyGen: дольше это не медленно, а не собралось.
POLL_EVERY = 6
POLL_CAP = 900

# Исходы, на которых ждать больше нечего. `nsfw` и `failed` Higgsfield
# возвращает с возвратом кредитов — значит это отказ, а не поломка сети,
# и повторять его тем же промптом бессмысленно.
DONE = "completed"
DEAD = ("failed", "nsfw", "canceled", "cancelled")

PREFIX = "hf-"
CREDITS = "higgsfield-credits.md"
CREDITS_HEAD = """# Клипы панели: что чем сделано

Заполняет код, когда генерация отдала файл. Таблица нужна для того же,
что `stock-credits.md` и `gen-credits.md`: через полгода отличить свою
съёмку от машинной, не открывая файл.

| Файл | Модель | Секунды | Запрос | Когда |
|---|---|---|---|---|
"""


class NoClip(RuntimeError):
    """Higgsfield не отдал клип."""


@dataclass(frozen=True)
class Model:
    """Одна модель Higgsfield: адрес, род входа и потолки длины."""

    path: str
    kind: str                       # "text" или "image"
    lo: int
    hi: int
    steps: tuple[int, ...] = ()     # закрытый список длин, если он есть
    extra: dict[str, object] = field(default_factory=dict)
    ratio: str = "aspect_ratio"     # имя поля пропорции, пусто — его нет


MODELS: dict[str, Model] = {
    # Текстовый путь. Дом по умолчанию: длина идёт целыми секундами, и
    # под карточку панели её можно подогнать, а не округлить до пятёрки.
    "kling-3": Model("kling-video/v3.0/std/text-to-video", "text", 3, 15,
                     extra={"sound": "off"}),
    "kling-3-turbo": Model("kling-video/v3.0/turbo/text-to-video", "text",
                           3, 15, extra={"sound": "off"}),
    "wan-2.7": Model("wan/v2.7/text-to-video", "text", 2, 15,
                     extra={"resolution": "720p"}),
    # Image-to-video. Длина закрытым списком: пять или десять.
    "kling-2.5-turbo": Model(
        "kling-video/v2.5-turbo/standard/image-to-video", "image", 5, 10,
        steps=(5, 10), ratio=""),
    "wan-2.7-image": Model("wan/v2.7/image-to-video", "image", 2, 15,
                           extra={"resolution": "720p"}, ratio=""),
}

DEFAULT = "kling-3"

# Что в кадре панели лишнее всегда, какой бы ни был запрос. Текст в
# первую голову: сгенерированные буквы приезжают кривыми, а свои слова
# панель кладёт сверху сама.
NEGATIVE = ("text, words, letters, captions, subtitles, watermark, logo, "
            "distorted hands, morphing faces, low quality, blurry")


def creds() -> str:
    """Пара «id и секрет» одной строкой. Пусто — ключа нет."""
    both = cfg.higgsfield_creds.strip()
    if both:
        return both
    if cfg.higgsfield_key_id and cfg.higgsfield_key_secret:
        return f"{cfg.higgsfield_key_id}:{cfg.higgsfield_key_secret}"
    return ""


def model(name: str = "") -> Model:
    key = (name or cfg.higgsfield_model or DEFAULT).strip()
    if key not in MODELS:
        raise NoClip(f"модели «{key}» в таблице нет: "
                     + ", ".join(sorted(MODELS)))
    return MODELS[key]


def seconds(m: Model, want: float) -> tuple[int, str]:
    """Длина клипа под длину блока. Вторым — строка, если не совпало.

    Короче блока клип встаёт последним кадром, длиннее — ускоряется;
    обе арифметики считает шаблон (`avatar_split.build`). Значит врать
    тут нечем, но и молчать нельзя: человек должен знать, что в карточке
    на 12 секунд пойдёт десятка.
    """
    need = max(1, int(want + 0.999))        # вверх: недобор заметнее
    if m.steps:
        fit = next((s for s in sorted(m.steps) if s >= need), max(m.steps))
    else:
        fit = min(max(need, m.lo), m.hi)
    if fit == need:
        return fit, ""
    return fit, (f"блок идёт {want:.1f} с, а модель даёт {fit} с — "
                 f"{'клип ускорится' if fit > need else 'хвост встанет кадром'}")


def body(m: Model, prompt: str, *, secs: int, image_url: str = "",
         negative: str = NEGATIVE, vertical: bool = True) -> dict:
    out: dict[str, object] = {"prompt": prompt[:2500], "duration": secs}
    if negative:
        out["negative_prompt"] = negative
    if m.ratio and vertical:
        # Панель вертикальная, и дорисовывать кадр до 9:16 кропом значит
        # терять то, за что заплачено.
        out[m.ratio] = "9:16"
    if m.kind == "image":
        if not image_url:
            raise NoClip(f"модель {m.path} идёт от кадра — нужен его "
                         "публичный адрес")
        out["image_url"] = image_url
    out.update(m.extra)
    return out


def _call(method: str, url: str, key: str, payload: dict | None = None,
          *, idem: str = "", timeout: int = 90) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Key {key}")
    req.add_header("accept", "application/json")
    if idem:
        req.add_header("Idempotency-Key", idem)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise NoClip(f"Higgsfield ответил {e.code}: {detail}") from e
    except OSError as e:
        raise NoClip(f"Higgsfield недостижим: {e}") from e
    try:
        got = json.loads(raw)
    except json.JSONDecodeError as e:
        raise NoClip(f"Higgsfield ответил не JSON: {raw[:200]}") from e
    if not isinstance(got, dict):
        raise NoClip(f"Higgsfield ответил не объектом: {raw[:200]}")
    return got


def idem_key(m: Model, payload: dict) -> str:
    """Отпечаток запроса. Тот же клип второй раз денег не стоит."""
    seed = json.dumps([m.path, payload], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def start(key: str, m: Model, payload: dict) -> str:
    """Поставить задачу. Возвращает адрес, по которому ждать."""
    got = _call("POST", f"{API}/{m.path.strip('/')}", key, payload,
                idem=idem_key(m, payload))
    where = str(got.get("status_url") or "")
    rid = str(got.get("request_id") or "")
    if not where and rid:
        where = f"{API}/requests/{rid}/status"
    if not where:
        raise NoClip(f"в ответе нет ни адреса ожидания, ни номера: "
                     f"{json.dumps(got, ensure_ascii=False)[:200]}")
    log.info("higgsfield: задача %s на %s", rid or "?", m.path)
    return where


def media_url(got: dict) -> str:
    """Адрес готового файла. Форма ответа у моделей расходится."""
    for node in (got.get("video"), got.get("raw"), got.get("output"),
                 got.get("result"), got.get("results")):
        if isinstance(node, dict):
            if node.get("url"):
                return str(node["url"])
            # Форма у моделей расходится ещё и глубиной: SDK достаёт
            # адрес из `jobs[0].results.raw.url`, а текстовые модели
            # кладут его сразу в `video.url`.
            inner = media_url(node)
            if inner:
                return inner
    for node in (got.get("videos"), got.get("results"), got.get("jobs")):
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    if item.get("url"):
                        return str(item["url"])
                    inner = media_url(item)
                    if inner:
                        return inner
    if isinstance(got.get("url"), str):
        return got["url"]
    return ""


def wait(key: str, where: str, *, cap: int = POLL_CAP,
         every: int = POLL_EVERY, sleep=time.sleep) -> str:
    """Дождаться файла. Возвращает его адрес."""
    spent = 0
    while spent <= cap:
        got = _call("GET", where, key)
        state = str(got.get("status") or "").lower()
        if state == DONE:
            url = media_url(got)
            if not url:
                raise NoClip("Higgsfield сказал «готово», а адреса файла "
                             "в ответе нет")
            return url
        if state in DEAD:
            why = str(got.get("error") or got.get("detail") or state)
            raise NoClip(f"генерация не состоялась: {why}")
        sleep(every)
        spent += every
    raise NoClip(f"Higgsfield не отдал клип за {cap} с — это не медленно, "
                 "а не собралось")


def fetch(url: str, out: Path, *, timeout: int = 300) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r, \
                out.open("wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
    except OSError as e:
        raise NoClip(f"клип не скачался: {e}") from e
    if out.stat().st_size < 10_000:
        raise NoClip(f"клип весит {out.stat().st_size} байт — это не видео")
    return out


def clip(prompt: str, out: Path, *, name: str = "", want: float = 5.0,
         image_url: str = "", key: str = "", vertical: bool = True,
         cap: int = POLL_CAP, sleep=time.sleep) -> tuple[Path, str]:
    """Один клип от запроса до файла. Вторым — строка «что не сошлось»."""
    key = key or creds()
    if not key:
        raise NoClip("ключа Higgsfield нет — в сеть не идём. Пара id и "
                     "секрета берётся в консоли higgsfield.ai, кладётся в "
                     "HIGGSFIELD_KEY_ID и HIGGSFIELD_KEY_SECRET")
    if not prompt.strip():
        raise NoClip("пустой запрос к генерации")
    m = model(name)
    secs, note = seconds(m, want)
    payload = body(m, prompt, secs=secs, image_url=image_url,
                   vertical=vertical)
    url = wait(key, start(key, m, payload), cap=cap, sleep=sleep)
    return fetch(url, out), note


def credit(folder: Path, out: Path, name: str, secs: int,
           prompt: str) -> None:
    """Строка в таблицу «что чем сделано». Пишется по факту файла."""
    table = folder / CREDITS
    if not table.exists():
        table.write_text(CREDITS_HEAD, encoding="utf-8")
    one = " ".join(prompt.split())[:160]
    with table.open("a", encoding="utf-8") as f:
        f.write(f"| {out.name} | {name} | {secs} | {one} | "
                f"{dt.date.today().isoformat()} |\n")
