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
семи ботов: Telegram начал бы отдавать обновления другому инстансу, и
мы получили бы `Conflict` на ровном месте.

Почему окружение собирается белым списком. `config.py` зовёт
`load_dotenv()`, поэтому в `os.environ` процесса бота лежат ключ API и
токены семи ботов. Унаследованное окружение отдало бы их подпроцессу.
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
from collections.abc import Awaitable, Callable
from typing import Any

from config import ROOT
from orchestrator import cli, design, desk, editor, research, strategy
from storage import brand as brand_store
from storage import db

log = logging.getLogger("bridge")

KEEP = cli.KEEP
FALLBACK_BINS = cli.FALLBACK_BINS
clean_env = cli.clean_env
which_claude = cli.which_claude

TASKS_DIR = ROOT / "tasks"

# События недели: вебинар, прогрев, запуск. Ведёт человек, читает Стратег.
EVENTS_PATH = "plans/events.md"

# Чем человек отвечает «событий нет». Ответ всё равно пишется в файл: штамп
# с датами окна и есть ответ на вопрос «про эту неделю уже спрашивали».
NO_EVENTS = {"нет", "ничего", "нету", "нет событий", "пусто", "-", "—"}

# Окружение подпроцесса, поиск бинаря и разбор отказов живут в
# `orchestrator/cli.py`: тем же пользуется роль, и две копии этой плиты
# разъехались бы на первой же починке.
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
    landed: str = ""                   # что посажено в базу, строкой человеку
    plan_ids: list[str] = field(default_factory=list)   # темы плана
    post_ids: list[str] = field(default_factory=list)   # темы с текстом
    design_ids: list[str] = field(default_factory=list)  # темы с макетом
    landed_obj: Any = None             # макет, который показывает Дизайнер

    @property
    def dir(self) -> Path:
        return TASKS_DIR / self.task_id

    @property
    def landed_ids(self) -> list[str]:
        """Все посаженные темы. Кнопки выбираются по спискам выше.

        Что посажено, знает только посадка, и разное сажается разными
        кнопками: под планом «Утвердить», под текстом «В дизайн».
        Один общий список годится для «сажали ли вообще», не больше.
        """
        return self.plan_ids + self.post_ids + self.design_ids


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
    """Незакрытые темы фактом: id, слот, формат, рубрика, статус, заголовок.

    Тому же служит, что и `_slots` у плана: Редактор, Редактор Reels и
    Дизайнер работают «по теме», и без списка Director пойдёт спрашивать
    базу сам. Формат отдаётся колонкой, а не выводом — какая тема чья,
    решает Director по своим правилам, а не Python по формату строки.

    Рубрика едет тем же фактом. Старый путь её отдаёт (`design._brief`),
    мост не отдавал — и Дизайнер в задаче `2026-09-01-design-01` написал,
    что списка рубрик нет, и поставил её по смыслу поста. Совпало почти
    дословно («ИНСТРУМЕНТ» против «Инструмент недели»), но это везение:
    рубрика лежала в базе и её просто не показали.
    """
    rows = db.q("SELECT id, date, plat, format, rubric, status, title "
                "FROM themes "
                "WHERE chat_id = ? AND status IN ('idea', 'draft', 'ready') "
                "ORDER BY date, id", chat_id)
    if not rows:
        return ["", "## Темы", "",
                "Незакрытых тем в базе нет. Если задача требует темы, "
                "скажи об этом в final.md, а не выдумывай её."]

    out = ["", "## Незакрытые темы", "",
           "| id | дата | площадка | формат | рубрика | статус | заголовок |",
           "|---|---|---|---|---|---|---|"]
    for r in rows[:MAX_THEMES]:
        title = (r["title"] or "").replace("|", "/")[:60]
        rubric = (r["rubric"] or "").replace("|", "/")[:40]
        out.append(f"| `{r['id']}` | {r['date'] or ''} | {r['plat'] or ''} "
                   f"| {r['format'] or ''} | {rubric} | {r['status']} "
                   f"| {title} |")
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


