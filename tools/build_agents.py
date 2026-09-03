#!/usr/bin/env python3
"""Сборка адаптеров субагентов из промптов ролей.

Зачем. Определение субагента (`.claude/agents/{name}.md`) приезжает в его
системный промпт **бесплатно**, без вызова инструмента. Всё, что лежит
снаружи, роль открывает сама — и каждое чтение это отдельный круг к
модели, оплачивающий весь накопленный контекст заново.

Стратег читал `roles/frame.md` и `roles/strategy.md`: тринадцать тысяч
знаков и два круга на каждом прогоне, ради текста, который не меняется
между задачами. Вклеенный в определение, он стоит ноль.

Дом у поведения остаётся один. Источник правды — `roles/*.md` и дельта в
`roles/adapters/{name}.md`; `.claude/agents/{name}.md` это **производное**,
как `research/profile-digest.md` производное от профиля. Править руками
нельзя: правку затрёт следующая сборка.

Границу держит не эта просьба, а стенд: цикл 20 пересобирает адаптеры в
памяти и сверяет с диском. Разошлось — проверка падает и называет команду.

Собрать:

    ./.venv/bin/python tools/build_agents.py

Проверить, ничего не записывая:

    ./.venv/bin/python tools/build_agents.py --check
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Разбор шапки роли и подстановка живут в `agent`: их читает и путь бота,
# и этот сборщик. Две копии разошлись бы молча — субагент получал бы
# заполненный каркас, а бот литеральные скобки, ровно как до 30.08.
from orchestrator.agent import fill, header, leftovers   # noqa: E402

ROLES = ROOT / "roles"
ADAPTERS = ROLES / "adapters"
OUT = ROOT / ".claude" / "agents"

# Кто собирается и из какого промпта роли. Роли не в списке остаются
# рукописными: перевод каждой это правка её дельты, а не работа генератора.
BUILT = {
    "researcher": "research.md",
    "strategist": "strategy.md",
}

BANNER = (
    "<!-- СОБРАНО КОДОМ, НЕ ПРАВИТЬ РУКАМИ.\n"
    "     Источник: roles/adapters/{name}.md + roles/frame*.md + roles/{role}\n"
    "     Пересобрать: ./.venv/bin/python tools/build_agents.py\n"
    "     Правка здесь исчезнет при следующей сборке. Правь источник. -->"
)


def split_front(text: str) -> tuple[str, str]:
    """Отделить frontmatter от тела. Без него адаптер не адаптер."""
    if not text.startswith("---\n"):
        raise SystemExit("в дельте нет frontmatter")
    end = text.index("\n---\n", 3)
    return text[: end + 5].rstrip("\n"), text[end + 5:].lstrip("\n")


# Подстановки, которых на этом пути нет в шапке роли. `brand_name` и слои
# приходят из задачи: на пути бота их подставляет `agent.BOT_FILLS`, здесь
# их адрес называет контракт.
TASK_FILLS = {
    "brand_name": "клиента, названного в `input.md`",
    "layers": ("Слои приходят разделами `input.md` и артефактами задачи в "
               "`tasks/{task-id}/`. Что где лежит, сказано в дельте выше."),
}


def filled(text: str, values: dict[str, str]) -> str:
    """Подставить и убедиться, что не осталось дырок.

    Сборщик строгий, в отличие от пути бота: адаптер пишется на диск и
    живёт до следующей сборки, поэтому пустая подстановка тут не «видна
    в логе», а вклеена в промпт субагента насовсем.
    """
    out = fill(text, values)
    if left := leftovers(out):
        raise SystemExit("нечем заполнить подстановки: " + ", ".join(left))
    return out


def body(path: Path) -> str:
    """Тело файла роли без служебного комментария версии и без своего h1.

    Свой `#` снимается потому, что в собранном файле заголовок первого
    уровня уже есть — имя роли. Второй ломает структуру, и модель читает
    каркас как отдельный документ, а не как часть своего промпта.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for ln in lines:
        if ln.startswith("<!--") and "version:" in ln:
            continue
        if ln.startswith("# "):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def build(name: str, role_file: str) -> str:
    front, delta = split_front((ADAPTERS / f"{name}.md").read_text("utf-8"))
    role_text = (ROLES / role_file).read_text(encoding="utf-8")
    values = {**TASK_FILLS, **header(role_text)}
    return "\n\n".join([
        front,
        BANNER.format(name=name, role=role_file),
        delta.rstrip(),
        "---",
        "# Общий каркас",
        filled(body(ROLES / "frame.md"), values),
        filled(body(ROLES / "frame-text.md"), values),
        "---",
        f"# Роль целиком (`roles/{role_file}`)",
        body(ROLES / role_file),
    ]) + "\n"


def main(check: bool = False) -> int:
    bad = []
    for name, role_file in sorted(BUILT.items()):
        want = build(name, role_file)
        path = OUT / f"{name}.md"
        have = path.read_text(encoding="utf-8") if path.exists() else ""
        if want == have:
            print(f"  = {path.relative_to(ROOT)} — совпадает")
            continue
        if check:
            bad.append(name)
            print(f"  ✗ {path.relative_to(ROOT)} — разошёлся с источником")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(want, encoding="utf-8")
        print(f"  + {path.relative_to(ROOT)} — {len(want)} знаков")

    if bad:
        print("\nПересобрать: ./.venv/bin/python tools/build_agents.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(check="--check" in sys.argv))
