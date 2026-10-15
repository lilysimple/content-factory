"""Распаковка клиента: от присланных материалов к папке бренда.

Живёт в личке, один на один с человеком, который завода ещё не видел.
Три шага: собрать материалы, показать срез и первичный профиль, добрать
опросником то, чего в материалах не нашлось. На выходе — папка бренда с
файлами, которые читает завод.

Почему это отдельный модуль, а не ветка `onboarding.py`: там анкета
тенанта-супергруппы, где боты уже добавлены, топики размечены и профиль
пишется в свой бренд. Здесь человек ещё не тенант и, может быть, им не
станет. Общее у них одно — `unpack.run`, и оно вынесено туда.

**Что бот читает сам, а что просит файлом.** Telegram отдаётся публично и
полно (`t.me/s/`), текст сайта тоже. В Instagram бот не ходит: в свой
аккаунт нас никто не пускал, а наш Chrome ловит 429 по IP. Оттуда данные
приходят выгрузкой Meta Business Suite, и это честнее скрейпинга — в
выгрузке есть охват, сохранения и репосты, которых в публичной странице
нет вовсе. LinkedIn и TikTok без входа не отдают ничего, и обещать их
чтение нельзя: они закрываются опросником.

**Файл пишется только по подтверждённому блоку.** Правило из спеки, и
здесь оно дороже, чем на онбординге: человек со стороны не станет
вычитывать шесть файлов, он посмотрит карточку и нажмёт кнопку. Значит
карточка обязана называть дыры вслух, а не показывать красивый профиль,
собранный на догадках.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from orchestrator import agent, files, instagram, refresh, research, sources
from orchestrator import unpack
from storage import brand as brand_store

log = logging.getLogger("intake")

# Сколько вопросов человек ответит за один заход. Больше — он бросит на
# середине, и в папке останется половина файлов без пометки, что это
# половина. Проверено не здесь, но правило то же, что у трёх спорных
# мест на онбординге.
QUIZ_LIMIT = 6

# Потолок одного ответа. Человек, вставивший в опросник свою стратегию
# целиком, не ошибся — но ответ на вопрос «где хотите быть через полгода»
# длиной в двадцать тысяч знаков это не ответ, это материал. Такой текст
# уходит в материалы и перечитывается распаковкой.
ANSWER_CHARS = 4000
AS_MATERIAL = 6000


@dataclass(frozen=True)
class Need:
    """Файл профиля: что в нём лежит и по каким заголовкам его читают."""
    key: str
    title: str
    what: str
    headings: tuple[str, ...]


# Утверждённый список: что должно лежать в папке бренда, чтобы завод
# работал целиком. `core` собирает распаковка (`unpack.to_core_md`),
# остальные пять — модель по ответам опросника.
#
# Порядок неслучаен: он же порядок опросника. Сверху то, без чего нельзя
# написать ни одного поста, снизу то, что улучшает уже работающее.
NEEDS: tuple[Need, ...] = (
    Need("core", "ЯДРО",
         "кто это, аудитория и её боли, голос бренда, формат постов",
         ("Кто это", "Аудитория", "Голос бренда", "Формат")),
    Need("goals", "Цели и метрики",
         "где бренд сейчас и где хочет быть, линейка продуктов и цен, "
         "пропорция воронки прогрев/продукт/личное",
         ("Точка А", "Точка Б", "Линейка", "Пропорция воронки")),
    Need("platforms", "Площадки",
         "какая площадка дом, какая для охвата, какая для доверия, "
         "сколько выходов в неделю и нормы оформления каждой",
         ("Связка", "Регулярность", "Нормы площадок")),
    Need("rubrics", "Рубрикатор",
         "постоянные рубрики бренда: название, о чём, как часто",
         ("Рубрики",)),
    Need("hooks", "Хуки и продвижение",
         "чем бренд ловит внимание в первой строке и что делает после "
         "публикации",
         ("Механика хука", "Продвижение")),
    Need("sources", "Источники",
         "свои каналы и 3–5 конкурентов, кого Ресёрчер читает каждую неделю",
         ("Свои каналы", "Кого смотреть")),
)

BY_KEY = {n.key: n for n in NEEDS}

# Площадки, которые завод умеет вести. Список закрытый: `platforms.md` с
# площадкой, которой у завода нет, читается как обещание.
KNOWN_PLATFORMS = ("telegram", "instagram", "youtube", "linkedin", "email",
                   "vk", "tiktok", "shorts", "reels")

PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
HEADING = re.compile(r"^#{1,3}\s+(.+)$", re.M)

# Выгрузка Meta Business Suite. Имена колонок у неё разъезжаются между
# локалями и версиями кабинета, поэтому опознаётся она по паре, которая
# держится во всех: ссылка на пост и время публикации.
META_LINK = ("permalink", "ссылка", "post link")
META_TIME = ("publish time", "date", "время публикации", "дата")


# ── усиление: остальные пункты онбординга ─────────────────────────────
#
# В спеке (`spec/02-онбординг.md`) человеку задаётся ровно четырнадцать
# вопросов: шаги O1–O15 минус O0 (выбор маски) и O2 («Ресёрчер уходит
# читать» — не вопрос). Базовый круг распаковки закрывает два из них:
# материалы это O1, опросник по дырам — O8.
#
# Остальные двенадцать идут **вторым заходом и только по согласию**. Так
# не из вежливости: человек, которому на первом разговоре выкатили
# четырнадцать вопросов, бросает на пятом, и в папке остаётся половина
# без пометки, что это половина. База на шести вопросах работает; всё
# дальше делает её лучше, а не возможной.
#
# Формулировки здесь жёсткие, а не собранные моделью. Вопрос «пришлите
# два своих текста, которые вам нравятся» не нуждается в сочинении, а
# вызов модели за ним — это минута тишины в чате на каждый пункт.
@dataclass(frozen=True)
class Deep:
    oid: str            # номер шага спеки, чтобы не разошлось
    key: str            # какой файл это усиливает
    title: str
    ask: str
    example: str = ""


DEEP: tuple[Deep, ...] = (
    Deep("O3", "goals", "Цель этапа",
         "Что для вас сейчас главное: заявки, рост аудитории, репутация "
         "или запуск? Одно слово, остальное подстроится под него."),
    Deep("O4", "core", "Свои эталоны",
         "Пришлите два-три своих текста, которые вам самим нравятся. Не "
         "лучшие по охвату — те, где звучит ваш голос.",
         "можно ссылками на посты или просто текстом"),
    Deep("O5", "core", "Антиэталон",
         "А теперь наоборот: текст, который вам не нравится. Можно чужой. "
         "Скажите одной строкой, что именно в нём не так.",
         "«вот так писать не хочу: сплошные восклицания»"),
    Deep("O6", "platforms", "Референсы визуала",
         "Есть бренды или страницы, чей визуал вам близок? Две-три ссылки "
         "или названия."),
    Deep("O7", "platforms", "Съёмка",
         "Готовы появляться в кадре — лицом и голосом? И есть ли у вас "
         "фотографии, которые можно ставить на обложки?"),
    Deep("O9", "goals", "Ресурс",
         "Сколько времени в неделю вы правда готовы отдавать контенту? "
         "Честная цифра важнее красивой: под два часа и под десять "
         "собираются разные планы.",
         "«часа три, и то не каждую неделю»"),
    Deep("O10", "goals", "Где вы сейчас",
         "Сколько у вас подписчиков на каждой площадке и сколько человек "
         "обычно видит один пост? Точные цифры не нужны, хватит порядка."),
    Deep("O11", "goals", "Где хотите быть",
         "Чего хотите достичь за полгода? Лучше числом или событием, а не "
         "словом «вырасти».",
         "«первый поток курса на десять человек»"),
    Deep("O12", "goals", "Что продаёте",
         "Напишите списком, что вы продаёте сегодня и по какой цене. Цену "
         "можно не называть — тогда напишу «цены позже», и это не помешает."),
    Deep("O13", "platforms", "Площадки и ритм",
         "Где вы хотите быть и сколько выходов в неделю выдержите? Если "
         "площадка одна — так и скажите, это нормальный ответ."),
    Deep("O14", "platforms", "Как писать",
         "Есть ли у вас правила текста, которые нельзя нарушать? Хэштеги, "
         "эмодзи, обращение на «ты» или «вы», запрещённые слова."),
    Deep("O15", "platforms", "Как оформлять",
         "Есть ли фирменные цвета, шрифты, логотип? Пришлите файлом или "
         "скажите, что их пока нет — тогда соберём на ваших фото."),
)

DEEP_BY_OID = {d.oid: d for d in DEEP}

# Всего вопросов человеку в спеке. Число держится проверкой, а не памятью:
# пункт, добавленный в спеку и забытый здесь, иначе пропадает молча.
SPEC_QUESTIONS = 14


def deep_left(answers: dict[str, Any]) -> list[Deep]:
    """Пункты усиления, на которые человек ещё не отвечал."""
    done = set(answers.get("deep", {}))
    return [d for d in DEEP if d.oid not in done]


# ── файл в чат: по разделам, с правкой ────────────────────────────────

def sections(text: str) -> list[tuple[str, str]]:
    """Файл на пары «заголовок, тело». Человек читает файл разделами."""
    out: list[tuple[str, str]] = []
    head, body = "", []
    for line in text.splitlines():
        if m := re.match(r"^#{1,3}\s+(.+)$", line):
            if head or body:
                out.append((head, "\n".join(body).strip()))
            head, body = m.group(1).strip(), []
        else:
            body.append(line)
    if head or body:
        out.append((head, "\n".join(body).strip()))
    return [(h, b) for h, b in out if h or b]


def as_messages(key: str, text: str, limit: int = 3500) -> list[str]:
    """Файл сообщениями для чата: заголовок жирным, тело как есть.

    Разделами, а не одним куском: человек правит «Голос бренда», а не
    «файл целиком», и чтобы найти этот раздел, он не должен прокручивать
    девять экранов. Потолок сообщения 4096 знаков держит границу сам.
    """
    need = BY_KEY.get(key)
    title = need.title if need else key
    out: list[str] = [f"<b>━━ {title}</b>  <code>{brand_store.PROFILE.get(key, key)}</code>"]
    for head, body in sections(text):
        block = (f"<b>{head}</b>\n{body}" if head else body).strip()
        if not block:
            continue
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            out.append(block[:cut if cut > 0 else limit].strip())
            block = block[cut if cut > 0 else limit:].strip()
        if out and len(out[-1]) + len(block) + 2 <= limit:
            out[-1] += "\n\n" + block
        else:
            out.append(block)
    return out


# ── материалы ─────────────────────────────────────────────────────────

@dataclass
class Material:
    """Что человек прислал. Одна строка — один присланный кусок."""
    kind: str                  # file | link | text | meta-export
    name: str
    chars: int = 0
    note: str = ""             # почему не взято, если не взято

    @property
    def ok(self) -> bool:
        return not self.note

    def line(self) -> str:
        if self.note:
            return f"✖️ {self.name} — {self.note}"
        size = f", {self.chars} знаков" if self.chars else ""
        return f"✔️ {self.name}{size}"


def is_meta_export(text: str) -> bool:
    """Похоже ли это на выгрузку Meta Business Suite.

    Проверяется по шапке, а не по имени файла: кабинет называет выгрузку
    датой, а человек переименовывает её как захочет.
    """
    head = text[:2000].lower()
    return (any(w in head for w in META_LINK)
            and any(w in head for w in META_TIME))


def _rows(text: str) -> list[dict[str, str]]:
    """Строки CSV словарями с ключами в нижнем регистре."""
    sample = text[:4000]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    out = []
    for row in reader:
        out.append({(k or "").strip().lower(): (v or "").strip()
                    for k, v in row.items()})
    return out


def _pick(row: dict[str, str], words: tuple[str, ...]) -> str:
    for key, val in row.items():
        if any(w in key for w in words):
            return val
    return ""


def _when(raw: str) -> str | None:
    """Время публикации из выгрузки. Формат кабинета — `%m/%d/%Y %H:%M`."""
    raw = raw.strip()
    for fmt in ("%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d.%m.%Y %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def meta_posts(text: str) -> tuple[list[dict[str, Any]], str]:
    """Посты из выгрузки Meta. Второе значение — ник, если он в ссылках.

    Цифры берутся те, которых в публичной странице нет вовсе: охват,
    сохранения, репосты. Медиана по лайкам на таком аккаунте почти
    бесполезна — на живом профиле это две штуки при 288 просмотрах.
    """
    posts: list[dict[str, Any]] = []
    name = ""
    for row in _rows(text):
        link = _pick(row, META_LINK)
        if not link:
            continue
        if not name:
            name = instagram.handle(link)
        code = link.rstrip("/").rsplit("/", 1)[-1]
        posts.append({
            "code": code,
            "text": _pick(row, ("description", "caption", "подпись", "текст")),
            "date": _when(_pick(row, META_TIME)),
            "likes": _pick(row, ("likes", "нравится")),
            "comments": _pick(row, ("comments", "комментар")),
            "views": _pick(row, ("views", "plays", "просмотр", "reach",
                                 "охват")),
            "video": "reel" in link or "reel" in str(row.values()).lower(),
        })
    return posts, name


def stash_meta(b, text: str, profile: str) -> str:
    """Разложить выгрузку в кэш профиля. Возвращает строку для человека."""
    posts, found = meta_posts(text)
    name = instagram.handle(profile) or found
    if not name:
        return ("выгрузка принята, но ника в ней нет — скажи, какой это "
                "аккаунт, и я разложу её в кэш")
    if not posts:
        return "в выгрузке не нашлось ни одной строки с ссылкой на пост"

    instagram.stash(b, {"profile": name, "posts": posts})
    raw = b.path(f"research/instagram/meta-export-{name}.csv")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(text, encoding="utf-8")
    return f"выгрузка @{name}: {len(posts)} постов легли в кэш профиля"


# Хосты, которые без входа не отдают содержимого. Список нужен именно
# здесь: `sources.classify` знает про Instagram отдельным видом, а
# LinkedIn, TikTok и Facebook числит обычными сайтами — и снятый с них
# текст это страница входа, то есть ноль фактуры под видом прочитанного.
# Молчаливое «прочитал» тут дороже отказа: профиль собирается на пустоте.
NO_PUBLIC = ("linkedin.com", "tiktok.com", "facebook.com", "fb.com",
             "twitter.com", "x.com", "threads.net", "vk.com")


def sort_links(urls: list[str]) -> tuple[list[str], list[str]]:
    """Ссылки на два списка: что читаем сами и что просим файлом.

    Вторая половина это не поломка, а честная граница. Обещать чтение
    Instagram и LinkedIn нельзя: первый отдаёт пустую оболочку и 401,
    второй без входа — страницу входа вместо постов.
    """
    mine, theirs = [], []
    for url in urls:
        kind, norm = sources.classify(url)
        low = norm.lower()
        blocked = any(h in low for h in NO_PUBLIC)
        (mine if kind in {"telegram", "website", "feed"} and not blocked
         else theirs).append(norm)
    return mine, theirs


ASK_EXPORT = (
    "Instagram публично не читается: страница отдаётся пустой, а за вход в "
    "чужой аккаунт я не берусь. Нужна выгрузка — она богаче скрейпинга, там "
    "есть охват, сохранения и репосты.\n\n"
    "Meta Business Suite → Insights → Content → <b>Export data</b> → вкладка "
    "Instagram → выбрать аккаунт → Generate → Download. Придёт CSV — пришли "
    "его сюда файлом."
)

ASK_MANUAL = (
    "{where} без входа ничего не отдаёт, и обещать чтение я не буду. "
    "Расскажи словами, что там происходит, или пришли выгрузку файлом — "
    "разберу."
)


# ── дыры ──────────────────────────────────────────────────────────────

def gaps(draft: unpack.Draft, answers: dict[str, Any]) -> list[str]:
    """Чего не хватает до полного комплекта. Ключи файлов, в порядке NEEDS.

    Считает код, а не модель: список утверждён, и роль, которой дали
    решать, чего не хватает, однажды решит, что хватает всего.
    """
    done = set(answers.get("written", []))
    out = []
    for need in NEEDS:
        if need.key in done:
            continue
        if need.key == "core":
            if not draft.data:
                out.append("core")
            continue
        out.append(need.key)
    return out


def weak_core(draft: unpack.Draft) -> list[str]:
    """Блоки ЯДРА, которые собраны на догадках. Человек видит их до кнопки."""
    return draft.weak()


# ── опросник ──────────────────────────────────────────────────────────

@dataclass
class Question:
    key: str
    ask: str
    example: str = ""
    why: str = ""

    def text(self) -> str:
        out = self.ask
        if self.why:
            out += f"\n\n<i>{self.why}</i>"
        if self.example:
            out += f"\n\nНапример: {self.example}"
        return out


_QUIZ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "ask", "example", "why"],
                "properties": {
                    "key": {"type": "string"},
                    "ask": {"type": "string"},
                    "example": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
        },
    },
}


async def quiz(user_id: int, draft: unpack.Draft, answers: dict[str, Any],
               need: list[str], limit: int = QUIZ_LIMIT) -> list[Question]:
    """Вопросы на добор. Дыры считает код, формулирует модель."""
    need = [k for k in need if k in BY_KEY][:limit]
    if not need:
        return []

    asked = answers.get("quiz", {})
    blocks = [f"## Черновик профиля\n\n{draft.card()}"]
    if asked:
        blocks.append("## На что человек уже ответил\n\n" + "\n\n".join(
            f"**{q}**\n{a}" for q, a in asked.items()))
    blocks.append("## Чего не хватает\n\n" + "\n".join(
        f"- `{BY_KEY[k].key}` — {BY_KEY[k].title}: {BY_KEY[k].what}"
        for k in need))

    raw = await agent.ask(
        "intake", user_id,
        f"Собери не больше {len(need)} вопросов, по одному на дыру. "
        "Верни только JSON по схеме из инструкции.\n\n"
        + "\n\n---\n\n".join(blocks),
        schema=_QUIZ_SCHEMA, max_tokens=4000)

    data = agent.parse_json(raw, who="распаковщик")
    out = []
    for item in data.get("questions", [])[:len(need)]:
        key = str(item.get("key", "")).strip()
        ask = str(item.get("ask", "")).strip()
        if key in BY_KEY and ask:
            out.append(Question(key, ask,
                                str(item.get("example", "")).strip(),
                                str(item.get("why", "")).strip()))
    return out


# ── сборка файлов ─────────────────────────────────────────────────────

async def build(user_id: int, key: str, draft: unpack.Draft,
                answers: dict[str, Any], current: str = "",
                fix: str = "") -> str:
    """Собрать один файл профиля. Возвращает markdown целиком.

    `fix` — просьба человека к готовому файлу. Она идёт последней и самой
    сильной: человек видел результат, а модель — нет.
    """
    need = BY_KEY[key]
    asked = answers.get("quiz", {})

    blocks = [f"## Черновик профиля\n\n{draft.card()}"]
    if asked:
        blocks.append("## Ответы человека\n\n" + "\n\n".join(
            f"**{q}**\n{a}" for q, a in asked.items()))
    if current.strip():
        blocks.append("## Текущий файл\n\nОн уже подтверждён. Уточни его, а "
                      "не пиши заново.\n\n" + current[:8000])

    heads = "\n".join(f"- `## {h}`" for h in need.headings)
    extra = ""
    if key == "goals":
        # Пропорцию проверяет код: доли, не дающие сотню, это опечатка, и
        # работать по ней хуже, чем по честной запасной 60/20/20.
        extra = ("\n\nПропорция воронки пишется строкой вида «прогрев 50, "
                 "продукт 30, личное 20» и обязана давать в сумме сто.")
    if key == "platforms":
        extra = ("\n\nПлощадки только из этого списка: "
                 + ", ".join(KNOWN_PLATFORMS) + ".")

    if fix.strip():
        blocks.append("## Человек прочитал файл и просит\n\n" + fix.strip()
                      + "\n\nЭто главнее всего остального: он видел "
                        "результат, ты нет. Остальное сохрани как есть.")

    prompt = (f"Собери файл `{brand_store.PROFILE[key]}` — {need.title}.\n\n"
              f"Что в нём должно быть: {need.what}.\n\n"
              f"Заголовки ровно эти, в этом порядке:\n{heads}{extra}\n\n"
              "Верни только текст файла.\n\n" + "\n\n---\n\n".join(blocks))

    return await agent.ask("intake", user_id, prompt, max_tokens=6000)


def check(key: str, text: str, current: str = "") -> str:
    """Отказ строкой или пустая строка. Границу держит код, не промпт.

    Роль просят про форму словами, и она почти всегда слушается. «Почти»
    здесь стоит дорого: файл профиля читают все роли завода неделями, и
    пропорция, не дающая сотню, врёт молча.
    """
    body = text.strip()
    if not body:
        return "вернулся пустой файл"
    if not body.lstrip().startswith("#"):
        return "ответ не похож на файл профиля"
    if left := PLACEHOLDER.findall(body):
        return f"в файле остались подстановки: {', '.join(sorted(set(left)))}"

    if current.strip():
        deny, _ = refresh.review(current, body)
        if deny:
            return deny

    need = BY_KEY.get(key)
    if need:
        have = " ".join(HEADING.findall(body)).lower()
        lost = [h for h in need.headings if h.lower() not in have]
        if lost:
            return "нет разделов: " + ", ".join(lost)

    if key == "goals" and research._funnel(body) is None:
        return ("пропорция воронки не читается или доли не дают сотню — "
                "по такой работать нельзя")
    if key == "platforms":
        low = body.lower()
        if not any(p in low for p in KNOWN_PLATFORMS):
            return "в файле не названо ни одной площадки, которую ведёт завод"
    if key == "sources":
        if not re.search(r"(https?://|t\.me/|@[A-Za-z0-9_]{3,})", body):
            return "в источниках нет ни одной ссылки и ни одного ника"
    return ""


# Пропорция воронки, назначенная кодом, когда модель её не собрала. Та же
# запасная, что у Ресёрчера (`research.BACKUP_FUNNEL`), и названная
# запасной вслух: молча подставленная цифра выглядит как согласованная.
FUNNEL_HEAD = "Пропорция воронки"


def fix_funnel(text: str) -> str:
    """Заменить нечитаемую пропорцию на запасную, назвав её запасной.

    Дописать нельзя, только заменить: `research._funnel` берёт **первое**
    совпадение по каждому слову, и старая строка с неверными долями
    перебила бы новую. Так и было в первом живом прогоне — файл целей
    отказом потерялся весь, из-за одной строки из двенадцати.
    """
    parts = re.split(r"^(?=#{1,3}\s)", text, flags=re.M)
    kept = [p for p in parts if FUNNEL_HEAD.lower() not in p.split("\n")[0].lower()]
    f = research.BACKUP_FUNNEL
    tail = (f"## {FUNNEL_HEAD}\n\n"
            f"прогрев {f['warm']}, продукт {f['prod']}, личное {f['pers']}\n\n"
            "Пропорция запасная: в ответах её не было, назначена кодом и "
            "человеком не подтверждена.\n")
    return "".join(kept).rstrip() + "\n\n" + tail


# ── запись в папку бренда ─────────────────────────────────────────────

@dataclass
class Written:
    slug: str = ""
    files: list[str] = field(default_factory=list)
    # Ключ → имя файла, только по записанным. Нужен тому, кто показывает
    # собранное человеку: порядок NEEDS, а не порядок словаря.
    files_by_key: dict[str, str] = field(default_factory=dict)
    denied: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def card(self) -> str:
        out = []
        if self.files:
            out.append("Записал: " + ", ".join(self.files))
        for key, why in self.denied.items():
            out.append(f"✖️ {brand_store.PROFILE.get(key, key)} — {why}")
        out += self.notes
        return "\n".join(out)


def brand_for(state: dict[str, Any], name: str):
    """Папка бренда. Slug не меняется никогда — на нём пути к артефактам."""
    store = brand_store.Store(_brands_path())
    if slug := state.get("slug"):
        if b := store.get(slug):
            return b
        log.warning("папки бренда %s нет, собираю заново", slug)
    return store.create(name)


def _brands_path():
    from config import cfg
    return cfg.brands_path


async def land(user_id: int, state: dict[str, Any], draft: unpack.Draft,
               keys: list[str]) -> Written:
    """Собрать и записать названные файлы. Отказ по файлу не рушит остальные."""
    answers = state.get("answers", {})
    b = brand_for(state, draft.brand_name())
    out = Written(slug=b.slug)

    core = draft.to_core_md(draft.brand_name())
    for key in keys:
        current = b.read(key)
        if key == "core":
            text, deny = core, check("core", core, current)
        else:
            text = await build(user_id, key, draft, answers, current,
                               fix=answers.get("fix", {}).get(key, ""))
            deny = check(key, text, current)
            if deny:
                # Один повтор с названной причиной. Модель почти всегда
                # чинит форму, увидев, что именно не сошлось, а человеку
                # второй круг опросника не предложишь.
                log.info("файл %s не сошёлся (%s), прошу переделать", key, deny)
                text = await build(user_id, key, draft, answers, current,
                                   fix=f"Предыдущая попытка не прошла "
                                       f"проверку: {deny}. Исправь именно "
                                       f"это, остальное сохрани.")
                deny = check(key, text, current)
            if deny and key == "goals" and FUNNEL_HEAD.lower() in text.lower():
                patched = fix_funnel(text)
                if not check(key, patched, current):
                    text, deny = patched, ""
                    out.notes.append("Пропорция воронки в целях запасная, "
                                     "60/20/20 — из ваших ответов её не "
                                     "собрать.")
        if deny:
            out.denied[key] = deny
            log.warning("файл %s не записан: %s", key, deny)
            continue
        await b.awrite(key, text, reason="распаковка клиента")
        out.files.append(brand_store.PROFILE[key])
        out.files_by_key[key] = brand_store.PROFILE[key]

    if csv_text := answers.get("meta_export"):
        out.notes.append(stash_meta(b, csv_text,
                                    answers.get("instagram", "")))
    return out
