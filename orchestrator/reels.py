"""Редактор Reels: из темы в суфлёрный текст.

Вход — тема из плана в формате `reels` или `shorts` (`themes.status =
'idea'`), выход — текст, который произносят вслух перед камерой.

Устройство то же, что у Редактора, и по той же причине: роль отвечает
структурой, а границу держит код. Модель отдаёт шесть блоков, чистый
суфлёрный текст **собирает код** — две точки правды на один текст
расходятся к третьему дублю.

Проверок две. `check_voice` ловит машинный текст и стоп-слова бренда,
`check_script` — то, что отличает речь от письма: цифру вместо слова,
строку не по дыханию, бюджет слов от хронометража. Кругов переделки два,
дальше честный отказ.

Механик от Ресёрчера нет, он не подключён. Поэтому роль идёт на профиле
и теме, и говорит об этом строкой, а не молчит.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import cfg
from orchestrator import agent, design
from orchestrator.strategy import SPOKEN
from storage import brand as brand_store
from storage import db
from validators import check_script, check_voice

log = logging.getLogger("reels")

store = brand_store.Store(cfg.brands_path)

MAX_ROUNDS = 2
MAX_TOKENS = 8000
PROFILE_LIMIT = 8000
VOICE_FLOOR = 3              # балл voice ниже — автоматический отказ

# Форматы, которые снимают, а не пишут. Набор один на завод, см. strategy.
FORMATS = SPOKEN

# Порядок жёсткий, названия совпадают с ключами в ответе роли.
BLOCKS: tuple[tuple[str, str], ...] = (
    ("hook",        "Хук"),
    ("recognition", "Узнавание"),
    ("reason",      "Причина"),
    ("shift",       "Сдвиг"),
    ("step",        "Шаг"),
    ("cta",         "CTA"),
)

# На тридцати секундах «Причина» и «Сдвиг» сливаются в один блок,
# поэтому пустой `shift` там не поломка, а спека.
MERGED_AT = 30

# Профиль нужен про голос и про то, как человек говорит вслух.
SECTIONS = ("Кто это", "Аудитория", "Голос", "Формат")

WORDS_PER_SEC = 2


class NoTheme(RuntimeError):
    """Снимать нечего: подходящей темы в плане нет."""


class ScriptRefused(RuntimeError):
    """Сценарий не прошёл проверку за отведённые круги."""


@dataclass
class Reel:
    theme: dict[str, Any]
    seconds: int = check_script.DEFAULT_SECONDS
    idea: str = ""
    blocks: dict[str, str] = field(default_factory=dict)
    spare: list[str] = field(default_factory=list)
    checks: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    rounds: int = 0

    @property
    def voice(self) -> int:
        return int(self.checks.get("voice", 0) or 0)

    @property
    def script(self) -> str:
        """Чистый суфлёрный текст. Пустая строка между блоками это пауза."""
        parts = [self.blocks.get(key, "").strip() for key, _ in BLOCKS]
        return "\n\n".join(p for p in parts if p)

    @property
    def words(self) -> int:
        return check_script.words(self.script)

    def score(self) -> str:
        if not self.checks:
            return "самопроверка не пришла"
        return ", ".join(f"{k} {v}" for k, v in self.checks.items())


# ── выбор темы ────────────────────────────────────────────────────────

ID_RX = re.compile(r"\b\d{4}-\d{2}-\d{2}-[a-z]+-\d{2}\b")
SEC_RX = re.compile(r"\b(\d{2})\s*(?:сек\w*|c|с)\b", re.I)


def seconds_from(ask: str) -> int:
    """Хронометраж из просьбы человека. По умолчанию сорок секунд."""
    if m := SEC_RX.search(ask or ""):
        want = int(m.group(1))
        # Ближайший хронометраж из таблицы бюджета: «45 секунд» это
        # просьба про длину, а не новая строка в спеке.
        return min(check_script.BUDGET, key=lambda s: abs(s - want))
    return check_script.DEFAULT_SECONDS


def _pick(chat_id: int, ask: str) -> dict[str, Any]:
    """Тема под ролик: формат `reels` или `shorts`.

    Названная по id тема берётся и в статусе `draft`: правка приходит
    к уже написанному сценарию, и требовать от него статус `idea`
    значит не находить ровно то, что человек сейчас правит. Молча,
    без id, берём только неначатое.
    """
    rows = db.q("SELECT * FROM themes WHERE chat_id = ? AND "
                "status IN ('idea', 'draft') ORDER BY date", chat_id)

    if m := ID_RX.search(ask):
        for r in rows:
            if r["id"] == m.group():
                if (r["format"] or "").lower() not in FORMATS:
                    raise NoTheme(f"тема {m.group()} это "
                                  f"{r['format'] or 'не ролик'}, "
                                  "сценарий ей не нужен")
                return dict(r)
        raise NoTheme(f"темы {m.group()} нет среди начатых и неначатых")

    fresh = [r for r in rows if r["status"] == "idea"]
    mine = [r for r in fresh if (r["format"] or "").lower() in FORMATS]
    if not mine:
        raise NoTheme("в плане нет ни одной неначатой темы под ролик"
                      if fresh else "в плане нет ни одной неначатой темы")

    # Совпадение по словам заголовка: «сценарий про файл ЯДРО».
    words = {w for w in re.findall(r"\w{4,}", (ask or "").lower())}
    if words:
        best, hits = None, 0
        for r in mine:
            title = set(re.findall(r"\w{4,}", (r["title"] or "").lower()))
            n = len(words & title)
            if n > hits:
                best, hits = r, n
        if best is not None and hits >= 2:
            return dict(best)

    return dict(mine[0])


# ── профиль ───────────────────────────────────────────────────────────

def _brand(chat_id: int):
    row = db.one("SELECT brand_slug FROM tenants WHERE chat_id = ?", chat_id)
    return store.get(row["brand_slug"]) if row and row["brand_slug"] else None


def _brief(theme: dict[str, Any], seconds: int) -> str:
    """Задание: тема, хронометраж, честная строка про недостающий слой."""
    lo, hi = check_script.budget(seconds)
    lines = ["## Тема из плана", ""]
    for label, key in (("id", "id"), ("дата", "date"), ("площадка", "plat"),
                       ("формат", "format"), ("рубрика", "rubric"),
                       ("цель", "goal"), ("архетип", "arch"),
                       ("рабочий заголовок", "title"), ("хук", "hook"),
                       ("кому и зачем", "why"), ("угол", "angle"),
                       ("ведущий заряд", "charge")):
        if theme.get(key):
            lines.append(f"- {label}: {theme[key]}")

    lines += [
        "", "## Хронометраж", "",
        f"{seconds} секунд, бюджет {lo}–{hi} слов на весь сценарий.",
        "",
        "## Слой, который не открылся", "",
        "Механик недели от Ресёрчера нет: он не подключён. Работаешь на "
        "теме и профиле бренда. Чужие приёмы не выдумываешь.",
        "",
        "Рабочий заголовок и хук это заготовка Стратега, а не финал. "
        "Довести их до произносимой фразы — твоя работа.",
    ]
    return "\n".join(lines)


# ── проверки кодом ────────────────────────────────────────────────────

def structure(reel: Reel) -> list[str]:
    """Блоки на месте и в порядке. Это проверяется до текста внутри них."""
    out = []
    for key, title in BLOCKS:
        if reel.blocks.get(key, "").strip():
            continue
        if key == "shift" and reel.seconds == MERGED_AT:
            continue                      # на тридцати секундах слит с «Причиной»
        out.append(f"блок «{title}» ({key}) пустой")

    spare = [s for s in reel.spare if s.strip()]
    if len(spare) < 2:
        out.append(f"запасных хуков {len(spare)}, нужно два")
    return out


RULE_RX = re.compile(r"^\[[a-z-]+\]\s*")


def _rules(problems: list[str], limit: int = 3) -> str:
    """Названия нарушенных правил без кусков забракованного текста.

    В находке лежит фрагмент сценария, и ему в чате не место: человек
    прочтёт отклонённую фразу и запомнит именно её.
    """
    names = [RULE_RX.sub("", p.split(": «", 1)[0]) for p in problems]
    return ", ".join(list(dict.fromkeys(names))[:limit])


def _problems(reel: Reel, stopwords: list[str]) -> list[str]:
    """Всё, что поймано кодом, одним списком для обратной связи роли."""
    out = structure(reel)
    script = reel.script
    if not script:
        return out or ["сценарий пуст"]

    out += [str(f) for f in check_script.check(
        script, seconds=reel.seconds, hook=reel.blocks.get("hook", ""))]
    out += [str(f) for f in check_voice.check(script, stopwords=stopwords)]
    if reel.voice < VOICE_FLOOR:
        out.append(f"твой собственный балл voice {reel.voice}: "
                   "по правилу это отказ")
    return out


# ── сборка ────────────────────────────────────────────────────────────

async def build(chat_id: int, ask: str, *, say=None) -> Reel:
    """Написать сценарий, переписывая пока код даёт отказ."""
    b = _brand(chat_id)
    if b is None:
        raise NoTheme("профиль бренда ещё не собран")

    theme = _pick(chat_id, ask)
    seconds = seconds_from(ask)
    stop = b.stopwords()

    parts = [s for s in (b.section("core", n) for n in SECTIONS) if s]
    profile = ("\n\n".join(parts) or b.read("core"))[:PROFILE_LIMIT]

    reel = Reel(theme=theme, seconds=seconds)
    extra = ""

    if say:
        await say(f"Пишу сценарий по теме <b>{theme.get('title') or theme['id']}</b> "
                  f"({theme.get('plat')} · {theme.get('format')} · "
                  f"{seconds} секунд).\nЭто займёт до минуты.")

    for attempt in range(1, MAX_ROUNDS + 1):
        reel.rounds = attempt
        prompt = (_brief(theme, seconds) + extra +
                  "\n\nОтветь одним JSON-объектом в формате из твоей секции "
                  "«Формат выдачи».")
        if (ask or "").strip():
            prompt += f"\n\n## Что сказал человек\n\n{ask.strip()}"

        answer = await agent.ask("reels", chat_id, prompt,
                                 brand_name=b.name(), profile=profile,
                                 max_tokens=MAX_TOKENS)
        data = agent.parse_json(answer, who="редактор reels")
        reel.idea = str(data.get("idea") or "").strip()
        reel.blocks = {k: str(v or "").strip()
                       for k, v in (data.get("blocks") or {}).items()
                       if isinstance(k, str)}
        reel.spare = [str(s).strip() for s in (data.get("spare_hooks") or [])
                      if str(s).strip()]
        reel.checks = {k: int(v) for k, v in (data.get("checks") or {}).items()
                       if isinstance(v, (int, float))}
        reel.notes = [str(n) for n in (data.get("notes") or [])]

        problems = _problems(reel, stop)
        log.info("круг %s: %s слов на %s секунд, самопроверка [%s], находок %s",
                 attempt, reel.words, seconds, reel.score(), len(problems))

        if not problems:
            return reel

        if attempt == MAX_ROUNDS:
            raise ScriptRefused(_rules(problems, limit=5))

        if say:
            await say(f"Первый вариант не прошёл проверку ({_rules(problems)}). "
                      "Переписываю.")

        extra = ("\n\n## Прошлый вариант отклонён\n\n"
                 "Проверка кодом поймала:\n"
                 + "\n".join(f"- {p}" for p in problems)
                 + "\n\nПерепиши сценарий целиком. Не оговаривайся и не "
                   "объясняй правку внутри текста.\n\n## Отклонённый "
                   "суфлёрный текст\n\n" + reel.script)

    raise ScriptRefused("круги переделки исчерпаны")


# ── выгрузка ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Beat:
    """Блок сценария с меткой времени и числом слов."""
    key: str
    title: str
    start: str
    end: str
    words: int
    text: str


def _clock(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def timings(reel: Reel) -> list[Beat]:
    """Блоки с метками времени, посчитанными по словам.

    Тайминги считаются от текста, а не берутся из таблицы спеки:
    сценарий на тридцать секунд с метками до 0:50 обманывает на съёмке.
    """
    out: list[Beat] = []
    clock = 0
    for key, title in BLOCKS:
        text = reel.blocks.get(key, "").strip()
        if not text:
            continue
        n = check_script.words(text)
        length = max(1, round(n / WORDS_PER_SEC))
        out.append(Beat(key, title, _clock(clock), _clock(clock + length),
                        n, text))
        clock += length
    return out


def _save(chat_id: int, b, reel: Reel) -> str:
    """Суфлёр в один файл, разбор в другой.

    Разделение не косметика: на съёмку едет чистый текст, и он же уходит
    Дизайнеру и Публикатору как `themes.asset`. Тайминги и запасные хуки
    нужны на правку, но в кадре они не звучат.
    """
    tid = reel.theme["id"]
    rel = f"posts/{tid}-script.md"
    head = (f"<!-- {tid} · {reel.theme.get('plat')} · "
            f"{reel.theme.get('format')} · {reel.seconds} сек · "
            f"{reel.words} слов -->\n\n")
    b.artifact(rel, head + reel.script + "\n")

    notes = [f"# Разбор сценария {tid}", "",
             f"Одна мысль: {reel.idea or '[не сформулирована]'}", "",
             f"Хронометраж {reel.seconds} сек, слов {reel.words}.", "",
             "## Блоки", ""]
    for beat in timings(reel):
        notes += [f"### {beat.title} · {beat.start}–{beat.end} · "
                  f"{beat.words} слов", "", beat.text, ""]
    notes += ["## Запасные хуки", ""]
    notes += [f"- {s}" for s in reel.spare] or ["- нет"]
    if reel.notes:
        notes += ["", "## Что не сошлось", ""] + [f"- {n}" for n in reel.notes]
    b.artifact(f"posts/{tid}-script-notes.md", "\n".join(notes) + "\n")

    with db.tx() as c:
        c.execute("UPDATE themes SET status = 'draft', asset = ?, "
                  "updated_at = datetime('now') WHERE id = ? AND chat_id = ?",
                  (rel, tid, chat_id))
    log.info("сценарий сохранён: %s", rel)
    return rel


# ── карточка и кнопки ─────────────────────────────────────────────────

_pending: dict[int, Reel] = {}
_awaiting_fix: set[int] = set()


def _kb(theme_id: str) -> InlineKeyboardMarkup:
    """id темы едет в кнопке: сценарий в памяти не переживает перезапуск."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Ок", callback_data=f"reel:ok:{theme_id}"),
        InlineKeyboardButton(text="✏️ Правки",
                             callback_data=f"reel:fix:{theme_id}"),
        InlineKeyboardButton(text="🎨 Обложка",
                             callback_data=f"reel:design:{theme_id}"),
    ]])


