"""Панель наблюдения за заводом: кто сейчас работает и что в очереди.

    ./.venv/bin/python tools/panel.py            # http://127.0.0.1:8765
    ./.venv/bin/python tools/panel.py --port 9000
    ./.venv/bin/python tools/panel.py --once     # один снимок в JSON, без сервера

Страница сама опрашивает `/state.json` раз в две секунды и рисует цех
в 3D: кирпич, бетон, фермы, окна, лампы на шнурах, а под ними —
рабочие места. У каждой роли стол, имя и человек за ним. Занятый
печатает, лампа над ним горит, круг на полу зелёный и секунды тикают;
ждущий очереди отодвинулся, круг янтарный; свободный откинулся в
кресле и погасил экран. Открывать в браузере на той же машине.

Сама сцена — `panel_web/`, three.js лежит в `vendor/`: панель
открывается без сети, потому что открывают её ровно тогда, когда
что-то пошло не так.

**Панель только читает.** База открывается `mode=ro`, и никаких
`UPDATE` здесь нет ни одного: панель смотрит на тот же `factory.db`, в
который завод пишет на ходу, и единственный способ не испортить ему
состояние — не иметь права писать вовсе.

Почему отдельный процесс, а не эндпоинт в боте. Завод в launchd держит
один экземпляр: Telegram разрешает один опрос на бота, и всякий лишний
порт внутри этого процесса — лишний повод его уронить. Панель падает
сама по себе и ничего за собой не тянет.

Почему stdlib, а не Streamlit. Streamlit тащит свой рантайм и сессии,
а показать нужно одну страницу с числами из SQLite. `http.server` на
локальном интерфейсе здесь дешевле на целую зависимость.

Чего панель не делает: не кладёт задачи, не утверждает тексты, не
публикует. Это смотровое окно, а не пульт — решения human-in-the-loop
остаются в Telegram, где у них есть кнопки и провенанс.
"""
from __future__ import annotations

import argparse
import errno
import json
import sqlite3
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import cfg                                            # noqa: E402
from orchestrator import bridge                                   # noqa: E402

AGENT = "space.lily.content-factory"

# Цех: у каждой роли имя и рабочее место. Имена не косметика — на них
# смотрят, когда спрашивают «кто сейчас занят», и «Лиза собирает план»
# читается быстрее, чем «workflow=plan status=running».
#
# Порядок столов — порядок цепи: сводка → план → текст и сценарий →
# макет → монтаж → выпуск. Глаз идёт слева направо, как работа.
STAFF: tuple[tuple[str, str, str, str], ...] = (
    ("Марк",  "Руководитель",    "🤝", "director"),
    ("Соня",  "Аналитик",        "🔍", "research"),
    ("Лиза",  "Стратег",         "🎯", "plan"),
    ("Дима",  "Идеи",            "💡", "idea"),
    ("Паша",  "Редактор",        "✍️", "post"),
    ("Рита",  "Сценарист",       "🎬", "reels"),
    ("Ника",  "Тренды",          "🛰", "trend"),
    ("Маша",  "Дизайнер",        "🎨", "design"),
    ("Гриша", "Монтажёр",        "🎞", "cut"),
    ("Боря",  "Выпускающий",     "📤", "publish"),
)

# Что человек делает, когда занят. Строка от первого лица стола.
DOING = {
    "director": "разбирает просьбу и собирает команду",
    "research": "собирает сводку недели",
    "plan": "расставляет слоты на неделю",
    "idea": "раскачивает тему до вариантов",
    "post": "пишет текст поста",
    "reels": "пишет суфлёр под ролик",
    "trend": "разбирает чужие рилсы",
    "design": "верстает макет",
}

_cache: dict[str, Any] = {"plant": (0.0, None)}


