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
from dataclasses import dataclass
from typing import Any

from orchestrator import agent, desk
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
