"""Редактор: из темы в готовый текст.

Вход — тема из плана (`themes.status = 'idea'`), выход — текст под одну
площадку в `posts/{id}.md`, статус темы `draft`.

Здесь впервые работает `validators/check_voice.py`. Правило каркаса:
**скрипт даёт отказ, модель даёт предупреждение.** Поэтому текст с
длинным тире или стоп-словом бренда не уезжает в чат с оговоркой, а
возвращается Редактору на переписывание с перечнем находок.

Кругов переделки два. Упереться в счётчик лучше, чем в счёт.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import cfg
from orchestrator import agent, design
from orchestrator.strategy import AUTO_PUBLISH, PLATFORMS
from storage import brand as brand_store
from storage import db
from validators import check_voice

log = logging.getLogger("editor")

store = brand_store.Store(cfg.brands_path)

MAX_ROUNDS = 2
MAX_TOKENS = 8000
PROFILE_LIMIT = 8000
VOICE_FLOOR = 3              # балл voice ниже — автоматический отказ

# Профиль Редактору нужен весь про голос: он им и пишет.
SECTIONS = ("Кто это", "Аудитория", "Голос", "Формат")

# Что критично на площадке. Дефолт продукта: `platforms.md` с нормами
# бренда собирается на шагах онбординга O13–O15, которых пока нет.
PLATFORM_SPEC = {
    "telegram": (
        "Сильная первая строка: её видно в списке чатов и в уведомлении. "
        "Абзацы по 1–3 строки, между блоками воздух. Разметка рендерится: "
        "<b>, <i>, <code>, <a href>. Ссылки живые. Хештеги в конце, "
        "если они есть у бренда."),
    "instagram": (
        "Первые ~125 знаков видно до кнопки «ещё», вся работа там. "
        "Разметка НЕ рендерится, пишешь простым текстом. Абзацы разделяешь "
        "пустой строкой. Хештеги в конце отдельным блоком."),
    "youtube": (
        "Площадка это поисковик. Первые две строки словами, которыми "
        "человек ищет, они видны до «ещё». Разметка НЕ рендерится. "
        "Дальше содержание видео и ссылки. Таймкоды, если они уместны."),
}


class NoTheme(RuntimeError):
    """Писать не о чем: подходящей темы в плане нет."""


class VoiceRefused(RuntimeError):
    """Текст не прошёл проверку голоса за отведённые круги."""


@dataclass
class Draft:
    theme: dict[str, Any]
    text: str = ""
    checks: dict[str, int] = field(default_factory=dict)
    hold: str = ""
    breaks: str = ""
    notes: list[str] = field(default_factory=list)
    rounds: int = 0

    @property
    def voice(self) -> int:
        return int(self.checks.get("voice", 0) or 0)

    def score(self) -> str:
        if not self.checks:
            return "самопроверка не пришла"
        return ", ".join(f"{k} {v}" for k, v in self.checks.items())


# ── выбор темы ────────────────────────────────────────────────────────

ID_RX = re.compile(r"\b\d{4}-\d{2}-\d{2}-[a-z]+-\d{2}\b")


def _pick(chat_id: int, ask: str) -> dict[str, Any]:
    """Какую тему писать.

    По убыванию точности: явный id в сообщении, совпадение по заголовку,
    ближайшая по дате неначатая тема. Угадывать молча нельзя, поэтому
    выбранная тема всегда называется человеку в шапке.
    """
    rows = db.q("SELECT * FROM themes WHERE chat_id = ? AND status = 'idea' "
                "ORDER BY date", chat_id)
    if not rows:
        raise NoTheme("в плане нет ни одной неначатой темы")

    if m := ID_RX.search(ask):
        for r in rows:
            if r["id"] == m.group():
                return dict(r)
        raise NoTheme(f"темы {m.group()} нет среди неначатых")

    # Совпадение по словам заголовка: «напиши пост про файл ЯДРО».
    words = {w for w in re.findall(r"\w{4,}", ask.lower())}
    if words:
        best, hits = None, 0
        for r in rows:
            title = set(re.findall(r"\w{4,}", (r["title"] or "").lower()))
            n = len(words & title)
            if n > hits:
                best, hits = r, n
        if best is not None and hits >= 2:
            return dict(best)

    return dict(rows[0])


# ── профиль ───────────────────────────────────────────────────────────

def _brand(chat_id: int):
    row = db.one("SELECT brand_slug FROM tenants WHERE chat_id = ?", chat_id)
    return store.get(row["brand_slug"]) if row and row["brand_slug"] else None


def _stopwords(b) -> list[str]:
    """Стоп-слова бренда из раздела «Голос» профиля.

    Их проверяет скрипт, а не модель: слово из стоп-листа это отказ, и
    отказ должен быть детерминированным.
    """
    voice = b.section("core", "Голос")
    if not voice:
        return []
    tail = voice.split("Стоп-слова", 1)
    if len(tail) < 2:
        return []
    out = []
    for line in tail[1].splitlines():
        line = line.strip()
        if line.startswith("###") or line.startswith("##"):
            break
        if line.startswith("- "):
            out.append(line[2:].strip())
    return [w for w in out if w]


def _brief(theme: dict[str, Any], spec: str) -> str:
    """Задание Редактору: тема, площадка, что критично."""
    lines = ["## Тема из плана", ""]
    for label, key in (("id", "id"), ("дата", "date"), ("площадка", "plat"),
                       ("формат", "format"), ("рубрика", "rubric"),
                       ("цель", "goal"), ("архетип", "arch"),
                       ("рабочий заголовок", "title"), ("хук", "hook"),
                       ("кому и зачем", "why"), ("угол", "angle"),
                       ("ведущий заряд", "charge")):
        if theme.get(key):
            lines.append(f"- {label}: {theme[key]}")

    lines += ["", "## Спецификация площадки", "", spec, "",
              "Рабочий заголовок и хук это заготовка Стратега, а не финал. "
              "Доводить формулировку до готовой — твоя работа."]
    return "\n".join(lines)


# ── сборка ────────────────────────────────────────────────────────────

async def build(chat_id: int, ask: str, *, say=None) -> Draft:
    """Написать текст по теме, переписывая пока скрипт даёт отказ.

    `say` — куда сообщать о ходе работы. Круг модели это десятки секунд,
    два круга уходят за минуту, и молчащий бот в это время неотличим от
    сломанного.
    """
    b = _brand(chat_id)
    if b is None:
        raise NoTheme("профиль бренда ещё не собран")

    theme = _pick(chat_id, ask)
    plat = theme.get("plat") or "telegram"
    spec = PLATFORM_SPEC.get(plat, PLATFORM_SPEC["telegram"])
    stop = _stopwords(b)

    parts = [s for s in (b.section("core", n) for n in SECTIONS) if s]
    profile = ("\n\n".join(parts) or b.read("core"))[:PROFILE_LIMIT]

    draft = Draft(theme=theme)
    extra = ""

    if say:
        await say(f"Пишу текст по теме <b>{theme.get('title') or theme['id']}</b> "
                  f"({theme.get('plat')} · {theme.get('format')}).\n"
                  "Это займёт до минуты.")

    for attempt in range(1, MAX_ROUNDS + 1):
        draft.rounds = attempt
        prompt = (_brief(theme, spec) + extra +
                  "\n\nОтветь одним JSON-объектом в формате из твоей секции "
                  "«Формат выдачи».")
        if ask.strip():
            prompt += f"\n\n## Что сказал человек\n\n{ask.strip()}"

        answer = await agent.ask("editor", chat_id, prompt,
                                 brand_name=b.name(), profile=profile,
                                 max_tokens=MAX_TOKENS)
        data = agent.parse_json(answer, who="редактор")
        draft.text = str(data.get("text") or "").strip()
        draft.checks = {k: int(v) for k, v in (data.get("checks") or {}).items()
                        if isinstance(v, (int, float))}
        draft.hold = str(data.get("hold") or "")
        draft.breaks = str(data.get("breaks") or "")
        draft.notes = [str(n) for n in (data.get("notes") or [])]

        if not draft.text:
            raise VoiceRefused("Редактор вернул пустой текст")

        findings = check_voice.check(draft.text, stopwords=stop)
        log.info("круг %s: %s знаков, самопроверка [%s], находок %s",
                 attempt, len(draft.text), draft.score(), len(findings))

        if not findings and draft.voice >= VOICE_FLOOR:
            return draft

        if attempt == MAX_ROUNDS:
            why = "; ".join(str(f) for f in findings[:5]) or \
                  f"балл voice {draft.voice} ниже {VOICE_FLOOR}"
            raise VoiceRefused(why)

        if say:
            # Называем правило, а не находку целиком: в находке лежит
            # кусок забракованного текста, и ему в чате не место.
            why = ", ".join(dict.fromkeys(f.rule for f in findings)) \
                or f"балл voice {draft.voice}"
            await say(f"Первый вариант не прошёл проверку голоса ({why}). "
                      "Переписываю.")

        # Находки возвращаются текстом: модель должна видеть, что именно
        # поймал скрипт, иначе второй круг повторит ту же ошибку.
        problems = [str(f) for f in findings]
        if draft.voice < VOICE_FLOOR:
            problems.append(f"твой собственный балл voice {draft.voice}: "
                            "по правилу это отказ")
        extra = ("\n\n## Прошлый вариант отклонён\n\n"
                 "Скрипт проверки поймал:\n"
                 + "\n".join(f"- {p}" for p in problems)
                 + "\n\nПерепиши текст целиком. Не оговаривайся, не объясняй "
                   "правку в тексте поста.\n\n## Отклонённый текст\n\n"
                 + draft.text)

    raise VoiceRefused("круги переделки исчерпаны")


def _save(chat_id: int, b, draft: Draft) -> str:
    """Текст в файл, путь и статус в базу."""
    tid = draft.theme["id"]
    rel = f"posts/{tid}.md"
    head = (f"<!-- {tid} · {draft.theme.get('plat')} · "
            f"{draft.theme.get('format')} · {len(draft.text)} знаков -->\n\n")
    b.artifact(rel, head + draft.text)

    with db.tx() as c:
        c.execute("UPDATE themes SET status = 'draft', asset = ?, "
                  "updated_at = datetime('now') WHERE id = ? AND chat_id = ?",
                  (rel, tid, chat_id))
    log.info("текст сохранён: %s", rel)
    return rel


# ── карточка и кнопки ─────────────────────────────────────────────────

_pending: dict[int, Draft] = {}
_awaiting_fix: set[int] = set()


def _kb(theme_id: str) -> InlineKeyboardMarkup:
    """id темы едет в самой кнопке.

    Черновик живёт в памяти процесса, а бот перезапускается. Без id
    любая кнопка под карточкой, пережившей рестарт, отвечает «уже
    неактуален» — и человек, который вчера согласовал текст, сегодня
    не может его принять. 64 байта Telegram на это хватает.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Ок", callback_data=f"post:ok:{theme_id}"),
        InlineKeyboardButton(text="✏️ Правки", callback_data=f"post:fix:{theme_id}"),
        InlineKeyboardButton(text="🎨 В дизайн",
                             callback_data=f"post:design:{theme_id}"),
    ]])