def _history(chat_id: int) -> list[str]:
    """Недавние темы и невышедшее — фактом из базы, а не поиском по папке.

    Без этого субагент шарил `Glob` по `posts/` и `plans/`: два круга к
    модели ради того, что `strategy.archive` отдаёт запросом. Хуже цены
    то, что ответы расходятся — файл поста остаётся лежать после того,
    как тему сняли, а `themes` знает её статус.
    """
    try:
        recent, left = strategy.archive(chat_id)
    except Exception as e:                       # noqa: BLE001
        log.warning("архив не собрался: %s", e)
        return []

    out = ["", "## Архив и невышедшее", ""]
    if recent:
        out += ["Недавние темы — повторять их нельзя:", ""]
        out += [f"- {r}" for r in recent]
    else:
        out.append("Архив пуст: это первая неделя бренда.")
    if left:
        out += ["", "Осталось невышедшим — это первые кандидаты в новую "
                    "неделю, а не мусор:", ""]
        out += [f"- {x}" for x in left]
    out += ["", "Посчитано `strategy.archive` по базе. Искать архив в "
                "`posts/` и `plans/` не надо: файл остаётся лежать и после "
                "того, как тему сняли, а статус знает база."]
    return out


def _events_block(text: str, chat_id: int) -> str:
    """Кусок `events.md` про это окно, а не файл целиком.

    Файл ведётся неделями и растёт: прошлый вебинар, попав в промпт вместе
    с нынешним, читается как ещё одно событие недели. Блок выбирается по
    дате из окна в заголовке; не нашли — отдаём файл как есть, потому что
    человек мог написать его свободной формой.
    """
    try:
        window = plan_window(chat_id)
    except Exception:                            # noqa: BLE001
        return text.strip()
    blocks = re.split(r"^(?=##\s)", text, flags=re.M)
    mine = [b.strip() for b in blocks
            if any(day in b.split("\n", 1)[0] for day in window)]
    return "\n\n".join(mine) if mine else text.strip()


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
        text = _events_block(b.path(EVENTS_PATH).read_text(encoding="utf-8"),
                             chat_id)

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


# ── события недели ────────────────────────────────────────────────────
#
# Спросить человека в момент прогона нельзя: мост это headless-запуск, и
# конец хода Director это конец процесса. Поэтому вопрос задаётся **до**
# запуска, обычным сообщением в чате, а ответ ложится в файл, который
# Стратег получает фактом в `input.md`.

def plan_window(chat_id: int) -> list[str]:
    """Окно плана датами. То же окно, что у слотов: дом правила один."""
    window, _ = strategy.free_slots(chat_id)
    return [d.isoformat() for d in window]


def events_known(chat_id: int) -> bool:
    """Заявлены ли события на это окно.

    Не «есть ли файл»: файл с прошлой недели ничего не говорит про эту, а
    Стратег, не знающий про вебинар в среду, прогрев к нему не поставит.
    Признак свежести — дата из окна, встреченная в тексте. Ответ «событий
    нет» пишется тем же кодом со штампом окна, поэтому второй раз про ту
    же неделю не спрашиваем.
    """
    b = desk.brand(chat_id)
    if b is None:
        return True                  # нет бренда — спрашивать не о чем
    path = b.path(EVENTS_PATH)
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return False
    try:
        window = plan_window(chat_id)
    except Exception:                            # noqa: BLE001
        return True                  # окно не посчиталось — не мучаем человека
    return any(day in text for day in window)


def events_question(chat_id: int) -> str:
    """Вопрос человеку перед планом. Окно называется датами, а не «неделей»."""
    try:
        window = plan_window(chat_id)
        span = f"{window[0]} — {window[-1]}"
    except Exception:                            # noqa: BLE001
        span = "ближайшую неделю"
    return ("Прежде чем планировать: что на неделе " + span + "?\n"
            "Вебинар, запуск, дедлайн, эфир — с датой. Под них ставится "
            "прогрев, а сам я о них не знаю.\n\n"
            "Ответь одним сообщением или напиши «нет».")


