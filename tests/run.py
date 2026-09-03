"""Прогнать циклы и посчитать исправность.

    ./.venv/bin/python tests/run.py          только офлайн, бесплатно
    ./.venv/bin/python tests/run.py --live   плюс живые вызовы модели

Офлайн-циклы подменяют модель заготовленным ответом и проверяют поведение
кода: ветки кнопок, запись в базу, границы. Они быстрые и ничего не стоят,
поэтому гоняются перед каждым коммитом.

Живые проверяют то, что подменой не поймать: что модель соблюдает формат,
что валидация не режет настоящий ответ, что кеш префикса читается. Они
стоят денег и минут, поэтому вызываются отдельно.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
PY = HERE.parent / ".venv" / "bin" / "python"

OFFLINE = [
    ("cycle01_strategy_plan",      "Стратег: жизненный цикл плана"),
    ("cycle03_strategy_edges",     "Стратег: кривые ответы и границы"),
    ("cycle05_strategy_platforms", "Стратег: площадки и слоты"),
    ("cycle07_editor_voice",       "Редактор: проверка голоса"),
    ("cycle09_designer",           "Дизайнер: ТЗ, рендер, проверки"),
    ("cycle11_publisher",          "Публикатор: комплект, дубли, пропуски"),
    ("cycle12_reels",              "Редактор Reels: суфлёр, речь, бюджет"),
    ("cycle14_profile",            "Профиль: правка словами, распаковка"),
    ("cycle15_research",           "Ресёрчер: своя статистика, правило трёх"),
    ("cycle18_bridge",             "Мост: окружение, задача, исходы"),
    ("cycle19_montage",            "Монтаж: паузы, караоке, обложка"),
    ("cycle20_index_harvest",      "Индекс, окно недели, посадка плана"),
    ("cycle21_cli",                "Вызов роли через CLI: флаги, ключ, исходы"),
]

LIVE = [
    ("cycle02_live_assistant",  "Ассистент, кеш, правка профиля"),
    ("cycle04_live_validation", "Валидация не режет настоящий план"),
    ("cycle06_live_platforms",  "Раскладка на три площадки"),
    ("cycle08_live_editor",     "Цепь план → текст"),
    ("cycle10_live_designer",   "Живой макет"),
    ("cycle13_live_reels",      "Живой сценарий ролика"),
    ("cycle16_live_research",   "Живая сводка недели"),
    # Модель тут не зовётся, но проход тяжёлый и чужими бинарниками:
    # whisper и Remotion — минуты работы, в офлайн-стенде им не место.
    ("cycle22_live_montage",    "Живой монтаж: whisper и Remotion"),
]

PASSED = re.compile(r"Все (\d+) проверок прошли")
FAILED = re.compile(r"ПРОВАЛЕНО (\d+) из (\d+)")


def run(name: str) -> tuple[int, int, list[str]]:
    """Вернуть (прошло, всего, список провалов)."""
    try:
        r = subprocess.run([str(PY), f"{name}.py"], cwd=HERE,
                           capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return 0, 1, [f"{name}: не уложился в 15 минут"]

    out = r.stdout + r.stderr
    if m := PASSED.search(out):
        n = int(m.group(1))
        return n, n, []
    if m := FAILED.search(out):
        bad, total = int(m.group(1)), int(m.group(2))
        fails = [l.strip(" ·") for l in out.splitlines()
                 if l.strip().startswith("·")]
        return total - bad, total, fails

    # Цикл не отработал вовсе: это тоже результат, и худший.
    tail = [l for l in out.strip().splitlines()[-4:] if l.strip()]
    return 0, 1, [f"{name} не отработал: " + " / ".join(tail)[:300]]


def main(live: bool) -> int:
    plan = OFFLINE + (LIVE if live else [])
    ok_all = total_all = 0
    broken: list[tuple[str, list[str]]] = []

    for name, title in plan:
        ok, total, fails = run(name)
        ok_all += ok
        total_all += total
        print(f"  {'✓' if not fails else '✗'} {ok:>3}/{total:<3} {title}")
        if fails:
            broken.append((name, fails))

    pct = ok_all * 100 / total_all if total_all else 0
    print(f"\n  исправность: {ok_all}/{total_all} = {pct:.1f}%")

    if broken:
        print("\n  что не сходится:")
        for name, fails in broken:
            for f in fails:
                print(f"    [{name}] {f}")

    return 0 if not broken else 1


if __name__ == "__main__":
    sys.exit(main(live="--live" in sys.argv))