def _db() -> sqlite3.Connection:
    """Только чтение: панель не имеет права испортить состояние завода."""
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _plant() -> dict[str, Any]:
    """Жив ли завод. `launchctl` спрашивается не чаще раза в пять секунд."""
    age, got = _cache["plant"]
    if got is not None and time.time() - age < 5:
        return got
    out = ""
    try:
        out = subprocess.run(
            ["launchctl", "print", f"gui/{__import__('os').getuid()}/{AGENT}"],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        pass
    state, pid = "", 0
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("state = ") and not state:
            state = s.removeprefix("state = ")
        if s.startswith("pid = ") and not pid:
            pid = int(s.removeprefix("pid = ") or 0)
    log = Path(__file__).resolve().parent.parent / "bot.log"
    quiet = int(time.time() - log.stat().st_mtime) if log.exists() else None
    got = {"state": state or "не найден", "pid": pid, "log_quiet_s": quiet}
    _cache["plant"] = (time.time(), got)
    return got


def _rows(conn: sqlite3.Connection, sql: str, *a: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, a)]


def _desks(running: list[dict[str, Any]], taken: list[dict[str, Any]],
           waiting: list[dict[str, Any]], due: int) -> list[dict[str, Any]]:
    """Кто за каким столом и чем занят.

    Состояние считается здесь, а не на странице: источник один — база, и
    страница не должна знать, что `taken` это «взято, но прогон ещё не
    начат». Два счёта одного состояния разъезжаются молча.
    """
    by_run = {r["workflow"]: r for r in running}
    grabbed = {q["workflow"] for q in taken}
    queued: dict[str, int] = {}
    for q in waiting:
        queued[q["workflow"]] = queued.get(q["workflow"], 0) + 1

    out = []
    for name, role, emoji, key in STAFF:
        desk = {"name": name, "role": role, "emoji": emoji, "key": key,
                "state": "idle", "doing": "", "task": "", "elapsed_s": None,
                "note": ""}
        if key == "cut":
            # Монтажёр идёт не через мост: его будит присланный файл.
            desk["note"] = "вне моста — ждёт присланный дубль"
        elif key == "publish":
            # Публикатор не AI-роль: комплект и потолки считает код.
            desk["note"] = (f"{due} в очереди на отправку" if due
                            else "всё отправлено")
            desk["state"] = "queue" if due else "idle"
        elif key == "director":
            if (live := [r for r in running if not r["stale"]]):
                desk.update(state="work", doing=DOING[key],
                            task=live[0]["task_id"],
                            elapsed_s=live[0]["elapsed_s"])
            elif waiting or taken:
                desk.update(state="queue",
                            note=f"{len(waiting) + len(taken)} в очереди")
        elif (r := by_run.get(key)):
            desk.update(state="stale" if r["stale"] else "work",
                        doing=DOING.get(key, ""), task=r["task_id"],
                        elapsed_s=r["elapsed_s"],
                        note="прогон висит дольше потолка — мост его спишет"
                        if r["stale"] else "")
        elif key in grabbed:
            desk.update(state="queue", note="взято, прогон ещё не начат")
        elif (n := queued.get(key)):
            desk.update(state="queue", note="ждёт очереди" if n == 1
                        else f"ждёт очереди, просьб {n}")
        out.append(desk)
    return out


