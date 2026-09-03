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

from orchestrator import agent, desk, design
from orchestrator.desk import NoWork
from orchestrator.strategy import SPOKEN
from storage import db
from validators import check_script, check_voice

log = logging.getLogger("reels")

MAX_ROUNDS = 2
# См. editor.MAX_TOKENS: мышление считается вместе с ответом.
MAX_TOKENS = 16000

# Схема ответа. Форму гарантирует API, а не просьба в промпте: до этого
# сорвавшийся ответ («вот текст поста:» перед скобкой) стоил круга, а их
# всего два. Схема закрытая — все поля обязательны, лишних нет, — иначе
# структурированный вывод её не примет. Держать в согласии с секцией
# «Формат выдачи» в roles/reels.md: расходятся — модель выполнит схему,
# а промпт соврёт человеку.
SCHEMA = {
    "type": "object",
    "properties": {
        "idea": {"type": "string"},
        "seconds": {"type": "integer"},
        "blocks": {
            "type": "object",
            "properties": {
                "hook": {"type": "string"},
                "recognition": {"type": "string"},
                "reason": {"type": "string"},
                "shift": {"type": "string"},
                "step": {"type": "string"},
                "cta": {"type": "string"},
            },
            "required": ["hook", "recognition", "reason", "shift", "step",
                         "cta"],
            "additionalProperties": False,
        },
        "spare_hooks": {"type": "array", "items": {"type": "string"}},
        "checks": {
            "type": "object",
            "properties": {
                "hook": {"type": "integer"},
                "recognition": {"type": "integer"},
                "step": {"type": "integer"},
                "voice": {"type": "integer"},
                "speech": {"type": "integer"},
            },
            "required": ["hook", "recognition", "step", "voice", "speech"],
            "additionalProperties": False,
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["idea", "seconds", "blocks", "spare_hooks", "checks",
                 "notes"],
    "additionalProperties": False,
}
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
    return desk.pick(
        chat_id, ask, statuses=("idea", "draft"),
        suits=lambda r: (r["format"] or "").lower() in FORMATS,
        wrong="тема {id} это {format}, сценарий ей не нужен",
        none="темы {id} нет среди начатых и неначатых",
        empty="в плане нет ни одной неначатой темы под ролик")


# ── профиль ───────────────────────────────────────────────────────────

def _brief(theme: dict[str, Any], seconds: int) -> str:
    """Задание: тема, хронометраж, честная строка про недостающий слой."""
    lo, hi = check_script.budget(seconds)
    lines = ["## Тема из плана", ""] + desk.brief(theme) + [
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
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиль бренда ещё не собран")

    theme = _pick(chat_id, ask)
    seconds = seconds_from(ask)
    stop = b.stopwords()

    profile = desk.profile(b, SECTIONS)

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
                                 max_tokens=MAX_TOKENS, schema=SCHEMA)
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


# ── нарезка снятого ──────────────────────────────────────────────────
#
# Выбор кусков из длинной записи уехал Монтажёру (`orchestrator/cut.py`,
# `roles/cut.md`) 03.09. Он жил здесь второй работой роли и занимал
# 1899 знаков промпта, которые сценарист читал на каждый сценарий.
# Редактор Reels пишет то, что человеку предстоит произнести; выбирать
# границы в уже произнесённом это другая работа и другой вход.

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

    desk.drafted(chat_id, tid, rel)
    log.info("сценарий сохранён: %s", rel)
    return rel


# ── карточка и кнопки ─────────────────────────────────────────────────

def _recover(chat_id: int, theme_id: str) -> Reel | None:
    """Поднять сценарий из базы, если память процесса его не помнит.

    Блоки обратно из файла не разбираются: на диске лежит суфлёр, и
    границы блоков в нём стёрты намеренно. Кнопкам хватает темы, а
    правка всё равно пересобирает сценарий заново.
    """
    row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                 theme_id, chat_id)
    return Reel(theme=dict(row)) if row is not None else None


table = desk.Desk("reels", corrections="voice-corrections.md",
                  recover=_recover)


def wants_fix(chat_id: int) -> bool:
    return table.wants_fix(chat_id)


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


async def run(reg, chat_id: int, ask: str, topic: str = "reels") -> None:
    table.clear(chat_id)

    async def say(text: str) -> None:
        await reg.say("reels", chat_id, text, topic=topic)

    try:
        reel = await build(chat_id, ask, say=say)
    except NoWork as e:
        await say(f"Снимать нечего: {e}. Нужна тема в формате reels "
                  "или shorts — это к Стратегу.")
        return
    except ScriptRefused as e:
        await say(f"Сценарий не прошёл проверку за два круга: {e}.\n\n"
                  "Выдавать с оговоркой не буду. Скажи, что поправить, "
                  "или возьмём другую тему.")
        return
    except agent.BudgetExceeded as e:
        await say(f"Остановился: {e}")
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("сценарий не собрался")
        await say(f"Сценарий не собрался: {desk.reason(e)}")
        return

    _save(chat_id, desk.brand(chat_id), reel)
    table.hold(chat_id, reel)

    await say(card(reel))
    # Суфлёр отдельным сообщением и без подписи роли: его копируют
    # целиком в телефон, и служебная строка сверху уедет вместе с ним.
    await reg.say("reels", chat_id, reel.script, topic=topic,
                  with_label=False, kb=_kb(reel.theme["id"]))


async def revise(reg, chat_id: int, instruction: str,
                 topic: str = "reels") -> None:
    """Пересобрать сценарий по правке человека."""
    reel = table.take(chat_id)
    if reel is None:
        await reg.say("reels", chat_id, "Этот сценарий уже неактуален.",
                      topic=topic)
        return

    table.note(chat_id, f"{reel.theme['id']} (сценарий)", instruction)
    await run(reg, chat_id,
              f"Правка к сценарию темы {reel.theme['id']} "
              f"({reel.seconds} сек): {instruction}", topic=topic)


async def on_callback(reg, chat_id: int, action: str,
                      topic: str = "reels") -> None:
    action, _, theme_id = action.partition(":")

    async def say(text: str) -> None:
        await reg.say("reels", chat_id, text, topic=topic)

    if action == "fix":
        reel = table.get(chat_id, theme_id)
        if reel is None:
            await say("Этот сценарий уже неактуален.")
            return
        table.await_fix(chat_id, reel)
        await say("Напиши одним сообщением, что поправить. Запишу правку "
                  "в профиль голоса и перепишу сценарий.")
        return

    if action not in {"ok", "design"}:
        return

    reel = table.take(chat_id, theme_id)
    if reel is None:
        await say("Этот сценарий уже неактуален.")
        return

    tid = reel.theme["id"]
    desk.ready(chat_id, tid)

    if action == "ok":
        await say(f"Готово, <code>{tid}</code> в статусе ready. "
                  "Обложку соберёт Дизайнер, подпись под видео "
                  "напишет Редактор.")
        return

    await say(f"Принял сценарий <code>{tid}</code> и передаю "
              "Дизайнеру на обложку.")
    await design.run(reg, chat_id, f"свёрстай обложку по теме {tid}",
                     topic="design")
