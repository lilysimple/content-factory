"""Цикл 14: профиль бренда — правка словами и распаковка.

Самый дорогой путь в проекте: модель переписывает `core.md` **целиком**,
и одна неудачная правка стирает работу онбординга. Здесь проверяется, что
границу держит код, а не промпт.
"""
from __future__ import annotations

import asyncio
import subprocess

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import agent, desk, refresh, unpack             # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

CALLS = {"n": 0, "prompts": []}


def install(answer):
    async def ask(role, chat_id, prompt, **kw):
        CALLS["n"] += 1
        CALLS["prompts"].append(prompt)
        return answer(prompt) if callable(answer) else answer
    agent.ask = ask
    CALLS["n"] = 0
    CALLS["prompts"].clear()


def repo_head() -> str:
    """HEAD репозитория кода. Он не должен шевелиться от прогона тестов."""
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=harness.REPO,
                       capture_output=True, text=True)
    return r.stdout.strip()


async def main() -> None:
    reg = FakeRegistry()
    b = desk.brand(CHAT)
    current = b.read("core")
    check("профиль бренда на месте", len(current) > 500, f"{len(current)} знаков")

    # ── 0. запись профиля не трогает чужой репозиторий ────────────────
    # Песочница лежит внутри репозитория кода и своего .git не имеет.
    # `git` ищет репозиторий вверх по дереву, поэтому `b.write` однажды
    # сделал три коммита в код завода — с `add -A` и чужим сообщением.
    print("\n0. Запись профиля не коммитит в чужой репозиторий")
    before = repo_head()
    version = b.write("core", current, reason="проверка стенда")
    check("HEAD репозитория кода не сдвинулся", repo_head() == before,
          "прогон тестов сделал коммит в код")
    check("версия профиля это дата, а не чужой sha",
          version.count("-") == 2 and len(version) == 10, version)

    # ── 1. распознавание просьбы ──────────────────────────────────────
    print("\n1. Что считается просьбой поправить профиль")
    check("«давай обновим стратегию» это правка",
          refresh.wants_edit("давай обновим стратегию бренда") is True)
    check("«обнови план на неделю» это не правка",
          refresh.wants_edit("обнови план на неделю") is False,
          "просьба к Стратегу уехала бы в профиль")
    check("одного глагола мало",
          refresh.wants_edit("поправь вот это") is False)

    # ── 2. хорошая правка проходит ────────────────────────────────────
    print("\n2. Хорошая правка")
    good = current.replace("## 10. Формат", "## 10. Формат\n\nНовая строка.")
    fatal, warns = refresh.review(current, good)
    check("отказа нет", fatal == "", fatal)
    check("предупреждений нет", warns == [], str(warns))

    install(good)
    reg.clear()
    await refresh.edit(reg, CHAT, "добавь строку в формат")
    check("модель позвана один раз", CALLS["n"] == 1, str(CALLS["n"]))
    check("текущий профиль уехал в промпт",
          "## Текущий профиль" in CALLS["prompts"][0])
    check("кнопки записи показаны",
          [x for x in reg.last().buttons] == ["edit:ok", "edit:no"],
          str(reg.last().buttons))
    check("файл ещё не тронут", b.read("core") == current,
          "профиль записан до подтверждения")

    await refresh.on_edit_callback(reg, CHAT, "ok")
    check("после подтверждения записан", b.read("core").strip() == good.strip(),
          "правка не доехала")
    b.write("core", current, reason="откат теста")

    # ── 3. пересказ вместо правки это отказ ───────────────────────────
    print("\n3. Пересказ вместо правки")
    digest = "# ЯДРО. Lily Space\n\nКоротко: консультант по AI.\n"
    fatal, _ = refresh.review(current, digest)
    check("усохший файл отбит", "усох" in fatal, fatal)

    install(digest)
    reg.clear()
    await refresh.edit(reg, CHAT, "перепиши профиль покороче")
    check("человеку сказано, что не принято", "не приму" in reg.texts(),
          reg.texts()[:120])
    check("кнопки записи не даны", not reg.last().buttons, str(reg.last().buttons))
    check("профиль не тронут", b.read("core") == current, "профиль пострадал")

    # ── 4. потерянный заголовок это отказ ─────────────────────────────
    print("\n4. Потерянный заголовок")
    headless = current.replace("# ЯДРО. Lily Space", "# Профиль")
    fatal, _ = refresh.review(current, headless)
    check("смена заголовка отбита", "заголовок" in fatal, fatal)

    not_a_file = "Конечно! Вот обновлённый профиль:\n\n# ЯДРО"
    fatal, _ = refresh.review(current, not_a_file)
    check("болтовня вместо файла отбита", "не похож" in fatal, fatal)

    fatal, _ = refresh.review(current, "#\n")
    check("пустой файл отбит", "пустой" in fatal, fatal)

    # ── 5. потери называются, но решает человек ───────────────────────
    print("\n5. Потери называются человеку")
    cut = current.replace("### Стоп-слова", "### Убрано")
    fatal, warns = refresh.review(current, cut)
    check("пропавший раздел это не отказ", fatal == "", fatal)
    check("пропавший раздел назван",
          any("Стоп-слова" in w for w in warns), str(warns))

    no_owner = current.replace("owner: bot", "")
    fatal, warns = refresh.review(current, no_owner)
    check("потерянный owner назван",
          any("owner" in w for w in warns), str(warns))

    marks = current.count("[уточнить факт]")
    if marks:
        filled = current.replace("[уточнить факт]", "выдумка", 1)
        fatal, warns = refresh.review(current, filled)
        check("исчезнувшая пометка названа",
              any("уточнить факт" in w for w in warns), str(warns))

    install(cut)
    reg.clear()
    await refresh.edit(reg, CHAT, "убери стоп-слова из ядра")
    check("предупреждение показано человеку", "⚠️" in reg.texts(),
          reg.texts()[:200])
    check("но кнопка записи есть",
          "edit:ok" in reg.last().buttons, str(reg.last().buttons))

    # ── 6. неуверенность распаковки видна до записи ───────────────────
    print("\n6. Неуверенные блоки в карточке ЯДРА")
    draft = unpack.Draft(data={
        "identity": {"who": "консультант по AI"},
        "audience": {"segments": ["руководители"], "pains": ["нет времени"]},
        "voice": {"sentence": "короткие", "address": "на ты"},
        "confidence": {"identity": 0.9, "audience": 0.8, "voice": 0.3},
    })
    check("слабый блок найден", draft.weak() == ["голос"], str(draft.weak()))
    card = draft.card()
    check("сказано в карточке", "собрано на догадках" in card, card[-200:])
    check("назван именно голос", "голос" in card.split("догадках")[-1],
          card[-160:])

    sure = unpack.Draft(data={"identity": {"who": "кто"},
                              "confidence": {"identity": 0.9, "voice": 0.8}})
    check("уверенная распаковка молчит",
          "догадках" not in sure.card(), sure.card()[-120:])

    absent = unpack.Draft(data={"identity": {"who": "кто"}})
    check("без поля confidence не падает", absent.weak() == [],
          str(absent.weak()))


asyncio.run(main())
raise SystemExit(report())
