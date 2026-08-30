"""Мост между Python и Claude Code.

Ответственность ровно одна: довезти задачу до Director и вернуть результат.

**Чего здесь нет и не должно появиться:** выбора субагентов, порядка их
вызова, промптов ролей и сборки результатов. Всё это живёт внутри Claude
Code. Как только сюда переедет хоть одно из перечисленного, решение «кого
звать» окажется размазано между `bots/router.py` и `.claude/CLAUDE.md`, и
к третьей правке никто не скажет, где на самом деле выбирается workflow.

Мост знает четыре вещи: как завести папку задачи, как запустить процесс,
как дождаться и как прочитать `final.md`.

Почему подпроцесс асинхронный. Полный workflow идёт минуты. Блокирующий
`subprocess.run` заморозил бы event loop aiogram, а с ним поллинг всех
восьми ботов: Telegram начал бы отдавать обновления другому инстансу, и
мы получили бы `Conflict` на ровном месте.

Почему окружение собирается белым списком. `config.py` зовёт
`load_dotenv()`, поэтому в `os.environ` процесса бота лежат ключ API и
токены восьми ботов. Унаследованное окружение отдало бы их подпроцессу.
Плюс `ANTHROPIC_API_KEY` в окружении увёл бы вызов на API-тариф, а мы
проверяем и используем подписочный вход.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import ROOT
from storage import db

log = logging.getLogger("bridge")

TASKS_DIR = ROOT / "tasks"

# Что получает подпроцесс. Всё остальное отрезается: см. шапку модуля.
# ANTHROPIC_API_KEY сюда не входит намеренно — авторизация идёт входом CLI,
# учётные данные лежат в Keychain, и подпроцесс берёт их сам.
KEEP = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "SHELL")

# Инструменты, которые нужны Director. `Task` обязателен: без него он не
# сможет позвать ни одного субагента, и весь смысл моста пропадёт.
TOOLS = "Read,Write,Edit,Glob,Grep,Bash,Task,TodoWrite"

# Полный workflow из трёх ролей идёт минуты. Потолок нужен не для скорости,
# а чтобы зависший процесс не держал задачу вечно: человек в это время
# видит молчание, а молчание неотличимо от поломки.
TIMEOUT = 900

WORKFLOWS = {"plan": "контент-план на неделю"}


class Busy(RuntimeError):
    """Уже идёт задача. MVP держит одну за раз."""


@dataclass
class Result:
    task_id: str
    ok: bool = False
    text: str = ""
    error: str = ""
    secs: float = 0.0
    cost: float | None = None          # диагностика, НЕ подтверждённое списание
    session_id: str = ""
    artifacts: list[str] = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return TASKS_DIR / self.task_id


# ── окружение ─────────────────────────────────────────────────────────

def clean_env() -> dict[str, str]:
    """Окружение подпроцесса. Белый список, а не наследование."""
    return {k: os.environ[k] for k in KEEP if k in os.environ}


# ── результат для Telegram ────────────────────────────────────────────

# Разрешённые Telegram теги. Всё, что не подошло под этот шаблон, — текст.
_TAG = re.compile(r"</?(b|i|code|a)(\s+href=\"[^\"<>]*\")?>", re.I)


def for_telegram(text: str) -> str:
    """Сделать `final.md` безопасным для отправки с `parse_mode=HTML`.

    Промпт это просьба, границу держит код — здесь ровно тот случай.
    `.claude/CLAUDE.md` просит Director писать только теги, которые
    Telegram понимает, но просьбу нечем подкрепить: `registry._send`
    пробрасывает `TelegramBadRequest` дальше, и одна угловая скобка в
    прозе («охват < 1000») роняет отправку целиком.

    Цена ошибки несимметрична. Задача при этом **успешна**: субагенты
    отработали, минуты и лимиты потрачены, `final.md` лежит на диске — а
    человек в чате видит молчание, неотличимое от поломки.

    Поэтому: экранируется всё, и возвращаются обратно только те теги,
    что сошлись в пары. Незакрытый `<b>` и непарный `</i>` остаются
    видимым текстом — некрасиво, но доходит. Уронить сообщение хуже, чем
    показать лишнюю скобку.
    """
    spans: list[tuple[int, int, str, str, bool]] = []
    stack: list[int] = []
    good: set[int] = set()

    for m in _TAG.finditer(text):
        raw, name = m.group(0), m.group(1).lower()
        closing = raw.startswith("</")
        i = len(spans)
        spans.append((m.start(), m.end(), raw, name, closing))
        if not closing:
            stack.append(i)
        elif stack and spans[stack[-1]][3] == name:
            good.add(stack.pop())
            good.add(i)
        # Непарное закрытие остаётся текстом: именно оно ломает разбор.

    # Незакрытые открытия так и не попали в `good` — и не попадут.
    out, pos = [], 0
    for i, (start, end, raw, _, _) in enumerate(spans):
        out.append(html.escape(text[pos:start], quote=False))
        out.append(raw if i in good else html.escape(raw, quote=False))
        pos = end
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


# ── задача на диске ───────────────────────────────────────────────────

def new_id(workflow: str, today: str) -> str:
    """Стабильный id задачи: ГГГГ-ММ-ДД-workflow-NN."""
    n = 1
    while (TASKS_DIR / f"{today}-{workflow}-{n:02d}").exists():
        n += 1
    return f"{today}-{workflow}-{n:02d}"


def running(chat_id: int) -> str:
    """id идущей задачи этого чата, пустая строка — свободно.

    Строка старше `TIMEOUT` живой быть не может: `run` убивает процесс по
    этому потолку и сам проставляет исход. Значит, такая строка осталась
    от процесса, которого больше нет, — launchd перезапустил завод, или
    инстанс сняли через `kill -9` (обычный `SIGTERM` он не берёт, это
    записано в CLAUDE.md).

    Без этой поправки «одна задача за раз» превращается в «ни одной
    больше никогда»: `create_task` вечно отвечает Busy, а способа снять
    зависшую строку у человека в Telegram нет.
    """
    row = db.one(
        "SELECT task_id FROM bridge_runs WHERE chat_id = ? "
        "AND status = 'running' "
        "AND started_at > datetime('now', ?) "
        "ORDER BY started_at DESC", chat_id, f"-{TIMEOUT} seconds")
    return row["task_id"] if row else ""


def sweep() -> int:
    """Пометить брошенные прогоны. Возвращает, сколько нашлось.

    Отдельно от `running`, потому что читатели разные: `running` решает,
    занято ли сейчас, а это чинит журнал, чтобы брошенный прогон не
    выглядел вечно идущим в статистике.
    """
    with db.tx() as c:
        cur = c.execute(
            "UPDATE bridge_runs SET status = 'failed', "
            "finished_at = datetime('now'), "
            "error = 'процесс исчез, исход неизвестен' "
            "WHERE status = 'running' AND started_at <= datetime('now', ?)",
            (f"-{TIMEOUT} seconds",))
        n = cur.rowcount
    if n:
        log.warning("брошенных прогонов найдено: %s", n)
    return n


def create_task(chat_id: int, ask: str, *, workflow: str, today: str,
                brand_slug: str = "", brand_path: str = "") -> str:
    """Завести папку задачи и `input.md`. Возвращает task_id.

    `input.md` это всё, что Director получает от Python. Никаких указаний,
    каких субагентов звать, здесь нет и быть не должно.
    """
    if busy := running(chat_id):
        raise Busy(busy)

    task_id = new_id(workflow, today)
    d = TASKS_DIR / task_id
    d.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# Задача {task_id}", "",
        f"Workflow: `{workflow}` — {WORKFLOWS.get(workflow, workflow)}",
        f"Дата: {today}",
    ]
    if brand_slug:
        lines.append(f"Бренд: `{brand_slug}`")
    if brand_path:
        lines.append(f"Папка бренда: `{brand_path}`")
    lines += ["", "## Запрос человека", "", ask.strip() or
              "Собери контент-план на неделю.", "",
              "## Куда положить результат", "",
              f"Артефакты ролей — в `tasks/{task_id}/`.",
              f"Финальный ответ человеку — в `tasks/{task_id}/final.md`.",
              "", "Этот файл читает человек в Telegram: пиши по-русски, "
              "без служебных пометок и без разметки, которой нет в Telegram.",
              "Всё, что не сошлось, называй строкой, а не проглатывай."]

    (d / "input.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with db.tx() as c:
        c.execute("INSERT INTO bridge_runs (task_id, chat_id, workflow) "
                  "VALUES (?, ?, ?)", (task_id, chat_id, workflow))
    log.info("задача %s заведена", task_id)
    return task_id


# ── запуск ────────────────────────────────────────────────────────────

def _prompt(task_id: str) -> str:
    """Единственное, что Python говорит Director. Намеренно коротко."""
    return (f"Выполни задачу из tasks/{task_id}/input.md. "
            f"Определи минимальный необходимый workflow сам и вызови нужных "
            f"субагентов. Финальный ответ человеку положи в "
            f"tasks/{task_id}/final.md.")


async def run(task_id: str) -> Result:
    """Запустить Claude Code на задаче и дождаться результата."""
    res = Result(task_id=task_id)
    binary = shutil.which("claude")
    if not binary:
        return _fail(res, "бинарь claude не найден в PATH")

    argv = [binary, "-p", _prompt(task_id), "--allowedTools", TOOLS,
            "--permission-mode", "acceptEdits", "--output-format", "json"]

    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(ROOT), env=clean_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(),
                                              timeout=TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            res.secs = round(time.monotonic() - started, 1)
            return _fail(res, f"не уложился в {TIMEOUT} с", status="timeout")
    except OSError as e:
        return _fail(res, f"не удалось запустить процесс: {e}")

    res.secs = round(time.monotonic() - started, 1)
    stdout = out.decode("utf-8", "replace").strip()
    stderr = err.decode("utf-8", "replace").strip()

    # Диагностика из JSON-вывода. Её отсутствие не делает задачу проваленной:
    # результат лежит в файле, а не в stdout.
    if stdout:
        try:
            data = json.loads(stdout)
            res.cost = data.get("total_cost_usd")
            res.session_id = str(data.get("session_id") or "")
        except json.JSONDecodeError:
            log.warning("%s: stdout не разобрался как JSON", task_id)

    if proc.returncode != 0:
        return _fail(res, _reason(stdout, stderr, proc.returncode))

    final = res.dir / "final.md"
    if not final.exists():
        return _fail(res, "Claude Code отработал, но final.md не появился")

    res.text = for_telegram(final.read_text(encoding="utf-8").strip())
    if not res.text:
        return _fail(res, "final.md пустой")

    res.ok = True
    res.artifacts = sorted(p.name for p in res.dir.glob("*.md"))
    _finish(res, "done")
    log.info("задача %s готова за %s с, артефактов %s",
             task_id, res.secs, len(res.artifacts))
    return res


def _reason(stdout: str, stderr: str, rc: int) -> str:
    """Человеческая причина отказа вместо кода возврата.

    Через неё проходит всё, что вернул CLI, поэтому «войди в CLI» человек
    видит одинаково, чем бы вызов ни упал.
    """
    blob = f"{stdout}\n{stderr}".lower()
    if "not logged in" in blob or "/login" in blob:
        return ("Claude Code не авторизован. Один раз выполни `claude` и "
                "`/login` в терминале")
    if "credit balance" in blob or "insufficient" in blob:
        return "закончились средства или исчерпан лимит"
    if "rate limit" in blob or "429" in blob:
        return "упёрлись в лимит запросов, попробуй позже"
    return (stderr or stdout or f"процесс вернул код {rc}")[:300]


def _fail(res: Result, why: str, *, status: str = "failed") -> Result:
    res.ok, res.error = False, why
    _finish(res, status)
    log.error("задача %s: %s", res.task_id, why)
    return res


def _finish(res: Result, status: str) -> None:
    """Записать исход. `cost` это диагностика CLI, а не факт списания."""
    with db.tx() as c:
        c.execute(
            "UPDATE bridge_runs SET status = ?, finished_at = datetime('now'),"
            " duration_s = ?, estimated_api_cost = ?, session_id = ?,"
            " error = ? WHERE task_id = ?",
            (status, res.secs, res.cost, res.session_id or None,
             res.error or None, res.task_id))
