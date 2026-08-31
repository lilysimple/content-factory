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
from orchestrator import desk, research, strategy
from storage import brand as brand_store
from storage import db

log = logging.getLogger("bridge")

TASKS_DIR = ROOT / "tasks"

# События недели: вебинар, прогрев, запуск. Ведёт человек, читает Стратег.
EVENTS_PATH = "plans/events.md"

# Что получает подпроцесс. Всё остальное отрезается: см. шапку модуля.
# ANTHROPIC_API_KEY сюда не входит намеренно — авторизация идёт входом CLI,
# учётные данные лежат в Keychain, и подпроцесс берёт их сам.
KEEP = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "SHELL")

# Где искать `claude`, если его нет в PATH.
#
# Это не перестраховка, а починка боевого отказа. Завод стоит на launchd,
# а launchd даёт агенту голый PATH — `/usr/bin:/bin:/usr/sbin:/sbin`, без
# `~/.local/bin`, куда ставится CLI. Из терминала мост работал, из-под
# автозапуска `shutil.which("claude")` возвращал None, и **любая** задача
# из Telegram падала бы на первой строке `run` с «бинарь не найден».
# Поймано аудитом 30.08: живой прогон делался руками из терминала, и
# телеграм-нога цепи ни разу не проверялась.
#
# Домашние пути разворачиваются от `HOME`, а не от текущего пользователя:
# на VPS завод пойдёт под своим аккаунтом.
FALLBACK_BINS = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)

# Инструменты, которые нужны Director. `Task` обязателен: без него он не
# сможет позвать ни одного субагента, и весь смысл моста пропадёт.
TOOLS = "Read,Write,Edit,Glob,Grep,Bash,Task,TodoWrite"

# Потолок нужен не для скорости, а чтобы зависший процесс не держал задачу
# вечно: человек в это время видит молчание, а молчание неотличимо от
# поломки.
#
# Величина измерена, а не угадана. Прогон 2026-08-30-plan-04: разведка
# минуту, Ресёрчер девять минут сорок пять секунд (живые запросы по девяти
# внешним каналам), следом Стратег — и его срезало прежним потолком в 900 с
# на середине. Три роли подряд в 15 минут не укладываются.
#
# Тридцать минут дают запас Стратегу и Идеатору и всё ещё ограничивают
# зависший процесс. Если workflow станет длиннее трёх ролей, потолок
# придётся считать заново — и снова замером.
TIMEOUT = 1800

# Что человек попросил, человеческими словами. Ключ — команда в Telegram.
#
# **Это не список ролей и не порядок вызова.** Кого звать, решает Director:
# на «текст поста» он может позвать одного Редактора, а может сперва
# Ресёрчера за фактурой. Как только здесь появится имя субагента, выбор
# workflow окажется размазан по двум слоям — см. шапку модуля.
MAX_THEMES = 30              # чтобы контракт не распухал на старом бренде

WORKFLOWS = {
    "plan":     "контент-план на неделю",
    "post":     "текст поста по теме",
    "reels":    "сценарий вертикального ролика",
    "research": "сводка недели и разбор статистики",
    "design":   "макет по готовому тексту",
    "idea":     "раскачать тему до заходов",
}


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
    said: str = ""                     # последняя реплика Director из JSON CLI
    artifacts: list[str] = field(default_factory=list)
    landed: str = ""                   # что из плана посажено в базу

    @property
    def dir(self) -> Path:
        return TASKS_DIR / self.task_id


# ── окружение ─────────────────────────────────────────────────────────

def clean_env() -> dict[str, str]:
    """Окружение подпроцесса. Белый список, а не наследование."""
    return {k: os.environ[k] for k in KEEP if k in os.environ}


def which_claude() -> str:
    """Путь к CLI. Сначала PATH, потом известные места установки.

    PATH под launchd беднее терминального, и на нём мост ломался целиком:
    см. `FALLBACK_BINS`. Возвращается абсолютный путь, поэтому подпроцессу
    хватает и голого PATH — искать себя ему уже не надо.
    """
    if found := shutil.which("claude"):
        return found
    for raw in FALLBACK_BINS:
        p = Path(raw).expanduser()
        if p.exists() and os.access(p, os.X_OK):
            log.info("claude найден мимо PATH: %s", p)
            return str(p)
    return ""


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
    """Стабильный id задачи: ГГГГ-ММ-ДД-workflow-NN.

    Занятыми считаются **и папка, и строка журнала**. Смотреть только на
    диск нельзя: `tasks/` лежит в `.gitignore`, папку переживает не всякая
    уборка и не переживает свежий клон, а `task_id` — это PRIMARY KEY.
    Свободный на диске номер, уже занятый в базе, ронял бы `INSERT`
    через `IntegrityError` — а из `on_plan` это исключение уходит наружу,
    и человек вместо ответа получает молчание.
    """
    n = 1
    while True:
        task_id = f"{today}-{workflow}-{n:02d}"
        if not (TASKS_DIR / task_id).exists() and not db.one(
                "SELECT 1 FROM bridge_runs WHERE task_id = ?", task_id):
            return task_id
        n += 1