def card(draft: Draft) -> str:
    """Текст плюс служебная шапка. Самопроверка в чат не идёт."""
    t = draft.theme
    head = (f"✍️ <b>{t.get('date')} · {t.get('plat')} · {t.get('format')}</b>\n"
            f"<code>{t['id']}</code> · {len(draft.text)} знаков")
    out = [head, "", draft.text]
    if draft.notes:
        out += ["", "⚠️ " + "; ".join(draft.notes)]
    return "\n".join(out)


def wants_fix(chat_id: int) -> bool:
    return chat_id in _awaiting_fix


async def run(reg, chat_id: int, ask: str, topic: str = "review") -> None:
    _awaiting_fix.discard(chat_id)

    async def say(text: str) -> None:
        await reg.say("editor", chat_id, text, topic=topic)

    try:
        draft = await build(chat_id, ask, say=say)
    except NoTheme as e:
        await reg.say("editor", chat_id,
                      f"Писать не о чем: {e}. Сначала план недели.",
                      topic=topic)
        return
    except VoiceRefused as e:
        await reg.say("editor", chat_id,
                      f"Текст не прошёл проверку голоса за два круга: {e}.\n\n"
                      "Выдавать с оговоркой не буду. Скажи, что поправить, "
                      "или начнём с другой темы.", topic=topic)
        return
    except agent.BudgetExceeded as e:
        await reg.say("editor", chat_id, f"Остановился: {e}", topic=topic)
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("текст не собрался")
        reason = getattr(e, "message", None) or str(e) or type(e).__name__
        await reg.say("editor", chat_id, f"Текст не собрался: {reason}",
                      topic=topic)
        return

    b = _brand(chat_id)
    _save(chat_id, b, draft)
    _pending[chat_id] = draft

    log.info("%s: hold=%s | breaks=%s", draft.theme["id"], draft.hold,
             draft.breaks)
    await reg.say("editor", chat_id, card(draft),
                  kb=_kb(draft.theme["id"]), topic=topic)