def snapshot() -> dict[str, Any]:
    """Один срез состояния. Числа берутся из базы, а не из памяти панели."""
    conn = _db()
    try:
        # Окно то же, что у моста: `bridge.running_rows` не считает живой
        # строку старше `TIMEOUT`, и панель не имеет права считать иначе.
        # Брошенный прогон висел бы «в работе» вечно — ровно то, ради чего
        # панель и открывают.
        running = _rows(conn,
                        "SELECT task_id, chat_id, workflow, "
                        "datetime(started_at, 'localtime') AS started_at, "
                        "MAX(0, CAST(strftime('%s','now') "
                        "- strftime('%s',started_at) AS INTEGER)) AS elapsed_s, "
                        "started_at <= datetime('now', ?) AS stale "
                        "FROM bridge_runs WHERE status = 'running' "
                        "ORDER BY started_at", f"-{bridge.TIMEOUT} seconds")
        taken = _rows(conn,
                      "SELECT id, workflow, ask, taken_at FROM bridge_queue "
                      "WHERE status = 'taken' ORDER BY id")
        waiting = _rows(conn,
                        "SELECT id, workflow, ask, created_at FROM bridge_queue "
                        "WHERE status = 'waiting' ORDER BY id")
        themes = {r["status"]: r["n"] for r in _rows(
            conn, "SELECT status, COUNT(*) AS n FROM themes GROUP BY status")}
        posts = {r["state"]: r["n"] for r in _rows(
            conn, "SELECT state, COUNT(*) AS n FROM posts GROUP BY state")}
        runs = _rows(conn,
                     "SELECT task_id, workflow, status, duration_s, "
                     "datetime(started_at, 'localtime') AS started_at, "
                     "estimated_api_cost, error FROM bridge_runs "
                     "ORDER BY started_at DESC LIMIT 12")
        today = _rows(conn,
                      "SELECT COUNT(*) AS n, "
                      "COALESCE(SUM(duration_s), 0) AS secs, "
                      "COALESCE(SUM(estimated_api_cost), 0) AS cost "
                      "FROM bridge_runs WHERE date(started_at) = date('now')")[0]
        tenants = _rows(conn,
                        "SELECT chat_id, brand_slug, brand_name, status, tz, "
                        "auto, auto_at FROM tenants ORDER BY created_at")
        due = _rows(conn,
                    "SELECT theme_id, platform, scheduled_at, state FROM posts "
                    "WHERE state <> 'pub' ORDER BY scheduled_at LIMIT 6")
    finally:
        conn.close()

    busy = ({r["workflow"] for r in running if not r["stale"]}
            | {r["workflow"] for r in taken})
    return {
        "at": time.strftime("%H:%M:%S"),
        "plant": _plant(),
        "lanes": {"total": bridge.LANES, "busy": sorted(busy)},
        "running": running, "taken": taken, "waiting": waiting,
        "themes": themes, "posts": posts, "runs": runs,
        "today": dict(today), "tenants": tenants, "due": due,
        "desks": _desks(running, taken, waiting, len(due)),
        "who": {key: name for name, _role, _e, key in STAFF},
    }


HERE = Path(__file__).resolve().parent

# Страница читается с диска на каждый запрос, а не вшита в этот файл:
# сцена правится чаще, чем логика, и перезапускать ради вёрстки сервер,
# который смотрит на живой завод, — лишний повод его потерять.
STATIC = {
    "/": (HERE / "panel_web/index.html", "text/html; charset=utf-8"),
    "/index.html": (HERE / "panel_web/index.html", "text/html; charset=utf-8"),
    "/office.js": (HERE / "panel_web/office.js",
                   "text/javascript; charset=utf-8"),
    # three.js лежит в репозитории, а не на CDN: панель должна открываться
    # без сети — её открывают ровно тогда, когда что-то не так.
    "/three.module.js": (HERE / "vendor/three.module.min.js",
                         "text/javascript; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "factory-panel"

    def _send(self, body: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                                  # noqa: N802
        if self.path.startswith("/state.json"):
            try:
                body = json.dumps(snapshot(), ensure_ascii=False).encode()
            except sqlite3.Error as e:
                body = json.dumps({"error": str(e)}).encode()
            self._send(body, "application/json; charset=utf-8")
        elif (hit := STATIC.get(self.path.split("?")[0])):
            path, kind = hit
            try:
                self._send(path.read_bytes(), kind)
            except OSError:
                self.send_error(500, "файла панели нет на диске")
        else:
            self.send_error(404)

    def log_message(self, *a: Any) -> None:
        """Тишина: журнал завода — `bot.log`, и мешать в него опросы панели
        значит потерять в нём настоящие события."""


def main() -> int:
    ap = argparse.ArgumentParser(description="Смотровое окно в завод.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="по умолчанию только эта машина")
    ap.add_argument("--once", action="store_true",
                    help="напечатать снимок в JSON и выйти")
    args = ap.parse_args()

    if not cfg.db_path.exists():
        print(f"базы нет: {cfg.db_path}", file=sys.stderr)
        return 1
    if args.once:
        print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
        return 0

    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        # Панель открывают второй раз чаще, чем первый: она уже работает в
        # соседнем окне. Трейсбек тут пугает на ровном месте.
        print(f"порт {args.port} занят — панель, похоже, уже открыта:\n"
              f"  http://{args.host}:{args.port}\n"
              f"другой порт: tools/panel.py --port {args.port + 1}",
              file=sys.stderr)
        return 1
    print(f"панель: http://{args.host}:{args.port}  (Ctrl-C чтобы закрыть)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
