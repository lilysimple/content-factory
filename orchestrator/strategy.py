"""Стратег: из профиля и архива в план недели.

Роль отдаёт структуру, а не текст, поэтому разговаривает JSON-ом: карточку
для чата рисует код. Так план можно положить в базу целиком, а не разбирать
обратно из сообщения, которое человек уже успел проскроллить.

Источник правды по плану это база (журнал решений, 13.08). Файл
`plans/ГГГГ-Wnn.md` в папке бренда пишется следом как выгрузка для человека
и для истории в git.

Слои контекста собираются кодом и кладутся в промпт фактами. Чего в блоке
«Слои недели» нет, того у Стратега нет: выдумывать занятый слот или чужую
метрику он не должен.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import cfg
from orchestrator import agent
from storage import brand as brand_store
from storage import db

log = logging.getLogger("strategy")

store = brand_store.Store(cfg.brands_path)

DAYS = 7                     # горизонт плана
ARCHIVE_LIMIT = 20           # сколько недавних тем показывать как «не повторяй»
PROFILE_LIMIT = 8000
MAX_TOKENS = 16000

# Секции профиля, по которым Стратег строит темы. Голос ему не нужен:
# формулировки доводит Редактор.
SECTIONS = ("Кто это", "Аудитория", "Цель", "Формат")

GOALS = {"warm": "прогрев", "prod": "продукт", "pers": "личное"}
BACKUP_FUNNEL = {"warm": 60, "prod": 20, "pers": 20}

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# Площадки и допустимые на них форматы. Планируются все три, публикуется
# автоматом только Telegram: Instagram требует Business-аккаунт с ревью
# приложения, YouTube — OAuth владельца, и то и другое это v2.
#
# Норм площадок в профиле пока нет: `platforms.md` собирается на шагах
# онбординга O13–O15. Поэтому набор здесь технический, а раскладка по нему
# это предложение Стратега, которое человек утверждает.
PLATFORMS: dict[str, tuple[str, ...]] = {
    "telegram":  ("пост", "анонс", "раздача", "опрос"),
    "instagram": ("карусель", "reels", "сторис"),
    "youtube":   ("видео", "shorts"),
}
AUTO_PUBLISH = ("telegram",)

# Форматы, которые произносят вслух: их берёт Редактор Reels, а не
# Редактор. Граница нужна обоим, поэтому живёт рядом с набором форматов.
SPOKEN = ("reels", "shorts")

Slot = tuple[str, str]                      # (дата, площадка)


class NoBrand(RuntimeError):
    """Профиля нет — планировать не по чему."""


class NoSlots(RuntimeError):
    """Все дни окна заняты — ставить новые темы некуда."""


@dataclass(frozen=True)
class Plan:
    themes: list[dict[str, Any]]
    context: list[str]
    unmet: list[str]

    def balance(self) -> dict[str, int]:
        """Доли воронки по факту, в процентах. Проверяется глазом человека."""
        total = len(self.themes)
        if not total:
            return {}
        counts = {g: 0 for g in GOALS}
        for t in self.themes:
            counts[t.get("goal", "warm")] = counts.get(t.get("goal", "warm"), 0) + 1
        return {g: round(n * 100 / total) for g, n in counts.items() if n}


# ── слои контекста ────────────────────────────────────────────────────

def _today(chat_id: int) -> date:
    row = db.one("SELECT tz FROM tenants WHERE chat_id = ?", chat_id)
    tz = (row["tz"] if row else None) or cfg.default_tz
    try:
        return datetime.now(ZoneInfo(tz)).date()
    except Exception:                                        # noqa: BLE001
        log.warning("часовой пояс %s не распознан, беру системный", tz)
        return date.today()


def _window(chat_id: int) -> list[date]:
    """Семь дней начиная с завтра.

    Не календарная неделя намеренно: если план просят в четверг, ждать
    понедельника значит выбросить четыре дня.
    """
    start = _today(chat_id) + timedelta(days=1)
    return [start + timedelta(days=i) for i in range(DAYS)]


def _busy(chat_id: int, window: list[date]) -> dict[Slot, str]:
    """Занятые слоты.

    Слот это пара «дата плюс площадка», а не день целиком: в один день
    выходят и пост в Telegram, и карусель в Instagram. Второй пост в тот
    же день на ТУ ЖЕ площадку не ставится.
    """
    lo, hi = window[0].isoformat(), window[-1].isoformat()
    rows = db.q("SELECT date, plat, title FROM themes WHERE chat_id = ? "
                "AND date BETWEEN ? AND ? AND status != 'skip'", chat_id, lo, hi)
    return {(r["date"], r["plat"] or "telegram"):
            r["title"] or "тема без заголовка" for r in rows}


def _free(chat_id: int, window: list[date]) -> tuple[dict[Slot, str], list[Slot]]:
    busy = _busy(chat_id, window)
    free = [(d.isoformat(), p) for d in window for p in PLATFORMS
            if (d.isoformat(), p) not in busy]
    return busy, free


def _fit(themes: list[dict[str, Any]],
         free: list[Slot]) -> tuple[list[dict[str, Any]], list[str]]:
    """Оставить темы, попавшие в свободные слоты.

    Промпт велит брать слоты только из списка свободных, но промпт это
    просьба, а не гарантия. Модель уже приносила дату вне окна, дату в
    прошлом и строку «как-нибудь на неделе»: такая тема встаёт в базу с
    битым id и ломает и сетку, и расписание.

    Отброшенное не пропадает молча — оно уезжает в «не сошлось».
    """
    allowed = set(free)
    kept: list[dict[str, Any]] = []
    rejected: list[str] = []
    taken: set[Slot] = set()

    for t in themes:
        day = str(t.get("date") or "").strip()
        plat = str(t.get("plat") or "").strip().lower()
        title = t.get("title") or "без заголовка"

        if plat not in PLATFORMS:
            rejected.append(f"«{title}» отброшена: площадка "
                            f"{plat or 'не указана'} не подключена")
            continue
        if (day, plat) not in allowed:
            rejected.append(f"«{title}» отброшена: {day or 'дата не указана'} "
                            f"на {plat} не входит в свободные слоты недели")
            continue
        if (day, plat) in taken:
            rejected.append(f"«{title}» отброшена: на {day} в {plat} "
                            "в этом же плане уже стоит другая тема")
            continue

        # Формат вне набора площадки не повод выбрасывать тему: смысл в
        # ней есть, а формат человек поправит кнопкой «Правки».
        fmt = str(t.get("format") or "").strip().lower()
        if fmt and fmt not in PLATFORMS[plat]:
            rejected.append(f"«{title}»: формат «{fmt}» не из набора "
                            f"{plat}, поставил как есть — проверь")

        t["plat"] = plat
        taken.add((day, plat))
        kept.append(t)

    return kept, rejected


def _archive(chat_id: int) -> list[str]:
    rows = db.q("SELECT date, title, goal FROM themes WHERE chat_id = ? "
                "ORDER BY date DESC LIMIT ?", chat_id, ARCHIVE_LIMIT)
    return [f"{r['date']} · {GOALS.get(r['goal'], r['goal'] or '—')} · "
            f"{r['title'] or '—'}" for r in rows]


def _leftovers(chat_id: int) -> list[str]:
    """Невышедшее — первые кандидаты в новую неделю, а не мусор."""
    rows = db.q("SELECT date, title FROM themes WHERE chat_id = ? "
                "AND status IN ('idea', 'draft') AND date < ? "
                "ORDER BY date DESC LIMIT 10",
                chat_id, _today(chat_id).isoformat())
    return [f"{r['date']} · {r['title'] or '—'}" for r in rows]


def _layers(chat_id: int, busy: dict[Slot, str], free: list[Slot],
            ask: str) -> str:
    """Блок «Слои недели». Недоступный слой называется вслух."""
    lines = ["## Слои недели", ""]

    lines.append("### 0. Календарь событий")
    lines.append("Календаря в v1 нет: события вносятся темой. "
                 "Занятые слоты ниже — это и есть учтённые события.")
    if busy:
        lines += [f"- {d} · {p} занято: {t}"
                  for (d, p), t in sorted(busy.items())]
    else:
        lines.append("- занятых слотов нет")
    lines.append("")

    lines.append("### Свободные слоты")
    lines.append("Слот это дата плюс площадка. Бери `date` и `plat` только "
                 "отсюда, пару целиком.")
    by_day: dict[str, list[str]] = {}
    for day, plat in free:
        by_day.setdefault(day, []).append(plat)
    for day in sorted(by_day):
        wd = WEEKDAYS[date.fromisoformat(day).weekday()]
        lines.append(f"- {day} ({wd}): " + ", ".join(sorted(by_day[day])))
    if not by_day:
        lines.append("- свободных слотов нет")
    lines.append("")

    lines.append("### 1. Дайджест недели")
    lines.append("Недельный ресёрч не подключён: дайджеста нет ни за эту "
                 "неделю, ни за прошлую. Работай в экспресс-режиме на профиле "
                 "и архиве и скажи об этом второй строкой «Контекста».")
    lines.append("")

    lines.append("### 2. Профиль и архив")
    archive = _archive(chat_id)
    lines.append("Рубрикатора и целей в профиле нет: `goals.md` и "
                 "`platforms.md` ещё не собраны. Пропорция воронки запасная, "
                 f"{BACKUP_FUNNEL['warm']}/{BACKUP_FUNNEL['prod']}/"
                 f"{BACKUP_FUNNEL['pers']}.")
    if archive:
        lines.append("Недавние темы, повторять их нельзя:")
        lines += [f"- {a}" for a in archive]
    else:
        lines.append("Архив пуст: это первая неделя бренда.")
    lines.append("")

    lines.append("### 3. Своя статистика")
    left = _leftovers(chat_id)
    lines.append("Метрики не подключены, разрезов по охвату нет.")
    if left:
        lines.append("Осталось в плане невышедшим:")
        lines += [f"- {x}" for x in left]
    lines.append("")

    lines.append("### 4. Лог решений")
    lines.append("Лога подтверждённых типов заголовков пока нет: "
                 "утверждённых батчей меньше одного.")
    lines.append("")

    lines.append("### Площадки и форматы")
    lines.append("Норм площадок в профиле нет: `platforms.md` ещё не собран. "
                 "Раскладка по площадкам это твоё предложение, человек его "
                 "утверждает. Скажи об этом строкой в «Контексте».")
    for plat, formats in PLATFORMS.items():
        lines.append(f"- `{plat}`: {', '.join(formats)}")
    lines.append("Публикуется автоматом только Telegram. Слоты Instagram и "
                 "YouTube человек выкладывает руками, поэтому не ставь их "
                 "больше, чем можно реально снять и смонтировать за неделю.")
    lines.append("")

    lines.append("### Запрос человека")
    lines.append(ask.strip() or "«план на неделю», без уточнений")

    return "\n".join(lines)


def _profile(chat_id: int) -> tuple[str, str, Any]:
    row = db.one("SELECT brand_slug, brand_name FROM tenants WHERE chat_id = ?",
                 chat_id)
    if not row or not row["brand_slug"]:
        raise NoBrand("профиль бренда ещё не собран")
    b = store.get(row["brand_slug"])
    if b is None:
        raise NoBrand(f"папка профиля {row['brand_slug']} не найдена")

    parts = [s for s in (b.section("core", n) for n in SECTIONS) if s]
    return b.name(), ("\n\n".join(parts) or b.read("core"))[:PROFILE_LIMIT], b


# ── запись плана ──────────────────────────────────────────────────────

def _theme_id(day: str, plat: str, n: int) -> str:
    return f"{day}-{plat}-{n:02d}"


def _save(chat_id: int, plan: Plan, core_version: str) -> list[dict[str, Any]]:
    """Положить темы в базу. id стабильный, поэтому считается один раз.

    Повторный прогон на ту же дату не перетирает утверждённое: занятые
    слоты Стратег получает в слоях и в них не целится, а совпадение id
    разруливается счётчиком внутри дня.
    """
    saved = []
    with db.tx() as c:
        for t in plan.themes:
            day = str(t.get("date") or "")
            plat = str(t.get("plat") or "telegram")
            n = 1
            while c.execute("SELECT 1 FROM themes WHERE id = ?",
                            (_theme_id(day, plat, n),)).fetchone():
                n += 1
            tid = _theme_id(day, plat, n)

            c.execute(
                "INSERT INTO themes (id, chat_id, date, plat, format, rubric, "
                "goal, arch, funnel_stage, title, hook, why, angle, charge, "
                "src, status, core_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "'plan','idea',?)",
                (tid, chat_id, day, plat, t.get("format"), t.get("rubric"),
                 t.get("goal"), t.get("arch"), t.get("funnel_stage"),
                 t.get("title"), t.get("hook"), t.get("why"), t.get("angle"),
                 t.get("charge"), core_version))
            saved.append({**t, "id": tid})
    log.info("план сохранён: %s тем", len(saved))
    return saved


def _markdown(brand_name: str, plan: Plan, themes: list[dict[str, Any]],
              window: list[date]) -> str:
    """Выгрузка плана человеку. База остаётся источником правды."""
    lines = [f"# План недели. {brand_name}", "",
             f"{window[0].isoformat()} — {window[-1].isoformat()}", "",
             "## Контекст недели", ""]
    lines += [f"- {c}" for c in plan.context]
    lines += ["", "## Темы", "",
              "| id | дата | площадка | рубрика | формат | цель | архетип | "
              "заголовок | хук | почему |", "|---|---|---|---|---|---|---|---|---|---|"]
    for t in themes:
        cells = [t["id"], t.get("date", ""), t.get("plat", ""),
                 t.get("rubric", ""), t.get("format", ""),
                 GOALS.get(t.get("goal", ""), t.get("goal", "")),
                 t.get("arch", ""), t.get("title", ""), t.get("hook", ""),
                 t.get("why", "")]
        lines.append("| " + " | ".join(str(c or "—").replace("|", "\\|")
                                       for c in cells) + " |")

    variants = [t for t in themes if t.get("variants")]
    if variants:
        lines += ["", "## Углы и варианты", ""]
        for t in variants:
            lines.append(f"### {t['id']} · {t.get('title', '')}")
            for v in t["variants"]:
                lines.append(f"- {v.get('angle', '—')} · заряд "
                             f"{v.get('charge', '—')}: {v.get('hook', '')}")
            lines.append("")

    if plan.unmet:
        lines += ["## Не сошлось", ""] + [f"- {u}" for u in plan.unmet]
    return "\n".join(lines)


def by_platform(themes: list[dict[str, Any]]) -> dict[str, int]:
    """Сколько тем на каждой площадке. Порядок как в PLATFORMS."""
    counts = {p: 0 for p in PLATFORMS}
    for t in themes:
        plat = t.get("plat", "")
        counts[plat] = counts.get(plat, 0) + 1
    return {p: n for p, n in counts.items() if n}


def card(brand_name: str, plan: Plan, themes: list[dict[str, Any]]) -> str:
    """Карточка батча для чата. Режется по абзацам в registry.say.

    Темы сгруппированы по дню: в один день теперь попадает несколько
    площадок, и списком подряд неделя перестаёт читаться расписанием.
    """
    out = ["🎯 <b>План недели</b>", ""]
    out += [c for c in plan.context]
    out.append("")

    days: dict[str, list[dict[str, Any]]] = {}
    for t in themes:
        days.setdefault(t.get("date", ""), []).append(t)

    for day in sorted(days):
        try:
            wd = WEEKDAYS[date.fromisoformat(day).weekday()]
        except ValueError:
            wd = "—"
        out.append(f"<b>━━ {day} {wd}</b>")

        for t in sorted(days[day], key=lambda x: x.get("plat", "")):
            goal = GOALS.get(t.get("goal", ""), t.get("goal", "—"))
            hand = "" if t.get("plat") in AUTO_PUBLISH else " ✋"
            out.append(f"<b>{t.get('plat') or '—'}</b> · "
                       f"{t.get('format') or '—'} · "
                       f"{t.get('rubric') or '—'} · {goal}{hand}")
            out.append(f"{t.get('title') or '—'}")
            if t.get("hook"):
                out.append(f"<i>{t['hook']}</i>")
            if t.get("why"):
                out.append(f"зачем: {t['why']}")
            for v in t.get("variants") or []:
                out.append(f"  ↳ {v.get('angle', '—')} · {v.get('charge', '—')}: "
                           f"{v.get('hook', '')}")
            out.append("")

    if plats := by_platform(themes):
        out.append("Площадки: " + ", ".join(f"{p} {n}" for p, n in plats.items()))
    if bal := plan.balance():
        out.append("Баланс: " + ", ".join(
            f"{GOALS.get(g, g)} {p}%" for g, p in bal.items()))

    manual = sum(n for p, n in by_platform(themes).items()
                 if p not in AUTO_PUBLISH)
    if manual:
        out.append(f"✋ {manual} слотов вне Telegram — их выкладываешь руками, "
                   "Публикатор туда не ходит.")
    if plan.unmet:
        out.append("")
        out.append("⚠️ Не сошлось: " + "; ".join(plan.unmet))
    return "\n".join(out)


# ── вход ──────────────────────────────────────────────────────────────

async def build(chat_id: int, ask: str) -> tuple[str, Plan, list[dict[str, Any]]]:
    """Собрать план недели, положить в базу и выгрузить файлом."""
    brand_name, profile, b = _profile(chat_id)
    window = _window(chat_id)
    busy, free = _free(chat_id, window)

    # Спрашивать модель некуда, если ставить некуда. Заодно не платим
    # за вызов, который всё равно нечем закрыть.
    if not free:
        raise NoSlots(
            f"с {window[0].isoformat()} по {window[-1].isoformat()} заняты "
            f"все слоты на всех площадках ({', '.join(PLATFORMS)})")

    answer = await agent.ask(
        "strategy", chat_id,
        "Собери план на неделю по слоям ниже. Ответь одним JSON-объектом "
        "в формате из твоей секции «Формат выдачи».\n\n"
        + _layers(chat_id, busy, free, ask),
        brand_name=brand_name, profile=profile, max_tokens=MAX_TOKENS)

    data = agent.parse_json(answer, who="стратег")
    themes, rejected = _fit(data.get("themes") or [], free)
    if rejected:
        log.warning("отброшено тем: %s", len(rejected))
    plan = Plan(themes=themes,
                context=[str(c) for c in (data.get("context") or [])],
                unmet=[str(u) for u in (data.get("unmet") or [])] + rejected)

    if not plan.themes:
        raise RuntimeError("Стратег не вернул ни одной темы на свободный слот")

    saved = _save(chat_id, plan, b.version())
    week = window[0].isocalendar()
    b.artifact(f"plans/{week.year}-W{week.week:02d}.md",
               _markdown(brand_name, plan, saved, window))
    return brand_name, plan, saved


# ── разговор с человеком ──────────────────────────────────────────────

# Батч, предложенный последним: по нему работают кнопки. Не утверждённый
# батч живёт в базе как `idea`, поэтому «другие темы» его удаляют, а не
# оставляют висеть вторым планом на те же даты.
_batch: dict[int, list[str]] = {}
_awaiting_fix: set[int] = set()


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Утвердить", callback_data="plan:ok"),
        InlineKeyboardButton(text="✏️ Правки", callback_data="plan:fix"),
        InlineKeyboardButton(text="🔄 Другие темы", callback_data="plan:redo"),
    ]])


def _drop(chat_id: int) -> int:
    """Убрать неутверждённый батч. Утверждённое и начатое не трогаем."""
    ids = _batch.pop(chat_id, [])
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with db.tx() as c:
        cur = c.execute(
            f"DELETE FROM themes WHERE chat_id = ? AND status = 'idea' "
            f"AND id IN ({marks})", (chat_id, *ids))
    return cur.rowcount


def wants_fix(chat_id: int) -> bool:
    """Человек нажал «Правки» и сейчас пишет, что именно поправить."""
    return chat_id in _awaiting_fix


async def run(reg, chat_id: int, ask: str, topic: str = "strategy") -> None:
    """Собрать план и показать батч на утверждение."""
    _awaiting_fix.discard(chat_id)
    await reg.say("strategy", chat_id, "Собираю план недели.", topic=topic)

    try:
        brand_name, plan, saved = await build(chat_id, ask)
    except NoBrand as e:
        await reg.say("strategy", chat_id,
                      f"Планировать не по чему: {e}.", topic=topic)
        return
    except NoSlots as e:
        await reg.say("strategy", chat_id,
                      f"Ставить некуда: {e}. Освободи день или сдвинь "
                      "существующие темы, тогда соберу.", topic=topic)
        return
    except agent.BudgetExceeded as e:
        await reg.say("strategy", chat_id, f"Остановился: {e}", topic=topic)
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("план не собрался")
        reason = getattr(e, "message", None) or str(e) or type(e).__name__
        await reg.say("strategy", chat_id,
                      f"План не собрался: {reason}", topic=topic)
        return

    _batch[chat_id] = [t["id"] for t in saved]
    await reg.say("strategy", chat_id, card(brand_name, plan, saved),
                  kb=_kb(), topic=topic)


async def revise(reg, chat_id: int, instruction: str,
                 topic: str = "strategy") -> None:
    """Пересобрать батч по правке человека."""
    _awaiting_fix.discard(chat_id)
    _drop(chat_id)
    await run(reg, chat_id, f"Правка к прошлому плану: {instruction}",
              topic=topic)


async def on_callback(reg, chat_id: int, action: str,
                      topic: str = "strategy") -> None:
    if action == "ok":
        ids = _batch.pop(chat_id, [])
        _awaiting_fix.discard(chat_id)
        if not ids:
            await reg.say("strategy", chat_id,
                          "Этот план уже неактуален, собери заново.", topic=topic)
            return
        # Темы остаются `idea`: утверждён смысл, а текстов ещё нет.
        # Следующий шаг за Редактором, и врать про него нельзя.
        await reg.say(
            "strategy", chat_id,
            f"Утвердил, {len(ids)} тем в плане. Выгрузка лежит в "
            "<code>plans/</code> в папке бренда.\n\n"
            "Дальше за Редактором: скажи «напиши пост», и он возьмёт "
            "ближайшую тему.", topic=topic)
        return

    if action == "fix":
        if chat_id not in _batch:
            await reg.say("strategy", chat_id,
                          "Этот план уже неактуален, собери заново.", topic=topic)
            return
        _awaiting_fix.add(chat_id)
        await reg.say("strategy", chat_id,
                      "Напиши одним сообщением, что поправить: тему, день, "
                      "перекос по воронке. Пересоберу батч.", topic=topic)
        return

    if action == "redo":
        dropped = _drop(chat_id)
        if not dropped:
            await reg.say("strategy", chat_id,
                          "Этот план уже неактуален, собери заново.", topic=topic)
            return
        await run(reg, chat_id, "Прошлый батч не подошёл. Дай другие темы: "
                                "другие углы и другие заходы, не перестановку "
                                "тех же.", topic=topic)
