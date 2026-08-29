"""Цикл 17: выжимка профиля — кеш по отпечатку и уведомление Стратега.

Стратег не перечитывает `goals.md` и `platforms.md` на каждый план: цели и
нормы площадок приходят выжимкой Ресёрчера. Проверяем ровно те границы,
из-за которых это вообще собрано.

Файлы не менялись — выжимка не пересобирается и не переписывается на диске:
иначе в папке бренда копится коммит на каждый план. Менялись — пересобирается
сама, без напоминания, и человек слышит об этом до того, как утвердит неделю
по старой версии.

Пропорцию воронки держит код: доли, которые не дают сотню, это опечатка, и
работать по ней хуже, чем по честной запасной.
"""
from __future__ import annotations

import asyncio

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import desk, refresh, research, strategy        # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

GOALS_OK = """# Цели этапа

## Пропорция воронки

- прогрев 50
- продукт 30
- личное 20

## Метрика этапа

Заявки на разбор, не охват.
"""

GOALS_BROKEN = """# Цели этапа

## Пропорция воронки

- прогрев 50
- продукт 30
- личное 40
"""

PLATFORMS = """# Площадки

## Telegram

Два поста в неделю, длинный текст, обложка обязательна.
"""


def digest_file():
    return desk.brand(CHAT).path(research.DIGEST_PATH)


def write(key: str, text: str) -> None:
    """Записать файл профиля мимо git: тесту нужен только диск."""
    b = desk.brand(CHAT)
    b.path(f"{key}.md").write_text(text, encoding="utf-8")


def drop(key: str) -> None:
    path = desk.brand(CHAT).path(f"{key}.md")
    if path.exists():
        path.unlink()


