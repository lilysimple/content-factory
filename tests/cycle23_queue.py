"""Цикл 23: очередь задач к мосту.

Мост держит один процесс Claude Code за раз — это потолок железа, и он
остаётся. Цикл про то, что происходит со **второй** просьбой, пока идёт
первая: раньше она получала `Busy` и исчезала, теперь ждёт своей очереди.

Три границы, ради которых цикл написан.

**Просьба копится, задача — нет.** В очереди лежит текст просьбы, а
`input.md` собирается в момент, когда очередь дошла. Иначе третья по счёту
задача описывала бы завод таким, каким он был двадцать минут и две
публикации назад: со свободными слотами, которые заняты, и темами `idea`,
у которых уже есть текст. Ровно на этом «напиши пост» три раза подряд
написал бы три текста по одной теме.

**Занятость спрашивается у моста, а не у очереди.** Задачу ставит не
только очередь — старые роли и стенд ходят в `create_task` напрямую, — и
два источника занятости разошлись бы молча.

**Взятая строка переживает перезапуск.** Завод падает и поднимается
launchd'ом посреди работы. Строка, оставшаяся `taken`, не идёт и не ждёт,
то есть просто пропала — а человек ждёт ответа.

Живых вызовов нет: `claude` подменяется скриптом.
"""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import bridge                                   # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

import bots.handlers as handlers                                  # noqa: E402

SANDBOX = harness.TMP
bridge.TASKS_DIR = SANDBOX / "tasks"          # боевую tasks/ не трогаем
BIN = SANDBOX / "bin"
BIN.mkdir(parents=True, exist_ok=True)
os.environ["PATH"] = f"{BIN}{os.pathsep}{os.environ['PATH']}"

TODAY = "2026-09-04"

reg = FakeRegistry()
handlers.registry = reg                       # никуда не ходим


def fake_claude(body: str) -> None:
    p = BIN / "claude"
    p.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def reset() -> None:
    with db.tx() as c:
        c.execute("DELETE FROM bridge_runs")
        c.execute("DELETE FROM bridge_queue")
    reg.clear()


def qrows(status: str = "") -> list:
    if status:
        return db.q("SELECT * FROM bridge_queue WHERE status = ? ORDER BY id",
                    status)
    return db.q("SELECT * FROM bridge_queue ORDER BY id")


