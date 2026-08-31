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
    "     Источник: roles/adapters/{name}.md + roles/frame.md + roles/{role}\n"
    "     Пересобрать: ./.venv/bin/python tools/build_agents.py\n"
    "     Правка здесь исчезнет при следующей сборке. Правь источник. -->"
)


def split_front(text: str) -> tuple[str, str]:
    """Отделить frontmatter от тела. Без него адаптер не адаптер."""
    if not text.startswith("---\n"):
        raise SystemExit("в дельте нет frontmatter")
    end = text.index("\n---\n", 3)
    return text[: end + 5].rstrip("\n"), text[end + 5:].lstrip("\n")


# Подстановки, которых на этом пути нет в задаче, а не в шапке роли.
# `brand_name` приходит в `input.md`, слои и секции профиля — тоже: на
# старом пути их подставлял бы вызывающий, здесь их адрес называет
# контракт.
TASK_FILLS = {
    "brand_name": "клиента, названного в `input.md`",
    "sections": "индекс профиля `research/profile-digest.md`",
    "layers": ("Слои приходят разделами `input.md` и артефактами задачи в "
               "`tasks/{task-id}/`. Что где лежит, сказано в дельте выше."),
}


def header(text: str) -> dict[str, str]:
    """Значения из шапки файла роли: `role_name:`, `upstream:` и так далее.

    Их никто не подставлял ни на одном пути: `frame.md` уезжает в модель
    дословно (`agent.py:100`, никакого `.format()`), поэтому раздел «Кто
    ты» всё это время сообщал роли буквально `{role_name}` и `{upstream}`.
    Значения лежали рядом, в шапке `roles/*.md`, и не были ни с чем
    связаны — поле без потребителя.
    """
    out = {}
    for ln in text.splitlines():
        if ln.startswith("#") or ln.startswith("<!--"):
            continue
        if ":" in ln and not ln.startswith(("-", " ", "|", "*")):
            key, _, val = ln.partition(":")
            key = key.strip()
            if key in ("role_name", "upstream", "downstream", "output",
                       "anti_scope"):
                out[key] = val.strip()
    return out


def fill(text: str, values: dict[str, str]) -> str:
    """Подставить, что известно. Неизвестное оставить видимым, а не пустым."""
    for key, val in values.items():
        text = text.replace("{" + key + "}", val)
    left = sorted(set(re.findall(r"\{([a-z_]+)\}", text)))
    if left:
        raise SystemExit("нечем заполнить подстановки: " + ", ".join(left))
    return text


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
        fill(body(ROLES / "frame.md"), values),
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