async def main() -> None:
    reg = FakeRegistry()
    b = desk.brand(CHAT)
    drop("goals")
    drop("platforms")
    if digest_file().exists():
        digest_file().unlink()

    # ── 1. без файлов профиля ─────────────────────────────────────────
    print("\n1. Файлов профиля нет")
    dg = research.profile_digest(b)
    check("выжимка собралась без модели", bool(dg.text))
    check("пересобрана в первый раз", dg.rebuilt is True)
    check("названо, чего нет", set(dg.missing) == {"goals", "platforms"},
          str(dg.missing))
    check("пропорция запасная", dg.backup is True and dg.ratio == "60/20/20",
          dg.ratio)
    check("запасная названа запасной", "запасная" in dg.text)
    check("файл выжимки записан", digest_file().exists())

    # ── 2. файлы не менялись — не пересобираем ────────────────────────
    print("\n2. Файлы не менялись")
    stamp = digest_file().stat().st_mtime_ns
    again = research.profile_digest(b)
    check("второй раз не пересобрана", again.rebuilt is False)
    check("отпечаток тот же", again.stamp == dg.stamp, again.stamp)
    check("файл не переписан", digest_file().stat().st_mtime_ns == stamp)
    check("текст поднялся из файла", again.text.strip() == dg.text.strip(),
          again.text[:120])
    check("пропорция поднялась из файла", again.ratio == "60/20/20")
    check("пропущенные файлы поднялись", set(again.missing) ==
          {"goals", "platforms"}, str(again.missing))

    # ── 3. профиль изменился — пересобрали сами ───────────────────────
    print("\n3. Профиль изменился")
    write("goals", GOALS_OK)
    fresh = research.profile_digest(b)
    check("смена файла замечена", fresh.rebuilt is True)
    check("отпечаток изменился", fresh.stamp != dg.stamp)
    check("пропорция взята из профиля", fresh.ratio == "50/30/20", fresh.ratio)
    check("пропорция больше не запасная", fresh.backup is False)
    check("goals больше не в пропущенных", "goals" not in fresh.missing,
          str(fresh.missing))
    check("platforms всё ещё пропущен", "platforms" in fresh.missing)
    check("метрика этапа доехала", "Заявки на разбор" in fresh.text)

    # Смена core.md тоже считается: план стоит и на нём.
    core = b.path("core.md")
    core.write_text(core.read_text(encoding="utf-8") + "\n<!-- правка -->\n",
                    encoding="utf-8")
    check("правка ядра пересобирает выжимку",
          research.profile_digest(b).rebuilt is True)

    # ── 4. пропорцию проверяет код ────────────────────────────────────
    print("\n4. Пропорция, которая не сходится")
    write("goals", GOALS_BROKEN)
    bad = research.profile_digest(b)
    check("доли не дали сотню — пропорция запасная",
          bad.backup is True and bad.ratio == "60/20/20", bad.ratio)
    check("человеку сказано, почему запасная", "не дают сотню" in bad.text)
    check("файл при этом не пропал из профиля", "goals" not in bad.missing)

    # ── 5. слои Стратега берут выжимку ────────────────────────────────
    print("\n5. Стратег получает выжимку")
    write("goals", GOALS_OK)
    write("platforms", PLATFORMS)
    research.profile_digest(b)                    # пересобрали до плана
    free = [("2026-08-20", "telegram")]
    layers = strategy._layers(CHAT, {}, free, "план")
    check("выжимка попала в слои", "Заявки на разбор" in layers)
    check("сказано, что профиль сам не перечитывается",
          "выжимкой Ресёрчера" in layers, layers[:200])
    check("пропорция из профиля в слоях", "50/30/20" in layers)
    check("нормы площадок доехали", "обложка обязательна" in layers)
    check("вшитой фразы про несобранный platforms.md больше нет",
          "`platforms.md` ещё не собран" not in layers)
    check("на второй план строки про смену файлов нет",
          "менялись" not in strategy._layers(CHAT, {}, free, "план"))

    write("goals", GOALS_BROKEN)
    check("смена файла видна в слоях",
          "менялись" in strategy._layers(CHAT, {}, free, "план"))

    # ── 6. Ресёрчер уведомляет Стратега ───────────────────────────────
    print("\n6. Уведомление")
    reg.clear()
    write("goals", GOALS_OK)
    said = await research.notify_profile(reg, CHAT)
    check("уведомление ушло", said is True)
    msg = reg.last()
    check("говорит Ресёрчер", msg and msg.role == "research")
    check("в топик Стратегии", msg and msg.topic == "strategy", msg.topic)
    check("названа новая пропорция", "50/30/20" in msg.text, msg.text[:200])
    check("сказано про прошлые планы", "прошлой версии" in msg.text)

    reg.clear()
    quiet = await research.notify_profile(reg, CHAT)
    check("без изменений роль молчит", quiet is False and not reg.sent,
          reg.texts()[:120])

    # ── 7. правка профиля через кнопку уведомляет сама ────────────────
    print("\n7. Правка профиля через кнопку")
    reg.clear()
    drop("goals")                                 # изменение мимо кнопки
    refresh._pending[CHAT] = unpacked()
    await refresh.on_callback(reg, CHAT, "yes")
    check("Ассистент отчитался человеку",
          any(s.role == "assistant" for s in reg.sent), reg.texts()[:120])
    check("Ресёрчер пересобрал выжимку следом",
          any(s.role == "research" and s.topic == "strategy"
              for s in reg.sent), reg.texts()[:200])

    # ── 8. выжимка не притворяется дайджестом ─────────────────────────
    print("\n8. Выжимка и недельная сводка не путаются")
    b.artifact("research/2026-W33.md", "# Сводка\n\nтекст сводки\n")
    week, text = research.latest(b)
    check("последней сводкой считается неделя", week == "2026-W33", week)
    check("в сводку не попала выжимка", "Выжимка профиля" not in text)


def unpacked():
    """Черновик профиля, как его отдаёт распаковка."""
    from orchestrator import unpack
    return unpack.Draft(data={
        "identity": {"who": "консультант по AI-трансформации",
                     "brand": "Lily Space"},
        "audience": {"segments": ["руководители"], "pains": ["нет времени"]},
        "voice": {"sentence": "короткие", "address": "на ты"},
        "confidence": {"identity": 0.9, "audience": 0.8, "voice": 0.8},
    })


asyncio.run(main())
raise SystemExit(report())