def card(reel: Reel) -> str:
    """Разбор: шапка, блоки с таймингами, запасные хуки."""
    t = reel.theme
    out = [f"🎬 <b>{t.get('date')} · {t.get('plat')} · {t.get('format')}</b>\n"
           f"<code>{t['id']}</code> · {reel.seconds} сек · {reel.words} слов"]
    if reel.idea:
        out += ["", f"Одна мысль: {reel.idea}"]

    out += [""]
    for beat in timings(reel):
        out.append(f"<b>{beat.title}</b> · {beat.start}–{beat.end} · "
                   f"{beat.words} слов")

    if reel.spare:
        out += ["", "Запасные хуки:"] + [f"· {s}" for s in reel.spare[:2]]
    if reel.notes:
        out += ["", "⚠️ " + "; ".join(reel.notes[:3])]
    return "\n".join(out)


def wants_fix(chat_id: int) -> bool:
    return chat_id in _awaiting_fix


async def run(reg, chat_id: int, ask: str, topic: str = "reels") -> None:
    _awaiting_fix.discard(chat_id)

    async def say(text: str) -> None:
        await reg.say("reels", chat_id, text, topic=topic)

    try:
        reel = await build(chat_id, ask, say=say)
    except NoTheme as e:
        await reg.say("reels", chat_id,
                      f"Снимать нечего: {e}. Нужна тема в формате reels "
                      "или shorts — это к Стратегу.", topic=topic)
        return
    except ScriptRefused as e:
        await reg.say("reels", chat_id,
                      f"Сценарий не прошёл проверку за два круга: {e}.\n\n"
                      "Выдавать с оговоркой не буду. Скажи, что поправить, "
                      "или возьмём другую тему.", topic=topic)
        return
    except agent.BudgetExceeded as e:
        await reg.say("reels", chat_id, f"Остановился: {e}", topic=topic)
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("сценарий не собрался")
        reason = getattr(e, "message", None) or str(e) or type(e).__name__
        await reg.say("reels", chat_id, f"Сценарий не собрался: {reason}",
                      topic=topic)
        return

    b = _brand(chat_id)
    _save(chat_id, b, reel)
    _pending[chat_id] = reel

    await reg.say("reels", chat_id, card(reel), topic=topic)
    # Суфлёр отдельным сообщением и без подписи роли: его копируют
    # целиком в телефон, и служебная строка сверху уедет вместе с ним.
    await reg.say("reels", chat_id, reel.script, topic=topic,
                  with_label=False, kb=_kb(reel.theme["id"]))


