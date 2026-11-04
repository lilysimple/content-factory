"""Завод в работе — понарошку. Снимок для скринкаста, без базы и без сети.

    ./.venv/bin/python tools/panel.py --demo

Панель на живом заводе показывает правду, а правда там обычно скучная:
роли заняты минутами в сутки, и записать скринкаст «как это работает»
нечем. Этот модуль собирает тот же снимок, что `panel.snapshot`, но из
придуманного сценария: сводка → план → текст и сценарий вместе → макет →
публикация, с очередью, завершениями и ценой прогонов.

**Демо не притворяется живым заводом.** В снимке стоит `demo: true`, и
страница держит на виду плашку «демонстрация». Скринкаст, снятый с
выдуманных чисел и выданный за живые, — это ровно тот сорт вранья,
который потом годами ходит по презентациям.

**Базу модуль не трогает вовсе** — ни на чтение, ни на запись. Снимок
считается из времени: `t = сейчас - старт`, и всё остальное выводится из
него. Значит демо можно открыть на машине без `factory.db`, и значит
никакая его правка не может испортить живое состояние.

Сценарий идёт по кругу (`CYCLE` секунд), чтобы скринкаст можно было
начать с любого места и не ждать.
"""
from __future__ import annotations

import time
from typing import Any

CYCLE = 210          # полный круг сценария, секунды
START = time.time()

# Сцена: что идёт в каждом окне времени. Окно — (с, по, что бежит,
# что лежит в очереди). Полосы моста две, и сценарий это уважает:
# больше двух одновременных прогонов здесь не бывает.
SCENE: tuple[tuple[int, int, tuple[str, ...], tuple[str, ...]], ...] = (
    (0,   14, (),                   ()),
    (14,  46, ("research",),        ("plan",)),
    (46,  62, ("plan",),            ("post", "reels")),
    (62,  98, ("post", "reels"),    ("design",)),
    (98,  126, ("design",),         ("trend",)),
    (126, 158, ("trend",),          ()),
    (158, 176, ("idea",),           ()),
    (176, 210, (),                  ()),
)

ASK = {
    "research": "собери сводку недели",
    "plan": "контент-план на неделю",
    "post": "напиши текст по теме 2026-10-21-telegram-01",
    "reels": "сценарий ролика по теме 2026-10-22-instagram-01",
    "design": "оформи пост 2026-10-21-telegram-01",
    "trend": "разбери залетающие рилсы",
    "idea": "раскачай тему 2026-10-23-telegram-01",
}

# Прогоны, уже закончившиеся к этому часу: лента справа не должна быть
# пустой, иначе демо выглядит как первый запуск.
DONE: tuple[tuple[str, str, float, float], ...] = (
    ("research", "done", 286.3, 1.86),
    ("plan", "done", 310.0, 2.80),
    ("post", "done", 94.5, 0.62),
    ("reels", "done", 128.2, 0.81),
    ("design", "done", 495.1, 6.56),
    ("trend", "done", 163.4, 1.43),
    ("post", "failed", 12.0, 0.04),
    ("plan", "done", 302.7, 2.57),
)


def _phase(t: float) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    for a, b, run, queue in SCENE:
        if a <= t < b:
            return a, run, queue
    return 0, (), ()


def snapshot(day: str | None = None) -> dict[str, Any]:
    """Тот же словарь, что отдаёт `panel.snapshot`, только придуманный."""
    from tools import panel                 # поздний импорт: демо вторично

    now = time.time()
    t = (now - START) % CYCLE
    begun, run_now, queue_now = _phase(t)
    day = day or time.strftime("%Y-%m-%d")

    running = [{"task_id": f"{day}-{w}-01", "chat_id": -100,
                "workflow": w, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed_s": int(t - begun), "stale": 0}
               for w in run_now]
    # Первая просьба в очереди уже взята полосой, остальные ждут: именно
    # так это выглядит у моста, и путать два состояния нельзя.
    taken = [{"id": 1, "workflow": queue_now[0], "ask": ASK[queue_now[0]],
              "taken_at": ""}] if queue_now else []
    waiting = [{"id": i + 2, "workflow": w, "ask": ASK[w], "created_at": ""}
               for i, w in enumerate(queue_now[1:])]

    done = int(t / CYCLE * len(DONE)) + 4
    runs = []
    for i, (w, st, secs, cost) in enumerate((DONE * 2)[:done][::-1][:12]):
        runs.append({"task_id": f"{day}-{w}-{i + 1:02d}", "workflow": w,
                     "status": st, "duration_s": secs,
                     "started_at": time.strftime("%Y-%m-%d %H:%M",
                                                 time.localtime(now - 900 * (i + 1))),
                     "estimated_api_cost": cost,
                     "error": "упёрлись в лимит подписки Claude Code"
                     if st == "failed" else None})
    for r in running:                        # идущие прогоны идут первыми
        runs.insert(0, {"task_id": r["task_id"], "workflow": r["workflow"],
                        "status": "running", "duration_s": None,
                        "started_at": r["started_at"][:16],
                        "estimated_api_cost": None, "error": None})

    spent = sum(r["estimated_api_cost"] or 0 for r in runs)
    due = [{"theme_id": f"{day}-telegram-0{i}", "platform": "telegram",
            "scheduled_at": f"{day} 1{i}:00", "state": "ready"}
           for i in (1, 2)]

    busy = {r["workflow"] for r in running} | {q["workflow"] for q in taken}
    return {
        "at": time.strftime("%H:%M:%S"),
        "demo": True,
        "plant": {"state": "running", "pid": 0, "log_quiet_s": int(t % 40)},
        "lanes": {"total": 2, "busy": sorted(busy)},
        "running": running, "taken": taken, "waiting": waiting,
        "themes": {"idea": 44 - len(runs) % 5, "draft": 6, "ready": 46,
                   "failed": 11},
        "posts": {"pub": 2},
        "runs": runs,
        "today": {"n": len(runs), "secs": sum(r["duration_s"] or 0 for r in runs),
                  "cost": spent},
        "tenants": [{"chat_id": -100, "brand_slug": "lily-space",
                     "brand_name": "Lily Space", "status": "ready",
                     "tz": "Europe/Moscow", "auto": 1, "auto_at": "10:00"}],
        "due": due,
        "desks": panel._desks(running, taken, waiting, len(due)),
        "who": {key: name for name, _r, _e, key in panel.STAFF},
    }
