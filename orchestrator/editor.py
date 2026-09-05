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
from orchestrator import agent, desk, design, research
from orchestrator.desk import NoWork
from orchestrator.strategy import AUTO_PUBLISH, SPOKEN
from storage import db
from validators import check_voice

log = logging.getLogger("editor")

MAX_ROUNDS = 2
# Мышление на Opus 5 считается в тот же потолок, что и ответ. На 8000
# длинный пост с рассуждением обрывался на середине — а обрыв это битый
# JSON, то есть второй круг и удвоенное время на ровном месте.
MAX_TOKENS = 16000

# Схема ответа. Форму гарантирует API, а не просьба в промпте: до этого
# сорвавшийся ответ («вот текст поста:» перед скобкой) стоил круга, а их
# всего два. Схема закрытая — все поля обязательны, лишних нет, — иначе
# структурированный вывод её не примет. Держать в согласии с секцией
# «Формат выдачи» в roles/editor.md: расходятся — модель выполнит схему,
# а промпт соврёт человеку.
SCHEMA = {
    "type": "object",
    "properties": {
        "theme_id": {"type": "string"},
        "text": {"type": "string"},
        "checks": {
            "type": "object",
            "properties": {
                "hook": {"type": "integer"},
                "recognition": {"type": "integer"},
                "pass_on": {"type": "integer"},
                "voice": {"type": "integer"},
                "freshness": {"type": "integer"},
            },
            "required": ["hook", "recognition", "pass_on", "voice",
                         "freshness"],
            "additionalProperties": False,
        },
        "hold": {"type": "string"},
        "breaks": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["theme_id", "text", "checks", "hold", "breaks", "notes"],
    "additionalProperties": False,
}
VOICE_FLOOR = 3              # балл voice ниже — автоматический отказ

# Профиль Редактору нужен весь про голос: он им и пишет.
SECTIONS = ("Кто это", "Аудитория", "Голос", "Формат")

# Что критично на площадке. Дефолт продукта: нормы бренда живут в его
# `platforms.md`, и раздел «Формат» профиля сильнее этого словаря везде,
# где они расходятся.
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

# Что критично в формате. Матрица полная: у каждого формата из набора
# Стратега (`strategy.PLATFORMS`), кроме произносимых вслух, здесь своя
# строка — их пишет Редактор Reels.
#
# До 05.09 этого словаря не было вовсе, и формат темы до модели не
# доезжал: спецификация приходила только по площадке, а «опрос»,
# «карусель» и «сторис» модель достраивала сама. Прогон по всем семи
# форматам показал, чем это кончается. Опрос вышел постом, а варианты
# голосования легли прозой внутрь текста с припиской «Публикатору нужно
# перенести их в поле вариантов Telegram» — которого у Публикатора нет.
# Карусель пришла сплошным текстом, хотя Дизайнер верстает её шестью
# карточками и пустого слота не примет. Сторис пришла тем же куском, что
# и пост. Ошибка тихая: текст каждый раз читается нормально, просто он
# не того формата, который заказан.
#
# `(площадка, None)` — запасная строка для темы без формата.
FORMAT_SPEC = {
    ("telegram", "пост"): (
        "Разбор или размышление, 600–1000 знаков. Заголовок первой "
        "строкой в <b>. Одна главная мысль в <blockquote>. Отступление "
        "целым абзацем в <i>. Закрытие — одно действие на сегодня."),
    ("telegram", "анонс"): (
        "Карточка, а не размышление. Порядок: тема с номером части, "
        "строка 📅 дата 🕕 время 📍 место, «Повестка» нумерованным "
        "списком из четырёх пунктов, «Что подготовить заранее» списком, "
        "строка «Уйдёте с …», условие участия и как придёт ссылка, блок "
        "хештегов. Личных вступлений, лирики, цитат и спойлеров нет. "
        "Нет времени или места — [уточнить факт], не выдумывай: по этой "
        "строке человек приходит или не приходит."),
    ("telegram", "раздача"): (
        "Короткая передача материала, 300–600 знаков: приветствие, что "
        "внутри списком, ссылка. Без подводки на три абзаца. Ссылки в "
        "задании нет — [уточнить факт] и строка в notes, что без файла "
        "публиковать нечего."),
    ("telegram", "опрос"): (
        "Вопрос залу, 200–500 знаков: сцена в двух строках, потом сам "
        "вопрос. Варианты последним блоком, нумерованным списком, 4–6 "
        "штук, каждый короче семи слов. Нативного опроса завод не "
        "отправляет: Публикатор шлёт обычное сообщение, голосование "
        "человек заводит руками — скажи это строкой в notes."),
    ("telegram", None): (
        "Формат у темы не проставлен. Пиши постом-разбором и назови это "
        "строкой в notes."),
    ("instagram", "карусель"): (
        "Шесть блоков, по одному на карточку, разделены пустой строкой: "
        "обложка-хук, что произошло, три пункта, финал. Обложка до 90 "
        "знаков, пункт до 200. Каждый блок держится сам по себе. Шесть "
        "ровно: столько карточек верстает Дизайнер, и слова на карточку "
        "он берёт только из твоего текста. Седьмой блок он выбросит, "
        "недостающий шестой ему взять неоткуда. "
        "Каждый из трёх пунктов несёт конкретику — шаг, цифру, механику "
        "или различение, — а не общее место: перечисление ролей и "
        "названий это не пункт. Финал несёт факт: дату или цифру. "
        "Дальше строка «## Подпись» отдельным абзацем и подпись под "
        "карусель, 600–900 знаков: на карточку помещается заголовок и "
        "одна фраза, поэтому польза целиком живёт здесь, а карточки её "
        "обещают. Хештеги в конце подписи. Строка-разделитель ровно "
        "такая: по ней код отделяет карточки от подписи, свою выдумывать "
        "нельзя."),
    ("instagram", "сторис"): (
        "Один кадр, до 200 знаков. Живая сцена, а не инструкция. "
        "Списков, повестки и призывов нет, один эмодзи в закрытии. "
        "Длиннее — это уже пост, а не сторис."),
    ("instagram", None): (
        "Формат у темы не проставлен. Пиши подписью под кадр и назови "
        "это строкой в notes."),
    ("youtube", "видео"): (
        "Описание под ролик. Первые две строки словами, которыми человек "
        "ищет. Дальше содержание ролика списком, что нужно для повтора, "
        "ссылки. Таймкоды не выдумывай: нет монтажа — строка в notes."),
    ("youtube", None): (
        "Формат у темы не проставлен. Пиши описанием под ролик и назови "
        "это строкой в notes."),
}


def spec(plat: str | None, fmt: str | None) -> tuple[str, str]:
    """Что критично на площадке и что критично в формате.

    Площадку и формат берём порознь: незнакомая площадка сваливается на
    Telegram, незнакомый формат — на запасную строку своей площадки. Обе
    подмены молчаливые по необходимости (текст всё равно надо написать),
    но запасная строка формата просит модель назвать подмену в `notes` —
    человек должен видеть, что формат до неё не доехал.
    """
    plat = plat or "telegram"
    if plat not in PLATFORM_SPEC:
        plat = "telegram"
    fmt = (fmt or "").strip() or None
    if (plat, fmt) not in FORMAT_SPEC:
        fmt = None
    return PLATFORM_SPEC[plat], FORMAT_SPEC[(plat, fmt)]


# ── чем калибруется голос ─────────────────────────────────────────────
#
# Два блока, и оба про то, как бренд звучит на самом деле, а не про то,
# как он себя описал. Живые тексты (`voice-samples/`) и правки, которые
# человек уже говорил вслух (`voice-corrections.md`).
#
# Оба лежат в стабильной части промпта, а не в теле запроса. Внутри
# тенанта они не меняются от поста к посту, и в хвосте за них платили бы
# полную цену на каждом круге переделки. Порядок внутри блока тот же
# закон префикса: образцы почти не меняются, правки прибавляются после
# каждой кнопки «Правки», — поэтому образцы идут первыми.

def _voice_stable(samples: list[tuple[str, str]],
                  learned: list[str]) -> str:
    """Как бренд звучит и что ему уже правили. Пусто — блока нет вовсе."""
    parts: list[str] = []

    if samples:
        out = ["## Как звучит бренд", "",
               f"Живых текстов бренда {len(samples)}. Это образцы голоса, "
               "а не темы и не источник фактов: по ним сверяют интонацию, "
               "длину фразы, чем текст начинается и чем кончается. "
               "Цитировать их, продолжать и брать из них темы нельзя — "
               "они про прошлое, а пишешь ты про заказанное."]
        for name, text in samples:
            out += ["", f"### {name}", "", text]
        parts.append("\n".join(out))

    if learned:
        parts.append("\n".join(
            ["## Правки, которые человек уже называл", "",
             "Он говорил это про прошлые тексты. Правка сильнее "
             "спецификации формата везде, где они расходятся: "
             "спецификация это дефолт продукта, а правка — слова "
             "владельца голоса.", ""]
            + [f"- {ln}" for ln in learned]))

    return "\n\n".join(parts)


class VoiceRefused(RuntimeError):
    """Текст не прошёл проверку голоса за отведённые круги."""


class LandRefused(RuntimeError):
    """Текст с моста не сел: нет темы, пустой текст, находка валидатора."""


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

def _pick(chat_id: int, ask: str) -> dict[str, Any]:
    """Ролики пишет Редактор Reels: суфлёрный текст это другая работа.

    Названную явно тему берём любую, а на «напиши пост» ближайший слот
    под ролик утаскивать нельзя — Редактору Reels нечего будет снимать,
    а человек получит пост вместо сценария.
    """
    return desk.pick(
        chat_id, ask, statuses=("idea", "draft"),
        suits=lambda r: (r["format"] or "").lower() not in SPOKEN,
        wrong="тема {id} под ролик, её пишет Редактор Reels",
        none="темы {id} нет среди начатых и неначатых",
        empty="неначатых тем под текст нет, есть только под ролики — "
              "это к Редактору Reels")


def _brief(theme: dict[str, Any], plat_spec: str, fmt_spec: str,
           facts: str = "") -> str:
    """Задание Редактору: тема, площадка, формат, что критично в каждом.

    Два блока спецификации, а не один: площадка задаёт, как текст
    выглядит в ленте, формат — какую работу он делает. Пост и опрос в
    одном Telegram расходятся сильнее, чем пост в Telegram и пост в
    Instagram.

    `facts` — фактура, собранная Ресёрчером под эту тему
    (`research.facts`). Её может не быть, и это нормальный случай: до
    05.09 её не было вовсе. Есть — это единственные чужие цифры, которые
    в текст можно ставить: у них есть канал и дата.
    """
    lines = (["## Тема из плана", ""] + desk.brief(theme) +
             ["", "## Спецификация площадки", "", plat_spec,
              "", "## Спецификация формата", "", fmt_spec, ""])
    if facts:
        lines += ["## Фактура под тему", "",
                  "Собрана Ресёрчером из чужих каналов. Цифру или "
                  "исследование отсюда ставить можно, называя источник и "
                  "дату. Чего здесь нет — того нет: по памяти чужие "
                  "цифры не добавляются.", "", facts, ""]
    lines.append("Рабочий заголовок и хук это заготовка Стратега, а не "
                 "финал. Доводить формулировку до готовой — твоя работа.")
    return "\n".join(lines)


# ── сборка ────────────────────────────────────────────────────────────

async def build(chat_id: int, ask: str, *, say=None) -> Draft:
    """Написать текст по теме, переписывая пока скрипт даёт отказ.

    `say` — куда сообщать о ходе работы. Круг модели это десятки секунд,
    два круга уходят за минуту, и молчащий бот в это время неотличим от
    сломанного.
    """
    b = desk.brand(chat_id)
    if b is None:
        raise NoWork("профиль бренда ещё не собран")

    theme = _pick(chat_id, ask)
    plat_spec, fmt_spec = spec(theme.get("plat"), theme.get("format"))
    facts = research.facts_for(b, theme["id"])
    stop = b.stopwords()

    profile = desk.profile(b, SECTIONS)
    samples = desk.voice_samples(b)
    stable = _voice_stable(samples, table.learned(b))

    draft = Draft(theme=theme)
    extra = ""

    if say:
        await say(f"Пишу текст по теме <b>{theme.get('title') or theme['id']}</b> "
                  f"({theme.get('plat')} · {theme.get('format')})"
                  + (", фактура Ресёрчера есть" if facts else "") + ".\n"
                  "Это займёт до минуты.")

    for attempt in range(1, MAX_ROUNDS + 1):
        draft.rounds = attempt
        prompt = (_brief(theme, plat_spec, fmt_spec, facts) + extra +
                  "\n\nОтветь одним JSON-объектом в формате из твоей секции "
                  "«Формат выдачи».")
        if ask.strip():
            prompt += f"\n\n## Что сказал человек\n\n{ask.strip()}"

        answer = await agent.ask("editor", chat_id, prompt,
                                 brand_name=b.name(), profile=profile,
                                 stable=stable, max_tokens=MAX_TOKENS,
                                 schema=SCHEMA)
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
            # Сверить голос не с чем — это находка, а не пустяк. Молчание
            # здесь неотличимо от «всё сошлось», а на деле текст написан
            # по описанию голоса, и подтвердить его нечем.
            if not samples:
                draft.notes.append(
                    "живых текстов бренда нет: <code>voice-samples/</code> "
                    "пуста, голос сверен по описанию из профиля, а не по "
                    "образцам")
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

    desk.drafted(chat_id, tid, rel)
    log.info("текст сохранён: %s", rel)
    return rel


def land(chat_id: int, data: dict[str, Any]) -> tuple[Draft, str]:
    """Посадить текст, собранный субагентом через мост.

    Публичный шов, а не деталь `run`, — ровно как `strategy.land` у плана.
    Писать текст умеют два пути, а проверять его валидатором, класть в
    `posts/{id}.md` и переводить тему в `draft` должен один код.

    До этого текст с моста не возвращался в завод вовсе: он оставался в
    `tasks/{id}/`, а папка задачи в `.gitignore`. Человек читал пост в
    чате, соглашался с ним — и соглашаться было не с чем: в базе тема
    оставалась `idea`, Дизайнеру и Публикатору текста не доставалось.

    Внешняя проверка здесь настоящая. Субагент прогоняет `check_voice` на
    себе для быстрой обратной связи, но верить ему на слово нельзя:
    промпт это просьба, границу держит код. Находка — отказ, тема
    остаётся `idea`, и человек слышит об этом.

    Возвращает черновик и путь выгрузки.
    """
    b = desk.brand(chat_id)
    if b is None:
        raise LandRefused("профиля бренда нет, класть текст некуда")

    tid = str(data.get("theme_id") or "").strip()
    if not tid:
        raise LandRefused("в контракте нет `theme_id`: непонятно, к какой "
                          "теме относится текст")

    row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                 tid, chat_id)
    if row is None:
        raise LandRefused(f"темы {tid} нет в базе")
    if row["status"] == "skip":
        raise LandRefused(f"тема {tid} снята")

    text = str(data.get("text") or "").strip()
    if not text:
        raise LandRefused(f"текст по теме {tid} пуст")

    draft = Draft(
        theme=dict(row), text=text,
        checks={k: int(v) for k, v in (data.get("checks") or {}).items()
                if isinstance(v, (int, float))},
        hold=str(data.get("hold") or ""),
        breaks=str(data.get("breaks") or ""),
        notes=[str(n) for n in (data.get("notes") or [])])

    findings = check_voice.check(text, stopwords=b.stopwords())
    if findings:
        raise LandRefused(
            f"текст по теме {tid} не прошёл проверку голоса: "
            + ", ".join(dict.fromkeys(f.rule for f in findings)))
    if draft.checks and draft.voice < VOICE_FLOOR:
        raise LandRefused(f"текст по теме {tid} отклонён самим Редактором: "
                          f"балл voice {draft.voice} ниже {VOICE_FLOOR}")

    rel = _save(chat_id, b, draft)
    log.info("текст с моста посажен: %s", rel)
    return draft, rel


