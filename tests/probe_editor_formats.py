"""Разведка: Редактор по всем форматам, которые он может получить.

Не цикл стенда, а замер. Ставит по теме на каждый формат из
`strategy.PLATFORMS`, кроме произносимых вслух, и гоняет `editor.build`.
Печатает текст, самопроверку, находки скрипта и время.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

import harness
from harness import CHAT

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import desk, editor                             # noqa: E402
from storage import db                                            # noqa: E402
from validators import check_voice                                # noqa: E402

db.init(cfg.db_path)
logging.basicConfig(level=logging.WARNING)

# Темы под каждый формат. Поля те же, что кладёт Стратег.
THEMES = [
    dict(id="2026-09-10-telegram-01", date="2026-09-10", plat="telegram", format="пост",
         rubric="Разбор ошибки", goal="warm", arch="Разбор ошибки",
         funnel_stage="доверие",
         title="Роль настроена, а текст всё равно не мой",
         hook="Агент пишет грамотно, а отправить это в канал неловко.",
         why="Не-технарям, которые уже поставили у себя роли.",
         angle="Разбор ошибки", charge="recognition"),
    dict(id="2026-09-11-telegram-01", date="2026-09-11", plat="telegram", format="анонс",
         rubric="Событие", goal="prod", arch="Анонс",
         funnel_stage="решение",
         title="Воркшоп «Соберите свою первую роль» — 18 сентября",
         hook="Два часа, приходите со своей задачей, уходите с рабочей ролью.",
         why="Ядру канала, которое читает про роли и ещё не собрало свою.",
         angle="Анонс события", charge="useful discovery"),
    dict(id="2026-09-12-telegram-01", date="2026-09-12", plat="telegram", format="раздача",
         rubric="Материалы", goal="warm", arch="Раздача",
         funnel_stage="доверие",
         title="Шаблон профиля бренда, по которому я настраиваю роли",
         hook="Файл, который я заполняю перед тем, как что-то настраивать.",
         why="Тем, кто просил «а можно посмотреть, как это выглядит».",
         angle="Передача материала", charge="useful discovery"),
    dict(id="2026-09-13-telegram-01", date="2026-09-13", plat="telegram", format="опрос",
         rubric="Разговор", goal="pers", arch="Вопрос залу",
         funnel_stage="узнавание",
         title="Где у вас застревает работа с агентом",
         hook="Интересно, у всех ли одно и то же место.",
         why="Ядру канала: собрать материал для следующего разбора.",
         angle="Вопрос залу", charge="recognition"),
    dict(id="2026-09-10-instagram-01", date="2026-09-10", plat="instagram",
         format="карусель", goal="warm", rubric="Показ процесса",
         arch="Показ процесса", funnel_stage="узнавание",
         title="Неделя из двенадцати выходов: как это помещается в жизнь",
         hook="Двенадцать выходов в неделю и ни одного вечера за клавиатурой.",
         why="Холодной аудитории Instagram, снимает возражение «это долго».",
         angle="Показ процесса", charge="useful discovery"),
    dict(id="2026-09-11-instagram-01", date="2026-09-11", plat="instagram",
         format="сторис", goal="pers", rubric="Из жизни",
         arch="Из жизни", funnel_stage="узнавание",
         title="Утро, кофе и роль, которая написала за меня три поста",
         hook="Пока варился кофе, завод собрал план недели.",
         why="Тем, кто следит за буднями и не читает длинного.",
         angle="Из жизни", charge="recognition"),
    dict(id="2026-09-12-youtube-01", date="2026-09-12", plat="youtube", format="видео",
         goal="warm", rubric="Разбор", arch="Показ процесса",
         funnel_stage="узнавание",
         title="Как я собрала контент-завод из семи ролей",
         hook="Показываю всю цепь: от сводки недели до опубликованного поста.",
         why="Тем, кто ищет «как автоматизировать контент» поиском.",
         angle="Показ процесса", charge="useful discovery"),
]

COLS = ("id", "date", "plat", "format", "rubric", "goal", "arch",
        "funnel_stage", "title", "hook", "why", "angle", "charge")


def seed() -> None:
    with db.tx() as c:
        for t in THEMES:
            c.execute(
                f"INSERT OR REPLACE INTO themes (chat_id, status, "
                f"{','.join(COLS)}) VALUES (?, 'idea', "
                f"{','.join('?' * len(COLS))})",
                (CHAT, *[t.get(k) for k in COLS]))


async def main() -> None:
    seed()
    b = desk.brand(CHAT)
    only = sys.argv[1:]

    for t in THEMES:
        if only and t["id"] not in only:
            continue
        print("\n" + "=" * 72)
        print(f"{t['plat']} · {t['format']} — {t['title']}")
        print("=" * 72)
        t0 = time.time()
        try:
            draft = await editor.build(CHAT, f"напиши текст по теме {t['id']}")
        except Exception as e:                                   # noqa: BLE001
            print(f"ОТКАЗ ({type(e).__name__}): {e}")
            continue
        dt = time.time() - t0
        findings = check_voice.check(draft.text, stopwords=b.stopwords())
        print(draft.text)
        print("-" * 72)
        print(f"{len(draft.text)} знаков · {dt:.0f} с · кругов {draft.rounds}")
        print(f"самопроверка: {draft.score()}")
        print(f"hold: {draft.hold}")
        print(f"breaks: {draft.breaks}")
        print(f"notes: {draft.notes}")
        print(f"находки скрипта: {[str(f) for f in findings]}")


asyncio.run(main())