async def revise(reg, chat_id: int, instruction: str,
                 topic: str = "reels") -> None:
    """Пересобрать сценарий по правке человека.

    Правка это обучающий сигнал, а не разовая просьба: она дописывается
    в `voice-corrections.md` бренда, как у Редактора.
    """
    _awaiting_fix.discard(chat_id)
    reel = _pending.get(chat_id)
    if reel is None:
        await reg.say("reels", chat_id, "Этот сценарий уже неактуален.",
                      topic=topic)
        return

    b = _brand(chat_id)
    if b is not None:
        b.append("voice-corrections.md",
                 f"- {reel.theme['id']} (сценарий): {instruction.strip()}")

    await run(reg, chat_id,
              f"Правка к сценарию темы {reel.theme['id']} "
              f"({reel.seconds} сек): {instruction}", topic=topic)


def _recover(chat_id: int, theme_id: str) -> Reel | None:
    """Поднять сценарий из базы, если память процесса его не помнит.

    Блоки обратно из файла не разбираются: на диске лежит суфлёр, и
    границы блоков в нём стёрты намеренно. Кнопкам хватает темы, а
    правка всё равно пересобирает сценарий заново.
    """
    row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                 theme_id, chat_id)
    return Reel(theme=dict(row)) if row is not None else None


