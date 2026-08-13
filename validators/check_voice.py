"""Детерминированная проверка текста перед выдачей.

Граница проведена честно: скриптом ловится только то, что описывается
шаблоном. Драматизация, симметричные фразы и абзацы-бантики регуляркой не
ловятся и остаются самопроверкой Редактора внутри промпта.

Правило: скрипт даёт ОТКАЗ, модель даёт ПРЕДУПРЕЖДЕНИЕ.
Отказ блокирует выдачу, предупреждение уходит в лог рядом с текстом.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass

# ── анти-AI: дефолт продукта, не свойство бренда ──────────────────────

EM_DASH = re.compile(r"[—―]")

# «не X, а Y» и родня. Требуем запятую перед «а», иначе ловим «не так, а...»
# в нормальных оборотах вроде «не знаю, а надо бы».
CONTRAST = [
    (re.compile(r"\bне\s+[^.!?,]{2,40},\s+а\s+", re.I), "не X, а Y"),
    (re.compile(r"\bдело\s+не\s+в\b[^.!?]{0,60}\bдело\s+в\b", re.I),
     "дело не в X, дело в Y"),
    (re.compile(r"\bне\s+вместо\b[^.!?]{0,20}\bа\s+рядом\b", re.I),
     "не вместо, а рядом"),
    (re.compile(r"\bда,\s+но\b", re.I), "да, но"),
]

AMPLIFIERS = [
    "поистине", "невероятно", "критически важно", "в корне меняет",
    "кардинально", "революционн", "прорыв", "меняет всё", "изменит всё",
    "must have", "маст хэв",
]

# Капслок длиннее трёх букв. Аббревиатуры вроде AI, HR, B2B не трогаем.
CAPS = re.compile(r"\b[А-ЯЁA-Z]{4,}\b")
CAPS_OK = {"CLAUDE", "CHATGPT", "OPENAI", "TELEGRAM", "YOUTUBE", "LINKEDIN",
           "INSTAGRAM", "REELS", "SHORTS", "SEO", "CRM", "PDF", "HTML"}


@dataclass
class Finding:
    kind: str          # anti-ai | brand | format
    rule: str
    fragment: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.rule}: «{self.fragment}»"


def _cut(text: str, start: int, end: int, pad: int = 24) -> str:
    return text[max(0, start - pad):min(len(text), end + pad)].replace("\n", " ").strip()


def check(text: str, *, stopwords: list[str] | None = None,
          allow_em_dash: bool = False) -> list[Finding]:
    """Прогнать текст. Пустой список означает, что отказа нет."""
    out: list[Finding] = []

    if not allow_em_dash:
        for m in EM_DASH.finditer(text):
            out.append(Finding("anti-ai", "длинное тире",
                               _cut(text, m.start(), m.end())))

    for rx, name in CONTRAST:
        for m in rx.finditer(text):
            out.append(Finding("anti-ai", name, _cut(text, m.start(), m.end(), 0)))

    low = text.lower()
    for word in AMPLIFIERS:
        idx = low.find(word)
        if idx >= 0:
            out.append(Finding("anti-ai", "усилитель-пустышка",
                               _cut(text, idx, idx + len(word))))

    for m in CAPS.finditer(text):
        if m.group().upper() not in CAPS_OK:
            out.append(Finding("anti-ai", "капслок", m.group()))

    for word in stopwords or []:
        idx = low.find(word.lower())
        if idx >= 0:
            out.append(Finding("brand", "стоп-слово бренда",
                               _cut(text, idx, idx + len(word))))

    return out


def main() -> int:
    text = sys.stdin.read()
    findings = check(text)
    if not findings:
        print("✓ отказов нет")
        return 0
    print(f"✗ {len(findings)} отказ(ов):")
    for f in findings:
        print(" ", f)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
