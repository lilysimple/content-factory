"""Тема вне плана: завести по факту просьбы, а не по слоту в плане.

    ./.venv/bin/python tools/theme_adhoc.py 2026-09-03-design-01 \
        --plat instagram --format карусель \
        --title "Как перестать бояться AI" --hook "Страшно не вам одному"

Печатает один id темы и ничего больше: его подставляют в `theme_id`
контракта Редактора и Дизайнера.

Зачем отдельный вход. Слоты в плане ставит Стратег, и до этого завести
тему умел ровно один путь — монтаж, по факту снятого дубля. Просьба
«сделай пост по такой теме, её нет в плане» упиралась в посадку:
`editor.land` и `design.land` отказывают словами «темы нет в базе», и
готовый текст оставался в `tasks/`, который в `.gitignore`.

Почему командой, а не полем в контракте. Тему надо завести **до** работы
ролей, а не после: в одном прогоне по одной теме идут Редактор и
Дизайнер, и второму нужен тот же id, что и первому. Посадка идёт после
конца прогона — id, заведённый там, Дизайнеру уже не достался бы.

Почему id считает код, а не роль. Он же считает его плану
(`strategy.land`) и монтажу: id это ключ, по которому тема связана с
текстом, макетом и публикацией, и разъехавшийся счётчик стоит дороже
любой экономии на вызове.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import cfg                                            # noqa: E402
from orchestrator import desk                                     # noqa: E402
from storage import db                                            # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Завести тему вне плана.")
    ap.add_argument("task", help="id задачи моста, например 2026-09-03-post-01")
    ap.add_argument("--plat", required=True,
                    help="площадка: " + ", ".join(desk.PLATS))
    ap.add_argument("--format", dest="fmt", default="",
                    help="формат темы словом: пост, карусель, reels, анонс")
    ap.add_argument("--title", required=True, help="рабочий заголовок")
    ap.add_argument("--hook", default="", help="хук одной строкой")
    ap.add_argument("--why", default="", help="кому и зачем")
    ap.add_argument("--rubric", default="", help="рубрика, если известна")
    args = ap.parse_args()

    db.init(cfg.db_path)

    # Чей это чат, знает база, а не аргумент: `chat_id` из командной
    # строки — это тема, заведённая соседнему тенанту из-за опечатки.
    row = db.one("SELECT chat_id FROM bridge_runs WHERE task_id = ?",
                 args.task)
    if row is None:
        print(f"задачи {args.task} нет в базе: тему заводить некому",
              file=sys.stderr)
        return 2

    try:
        theme = desk.adhoc(row["chat_id"], plat=args.plat, fmt=args.fmt,
                           title=args.title, hook=args.hook, why=args.why,
                           rubric=args.rubric)
    except desk.NoWork as e:
        print(str(e), file=sys.stderr)
        return 1

    print(theme["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
