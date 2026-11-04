"""Цикл 30: панель наблюдения.

Панель смотрит в ту же базу, в которую завод пишет на ходу, и её главный
риск не в вёрстке, а в двух вещах: записать туда что-нибудь и соврать про
состояние стола. Поэтому проверяется право на запись (его не должно быть)
и раскладка прогонов по людям: бегущий прогон, взятая задача и просьба в
очереди выглядят по-разному, и путать их нельзя — «работает» вместо
«ждёт» читается как зависший завод.

Модель здесь не участвует: панель только читает.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import harness
from harness import CHAT, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import bridge                                   # noqa: E402
from storage import db                                            # noqa: E402
from tools import panel                                           # noqa: E402

db.init(cfg.db_path)


def wipe() -> None:
    with db.tx() as c:
        c.execute("DELETE FROM bridge_runs")
        c.execute("DELETE FROM bridge_queue")


def by_name(desks: list[dict], name: str) -> dict:
    return next(d for d in desks if d["name"] == name)


def main() -> int:
    wipe()

    snap = panel.snapshot()
    check("снимок несёт столы", len(snap["desks"]) == len(panel.STAFF))
    check("на пустом заводе все свободны",
          all(d["state"] == "idle" for d in snap["desks"]
              if d["key"] not in ("cut", "publish")))
    check("у каждого стола есть имя и должность",
          all(d["name"] and d["role"] for d in snap["desks"]))

    with db.tx() as c:
        c.execute("INSERT INTO bridge_runs (task_id, chat_id, workflow, "
                  "status, started_at) VALUES ('2026-01-01-plan-01', ?, "
                  "'plan', 'running', datetime('now', '-70 seconds'))", (CHAT,))
        c.execute("INSERT INTO bridge_queue (chat_id, workflow, ask, status) "
                  "VALUES (?, 'design', 'свёрстай', 'taken')", (CHAT,))
        c.execute("INSERT INTO bridge_queue (chat_id, workflow, ask, status) "
                  "VALUES (?, 'post', 'напиши', 'waiting')", (CHAT,))

    snap = panel.snapshot()
    desks = snap["desks"]
    plan = by_name(desks, "Лиза")
    check("бегущий прогон сажает человека за работу",
          plan["state"] == "work" and plan["task"] == "2026-01-01-plan-01",
          str(plan))
    check("секунды на бегущем прогоне идут от старта",
          60 <= (plan["elapsed_s"] or 0) <= 120, str(plan["elapsed_s"]))
    check("занятый говорит, чем занят", bool(plan["doing"]), str(plan))

    check("руководитель занят, пока занят кто угодно",
          by_name(desks, "Марк")["state"] == "work")
    check("взятая задача это очередь, а не работа",
          by_name(desks, "Маша")["state"] == "queue"
          and "прогон" in by_name(desks, "Маша")["note"],
          str(by_name(desks, "Маша")))
    check("просьба в очереди не выдаётся за взятую",
          "ждёт" in by_name(desks, "Паша")["note"],
          str(by_name(desks, "Паша")))
    check("незанятый остаётся свободен",
          by_name(desks, "Соня")["state"] == "idle")
    check("Монтажёр говорит, что он вне моста",
          "вне моста" in by_name(desks, "Гриша")["note"])

    check("полосы считаются по тем же правилам, что у моста",
          snap["lanes"]["total"] == 2
          and set(snap["lanes"]["busy"]) == {"plan", "design"},
          str(snap["lanes"]))

    # Брошенный прогон: мост такую строку живой не считает, и панель
    # не имеет права показывать её работой — иначе человек видит «идёт»
    # там, где процесса давно нет.
    wipe()                       # очередь из прошлого блока сюда не мешается
    with db.tx() as c:
        c.execute("INSERT INTO bridge_runs (task_id, chat_id, workflow, "
                  "status, started_at) VALUES ('2026-01-01-plan-02', ?, "
                  "'plan', 'running', datetime('now', ?))",
                  (CHAT, f"-{bridge.TIMEOUT + 600} seconds"))
    desks = panel.snapshot()["desks"]
    stuck = by_name(desks, "Лиза")
    check("брошенный прогон не выдаётся за работу",
          stuck["state"] == "stale", str(stuck))
    check("про зависший прогон сказано словами",
          "спишет" in stuck["note"], str(stuck))
    check("руководитель не занят чужим зависшим прогоном",
          by_name(desks, "Марк")["state"] == "idle")
    check("зависшая полоса не считается занятой",
          panel.snapshot()["lanes"]["busy"] == [],
          str(panel.snapshot()["lanes"]))

    # Демонстрация для скринкаста: своя выдумка, база не читается вовсе,
    # и снимок обязан называть себя демонстрацией — иначе запись с
    # придуманными числами уедет в презентацию как живая.
    from tools import panel_demo                                  # noqa: E402
    shot = panel_demo.snapshot()
    check("демо называет себя демонстрацией", shot.get("demo") is True)
    check("у демо те же ключи, что у живого снимка",
          set(panel.snapshot()) - {"demo"} <= set(shot),
          str(set(panel.snapshot()) - set(shot)))
    check("демо не зовёт базу",
          # Путь от корня репозитория, а не от текущей папки: `run.py`
          # запускает циклы из `tests/`, и относительный путь роняет
          # весь цикл `FileNotFoundError` вместо одной проверки.
          "sqlite3" not in (harness.REPO / "tools" / "panel_demo.py")
          .read_text("utf-8"))
    # Сценарий прокручивается целиком: он обязан уважать полосы моста,
    # иначе демо показывает завод, которого не бывает.
    seen, over = set(), []
    base = panel_demo.START
    for t in range(0, panel_demo.CYCLE, 2):
        panel_demo.START = time.time() - t
        shot_t = panel_demo.snapshot()
        live = [x for x in shot_t["desks"] if x["state"] == "work"]
        seen |= {x["key"] for x in live}
        # Марк занят всегда, пока идёт любой прогон: он не полоса.
        if len(shot_t["running"]) > 2:
            over.append(t)
    panel_demo.START = base
    check("за круг демо показывает все роли моста",
          {"research", "plan", "post", "reels", "design", "trend", "idea"}
          <= seen, str(sorted(seen)))
    check("демо не нарушает потолок полос моста", not over, str(over[:3]))

    conn = panel._db()
    try:
        conn.execute("UPDATE themes SET title = 'правка панелью'")
        wrote = True
    except sqlite3.OperationalError:
        wrote = False
    finally:
        conn.close()
    check("панель не имеет права писать в базу", not wrote)

    wipe()
    return report()


raise SystemExit(main())