async def on_callback(reg, chat_id: int, action: str,
                      topic: str = "reels") -> None:
    action, _, theme_id = action.partition(":")
    reel = _pending.get(chat_id)
    if reel is not None and theme_id and reel.theme["id"] != theme_id:
        reel = None
    if reel is None and theme_id:
        reel = _recover(chat_id, theme_id)

    if action in {"ok", "design"}:
        _pending.pop(chat_id, None)
        _awaiting_fix.discard(chat_id)
        if reel is None:
            await reg.say("reels", chat_id, "Этот сценарий уже неактуален.",
                          topic=topic)
            return

        tid = reel.theme["id"]
        with db.tx() as c:
            c.execute("UPDATE themes SET status = 'ready', "
                      "updated_at = datetime('now') "
                      "WHERE id = ? AND chat_id = ?", (tid, chat_id))

        if action == "ok":
            await reg.say("reels", chat_id,
                          f"Готово, <code>{tid}</code> в статусе ready. "
                          "Обложку соберёт Дизайнер, подпись под видео "
                          "напишет Редактор.", topic=topic)
            return

        await reg.say("reels", chat_id,
                      f"Принял сценарий <code>{tid}</code> и передаю "
                      "Дизайнеру на обложку.", topic=topic)
        await design.run(reg, chat_id, f"свёрстай обложку по теме {tid}",
                         topic="design")
        return

    if action == "fix":
        if reel is None:
            await reg.say("reels", chat_id, "Этот сценарий уже неактуален.",
                          topic=topic)
            return
        # Поднятый из базы сценарий кладём обратно в память: иначе
        # кнопка спросит правку, а следующее сообщение упрётся в
        # «уже неактуален».
        _pending[chat_id] = reel
        _awaiting_fix.add(chat_id)
        await reg.say("reels", chat_id,
                      "Напиши одним сообщением, что поправить. Запишу правку "
                      "в профиль голоса и перепишу сценарий.", topic=topic)