async def revise(reg, chat_id: int, instruction: str,
                 topic: str = "review") -> None:
    """Пересобрать текст по правке человека.

    Правка это обучающий сигнал, а не разовая просьба: она дописывается
    в `voice-corrections.md` бренда, чтобы следующий текст её уже учитывал.
    """
    _awaiting_fix.discard(chat_id)
    draft = _pending.get(chat_id)
    if draft is None:
        await reg.say("editor", chat_id, "Этот текст уже неактуален.",
                      topic=topic)
        return

    b = _brand(chat_id)
    if b is not None:
        b.append("voice-corrections.md",
                 f"- {draft.theme['id']}: {instruction.strip()}")

    await run(reg, chat_id,
              f"Правка к тексту темы {draft.theme['id']}: {instruction}",
              topic=topic)


def _recover(chat_id: int, theme_id: str) -> Draft | None:
    """Поднять черновик из базы, если память процесса его не помнит."""
    row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                 theme_id, chat_id)
    if row is None:
        return None
    b = _brand(chat_id)
    text = ""
    if b is not None and row["asset"]:
        raw = b.read(row["asset"])
        text = raw.split("-->", 1)[-1].strip() if raw.startswith("<!--") else raw
    return Draft(theme=dict(row), text=text)


async def on_callback(reg, chat_id: int, action: str,
                      topic: str = "review") -> None:
    action, _, theme_id = action.partition(":")
    draft = _pending.get(chat_id)
    if draft is None and theme_id:
        draft = _recover(chat_id, theme_id)
    elif draft is not None and theme_id and draft.theme["id"] != theme_id:
        # Нажали кнопку под старой карточкой, а в памяти уже другая тема.
        draft = _recover(chat_id, theme_id)

    if action == "ok":
        _pending.pop(chat_id, None)
        _awaiting_fix.discard(chat_id)
        if draft is None:
            await reg.say("editor", chat_id, "Этот текст уже неактуален.",
                          topic=topic)
            return
        tid = draft.theme["id"]
        with db.tx() as c:
            c.execute("UPDATE themes SET status = 'ready', "
                      "updated_at = datetime('now') "
                      "WHERE id = ? AND chat_id = ?", (tid, chat_id))
        plat = draft.theme.get("plat")
        tail = ("Публикатор ещё не подключён, выкладываешь сама."
                if plat in AUTO_PUBLISH else
                f"{plat} публикуется руками в любом случае.")
        await reg.say("editor", chat_id,
                      f"Готово, <code>{tid}</code> в статусе ready. {tail}",
                      topic=topic)
        return

    if action == "fix":
        if draft is None:
            await reg.say("editor", chat_id, "Этот текст уже неактуален.",
                          topic=topic)
            return
        _awaiting_fix.add(chat_id)
        await reg.say("editor", chat_id,
                      "Напиши одним сообщением, что поправить. Запишу правку "
                      "в профиль голоса и перепишу.", topic=topic)
        return

    if action == "design":
        _pending.pop(chat_id, None)
        _awaiting_fix.discard(chat_id)
        if draft is None:
            await reg.say("editor", chat_id, "Этот текст уже неактуален.",
                          topic=topic)
            return

        # «В дизайн» это и приёмка текста тоже: Дизайнер работает только с
        # `ready`, а отправлять в вёрстку неутверждённый текст незачем.
        tid = draft.theme["id"]
        with db.tx() as c:
            c.execute("UPDATE themes SET status = 'ready', "
                      "updated_at = datetime('now') "
                      "WHERE id = ? AND chat_id = ?", (tid, chat_id))
        await reg.say("editor", chat_id,
                      f"Принял текст <code>{tid}</code> и передаю Дизайнеру.",
                      topic=topic)
        await design.run(reg, chat_id, f"свёрстай макет по теме {tid}",
                         topic="design")