def save_events(chat_id: int, answer: str) -> bool:
    """Записать события окна в `plans/events.md`. Возвращает True, если есть что.

    Пишется всегда, включая «нет»: штамп с датами окна — это и есть ответ
    на вопрос «спрашивали ли уже про эту неделю».
    """
    b = desk.brand(chat_id)
    if b is None:
        return False
    try:
        window = plan_window(chat_id)
    except Exception:                            # noqa: BLE001
        return False

    said = (answer or "").strip()
    none = said.lower().strip(".!… ") in NO_EVENTS
    block = [f"## Неделя {window[0]} — {window[-1]}", ""]
    block += ["- событий не заявлено"] if none else \
             [ln if ln.startswith("-") else f"- {ln}"
              for ln in said.splitlines() if ln.strip()]
    b.append(EVENTS_PATH, "\n" + "\n".join(block) + "\n")
    return not none



# Какие факты кладутся в `input.md` под какой workflow. Факты, не роли:
# свободный слот и незакрытая тема — это данные задачи, как папка бренда.
CONTEXT = {
    "plan":     (_slots, _index, _digest, _events, _history),
    "post":     (_themes, _index),
    "reels":    (_themes, _index),
    "design":   (_themes, _index),
    "idea":     (_themes, _index, _digest, _history),
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


# Одно событие — одна строка stdout. Строка бывает длинной: `init` со
# списком инструментов уже около пяти килобайт.
EVENT_LIMIT = 8 * 1024 * 1024

# Как зовут субагентов человеку. Ключ — имя из `.claude/agents/`.
# Незнакомое имя показывается как есть: новый субагент лучше появится в
# чате безымянным, чем пропадёт из отчёта совсем.
AGENT_NAMES = {
    "researcher": "Ресёрчер",
    "strategist": "Стратег",
    "ideator": "Идеатор",
    "writer": "Редактор",
    "reels": "Редактор Reels",
    "designer": "Дизайнер",
}

# Куда сообщать о ходе прогона. Возвращать ничего не надо, упасть — нельзя:
# прогресс это удобство, а не работа, и ронять из-за него задачу незачем.
StepCb = Callable[[str], Awaitable[None]]


def _agent_of(block: dict[str, Any]) -> tuple[str, str]:
    """Из блока `tool_use` — (id вызова, имя субагента). Не субагент — («», «»).

    Имя ведём по id, а не по порядку: субагентов можно звать несколькими
    в одном ходу, и тогда «закончил» без id приписывается не тому.
    """
    if block.get("type") != "tool_use":
        return "", ""
    if block.get("name") not in ("Task", "Agent"):
        return "", ""
    raw = str((block.get("input") or {}).get("subagent_type") or "").strip()
    if not raw:
        return "", ""
    return str(block.get("id") or ""), AGENT_NAMES.get(raw, raw)


async def _stream(stdout: asyncio.StreamReader, res: Result,
                  on_step: StepCb | None) -> str:
    """Прочитать поток событий CLI. Возвращает сырой stdout для диагностики.

    Раньше мост ждал процесс молча до получаса и брал из него один JSON в
    конце. Человек в чате видел «займёт до минуты» и тишину, а мы —
    единственную цифру, длительность всего прогона: на вопрос «где ушли
    четыре минуты из пяти» ответить было нечем.

    Здесь разбирается три вида событий и игнорируется всё остальное:

      assistant  → вызовы субагентов, из них строится прогресс и тайминги
      result     → итог: цена, сессия, последняя реплика Director
      прочее     → системная инициализация, лимиты, результаты инструментов

    Разбор ничего не решает: `final.md` по-прежнему единственный контракт.
    Событие, которое не разобралось, — строка в логе, а не провал задачи.
    """
    raw: list[str] = []
    started: dict[str, tuple[str, float]] = {}    # id вызова → (имя, когда)

    async def tell(text: str) -> None:
        if on_step is None:
            return
        try:
            await on_step(text)
        except Exception as e:                    # noqa: BLE001
            # Не дошло сообщение о ходе работы — сама работа не при чём.
            log.warning("%s: прогресс не ушёл: %s", res.task_id, e)

    while True:
        try:
            line = await stdout.readline()
        except (ValueError, asyncio.LimitOverrunError):
            # Строка длиннее буфера. Событие потеряно, прогон — нет.
            log.warning("%s: событие длиннее %s байт, пропущено",
                        res.task_id, EVENT_LIMIT)
            continue
        if not line:
            break

        text = line.decode("utf-8", "replace").rstrip()
        if not text:
            continue
        raw.append(text)

        try:
            ev = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue

        kind = ev.get("type")

        if kind == "assistant":
            for blk in (ev.get("message") or {}).get("content") or []:
                if not isinstance(blk, dict):
                    continue
                call_id, name = _agent_of(blk)
                if name:
                    started[call_id] = (name, time.monotonic())
                    log.info("%s: пошёл %s", res.task_id, name)
                    await tell(f"{name} взялся за работу.")

        elif kind == "user":
            for call_id in _results_in(ev):
                if call_id not in started:
                    continue                      # обычный инструмент, не роль
                name, at = started.pop(call_id)
                secs = round(time.monotonic() - at)
                log.info("%s: %s закончил за %s с", res.task_id, name, secs)
                await tell(f"{name} закончил, {secs} с.")

        elif kind == "result":
            res.cost = ev.get("total_cost_usd")
            res.session_id = str(ev.get("session_id") or "")
            res.said = str(ev.get("result") or "").strip()
            if stats := ev.get("subagent_stats"):
                log.info("%s: субагентов запущено %s, завершилось %s",
                         res.task_id, stats.get("spawned"),
                         stats.get("completed"))

    return "\n".join(raw)


def _results_in(ev: dict[str, Any]) -> list[str]:
    """Id вызовов, чьи результаты приехали этим событием `user`."""
    out = []
    for blk in (ev.get("message") or {}).get("content") or []:
        if isinstance(blk, dict) and blk.get("type") == "tool_result":
            if call_id := str(blk.get("tool_use_id") or ""):
                out.append(call_id)
    return out


async def run(task_id: str, on_step: StepCb | None = None) -> Result:
    """Запустить Claude Code на задаче и дождаться результата.

    `on_step` зовётся по ходу прогона: «пошёл Ресёрчер», «Ресёрчер
    закончил». Не передан — мост работает ровно как раньше, молча.
    """
    res = Result(task_id=task_id)
    binary = which_claude()
    if not binary:
        return _fail(res, "бинарь claude не найден: ни в PATH, ни в "
                          + ", ".join(FALLBACK_BINS))

    # `--verbose` тут не про болтливость: без него CLI отказывается отдавать
    # поток событий вместе с `-p`. Формат `stream-json` — тот же результат,
    # что и `json`, но строкой на событие, и последняя строка это `result`.
    argv = [binary, "-p", _prompt(task_id), "--allowedTools", TOOLS,
            "--permission-mode", "acceptEdits",
            "--output-format", "stream-json", "--verbose"]

    # Цифры снимаются до старта: три с половиной секунды сети здесь дешевле
    # восьми ходов модели на разведку там.
    await snapshot(task_id)

    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(ROOT), env=clean_env(),
            # Без DEVNULL CLI три секунды ждёт данных на stdin и говорит об
            # этом предупреждением. Ждать нечего: задача уже лежит в argv.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            # Одно событие это одна строка, и строка бывает длинной: init со
            # списком инструментов уже пять килобайт, ответ роли — сотни.
            # Дефолтные 64 КБ рвут разбор ровно на самой интересной задаче.
            limit=EVENT_LIMIT)

        # stdout читаем построчно, stderr — целиком и параллельно. Иначе
        # полный буфер stderr остановит процесс, а мы будем ждать его stdout.
        events = asyncio.create_task(_stream(proc.stdout, res, on_step))
        errors = asyncio.create_task(proc.stderr.read())
        try:
            await asyncio.wait_for(
                asyncio.gather(events, errors, proc.wait()), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            events.cancel()
            errors.cancel()
            proc.kill()
            await proc.wait()
            res.secs = round(time.monotonic() - started, 1)
            await harvest(res)
            return _fail(res, f"не уложился в {TIMEOUT} с", status="timeout")
        except Exception as e:                    # noqa: BLE001
            # Разбор потока не должен уносить задачу трейсбеком в чат:
            # работа могла удаться и лежать на диске. Гасим процесс,
            # собираем что есть и говорим человеку словами.
            events.cancel()
            errors.cancel()
            proc.kill()
            await proc.wait()
            res.secs = round(time.monotonic() - started, 1)
            await harvest(res)
            log.exception("%s: разбор потока сорвался", task_id)
            return _fail(res, f"поток событий не разобрался: {e}")
    except OSError as e:
        return _fail(res, f"не удалось запустить процесс: {e}")

    res.secs = round(time.monotonic() - started, 1)
    await harvest(res)
    stdout = events.result()
    stderr = errors.result().decode("utf-8", "replace").strip()

    if proc.returncode != 0:
        return _fail(res, cli.reason(stdout, stderr, proc.returncode, res.said))

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
    res.artifacts = _artifacts(res)
    _finish(res, "done")
    log.info("задача %s готова за %s с, артефактов %s",
             task_id, res.secs, len(res.artifacts))
    return res


STATS_FILE = "stats.md"

# Что Python кладёт в папку задачи сам. В список артефактов это не идёт:
# человек читает его как «что отдали роли», а не «что лежит в папке».
OWN_FILES = ("input.md", STATS_FILE)

# Каким workflow цифры нужны фактом. Сводка и план стоят на них целиком,
# «раскачай тему» опирается на охват — остальным они не нужны, и снимать
# их значило бы платить временем за то, что никто не прочтёт.
NEEDS_STATS = ("research", "plan", "idea")


async def snapshot(task_id: str) -> str:
    """Снять цифры в `tasks/{id}/stats.md` и дописать адрес в `input.md`.

    Раньше Ресёрчеру не кладли ничего, и он добывал цифры сам: искал
    `sources.fetch` в коде, писал скрипт, гонял, правил. Восемь ходов
    модели на работу, которая занимает три с половиной секунды сети — и
    каждый ход перечитывает накопленный контекст.

    Поэтому цифры снимает Python **до** запуска подпроцесса: сеть уходит
    с часов модели совсем. Считает `research.snapshot`, то есть `measure`
    и `last_week` — тот же код, что у старого Ресёрчера.

    Файл отдельный, а не раздел `input.md`: цифры нужны Ресёрчеру, а
    Director их не читает и платить за них своим контекстом не должен.
    Это второй писатель `input.md` после `create_task`, и оба — Python.
    Провенанс от этого не мутнеет: Director по-прежнему не пишет туда
    ничего.

    Сеть упала — задача не валится: дыра называется строкой, и роль
    работает на том, что есть.
    """
    row = db.one("SELECT chat_id, workflow FROM bridge_runs WHERE task_id = ?",
                 task_id)
    if not row or row["workflow"] not in NEEDS_STATS:
        return ""

    b = desk.brand(row["chat_id"])
    if b is None:
        return ""

    try:
        text, gaps = await research.snapshot(b)
    except Exception as e:                       # noqa: BLE001
        log.warning("%s: цифры не снялись: %s", task_id, e)
        gaps = [f"снять цифры не удалось: {e}"]
        text = ""

    d = TASKS_DIR / task_id
    d.mkdir(parents=True, exist_ok=True)
    if text:
        (d / STATS_FILE).write_text(
            "# Цифры за окно\n\n"
            "Сняты кодом завода (`research.snapshot`) до запуска задачи. "
            "Пересчитывать их не надо и спорить с ними нечем: у арифметики "
            "один дом.\n\n" + text + "\n", encoding="utf-8")

    out = ["", "## Цифры сняты", ""]
    if text:
        out += [f"Своя статистика и внешний срез — в `tasks/{task_id}/"
                f"{STATS_FILE}`. Окно, медианы и покрытие посчитаны кодом; "
                "лент заново не читай и медианы сам не считай.", ""]
    else:
        out += ["Цифры снять не удалось, файла нет.", ""]
    if gaps:
        out += ["Чего в цифрах нет:", ""] + [f"- {g}" for g in gaps]

    inp = d / "input.md"
    if inp.exists():
        inp.write_text(inp.read_text(encoding="utf-8").rstrip() + "\n"
                       + "\n".join(out) + "\n", encoding="utf-8")
    log.info("%s: цифры сняты, дыр %s", task_id, len(gaps))
    return "\n".join(out)


JSON_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def _contract(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Машинный контракт из артефакта роли. Ошибку называем, а не глотаем.

    Берётся последний блок ```json в файле: перед ним лежит проза для
    человека, и в ней могут быть свои примеры.
    """
    m = None
    for m in JSON_FENCE.finditer(path.read_text(encoding="utf-8")):
        pass
    if m is None:
        return None, "машинный контракт в нём не найден"
    try:
        return json.loads(m.group(1)), ""
    except json.JSONDecodeError as e:
        log.warning("%s: контракт не разобрался: %s", path.name, e)
        return None, "его машинный контракт не разобрался"


def _post_files(res: Result) -> list[Path]:
    """Артефакты Редактора. Их может быть несколько: тема на площадку.

    `final.md` не берётся: это письмо человеку, а не артефакт роли. Пока
    Редактор писал текст прямо туда, посадить его было нельзя — рядом с
    прозой Director контракт роли не отличить от пересказа.
    """
    return sorted(p for p in res.dir.glob("post*.md") if p.is_file())


def _land_posts(res: Result, chat_id: int) -> str:
    """Посадить тексты Редактора: файл в бренд, тема в `draft`.

    Проверку и запись делает `editor.land` — тот же валидатор голоса и та
    же запись, что у старого Редактора. Отказ валидатора это отказ: тема
    остаётся `idea`, и человек слышит почему, а не получает кнопку
    «Ок» под текстом, которого в заводе нет.
    """
    files = _post_files(res)
    if not files:
        return ""

    landed, notes = [], []
    for path in files:
        data, why = _contract(path)
        if data is None:
            notes.append(f"{path.name}: {why} — в базу не записан")
            continue
        try:
            draft, rel = editor.land(chat_id, data)
        except Exception as e:                   # noqa: BLE001
            log.warning("%s: текст не сел: %s", res.task_id, e)
            notes.append(f"текст не сел: {e}")
            continue
        landed.append(draft.theme["id"])
        notes.append(f"Текст по теме {draft.theme['id']} записан "
                     f"в {rel}, тема в статусе draft.")
        if draft.notes:
            notes.append("Редактор пометил: " + "; ".join(draft.notes[:3]))

    res.post_ids = landed
    res.landed = " ".join(notes)
    log.info("%s: посажено текстов %s из %s",
             res.task_id, len(landed), len(files))
    return res.landed


async def _land_design(res: Result, chat_id: int) -> str:
    """Посадить макет Дизайнера: файлы в папку бренда, PNG рядом.

    Асинхронна из-за рендера: PNG снимает headless Chrome, и ждать его
    приходится по-настоящему. Из-за неё асинхронен и весь `harvest`.

    Показ человеку сюда не входит: файлы кладёт мост, а картинки с
    кнопками отдаёт `design.show` — тот же код, которым показывает старый
    Дизайнер. Иначе у показа завелась бы вторая копия.
    """
    art = res.dir / "design.md"
    if not art.exists():
        return ""

    data, why = _contract(art)
    if data is None:
        return f"Макет собран, но {why} — в папку бренда не записан."

    try:
        lay = await design.land(chat_id, data)
    except Exception as e:                       # noqa: BLE001
        log.warning("%s: макет не сел: %s", res.task_id, e)
        return f"Макет собран, но не сел: {e}"

    res.landed_obj = lay
    res.design_ids = [lay.theme["id"]]
    note = [f"Макет по теме {lay.theme['id']}: {len(lay.pngs)} PNG и "
            f"{len(lay.htmls)} HTML в posts/ папки бренда."]
    if lay.findings:
        note.append("Проверка поймала: " + "; ".join(lay.findings[:3]))
    res.landed = " ".join(note)
    return res.landed


def _land_plan(res: Result, chat_id: int) -> str:
    """Посадить план Стратега: темы в базу, выгрузка в папку бренда.

    Проверку и запись делает `strategy.land` — тот же код, которым сажает
    план старый путь. Дом у правила один.
    """
    art = res.dir / "strategy.md"
    if not art.exists():
        return ""

    data, why = _contract(art)
    if data is None:
        return f"План собран, но {why} — в базу не записан."

    try:
        plan, saved, rel = strategy.land(chat_id, data)
    except Exception as e:                       # noqa: BLE001
        log.warning("%s: план не сел: %s", res.task_id, e)
        return f"План собран, но в базу не сел: {e}"

    res.plan_ids = [t["id"] for t in saved]
    note = [f"Записано тем: {len(saved)}. Выгрузка — {rel}."]
    if plan.unmet:
        note.append("Не сошлось: " + "; ".join(plan.unmet[:4]))
    log.info("%s: посажено тем %s, отброшено %s",
             res.task_id, len(saved), len(plan.unmet))
    return " ".join(note)


async def harvest(res: Result) -> str:
    """Посадить собранное субагентом в состояние завода.

    До этого результат нового пути не возвращался в завод вовсе: субагент
    писал `strategy.md`, папка задачи лежала в `.gitignore`, и следующий
    прогон видел те же слоты свободными. Хуже того, слот на выходе не
    проверял никто — `strategy._fit` живёт внутри старого `build`, куда
    мост не заходит. Дата вне окна и второй пост в тот же день доезжали до
    человека как рабочий план.

    **Что сажать, решают файлы на диске, а не объявленный workflow.**
    Минимальный workflow это работа Director, и он вправе свернуть план до
    одного текста: тема уже стоит в базе статусом `idea`, Стратег не
    нужен. Прогон 2026-08-31-plan-04 так и прошёл — шапка задачи говорила
    `plan`, реально отработал один Редактор, — и посадка, разбиравшая
    объявленный workflow, не нашла `strategy.md` и молча вышла. Готовый
    текст восемь минут пролежал в `tasks/`, который в `.gitignore`.

    Файл на диске это факт: `strategy.md` значит Стратег отработал,
    `post*.md` — Редактор, `design.md` — Дизайнер. Разбирать надо факт.

    Идёт **всегда**, а не только при удаче: прогон 2026-08-31-plan-01 упёрся
    в потолок на Идеаторе, а `strategy.md` лежал готовым с 00:18. Работа,
    которую выбросили из-за таймаута на следующем шаге, оплачена полностью.

    Возвращает строку для человека; пусто — сажать было нечего.
    """
    row = db.one("SELECT chat_id FROM bridge_runs WHERE task_id = ?",
                 res.task_id)
    if not row:
        return ""
    chat_id = row["chat_id"]

    notes = [_land_plan(res, chat_id), _land_posts(res, chat_id),
             await _land_design(res, chat_id)]

    res.landed = " ".join(n for n in notes if n)
    return res.landed


def _fail(res: Result, why: str, *, status: str = "failed") -> Result:
    """Записать провал — вместе с тем, что всё-таки легло на диск.

    Прогон 2026-08-30-plan-04 упёрся в потолок на Стратеге, а `research.md`
    к тому моменту уже лежал: девять минут живых запросов. Человек об этом
    не узнал — мост сказал только «не уложился». Работа, которую не назвали,
    неотличима от несделанной, и её закажут заново.
    """
    res.ok, res.error = False, why
    if res.dir.exists():
        res.artifacts = _artifacts(res)
    if res.artifacts:
        res.error += ". Успело лечь на диск: " + ", ".join(res.artifacts)
    if res.landed:
        res.error += ". " + res.landed
    _finish(res, status)
    log.error("задача %s: %s", res.task_id, res.error)
    return res


def _artifacts(res: Result) -> list[str]:
    """Что отдали роли. Свои файлы Python сюда не приписывает."""
    return sorted(p.name for p in res.dir.glob("*.md")
                  if p.name not in OWN_FILES)


def _finish(res: Result, status: str) -> None:
    """Записать исход. `cost` это диагностика CLI, а не факт списания."""
    with db.tx() as c:
        c.execute(
            "UPDATE bridge_runs SET status = ?, finished_at = datetime('now'),"
            " duration_s = ?, estimated_api_cost = ?, session_id = ?,"
            " error = ? WHERE task_id = ?",
            (status, res.secs, res.cost, res.session_id or None,
             res.error or None, res.task_id))