async def main() -> None:
    # ── пачка просьб копится, а не отбивается отказом ──────────────────
    reset()
    _, first = bridge.enqueue(CHAT, "напиши пост", workflow="post",
                              topic="review")
    _, second = bridge.enqueue(CHAT, "ещё один пост", workflow="post",
                               topic="review")
    _, third = bridge.enqueue(CHAT, "и сценарий", workflow="reels",
                              topic="reels")
    check("три просьбы встали в очередь", len(qrows("waiting")) == 3,
          str(len(qrows("waiting"))))
    check("места считаются по порядку", (first, second, third) == (1, 2, 3),
          f"{first}, {second}, {third}")
    check("порядок сохранён", [r["ask"] for r in qrows("waiting")] ==
          ["напиши пост", "ещё один пост", "и сценарий"])

    # Постановка не создаёт ни папки, ни строки прогона: пока просьба
    # ждёт, задачи не существует, и `input.md` соберётся по свежим фактам.
    check("папок задач при постановке не заводится",
          not list((bridge.TASKS_DIR).glob("*")) if bridge.TASKS_DIR.exists()
          else True)
    check("журнал прогонов пуст", not db.q("SELECT 1 FROM bridge_runs"))

    # ── потолок очереди ───────────────────────────────────────────────
    full = False
    for i in range(bridge.QUEUE_MAX):
        try:
            bridge.enqueue(CHAT, f"пост {i}", workflow="post", topic="review")
        except bridge.QueueFull:
            full = True
            break
    check("очередь не растёт бесконечно", full,
          f"поставилось больше {bridge.QUEUE_MAX}")

    # ── снять очередь ─────────────────────────────────────────────────
    dropped = bridge.drop_waiting(CHAT)
    check("снятие убирает всё ожидающее", dropped >= 3 and not qrows("waiting"),
          str(dropped))
    check("снятые строки не исчезают из журнала", len(qrows("dropped")) == dropped)

    # ── занятость: очередь спрашивает мост, а не себя ─────────────────
    reset()
    bridge.enqueue(CHAT, "напиши пост", workflow="post", topic="review")
    busy_id = bridge.create_task(CHAT, "задача мимо очереди",
                                 workflow="plan", today=TODAY)
    check("мост занят задачей помимо очереди", bridge.running() == busy_id)
    check("очередь не берёт строку, пока мост занят", bridge.take() is None,
          "два источника занятости разошлись бы молча")

    with db.tx() as c:
        c.execute("UPDATE bridge_runs SET status = 'done' WHERE task_id = ?",
                  (busy_id,))
    row = bridge.take()
    check("свободный мост отдаёт первую строку", row is not None and
          row["ask"] == "напиши пост")
    check("взятая строка больше не ждёт", not qrows("waiting"))
    check("взятая строка не потеряна", len(qrows("taken")) == 1)

    # ── взятая строка переживает перезапуск ───────────────────────────
    # launchd поднял завод посреди работы: процесса нет, строка `taken`.
    # Без возврата она не идёт и не ждёт, а человек ждёт ответа.
    back = bridge.unstick()
    check("брошенная строка вернулась в очередь", back == 1 and
          len(qrows("waiting")) == 1, str(back))

    # ── полный проход: постановка → разбор → ответ ────────────────────
    reset()
    # Id задачи не известен заранее: папку заводит `_serve`, когда очередь
    # дошла. Вынимаем его из промпта — там он лежит путём к `input.md`, —
    # а пишем в песочницу: боевая `tasks/` тут ни при чём.
    fake_claude(
        f'D="{bridge.TASKS_DIR}"\n'
        'T=$(printf "%s\\n" "$@" | '
        'sed -n "s|.*tasks/\\([^/]*\\)/input.md.*|\\1|p" | head -1)\n'
        'printf "Готово." > "$D/$T/final.md"\n'
        'echo \'{"type":"result","subtype":"success","is_error":false,'
        '"result":"ок","duration_ms":1200,"session_id":"s1"}\'\n')

    await handlers.bridge_task(CHAT, "напиши пост", "post", "review")
    said = reg.texts()
    check("постановка отвечает сразу", "Приняла задачу" in said, said[:120])
    check("постановка не запускает прогон", not db.q("SELECT 1 FROM bridge_runs"),
          "обработчик сообщения не должен ждать получасовой прогон")

    await handlers.bridge_task(CHAT, "ещё один", "post", "review")
    said = reg.texts()
    check("вторая просьба принята, а не отбита",
          "2-я в очереди" in said and "Уже работаю" not in said, said[-200:])
    check("человеку названо, сколько впереди", "впереди 1 задача" in said,
          said[-200:])

    # Один оборот насоса берёт ровно одну строку.
    row = bridge.take()
    check("насос берёт первую", row is not None and row["ask"] == "напиши пост")
    reg.clear()
    await handlers._serve(row)

    said = reg.texts()
    check("о начале работы сказано", "Берусь" in said, said[:120])
    check("результат доехал", "Готово." in said, said[:200])
    check("строка закрыта исходом", qrows("done") and
          qrows("done")[0]["task_id"], str(qrows()))
    check("вторая строка всё ещё ждёт своей очереди",
          len(qrows("waiting")) == 1)

    # ── input.md собирается при разборе, а не при постановке ──────────
    q = qrows("done")[0]
    task_dir = bridge.TASKS_DIR / q["task_id"]
    check("папка задачи завелась при разборе", (task_dir / "input.md").exists(),
          str(task_dir))

    # ── сорванный прогон закрывает строку, а не вешает насос ──────────
    reset()
    fake_claude("exit 3\n")
    bridge.enqueue(CHAT, "упадёт", workflow="post", topic="review")
    row = bridge.take()
    reg.clear()
    await handlers._serve(row)
    check("провал назван человеку", "не довела" in reg.texts().lower(),
          reg.texts()[:150])
    check("строка закрыта, а не осталась взятой",
          not qrows("waiting") and not qrows("taken"), str(qrows()))

    # ── склонение в отчёте ────────────────────────────────────────────
    check("одна задача", handlers._tasks(1) == "1 задача", handlers._tasks(1))
    check("две задачи", handlers._tasks(2) == "2 задачи", handlers._tasks(2))
    check("пять задач", handlers._tasks(5) == "5 задач", handlers._tasks(5))
    check("одиннадцать задач", handlers._tasks(11) == "11 задач",
          handlers._tasks(11))

    reset()


asyncio.run(main())
raise SystemExit(report())
