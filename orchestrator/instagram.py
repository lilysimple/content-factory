"""Профили Instagram: кэш наполняет Chrome, завод читает кэш.

Instagram публичного доступа не даёт: страница отдаётся пустой оболочкой,
контент подгружает скрипт, а `api/v1` без залогиненной сессии отвечает
пустотой или капчей. Поэтому сеть здесь развязана с чтением:

    tools/instagram_pull.py   → research/instagram/<ник>.json   → этот модуль

Сборщик гоняет **твой** Chrome с отдельным профилем, в котором ты один раз
вошла руками, и складывает посты в кэш папки бренда. Завод в сеть за
Instagram не ходит вовсе: он читает JSON и отдаёт его тем же кодом, что и
ленту Telegram, — `sources.Source` с датированными постами, окно недели,
`research.measure`.

Отсюда главное свойство: **кэш стареет молча**. Файл недельной давности
выглядит точно так же, как снятый утром, и разницу видно только по полю
`pulled`. Поэтому возраст проверяется здесь и называется дырой, а не
подразумевается свежим.

Метрика у профиля не просмотры, а лайки: сетка отдаёт лайки и
комментарии у всего, просмотры — только у видео. Медиана считается по
лайкам, и подписана она тоже лайками. Складывать одно с другим нельзя.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

from orchestrator import sources

log = logging.getLogger("instagram")

CACHE = "research/instagram"

# Сколько дней кэшу можно быть, чтобы покрывать прошлую неделю. Сводка
# гоняется в понедельник по окну «позапрошлый понедельник — воскресенье»,
# так что снятое в субботу окно уже не закрывает. Восемь дней это запас
# на «собрала в пятницу, сводку сделала в понедельник».
STALE_DAYS = 8

HANDLE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def handle(url: str) -> str:
    """`https://instagram.com/lily/reels?x=1` → `lily`. Не профиль — пусто.

    Адреса в `sources.md` человек пишет как придётся: с `www`, с хвостом
    `/reels`, со слэшем и без. Ник это первый сегмент пути, и только он.
    """
    u = url.strip().rstrip("/")
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = re.sub(r"^www\.", "", u, flags=re.I)
    if u.lower().startswith("instagram.com"):
        u = u[len("instagram.com"):].lstrip("/")
    u = u.split("?")[0].split("#")[0].split("/")[0]
    return u if HANDLE.match(u) else ""


def is_profile(url: str) -> bool:
    """Строка из `sources.md` про Instagram? Голое `@имя` — нет, это Telegram."""
    kind, _ = sources.classify(url)
    return kind == "instagram" and bool(handle(url))


def cache_path(b, name: str) -> Path:
    return b.path(f"{CACHE}/{name.lower()}.json")


def _when(raw: str | None) -> datetime | None:
    """Дата поста. Не разобралась — прочерк, а не сегодня.

    Та же причина, что у фидов: дата «на всякий случай» затащит в окно
    недели прошлогодний пост, ради чего окно и заведено.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _int(value) -> int | None:
    """Число или прочерк. Ноль означает «реально ноль», а не «не отдали»."""
    if value is None or value is True or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read(b, name: str) -> tuple[sources.Source, str]:
    """Кэш профиля в `Source`. Второе значение — дыра, пустая строка если нет.

    Формы две — файла нет и файл битый, — и обе это дыра, а не отказ:
    сводка на части источников честнее молчания.
    """
    path = cache_path(b, name)
    url = f"https://www.instagram.com/{name}/"
    src = sources.Source(url=url, kind="instagram")

    if not path.exists():
        src.error = "кэша нет"
        return src, (f"@{name}: кэша нет — собери "
                     f"`tools/instagram_pull.py --profile {name}`")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        src.error = f"кэш не читается: {type(e).__name__}"
        return src, f"@{name}: кэш не читается ({type(e).__name__}), пересними"

    src.title = str(data.get("title") or f"@{name}")
    src.description = str(data.get("bio") or "")
    if subs := _int(data.get("followers")):
        src.subscribers = f"{subs} подписчиков"

    for row in data.get("posts") or []:
        text = str(row.get("text") or "").strip()
        if not text:
            # Кадр без подписи. В Telegram такие тоже пропускаются: без
            # текста читать в них нечего, а в медиану они внесли бы
            # пустую строку с числом.
            continue
        src.posts.append(sources.Post(
            text=text,
            views=_int(row.get("views")),
            date=_when(row.get("date")),
            likes=_int(row.get("likes")),
            comments=_int(row.get("comments")),
        ))

    src.ok = bool(src.posts)
    if not src.ok:
        src.error = "в кэше нет ни одного поста с подписью"
        return src, f"@{name}: в кэше нет постов с подписью — пересними"

    return src, _aged(name, data.get("pulled"))


def _aged(name: str, pulled: str | None, today: date | None = None) -> str:
    """Кэш старше `STALE_DAYS` — дыра строкой. Возраст не виден на глаз."""
    when = _when(pulled)
    if when is None:
        return f"@{name}: в кэше нет даты сбора — считать его свежим нельзя"
    age = ((today or date.today()) - when.date()).days
    if age > STALE_DAYS:
        return (f"@{name}: кэш снят {when:%d.%m}, {age} дней назад — "
                "свежие посты в срез не попали")
    return ""


def collect(b, urls: list[str]) -> tuple[list[sources.Source], list[str]]:
    """Профили из списка источников — в `Source`, дыры — строками.

    Список тот же `research/sources.md`: строка со ссылкой на профиль
    Instagram, а не отдельный файл. Второго списка «за чем следим» в
    проекте быть не должно — он разъедется с первым.
    """
    out, gaps = [], []
    for url in urls:
        if not is_profile(url):
            continue
        name = handle(url)
        src, gap = read(b, name)
        if gap:
            gaps.append(gap)
        if src.ok:
            out.append(src)
    return out, gaps


def stash(b, data: dict) -> Path:
    """Записать снятое сборщиком. Проверка формы здесь, а не в сборщике.

    Сборщик ходит в чужую вёрстку и меняется чаще всех; читатель у кэша
    один — этот модуль. Поэтому и форму держит он.
    """
    name = handle(str(data.get("profile") or ""))
    if not name:
        raise ValueError(f"ник не разобрался: {data.get('profile')!r}")
    posts = data.get("posts") or []
    if not isinstance(posts, list):
        raise ValueError("posts должен быть списком")

    body = {
        "profile": name,
        "url": f"https://www.instagram.com/{name}/",
        "title": str(data.get("title") or f"@{name}"),
        "bio": str(data.get("bio") or ""),
        "followers": _int(data.get("followers")),
        "pulled": (data.get("pulled")
                   or datetime.now().astimezone().isoformat(timespec="seconds")),
        "posts": [{
            "code": str(p.get("code") or ""),
            "text": str(p.get("text") or ""),
            "date": p.get("date"),
            "likes": _int(p.get("likes")),
            "comments": _int(p.get("comments")),
            "views": _int(p.get("views")),
            "video": bool(p.get("video")),
        } for p in posts],
    }
    path = cache_path(b, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    log.info("кэш профиля @%s: %s постов", name, len(body["posts"]))
    return path
