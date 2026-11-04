"""Монтажёр: из расшифровки — границы кусков.

Единственное место, где монтаж зовёт модель. Всё остальное на этом пути
детерминировано намеренно: `footage.py` считает паузы, активную зону и
транскрипт, `montage.py` режет и рендерит. Решить «где мысль
закончилась» им нечем, и промпт им для этого не выдаётся — они получают
готовый список кусков.

**Роль жила в промпте Редактора Reels до 03.09.** Секция «Отдельная
работа: нарезка длинной записи» занимала 1899 знаков из 8060 и читалась
сценаристом на каждую просьбу написать сценарий — работа не по адресу,
ровно как правила письма у Дизайнера. Число вызовов модели от переезда
не изменилось: он был один и остался один, сменился промпт.

Две работы роли отличаются числом кусков, а не формой ответа. Длинная
запись даёт до пяти кусков, короткий дубль ровно один — там модель не
выбирает между кусками, а обрезает края: «так, сейчас, поехали» в начале
и «ну вот как-то так» в конце.

Границы всё равно проверяет код (`_fit`). Промпт просит непересекающиеся
куски внутри записи, а в ответе приходило и то, что длиннее записи, и
куски внахлёст. Это ровно тот случай, про который написано в CLAUDE.md:
промпт это просьба.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator import agent, desk, frames, panelshot
# Разбор пункта живёт в монтаже: он же считает их секунды. Импорт
# отложенный — монтаж сам читает `cut`, и прямой импорт замкнул бы круг.


def montage_point(text: str) -> dict:
    from orchestrator.montage import point
    return point(text)
from orchestrator.desk import NoWork

log = logging.getLogger("cut")

# Профиль нужен, чтобы отличить сильный кусок от проходного: кому это
# говорится и о чём бренд вообще. Голос сюда не входит — Монтажёр не
# пишет, он выбирает уже сказанное.
SECTIONS = ("Кто это", "Аудитория", "Формат")

FRAG_MIN, FRAG_MAX = 20.0, 60.0
FRAG_WANT = 5                # больше пяти на одну запись человек не смотрит
FRAG_TOKENS = 4000

# Пол короткого дубля свой и низкий. Вилка 20–60 это про выбор куска из
# длинной записи; дубль на восемнадцать секунд человек снял целиком, и
# отказать ему в монтаже из-за границы, придуманной для нарезки, значит
# сломать работающий путь ради аккуратности.
WHOLE_MIN = 5.0

# Нарезка отвечает другой формой, чем сценарий, — своя схема. Числа тут
# именно number: время приходит дробным. Разбор в `_fit` остаётся строгим
# и без схемы: он ловит и «0:20» строкой, и куски за границей записи.
FRAG_SCHEMA = {
    "type": "object",
    "properties": {
        "fragments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "hook": {"type": "string"},
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["start", "end", "hook", "title", "why"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["fragments", "notes"],
    "additionalProperties": False,
}
TRANSCRIPT_LINE = 12         # слов в строке расшифровки


@dataclass
class Fragment:
    start: float
    end: float
    hook: str
    title: str
    why: str = ""

    @property
    def seconds(self) -> float:
        return self.end - self.start


def transcript(words: list[Any], line: int = TRANSCRIPT_LINE) -> str:
    """Расшифровка строками с меткой времени — вход для выбора кусков.

    Метка нужна на каждой строке: без неё модель называет границы «на
    третьей минуте», а код не умеет резать по прозе.
    """
    out: list[str] = []
    for i in range(0, len(words), line):
        chunk = words[i:i + line]
        if not chunk:
            continue
        out.append(f"[{chunk[0].start:.0f}] "
                   + " ".join(w.text for w in chunk))
    return "\n".join(out)


def _fit(raw: list[dict[str, Any]], duration: float, *,
         lo: float = FRAG_MIN,
         hi: float | None = FRAG_MAX) -> tuple[list[Fragment], list[str]]:
    """Оставить куски, которые можно смонтировать. Отброшенное — назвать."""
    good: list[Fragment] = []
    lost: list[str] = []

    def _at(d: dict[str, Any]) -> float:
        """Ключ сортировки, который не падает на «start»: «0:20».

        Сортировка идёт до разбора, и нечисловое время роняло всю
        нарезку целиком — вместо того, чтобы отбросить один кусок и
        назвать его человеку. Поймано стендом, цикл 12.
        """
        try:
            return float(d.get("start"))
        except (TypeError, ValueError):
            return float("inf")               # такие уедут в конец и отпадут

    items = sorted(raw, key=_at)
    for d in items:
        try:
            start = float(d.get("start"))
            end = float(d.get("end"))
        except (TypeError, ValueError):
            lost.append(f"«{str(d.get('hook') or '?')[:40]}»: время не число")
            continue

        hook = str(d.get("hook") or "").strip()
        title = str(d.get("title") or "").strip() or hook
        name = f"«{(hook or title or '?')[:40]}»"

        if not hook:
            lost.append(f"кусок {start:.0f}–{end:.0f} с: без хука")
            continue
        if start < 0 or end > duration + 0.5:
            lost.append(f"{name}: {start:.0f}–{end:.0f} с не помещается "
                        f"в запись ({duration:.0f} с)")
            continue
        if end - start < lo:
            lost.append(f"{name}: {end - start:.0f} с — короче {lo:.0f}")
            continue
        if hi is not None and end - start > hi:
            lost.append(f"{name}: {end - start:.0f} с — длиннее {hi:.0f}")
            continue
        if good and start < good[-1].end:
            lost.append(f"{name}: наезжает на предыдущий кусок")
            continue

        good.append(Fragment(start, min(end, duration), hook, title,
                             str(d.get("why") or "").strip()))
    return good, lost


async def fragments(chat_id: int, words: list[Any], duration: float, *,
                    want: int = FRAG_WANT,
                    whole: bool = False,
                    ask: str = "") -> tuple[list[Fragment], list[str]]:
    """Выбрать куски на рилсы из расшифровки.

    `whole` — короткий дубль, снятый под один ролик: кусок ровно один и
    верхней границы у него нет. Отличаются задание и вилка, промпт роли
    и схема ответа те же.
    """
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиль бренда ещё не собран")

    if whole:
        task = (
            "Это короткий дубль, снятый под один ролик, а не длинная "
            f"запись. Он идёт {duration:.0f} секунд. Кусок ровно один: "
            "скажи, с какой секунды он начинается и на какой кончается. "
            "Правила — в твоей секции «Короткий дубль». Сомневаешься, "
            "резать ли, — не режь.")
        want, lo, hi = 1, WHOLE_MIN, None
    else:
        task = (
            "Это нарезка длинной записи, а не написание сценария. Запись "
            f"идёт {duration:.0f} секунд. Выбери до {want} кусков, каждый "
            "из которых работает отдельным роликом. Правила — в твоей "
            "секции «Длинная запись».")
        lo, hi = FRAG_MIN, FRAG_MAX

    prompt = ("## Задача\n\n" + task
              + "\n\nВремя фрагментов в секундах от начала записи.\n\n"
              "## Расшифровка\n\n" + transcript(words))
    if (ask or "").strip():
        prompt += f"\n\n## Что сказал человек\n\n{ask.strip()}"
    prompt += "\n\nОтветь одним JSON-объектом в формате из твоей секции."

    answer = await agent.ask("cut", chat_id, prompt, brand_name=b.name(),
                             profile=desk.profile(b, SECTIONS),
                             max_tokens=FRAG_TOKENS, schema=FRAG_SCHEMA)
    data = agent.parse_json(answer, who="монтажёр")

    good, lost = _fit(list(data.get("fragments") or []), duration,
                      lo=lo, hi=hi)
    lost += [str(n) for n in (data.get("notes") or []) if str(n).strip()]
    log.info("%s: взято %s кусков, отброшено %s",
             "дубль" if whole else "нарезка", len(good), len(lost))
    return good[:want], lost


# ── панель сплита ─────────────────────────────────────────────────────
#
# Вторая работа той же роли: по той же расшифровке расставить блоки
# панели. Отдельным вызовом, а не полем в ответе про границы: панель
# просят редко, а секция промпта и схема ответа приехали бы в контекст
# на каждый обычный монтаж.
#
# **Блок стоит на слове, а не на секунде.** Модель называет фразу, код
# ищет её в расшифровке (`footage.anchor`) и считает секунду сам. Так же
# сделаны пункты списка внутри блока, и по той же причине: секунды
# готового ролика — это секунды после выброшенных пауз, модель их не
# видела и посчитать не может. Просить у неё арифметику, которую она не
# в состоянии проверить, значит получить её выдуманной.

PANEL_KINDS = ("card", "icon", "counter", "scale", "media", "art",
               # Инфографика: поток со стрелками, плитки, окно
               # терминала, плашки-слова и фраза с подсветкой слова.
               # Все пять живут пунктами, и каждый пункт приходит на
               # своём слове (`montage.point`).
               "flow", "grid", "terminal", "chips", "mark", "bars")
# Сорта, у которых пункты — вся карточка: без них показывать нечего.
POINT_KINDS = ("flow", "grid", "terminal", "chips", "bars")
POINTS_MIN = 2
# Картинок по теме на ролик. Каждая это вызов Nano Banana за деньги и
# полминуты ожидания (три идут разом), поэтому потолок есть, но высокий:
# около одной на шесть секунд ролика. Было три — решение человека 16.09.
ART_MAX = 10
# Блок каждые пять-семь секунд: на минуту ролика это десять-двенадцать.
# Шесть блоков оставляли панель пустой по полминуты — решение человека
# 16.09, после живого сплита: картинок на рилс нужно около десяти.
PANEL_WANT = 14
# Четырнадцать блоков с обложкой и мышлением не влезали бы в 4000: на
# Opus 5 `max_tokens` считает мышление вместе с ответом, и JSON обрывался бы.
PANEL_TOKENS = 8000
SCALE_MIN = 2                # шкала с одной ступенью это не шкала

PANEL_SCHEMA = {
    "type": "object",
    "properties": {
        "panel": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string"},
                    "kind": {"type": "string", "enum": list(PANEL_KINDS)},
                    "kicker": {"type": "string"},
                    "icon": {"type": "string"},
                    "lines": {"type": "array", "items": {"type": "string"}},
                    "items": {"type": "array", "items": {"type": "string"}},
                    "value": {"type": "number"},
                    "value_phrase": {"type": "string"},
                    "full": {"type": "boolean"},
                    "url": {"type": "string"},
                    "media": {"type": "integer"},
                    "mark": {"type": "string"},
                    "art": {"type": "string"},
                },
                "required": ["phrase", "kind", "kicker", "lines", "items",
                             "value", "value_phrase", "full", "url",
                             "media", "mark", "art"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
        # Обложка сплита: заголовок на белых плашках, слова цветом и
        # продукт, чей знак стоит сверху. Пишет роль, потому что это тот
        # же выбор, что и панель, — что в записи главное.
        "cover": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "accent": {"type": "array", "items": {"type": "string"}},
                "url": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["title", "accent", "url", "name"],
            "additionalProperties": False,
        },
    },
    "required": ["panel", "notes", "cover"],
    "additionalProperties": False,
}


@dataclass
class Slide:
    """Блок панели до привязки ко времени.

    Секунд здесь нет вовсе, и это главное отличие от `montage.Block`:
    `Slide` говорит «на этой фразе», `Block` — «с этой секунды готового
    ролика». Перевод делает монтаж, когда расшифровка уже нарезана.
    """
    phrase: str
    kind: str = "card"
    # Значок сцены: имя из набора `frames.ICONS`, проверено при разборе.
    icon: str = ""
    kicker: str = ""
    lines: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    value: int | None = None
    value_phrase: str = ""
    full: bool = False
    # Сайт продукта, который прозвучал. Для `icon` код берёт с него знак,
    # для `card` — скрин страницы (`panelshot`). Пусто — блок словами.
    url: str = ""
    # Что сняли по `url`. Ставит монтаж, а не роль: живёт на слайде, а не
    # на блоке, потому что блоки пересчитываются от слайдов на каждой
    # пересборке, и картинка не должна теряться вместе с секундами.
    image: Path | None = None
    # `media`: номер материала из альбома, с единицы. Во что он
    # превратился — картинка в `image` или запись в `video` — решает
    # монтаж по файлу, а не роль.
    media: int = 0
    # `mark`: слово или строка, которую видно на этом материале и о
    # которой идёт речь. Есть — монтаж снимает с записи кадр, где это
    # слово нашлось, наезжает на него и подчёркивает (`montage._mark`).
    # Нет — материал идёт записью, как шёл до 22.09.
    mark: str = ""
    # Где `mark` лежит на снятом кадре: доли кадра от левого верхнего
    # угла. Ставит монтаж после распознавания, а не роль: на глаз этого
    # не знает никто.
    spot: tuple[float, float, float, float] | None = None
    # Сам кадр, снятый с записи там, где слово видно. Лежит рядом с
    # записью, а не вместо неё: запись идёт первой половиной блока.
    shot: Path | None = None
    video: Path | None = None
    video_len: float = 0.0
    media_size: tuple[int, int] = (0, 0)
    # `art`: бриф картинки по теме для Nano Banana, по-английски.
    art: str = ""


def _slides(raw: list[dict[str, Any]], *, materials: int = 0,
            art: bool = False) -> tuple[list[Slide], list[str]]:
    """Оставить блоки, которые можно нарисовать. Отброшенное — назвать.

    Проверяется здесь только то, что видно без расшифровки: сорт, набор
    полей, непустая фраза. Существование фразы в записи проверяет
    монтаж — у него есть слова, а у роли их уже нет.
    """
    good: list[Slide] = []
    lost: list[str] = []
    full_taken = False
    arts = 0

    for d in raw:
        phrase = str(d.get("phrase") or "").strip()
        kind = str(d.get("kind") or "card").strip().lower() or "card"
        name = f"«{(phrase or str(d.get('kicker') or '?'))[:40]}»"

        if not phrase:
            lost.append(f"блок {kind}: без фразы, ставить не на что")
            continue
        if kind not in PANEL_KINDS:
            lost.append(f"{name}: сорт «{kind}» не бывает")
            continue

        lines = [str(s).strip() for s in (d.get("lines") or []) if str(s).strip()]
        items = [str(s).strip() for s in (d.get("items") or []) if str(s).strip()]

        try:
            value = int(float(d.get("value"))) if d.get("value") is not None \
                else None
        except (TypeError, ValueError):
            value = None

        if kind == "counter" and not value:
            lost.append(f"{name}: счётчик без числа")
            continue
        if kind in POINT_KINDS and len(items) < POINTS_MIN:
            lost.append(f"{name}: у сорта «{kind}» пунктов {len(items)}, "
                        f"нужно хотя бы {POINTS_MIN} — пустая карточка")
            continue
        if kind == "bars":
            # Столбик без числа рисовать нечем: высота берётся из него.
            flat = [it for it in items
                    if montage_point(it).get("value") is None]
            if flat:
                lost.append(f"{name}: у столбиков нет числа — "
                            + ", ".join(f"«{t[:24]}»" for t in flat[:3]))
                continue
        if kind == "mark" and not lines:
            lost.append(f"{name}: «mark» это фраза, которую подсвечивают, "
                        "а строк нет")
            continue
        if kind == "scale" and len(items) < SCALE_MIN:
            lost.append(f"{name}: ступеней в шкале {len(items)}, "
                        f"нужно хотя бы {SCALE_MIN}")
            continue
        mark = str(d.get("icon") or "").strip().lower()
        if mark and not frames.icon(mark):
            lost.append(f"{name}: значка «{mark}» в наборе нет — "
                        "карточка пойдёт без него")
            mark = ""
        if kind in ("card", "icon") and not lines and not items:
            lost.append(f"{name}: пустая карточка")
            continue

        try:
            media = int(d.get("media") or 0)
        except (TypeError, ValueError):
            media = 0
        brief = str(d.get("art") or "").strip()
        if kind == "media" and not 1 <= media <= materials:
            lost.append(f"{name}: материала №{media} в альбоме нет"
                        if materials else f"{name}: альбома к дублю нет")
            continue
        if kind == "art":
            if not art:
                lost.append(f"{name}: картинки по теме не будет — "
                            "нет ключа Nano Banana")
                brief = ""
            elif not brief:
                lost.append(f"{name}: картинка без брифа")
            elif arts >= ART_MAX:
                lost.append(f"{name}: картинок по теме больше {ART_MAX}")
                brief = ""
            if not brief:
                # Картинки не будет, но слова у блока есть — он остаётся
                # карточкой, а не пропадает с панели.
                if not lines and not items:
                    continue
                kind = "card"
            else:
                arts += 1

        # Второй блок на весь кадр это не разгон, а мигание: дубль
        # уходит и возвращается несколько раз за ролик.
        full = bool(d.get("full"))
        if full and full_taken:
            full = False
            lost.append(f"{name}: второй блок на весь кадр — показан "
                        "на своей половине")
        full_taken = full_taken or full

        # Адрес у счётчика и шкалы рисовать некуда, а негодный адрес
        # в сеть не пускаем: блок останется словами, и это названо.
        url = str(d.get("url") or "").strip()
        if url and kind not in ("card", "icon"):
            url = ""
        if url and panelshot.safe_url(url) is None:
            lost.append(f"{name}: адрес «{url[:40]}» не годится — "
                        "блок словами")
            url = ""

        good.append(Slide(
            phrase=phrase, kind=kind, icon=mark,
            kicker=str(d.get("kicker") or "").strip(),
            lines=lines, items=items, value=value,
            value_phrase=str(d.get("value_phrase") or "").strip(),
            full=full, url=url,
            media=media if kind == "media" else 0,
            mark=str(d.get("mark") or "").strip() if kind == "media" else "",
            art=brief if kind == "art" else ""))
    return good, lost


async def panel(chat_id: int, words: list[Any], *,
                want: int = PANEL_WANT, ask: str = "",
                materials: list[str] | None = None,
                art: bool = False
                ) -> tuple[list[Slide], list[str], dict[str, Any]]:
    """Блоки панели сплита по расшифровке."""
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиль бренда ещё не собран")

    prompt = (
        "## Задача\n\n"
        "Собери панель сплита по этой записи — правила в твоей секции "
        f"«Вторая работа: панель сплита». Блоков не больше {want}. "
        "Границы кусков сейчас не нужны: это отдельная работа, и её уже "
        "сделали.\n\n"
        "Каждый блок стоит на фразе из расшифровки, а не на секунде: "
        "секунды считает код.\n\n"
        "## Расшифровка\n\n" + transcript(words))
    materials = materials or []
    if materials:
        prompt += ("\n\n## Материалы из альбома\n\n"
                   "Прислал человек вместе с дублем, по порядку. Поставь "
                   "каждый на свою фразу блоком `media`.\n\n"
                   + "\n".join(f"{i}. {m}" for i, m in
                               enumerate(materials, 1)))
    else:
        prompt += "\n\n## Материалы из альбома\n\nНе прислали."
    prompt += ("\n\n## Картинки по теме\n\n"
               + (f"Доступны, не больше {ART_MAX} на ролик."
                  if art else "Недоступны: блок `art` не ставь."))
    if (ask or "").strip():
        prompt += f"\n\n## Что сказал человек\n\n{ask.strip()}"
    prompt += "\n\nОтветь одним JSON-объектом в формате из твоей секции."

    answer = await agent.ask("cut", chat_id, prompt, brand_name=b.name(),
                             profile=desk.profile(b, SECTIONS),
                             max_tokens=PANEL_TOKENS, schema=PANEL_SCHEMA)
    data = agent.parse_json(answer, who="монтажёр")

    good, lost = _slides(list(data.get("panel") or []),
                         materials=len(materials), art=art)
    lost += [str(n) for n in (data.get("notes") or []) if str(n).strip()]
    log.info("панель: взято %s блоков, отброшено %s", len(good), len(lost))
    cover = data.get("cover") if isinstance(data.get("cover"), dict) else {}
    return good[:want], lost, cover


# ── слои поверх дубля ─────────────────────────────────────────────────
#
# Третья работа той же роли и второй формат монтажа, рядом со сплитом.
# Отдельным вызовом по той же причине, что и панель: секция промпта со
# схемой ответа приезжала бы в контекст на каждый обычный монтаж.
#
# Разница со сплитом не в наборе украшений, а в том, делится ли кадр.
# Сплит отдаёт половину панели и держит зрителя второй дорожкой; слои
# кадра не делят — дубль идёт во весь экран, человек в кадре всё время,
# и поверх ложатся единицы вещей. Поэтому форматы взаимно исключены:
# заголовку во всю ширину в сплите просто некуда встать.
#
# Слой, как и блок панели, **стоит на слове, а не на секунде**, и
# кончается тоже на слове. Конец здесь свой у каждого слоя, а не «до
# следующего»: слои не сменяют друг друга, они живут поверх кадра и
# нормально пересекаются — заголовок наверху, плашка посередине.

LAYER_KINDS = ("title", "proof", "chrome")
ARROWS = ("none", "down", "left", "right")

# Слоёв на ролик единицы, а не десяток: каждый закрывает собой часть
# кадра, и второй поверх первого — уже мусор. Шесть это потолок, а не
# цель; на тридцатисекундном референсе их было четыре.
LAYERS_WANT = 6
LAYERS_TOKENS = 6000

# Заголовок во всю ширину кадра. Длиннее строки не помещается даже
# сжатым гротеском: она уедет кеглем вниз и перестанет быть плашкой.
TITLE_LINE = 18
TITLE_LINES = 3
# Рукописный акцент это слово или два, а не фраза: его пишут от руки
# поверх заголовка, и длинная строка там не читается вовсе.
SCRIPT_MAX = 28
# Плашка площадки: имя и подпись под ним, обе в одну строку без переноса.
CHROME_HEAD = 48
CHROME_NOTE = 64
# Строк подсветки на снимок. Больше — зритель не успевает прочесть, и
# снимок превращается в мигание маркером.
MARKS_MAX = 4
# Снимков страницы на ролик. Страница занимает кадр целиком, и вторая
# подряд превращает ролик в чтение вслух.
PROOFS_MAX = 1

LAYERS_SCHEMA = {
    "type": "object",
    "properties": {
        "layers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string"},
                    "until": {"type": "string"},
                    "kind": {"type": "string", "enum": list(LAYER_KINDS)},
                    "lines": {"type": "array", "items": {"type": "string"}},
                    "script": {"type": "string"},
                    "arrow": {"type": "string", "enum": list(ARROWS)},
                    "url": {"type": "string"},
                    "marks": {"type": "array", "items": {"type": "string"}},
                    "open_phrase": {"type": "string"},
                },
                "required": ["phrase", "until", "kind", "lines", "script",
                             "arrow", "url", "marks", "open_phrase"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["layers", "notes"],
    "additionalProperties": False,
}


@dataclass
class Overlay:
    """Слой до привязки ко времени.

    Как и `Slide`, секунд не знает: роль говорит «с этой фразы и до
    этой», секунды считает монтаж по нарезанной расшифровке.
    """
    phrase: str
    kind: str = "title"
    until: str = ""
    lines: list[str] = field(default_factory=list)
    script: str = ""
    arrow: str = "none"
    # `proof`: адрес страницы, которую снимаем, и строки на ней, которые
    # подсвечиваем маркером. Строки ищутся в снятой странице, а не в
    # расшифровке: человек говорит своими словами, страница — своими.
    url: str = ""
    marks: list[str] = field(default_factory=list)
    # Фраза, на которой снимок разворачивается во весь кадр. Пусто —
    # остаётся карточкой в углу.
    open_phrase: str = ""
    # Что сняли по `url`. Ставит монтаж после `panelshot.proof`: сам
    # снимок, его размер и места найденных строк в долях снимка.
    image: Path | None = None
    image_size: tuple[int, int] = (0, 0)
    spots: list[tuple[str, float, float, float, float]] = \
        field(default_factory=list)


def _layers(raw: list[dict[str, Any]]) -> tuple[list[Overlay], list[str]]:
    """Оставить слои, которые можно нарисовать. Отброшенное — назвать.

    Проверяется то же, что у панели: видимое без расшифровки. Фразу в
    записи и страницу по адресу проверяет монтаж — у него есть слова и
    есть сеть.
    """
    good: list[Overlay] = []
    lost: list[str] = []
    proofs = 0

    for d in raw:
        phrase = str(d.get("phrase") or "").strip()
        kind = str(d.get("kind") or "title").strip().lower() or "title"
        name = f"«{(phrase or '?')[:40]}»"

        if not phrase:
            lost.append(f"слой {kind}: без фразы, ставить не на что")
            continue
        if kind not in LAYER_KINDS:
            lost.append(f"{name}: слоя «{kind}» не бывает")
            continue

        lines = [str(s).strip() for s in (d.get("lines") or [])
                 if str(s).strip()]
        script = str(d.get("script") or "").strip()
        arrow = str(d.get("arrow") or "none").strip().lower()
        if arrow not in ARROWS:
            arrow = "none"

        if kind == "title":
            if not lines:
                lost.append(f"{name}: заголовок без строк")
                continue
            if len(lines) > TITLE_LINES:
                lost.append(f"{name}: строк в заголовке {len(lines)}, "
                            f"больше {TITLE_LINES} не влезает — "
                            "лишние отброшены")
                lines = lines[:TITLE_LINES]
            long = [s for s in lines if len(s) > TITLE_LINE]
            if long:
                lost.append(f"{name}: строка длиннее {TITLE_LINE} знаков — "
                            + ", ".join(f"«{s[:24]}»" for s in long[:2]))
                continue
            if len(script) > SCRIPT_MAX:
                lost.append(f"{name}: рукописный акцент длиннее "
                            f"{SCRIPT_MAX} знаков — слой пойдёт без него")
                script = ""
            # Стрелка без акцента показывает в пустоту: рисуется она от
            # рукописного слова, и без него ей неоткуда начаться.
            if arrow != "none" and not script:
                arrow = "none"
                lost.append(f"{name}: стрелка без рукописного акцента — "
                            "ей неоткуда начаться")

        if kind == "chrome":
            if not lines:
                lost.append(f"{name}: плашка без текста")
                continue
            head = lines[0]
            note = lines[1] if len(lines) > 1 else ""
            if len(head) > CHROME_HEAD or len(note) > CHROME_NOTE:
                lost.append(f"{name}: текст плашки не влезает в строку — "
                            f"имя до {CHROME_HEAD} знаков, подпись до "
                            f"{CHROME_NOTE}")
                continue
            lines = [head] + ([note] if note else [])

        url = str(d.get("url") or "").strip()
        marks = [str(s).strip() for s in (d.get("marks") or [])
                 if str(s).strip()]
        if kind == "proof":
            if not url:
                lost.append(f"{name}: снимок без адреса страницы")
                continue
            if panelshot.safe_url(url) is None:
                lost.append(f"{name}: адрес «{url[:40]}» не годится — "
                            "снимка не будет")
                continue
            # Потолок считается после адреса, а не до. Иначе негодный
            # адрес во втором снимке назывался бы человеку потолком, и
            # он пошёл бы убирать лишний слой вместо того, чтобы
            # поправить ссылку.
            if proofs >= PROOFS_MAX:
                lost.append(f"{name}: снимок страницы в ролике уже есть — "
                            "второй читать некогда")
                continue
            if not marks:
                lost.append(f"{name}: снимок без строк для подсветки — "
                            "страница поедет карточкой, без маркера")
            if len(marks) > MARKS_MAX:
                lost.append(f"{name}: строк подсветки {len(marks)}, "
                            f"больше {MARKS_MAX} не читается — "
                            "лишние отброшены")
                marks = marks[:MARKS_MAX]
            proofs += 1
        else:
            url, marks = "", []

        good.append(Overlay(
            phrase=phrase, kind=kind,
            until=str(d.get("until") or "").strip(),
            lines=lines, script=script if kind == "title" else "",
            arrow=arrow if kind == "title" else "none",
            url=url, marks=marks,
            open_phrase=(str(d.get("open_phrase") or "").strip()
                         if kind == "proof" else "")))
    return good, lost


async def layers(chat_id: int, words: list[Any], *,
                 want: int = LAYERS_WANT, ask: str = ""
                 ) -> tuple[list[Overlay], list[str]]:
    """Слои поверх дубля по расшифровке."""
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиль бренда ещё не собран")

    prompt = (
        "## Задача\n\n"
        "Собери слои поверх этого дубля — правила в твоей секции "
        f"«Третья работа: слои поверх дубля». Слоёв не больше {want}. "
        "Кадр не делится: дубль идёт во весь экран, панели здесь нет, и "
        "границы кусков сейчас не нужны — это отдельная работа.\n\n"
        "Каждый слой стоит на фразе из расшифровки и на ней же "
        "кончается: секунды считает код.\n\n"
        "## Расшифровка\n\n" + transcript(words))
    if (ask or "").strip():
        prompt += f"\n\n## Что сказал человек\n\n{ask.strip()}"
    prompt += "\n\nОтветь одним JSON-объектом в формате из твоей секции."

    answer = await agent.ask("cut", chat_id, prompt, brand_name=b.name(),
                             profile=desk.profile(b, SECTIONS),
                             max_tokens=LAYERS_TOKENS, schema=LAYERS_SCHEMA)
    data = agent.parse_json(answer, who="монтажёр")

    good, lost = _layers(list(data.get("layers") or []))
    lost += [str(n) for n in (data.get("notes") or []) if str(n).strip()]
    log.info("слои: взято %s, отброшено %s", len(good), len(lost))
    return good[:want], lost
