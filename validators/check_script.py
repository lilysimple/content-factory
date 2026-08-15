"""Детерминированная проверка суфлёрного текста.

Дополняет `check_voice.py`, а не заменяет его: тот ловит машинный текст и
стоп-слова бренда, этот — то, что отличает речь от письма. Правило то же:
скрипт даёт ОТКАЗ, модель даёт предупреждение.

Что здесь проверяется, проверяется потому, что на записи это слышно.
Цифра на экране суфлёра читается вслух по-разному («20» это «двадцать»
или «двадцатый»), строка длиннее сорока знаков не помещается в кадр
прокрутки, эмодзи произнести нельзя, а скобка с ремаркой уезжает в речь
целиком. Всё это описывается шаблоном, поэтому живёт здесь, а не в
самопроверке роли.
"""
from __future__ import annotations

import re
import sys

from validators.check_voice import Finding

# Бюджет слов от хронометража: спокойная речь идёт ~2 слова в секунду.
BUDGET: dict[int, tuple[int, int]] = {
    30: (60, 70),
    40: (80, 95),
    50: (100, 115),
}
DEFAULT_SECONDS = 40

# Границы бюджета широкие на десятую часть. Жёсткая граница на слово
# ловила бы округление, а не проблему: 79 слов вместо 80 звучат ровно
# так же, а 130 вместо 95 не помещаются в хронометраж вообще.
SLACK = 0.1

LINE_CHARS = 40              # строка суфлёра, до скольких знаков
LINE_WORDS = 7               # одна речевая фраза, сколько слов
HOOK_WORDS = 10              # хук должен уложиться в три секунды

WORD = re.compile(r"[^\W\d_]+", re.U)
DIGIT = re.compile(r"\d")
EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⬀-⯿]")
MARKUP = re.compile(r"<[a-zA-Z/][^>]*>|\*\*|__|`|^#{1,6}\s", re.M)
BRACKET = re.compile(r"\([^)]{2,}\)")
# «т.е.», «т.д.», «т.к.», «др.» — вслух это спотыкание.
SHORTHAND = re.compile(r"\b(?:т\.\s?[едкпн]\.|др\.|пр\.|см\.)", re.I)


def words(text: str) -> int:
    """Слов в тексте. Цифры словом не считаются: их тут быть не должно."""
    return len(WORD.findall(text))


def budget(seconds: int) -> tuple[int, int]:
    """Целевая вилка слов под хронометраж."""
    return BUDGET.get(seconds, BUDGET[DEFAULT_SECONDS])


def _bounds(seconds: int) -> tuple[int, int]:
    lo, hi = budget(seconds)
    return round(lo * (1 - SLACK)), round(hi * (1 + SLACK))


def check(script: str, *, seconds: int = DEFAULT_SECONDS,
          hook: str = "") -> list[Finding]:
    """Прогнать суфлёрный текст. Пустой список означает, что отказа нет."""
    out: list[Finding] = []

    for m in DIGIT.finditer(script):
        line = script[max(0, m.start() - 20):m.start() + 20].replace("\n", " ")
        out.append(Finding("speech", "цифра вместо слова", line.strip()))
        break                       # одной находки хватает, текст переписывается

    if m := EMOJI.search(script):
        out.append(Finding("speech", "эмодзи в суфлёре", m.group()))

    if m := MARKUP.search(script):
        out.append(Finding("speech", "разметка в суфлёре", m.group().strip()))

    if m := BRACKET.search(script):
        out.append(Finding("speech", "ремарка в скобках", m.group()))

    if m := SHORTHAND.search(script):
        out.append(Finding("speech", "сокращение вслух", m.group()))

    for raw in script.splitlines():
        line = raw.strip()
        if not line:
            continue
        if len(line) > LINE_CHARS:
            out.append(Finding("format", f"строка длиннее {LINE_CHARS} знаков",
                               line))
        elif words(line) > LINE_WORDS:
            out.append(Finding("format", f"в строке больше {LINE_WORDS} слов",
                               line))

    if hook.strip():
        n = words(hook)
        if n > HOOK_WORDS:
            out.append(Finding("format",
                               f"хук {n} слов, потолок {HOOK_WORDS}",
                               hook.strip().replace("\n", " ")))

    total = words(script)
    lo, hi = _bounds(seconds)
    if total < lo or total > hi:
        target = budget(seconds)
        out.append(Finding("format", "бюджет слов",
                           f"{total} слов на {seconds} секунд, "
                           f"нужно {target[0]}–{target[1]}"))

    # Длинных строк бывает много, и списком они забивают весь промпт
    # обратной связи. Правило важнее перечня попаданий.
    return _thin(out)


def _thin(findings: list[Finding]) -> list[Finding]:
    """Оставить по две находки на правило: третья ничего не добавляет."""
    seen: dict[str, int] = {}
    out = []
    for f in findings:
        seen[f.rule] = seen.get(f.rule, 0) + 1
        if seen[f.rule] <= 2:
            out.append(f)
    return out


def main() -> int:
    text = sys.stdin.read()
    findings = check(text)
    if not findings:
        print(f"✓ отказов нет, {words(text)} слов")
        return 0
    print(f"✗ {len(findings)} отказ(ов):")
    for f in findings:
        print(" ", f)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