def running() -> str:
    """id идущей задачи, пустая строка — свободно.

    Считается по всем чатам, а не по одному. `Busy` обещает «одна задача
    за раз», и фильтр по `chat_id` тихо превращал это в «одна на чат»:
    два чата подняли бы два процесса Claude Code по полчаса каждый, оба
    пишущие в один `tasks/` и одну базу. Сегодня тенант один, поэтому
    поймать это было негде — а MVP тем временем обещал не то, что делал.

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
        "SELECT task_id FROM bridge_runs WHERE status = 'running' "
        "AND started_at > datetime('now', ?) "
        "ORDER BY started_at DESC", f"-{TIMEOUT} seconds")
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


def _slots(chat_id: int) -> list[str]:
    """Окно плана и свободные слоты — фактом, а не заданием посчитать.

    Считает `strategy.free_slots`, то есть тот же код, которым пользуется
    старый Стратег. Без этого Director считал их сам: в progoне plan-05 —
    двумя запросами в базу мимо `orchestrator/strategy.py`. Ответ совпал,
    но это совпадение: правка `_window` или `_busy` развела бы два пути
    молча, а «границу держит код» — самый дорогой урок этого проекта.

    Это не выбор workflow и не указание, кого звать: свободный слот такой
    же факт задачи, как папка бренда. Решает, что с ним делать, Director.

    Отказ базы задачу не валит: без списка Director посчитает сам, как и
    раньше. Пустой план хуже неточного контракта.
    """
    try:
        window, free = strategy.free_slots(chat_id)
    except Exception as e:                       # noqa: BLE001 — см. докстринг
        log.warning("свободные слоты не посчитались: %s", e)
        return ["", "## Слоты", "",
                "Посчитать свободные слоты не удалось — посмотри сам, "
                "источник правды `orchestrator/strategy.py:free_slots`."]

    by_day: dict[str, list[str]] = {}
    for day, plat in free:
        by_day.setdefault(day, []).append(plat)

    out = ["", "## Окно плана и свободные слоты", "",
           f"Окно — семь дней начиная с завтра: {window[0]} — {window[-1]}.",
           "", "Свободные пары «дата плюс площадка»:", ""]
    out += [f"- {d}: {', '.join(p)}" for d, p in sorted(by_day.items())]
    out += ["", "Посчитано `strategy.free_slots` — тем же кодом, что у "
            "старого Стратега. Пересчитывать не надо: занятое сюда не "
            "попало. Слот вне этого списка в план не ставится."]
    return out


def _themes(chat_id: int) -> list[str]:
    """Незакрытые темы фактом: id, слот, формат, статус, заголовок.

    Тому же служит, что и `_slots` у плана: Редактор, Редактор Reels и
    Дизайнер работают «по теме», и без списка Director пойдёт спрашивать
    базу сам. Формат отдаётся колонкой, а не выводом — какая тема чья,
    решает Director по своим правилам, а не Python по формату строки.
    """
    rows = db.q("SELECT id, date, plat, format, status, title FROM themes "
                "WHERE chat_id = ? AND status IN ('idea', 'draft', 'ready') "
                "ORDER BY date, id", chat_id)
    if not rows:
        return ["", "## Темы", "",
                "Незакрытых тем в базе нет. Если задача требует темы, "
                "скажи об этом в final.md, а не выдумывай её."]

    out = ["", "## Незакрытые темы", "",
           "| id | дата | площадка | формат | статус | заголовок |",
           "|---|---|---|---|---|---|"]
    for r in rows[:MAX_THEMES]:
        title = (r["title"] or "").replace("|", "/")[:60]
        out.append(f"| `{r['id']}` | {r['date'] or ''} | {r['plat'] or ''} "
                   f"| {r['format'] or ''} | {r['status']} | {title} |")
    if len(rows) > MAX_THEMES:
        out.append("")
        out.append(f"Показаны первые {MAX_THEMES} из {len(rows)}.")
    out += ["", "Статусы: `idea` не начата, `draft` текст есть и не принят, "
            "`ready` принят. Тему берут по `id` — придумывать его не надо."]
    return out


def _nothing(chat_id: int) -> list[str]:
    """Workflow, которому фактов из базы не нужно."""
    return []


def _index(chat_id: int) -> list[str]:
    """Индекс профиля: путь и отпечаток, а не содержимое.

    Профиль читает не роль, а код: `research.profile_digest` нарезает
    `core.md`, `goals.md` и `platforms.md` и держит результат в папке
    бренда. Отпечаток считается по `stat()`, поэтому при живом кэше файлы
    профиля не читаются вовсе.

    В `input.md` уезжает путь, а не текст: индекс нужен Стратегу, а
    Director его не читает и платить за него в своём контексте не должен.
    """
    b = desk.brand(chat_id)
    if b is None:
        return ["", "## Индекс профиля", "",
                "Профиль бренда не собран. Роли работают на дефолтах и "
                "говорят об этом строкой."]
    try:
        dg = research.profile_digest(b)
    except Exception as e:                       # noqa: BLE001
        log.warning("индекс профиля не собрался: %s", e)
        return ["", "## Индекс профиля", "",
                f"Собрать индекс не удалось: {e}. Читай `core.md` сам."]

    out = ["", "## Индекс профиля", "",
           f"Файл: `{research.DIGEST_PATH}` в папке бренда",
           f"Отпечаток: `{dg.stamp}`",
           "",
           "Это то, что читают вместо `core.md`: собран кодом, "
           "пересобирается только при смене файлов профиля. Целиком "
           "`core.md` открывать не надо — в индексе уже нарезаны нужные "
           "секции. Исключение одно: «Голос бренда» и стоп-слова живут в "
           "`core.md`, оттуда их берут те, кто пишет текст."]
    if dg.missing:
        names = ", ".join(brand_store.PROFILE.get(k, k) for k in dg.missing)
        out += ["", f"Нет в профиле: {names}. Индекс говорит, чем это "
                    "заменено, и роль называет дыру строкой."]
    out.append(f"Пропорция воронки: {dg.ratio}"
               + (" — запасная, в профиле её нет."
                  if dg.backup else " — из `goals.md`, это правило бренда."))
    # Пересборка это факт о задаче, а не служебная деталь: план прошлой
    # недели стоял на другой версии профиля, и утверждённое тогда стоит
    # сверить, а не считать согласованным.
    out.append("Файлы профиля менялись: индекс пересобран только что."
               if dg.rebuilt else
               "Файлы профиля не менялись: индекс взят готовым, ни один из "
               "них не читался.")
    return out


def _digest(chat_id: int) -> list[str]:
    """Последняя сводка Ресёрчера: путь, неделя и покрывает ли она окно.

    Текст сюда не едет по той же причине, что и индекс: сводка нужна
    Стратегу, а не Director. Главное, что сообщается фактом, — покрывает
    ли последняя сводка прошлую неделю. Сводка за W33 в плане на W36 это
    не фактура, а археология, и решать это должен не тон промпта.
    """
    b = desk.brand(chat_id)
    if b is None:
        return []
    week, _ = research.latest(b)
    want = research.last_week()

    out = ["", "## Сводка Ресёрчера", ""]
    if not week:
        out += [f"Сводок в папке бренда нет вовсе, а нужна за {want.name} "
                f"({want.start} — {want.end}). Плана без фактуры это не "
                "запрещает, но строка об этом обязательна."]
        return out

    out.append(f"Последняя: `research/{week}.md` в папке бренда")
    if week == want.name:
        out += ["", f"Покрывает прошлую неделю ({want.start} — {want.end}). "
                    "Это свежая фактура: читай её и не пересобирай."]
    else:
        out += ["", f"Прошлая неделя это {want.name} ({want.start} — "
                    f"{want.end}), а последняя сводка за {week}. Свежей "
                    "фактуры нет: либо зови Ресёрчера, либо скажи строкой, "
                    "что план стоит на старой сводке, и назови её неделю."]
    return out


def _events(chat_id: int) -> list[str]:
    """События недели: вебинар, прогрев, запуск. Факт, а не догадка.

    Без этого файла Стратег планирует неделю, ничего не зная про вебинар
    в среду, и прогрев к нему не ставит. Спросить в headless-прогоне
    некого: конец хода Director это конец процесса, поэтому источник —
    файл, который человек ведёт сам.
    """
    b = desk.brand(chat_id)
    if b is None:
        return []
    text = ""
    path = b.path(EVENTS_PATH)
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()

    out = ["", "## События недели", ""]
    if not text:
        out += [f"`{EVENTS_PATH}` не заведён или пуст: событий на неделю не "
                "заявлено. Планируй без привязки к запуску и скажи об этом "
                "строкой — прогрев к вебинару, о котором ты не знаешь, "
                "поставить нельзя."]
        return out
    out += [f"Из `{EVENTS_PATH}` папки бренда:", ""]
    out += [ln for ln in text.splitlines() if ln.strip()]
    out += ["", "Это заявленные человеком события. Слот под прогрев ставится "
                "от них, а даты сверяются со списком свободных слотов выше."]
    return out


# Какие факты кладутся в `input.md` под какой workflow. Факты, не роли:
# свободный слот и незакрытая тема — это данные задачи, как папка бренда.
CONTEXT = {
    "plan":     (_slots, _index, _digest, _events),
    "post":     (_themes, _index),
    "reels":    (_themes, _index),
    "design":   (_themes, _index),
    "idea":     (_themes, _index, _digest),
    "research": (_index,),
}


def create_task(chat_id: int, ask: str, *, workflow: str, today: str,
                brand_slug: str = "", brand_path: str = "") -> str:
    """Завести папку задачи и `input.md`. Возвращает task_id.

    `input.md` это всё, что Director получает от Python. Никаких указаний,
    каких субагентов звать, здесь нет и быть не должно.
    """
    if busy := running():
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
        # Путь нормализуется здесь, а не у вызывающего. В прогоне plan-05
        # сюда приехало `…/content-factory/../content-factory-brands/…`:
        # рабочий путь, но настолько неопрятный, что Director им не
        # воспользовался и пошёл угадывать относительный — промахнулся и
        # потратил лишний ход. Факт в контракте должен выглядеть как факт.
        lines.append(f"Папка бренда: `{Path(brand_path).resolve()}`")
    for build in CONTEXT.get(workflow, (_nothing,)):
        lines += build(chat_id)
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
    binary = which_claude()
    if not binary:
        return _fail(res, "бинарь claude не найден: ни в PATH, ни в "
                          + ", ".join(FALLBACK_BINS))

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
            harvest(res)
            return _fail(res, f"не уложился в {TIMEOUT} с", status="timeout")
    except OSError as e:
        return _fail(res, f"не удалось запустить процесс: {e}")

    res.secs = round(time.monotonic() - started, 1)
    harvest(res)
    stdout = out.decode("utf-8", "replace").strip()
    stderr = err.decode("utf-8", "replace").strip()

    # Диагностика из JSON-вывода. Её отсутствие не делает задачу проваленной:
    # результат лежит в файле, а не в stdout.
    if stdout:
        try:
            data = json.loads(stdout)
            res.cost = data.get("total_cost_usd")
            res.session_id = str(data.get("session_id") or "")
            res.said = str(data.get("result") or "").strip()
        except json.JSONDecodeError:
            log.warning("%s: stdout не разобрался как JSON", task_id)

    if proc.returncode != 0:
        return _fail(res, _reason(stdout, stderr, proc.returncode, res.said))

    final = res.dir / "final.md"
    if not final.exists():
        # Проглотить последнюю реплику Director нельзя. Первый живой прогон
        # (2026-08-30-plan-03) кончился ровно здесь: он запустил субагента
        # **в фоне** и закончил ход словами «подхвачу, когда закончит». В
        # headless-режиме конец хода это конец процесса, фоновый субагент
        # умирает вместе с ним. 274 секунды и почти доллар — а причину
        # пришлось искать в транскрипте сессии, потому что мост сказал
        # только «файла нет». Теперь он говорит и почему.
        why = "Claude Code отработал, но final.md не появился"
        if res.said:
            why += f". Director напоследок сказал: {res.said[:400]}"
        return _fail(res, why)

    res.text = for_telegram(final.read_text(encoding="utf-8").strip())
    if not res.text:
        return _fail(res, "final.md пустой")

    if res.landed:
        res.text += "\n\n" + for_telegram(res.landed)

    res.ok = True
    res.artifacts = sorted(p.name for p in res.dir.glob("*.md"))
    _finish(res, "done")
    log.info("задача %s готова за %s с, артефактов %s",
             task_id, res.secs, len(res.artifacts))
    return res


JSON_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def harvest(res: Result) -> str:
    """Посадить собранный субагентом план в состояние завода.

    До этого результат нового пути не возвращался в завод вовсе: субагент
    писал `strategy.md`, папка задачи лежала в `.gitignore`, и следующий
    прогон видел те же слоты свободными. Хуже того, слот на выходе не
    проверял никто — `strategy._fit` живёт внутри старого `build`, куда
    мост не заходит. Дата вне окна и второй пост в тот же день доезжали до
    человека как рабочий план.

    Проверку и запись делает `strategy.land` — тот же код, которым сажает
    план старый путь. Дом у правила один.

    Идёт **всегда**, а не только при удаче: прогон 2026-08-31-plan-01 упёрся
    в потолок на Идеаторе, а `strategy.md` лежал готовым с 00:18. Работа,
    которую выбросили из-за таймаута на следующем шаге, оплачена полностью.

    Возвращает строку для человека; пусто — сажать было нечего.
    """
    row = db.one("SELECT chat_id, workflow FROM bridge_runs WHERE task_id = ?",
                 res.task_id)
    if not row or row["workflow"] != "plan":
        return ""

    art = res.dir / "strategy.md"
    if not art.exists():
        return ""

    m = None
    for m in JSON_FENCE.finditer(art.read_text(encoding="utf-8")):
        pass                        # берём последний блок: он машинный контракт
    if m is None:
        log.warning("%s: в strategy.md нет блока json", res.task_id)
        return "План собран, но машинный контракт в нём не найден — в базу не записан."

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        log.warning("%s: контракт Стратега не разобрался: %s", res.task_id, e)
        return "План собран, но его машинный контракт не разобрался — в базу не записан."

    try:
        plan, saved, rel = strategy.land(row["chat_id"], data)
    except Exception as e:                       # noqa: BLE001
        log.warning("%s: план не сел: %s", res.task_id, e)
        return f"План собран, но в базу не сел: {e}"

    note = [f"Записано тем: {len(saved)}. Выгрузка — {rel}."]
    if plan.unmet:
        note.append("Не сошлось: " + "; ".join(plan.unmet[:4]))
    res.landed = " ".join(note)
    log.info("%s: посажено тем %s, отброшено %s",
             res.task_id, len(saved), len(plan.unmet))
    return res.landed


def _reason(stdout: str, stderr: str, rc: int, said: str = "") -> str:
    """Человеческая причина отказа вместо кода возврата.

    Через неё проходит всё, что вернул CLI, поэтому «войди в CLI» человек
    видит одинаково, чем бы вызов ни упал.

    `said` — разобранное поле `result` из JSON CLI, то есть уже готовая
    человеческая строка. Раньше она игнорировалась, и в запасной ветке
    человеку уезжал **весь блок JSON**: при лимите сессии он получал в
    Telegram `{"type":"result","subtype":"error_during_execution",…}`
    вместо «упёрлись в лимит». Замерено живьём 30.08 — это самый вероятный
    отказ на подписке, и он единственный не имел своей ветки.

    Лимит подписки отделён от баланса намеренно. Денег новый путь не
    тратит вовсе (раздел 11 миграции), и строка «закончились средства»
    отправила бы человека пополнять баланс, которому ничего не грозит.
    """
    blob = f"{stdout}\n{stderr}".lower()
    if "not logged in" in blob or "/login" in blob:
        return ("Claude Code не авторизован. Один раз выполни `claude` и "
                "`/login` в терминале")
    if "credit balance" in blob or "insufficient" in blob:
        return "закончились средства или исчерпан лимит"
    if "session limit" in blob or "usage limit" in blob:
        # Время сброса CLI кладёт в ту же строку — отдаём как есть.
        why = "упёрлись в лимит подписки Claude Code (не в баланс API)"
        return f"{why}. {said}" if said else why
    if "rate limit" in blob or "429" in blob:
        return "упёрлись в лимит запросов, попробуй позже"
    return (said or stderr or stdout or f"процесс вернул код {rc}")[:300]


def _fail(res: Result, why: str, *, status: str = "failed") -> Result:
    """Записать провал — вместе с тем, что всё-таки легло на диск.

    Прогон 2026-08-30-plan-04 упёрся в потолок на Стратеге, а `research.md`
    к тому моменту уже лежал: девять минут живых запросов. Человек об этом
    не узнал — мост сказал только «не уложился». Работа, которую не назвали,
    неотличима от несделанной, и её закажут заново.
    """
    res.ok, res.error = False, why
    if res.dir.exists():
        res.artifacts = sorted(p.name for p in res.dir.glob("*.md"))
    done = [a for a in res.artifacts if a != "input.md"]
    if done:
        res.error += ". Успело лечь на диск: " + ", ".join(done)
    if res.landed:
        res.error += ". " + res.landed
    _finish(res, status)
    log.error("задача %s: %s", res.task_id, res.error)
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
