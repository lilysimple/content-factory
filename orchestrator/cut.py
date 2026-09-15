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

from orchestrator import agent, desk, panelshot
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

PANEL_KINDS = ("card", "icon", "counter", "scale", "media", "art")
# Картинок по теме на ролик. Каждая это вызов Nano Banana за деньги и
# полминуты ожидания, а панель из одних картинок — слайд-шоу.
ART_MAX = 3
PANEL_WANT = 6               # на сорок секунд больше шести не читается
PANEL_TOKENS = 4000
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
                    "lines": {"type": "array", "items": {"type": "string"}},
                    "items": {"type": "array", "items": {"type": "string"}},
                    "value": {"type": "number"},
                    "value_phrase": {"type": "string"},
                    "full": {"type": "boolean"},
                    "url": {"type": "string"},
                    "media": {"type": "integer"},
                    "art": {"type": "string"},
                },
                "required": ["phrase", "kind", "kicker", "lines", "items",
                             "value", "value_phrase", "full", "url",
                             "media", "art"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["panel", "notes"],
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
        if kind == "scale" and len(items) < SCALE_MIN:
            lost.append(f"{name}: ступеней в шкале {len(items)}, "
                        f"нужно хотя бы {SCALE_MIN}")
            continue
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
            phrase=phrase, kind=kind,
            kicker=str(d.get("kicker") or "").strip(),
            lines=lines, items=items, value=value,
            value_phrase=str(d.get("value_phrase") or "").strip(),
            full=full, url=url,
            media=media if kind == "media" else 0,
            art=brief if kind == "art" else ""))
    return good, lost


async def panel(chat_id: int, words: list[Any], *,
                want: int = PANEL_WANT, ask: str = "",
                materials: list[str] | None = None,
                art: bool = False) -> tuple[list[Slide], list[str]]:
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
    return good[:want], lost