# ── карточка и кнопки ─────────────────────────────────────────────────

def _recover(chat_id: int, theme_id: str) -> Draft | None:
    """Поднять черновик из базы, если память процесса его не помнит."""
    row = db.one("SELECT * FROM themes WHERE id = ? AND chat_id = ?",
                 theme_id, chat_id)
    if row is None:
        return None
    b = desk.brand(chat_id)
    text = ""
    if b is not None and row["asset"]:
        raw = b.read(row["asset"])
        text = raw.split("-->", 1)[-1].strip() if raw.startswith("<!--") else raw
    return Draft(theme=dict(row), text=text)


table = desk.Desk("editor", corrections="voice-corrections.md",
                  recover=_recover)


def wants_fix(chat_id: int) -> bool:
    return table.wants_fix(chat_id)


def kb(theme_id: str, prefix: str = "post") -> InlineKeyboardMarkup:
    """id темы едет в самой кнопке.

    Черновик живёт в памяти процесса, а бот перезапускается. Без id
    любая кнопка под карточкой, пережившей рестарт, отвечает «уже
    неактуален» — и человек, который вчера согласовал текст, сегодня
    не может его принять. 64 байта Telegram на это хватает.

    Префикс говорит, чей это текст: `post` — старый Редактор, `bpost` —
    субагент через мост. Кнопки те же самые, а вот правка расходится:
    старому Редактору её отдаёт `revise`, субагенту — новый прогон, потому
    что он живёт ровно один ход и договорить с ним нельзя.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Ок",
                             callback_data=f"{prefix}:ok:{theme_id}"),
        InlineKeyboardButton(text="✏️ Правки",
                             callback_data=f"{prefix}:fix:{theme_id}"),
        InlineKeyboardButton(text="🎨 В дизайн",
                             callback_data=f"{prefix}:design:{theme_id}"),
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


def handoff(theme: dict[str, Any]) -> str:
    """Что будет с текстом дальше. Состояние из данных, а не из фразы:
    кнопка полторы недели отвечала, что Публикатора нет, когда он был."""
    plat = theme.get("plat")
    if plat not in AUTO_PUBLISH:
        return f"{plat} публикуется руками в любом случае."
    if not cfg.publish_channel:
        return ("Публикатор возьмёт его в очередь, когда будет задан канал: "
                "<code>PUBLISH_CHANNEL</code> пуст.")
    return "Публикатор покажет превью в «Очередь», когда придёт дата."


async def run(reg, chat_id: int, ask: str, topic: str = "review") -> None:
    table.clear(chat_id)

    async def say(text: str) -> None:
        await reg.say("editor", chat_id, text, topic=topic)

    try:
        draft = await build(chat_id, ask, say=say)
    except NoWork as e:
        await say(f"Писать не о чем: {e}. Сначала план недели.")
        return
    except VoiceRefused as e:
        await say(f"Текст не прошёл проверку голоса за два круга: {e}.\n\n"
                  "Выдавать с оговоркой не буду. Скажи, что поправить, "
                  "или начнём с другой темы.")
        return
    except agent.BudgetExceeded as e:
        await say(f"Остановился: {e}")
        return
    except Exception as e:                                   # noqa: BLE001
        log.exception("текст не собрался")
        await say(f"Текст не собрался: {desk.reason(e)}")
        return

    _save(chat_id, desk.brand(chat_id), draft)
    table.hold(chat_id, draft)

    log.info("%s: hold=%s | breaks=%s", draft.theme["id"], draft.hold,
             draft.breaks)
    await reg.say("editor", chat_id, card(draft),
                  kb=kb(draft.theme["id"]), topic=topic)


async def revise(reg, chat_id: int, instruction: str,
                 topic: str = "review") -> None:
    """Пересобрать текст по правке человека."""
    draft = table.take(chat_id)
    if draft is None:
        await reg.say("editor", chat_id, "Этот текст уже неактуален.",
                      topic=topic)
        return

    table.note(chat_id, draft.theme["id"], instruction)
    await run(reg, chat_id,
              f"Правка к тексту темы {draft.theme['id']}: {instruction}",
              topic=topic)


async def on_callback(reg, chat_id: int, action: str,
                      topic: str = "review") -> None:
    action, _, theme_id = action.partition(":")

    async def say(text: str) -> None:
        await reg.say("editor", chat_id, text, topic=topic)

    if action == "fix":
        draft = table.get(chat_id, theme_id)
        if draft is None:
            await say("Этот текст уже неактуален.")
            return
        table.await_fix(chat_id, draft)
        await say("Напиши одним сообщением, что поправить. Запишу правку "
                  "в профиль голоса и перепишу.")
        return

    if action not in {"ok", "design"}:
        return

    draft = table.take(chat_id, theme_id)
    if draft is None:
        await say("Этот текст уже неактуален.")
        return

    # «В дизайн» это и приёмка текста тоже: Дизайнер работает только с
    # `ready`, а отправлять в вёрстку неутверждённый текст незачем.
    tid = draft.theme["id"]
    desk.ready(chat_id, tid)

    if action == "ok":
        await say(f"Готово, <code>{tid}</code> в статусе ready. "
                  + handoff(draft.theme))
        return

    await say(f"Принял текст <code>{tid}</code> и передаю Дизайнеру.")
    await design.run(reg, chat_id, f"свёрстай макет по теме {tid}",
                     topic="design")
