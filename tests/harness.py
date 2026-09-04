"""Стенд: гоняем завод без Telegram и без боевой базы.

Реестр подделан — say() складывает сообщения в список вместо отправки.
База и папка брендов копируются во временную директорию, поэтому прогон
ничего не портит.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

# Пути выводятся от самого файла: стенд не должен знать, где лежит
# репозиторий, иначе он работает ровно на одной машине.
REPO = Path(__file__).resolve().parents[1]
BRANDS = REPO.parent / "content-factory-brands"
TMP = Path(__file__).parent / ".sandbox"

CHAT = -1003990495505


def live_pids() -> list[int]:
    """Пиды живого завода: `main.py`, запущенный из этого репозитория.

    Одного `pgrep` мало. Имя `main.py` носит половина питоновских
    проектов, и стенд, отказавшийся стартовать из-за чужого процесса,
    хуже стенда без проверки: он врёт про причину. Поэтому у каждого
    кандидата спрашивается рабочая папка, и совпадение с репозиторием
    и есть доказательство.

    `lsof` тут не роскошь: на macOS `ps` отдаёт путь к интерпретатору,
    а не к скрипту, и cwd по нему не узнать. Нет `lsof` — считаем, что
    завода нет: ложная тревога дороже пропуска, прогон всё равно
    покажет гонку отказами.
    """
    try:
        found = subprocess.run(["pgrep", "-f", "main.py"],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []

    pids = []
    for raw in found.stdout.split():
        try:
            pid = int(raw)
        except ValueError:
            continue
        try:
            where = subprocess.run(["lsof", "-a", "-p", raw, "-d", "cwd", "-Fn"],
                                   capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in where.stdout.splitlines():
            if line.startswith("n") and line[1:] == str(REPO):
                pids.append(pid)
                break
    return pids


def refuse_if_live() -> None:
    """Стенд не стартует рядом с работающим заводом.

    Причина не в чистоте, а в честности отчёта. Стенд копирует папку
    бренда и боевую базу, а завод в них пишет: копирование ловит
    `shutil.Error`, уборка — `Directory not empty`, и цикл падает целиком,
    не дойдя до проверок. Выглядит это как поломка Дизайнера или
    Публикатора, хотя сломана песочница.

    Так и случилось 04.09: два прогона подряд на одном и том же коде
    уронили разные наборы циклов — 9, 15, 18, 19, 20, потом 9, 11, 12,
    14, 15, 18, 19, 21, — а цикл 20, упавший в первый раз, во второй
    прошёл все 130 проверок. Полчаса ушло на поиск регрессии, которой не
    было.

    Отказ громкий и с командой: молчаливый пропуск вернул бы ту же
    неопределённость, ради которой всё и затевалось.
    """
    if os.getenv("FACTORY_TESTS_ALLOW_LIVE") == "1":
        return
    pids = live_pids()
    if not pids:
        return
    label = "space.lily.content-factory"
    sys.exit(
        "\n  Стенд не запущен: завод работает.\n\n"
        f"  Живой процесс: {', '.join(str(p) for p in pids)}. Он пишет в папку\n"
        "  бренда и в боевую базу, пока стенд их копирует, поэтому циклы\n"
        "  падают на песочнице и врут про причину.\n\n"
        "  Снять завод, прогнать стенд, поднять обратно:\n\n"
        f"    launchctl bootout gui/$(id -u)/{label}\n"
        "    ./.venv/bin/python tests/run.py\n"
        "    ./deploy/install-agent.sh\n\n"
        "  Осознанно рядом с живым заводом: FACTORY_TESTS_ALLOW_LIVE=1\n"
    )


def setup() -> None:
    """Свежая песочница на каждый прогон."""
    refuse_if_live()

    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    shutil.copy(REPO / "factory.db", TMP / "factory.db")
    shutil.copytree(BRANDS / "lily-space", TMP / "brands" / "lily-space")

    # Боевая база живёт своей жизнью: бот пишет туда темы, тексты и счётчик
    # вызовов. Тест, который зависит от их количества, начинает падать от
    # чужой работы. Профиль тенанта оставляем, производные данные чистим.
    con = sqlite3.connect(TMP / "factory.db")
    for table in ("themes", "posts", "metrics", "llm_usage"):
        try:
            con.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass                                  # таблицы может не быть
    con.commit()
    con.close()

    # Артефакты прошлых прогонов бота тоже мешают: цикл считает файлы.
    # `research` в этом списке не случайно: живая сводка, снятая с канала,
    # доехала бы до Стратега и сломала цикл, который проверяет поведение
    # без дайджеста. Тест не должен зависеть от того, что бот наработал.
    for folder in ("posts", "plans", "research"):
        path = TMP / "brands" / "lily-space" / folder
        if path.is_dir():
            for f in path.iterdir():
                if f.is_file():
                    f.unlink()

    # Фотобанк чистим не целиком, а от машинных кадров: своя съёмка это
    # профиль тенанта, а сгенерированное и стоковое — производное, и
    # попадает оно туда живой работой бота.
    #
    # Без этого `cycle09` требует банк без единого `gen-`, получает его
    # с кадром, который человек выбрал кнопкой вчера, и падает. Причём
    # падает не сразу: первый прогон на чистом бренде проходит, а все
    # следующие нет, и выглядит это как плавающий тест, а не как
    # зависимость от чужой работы.
    assets = TMP / "brands" / "lily-space" / "design" / "assets"
    for f in (assets / "images").glob("*"):
        if f.is_file() and f.name.startswith(("gen-", "stock-")):
            f.unlink()
    for name in ("gen-credits.md", "stock-credits.md"):
        (assets / name).unlink(missing_ok=True)
    shutil.rmtree(assets / ".gen", ignore_errors=True)

    os.environ["DB_PATH"] = str(TMP / "factory.db")
    os.environ["BRANDS_PATH"] = str(TMP / "brands")
    sys.path.insert(0, str(REPO))


class Say:
    """Одно отправленное сообщение."""

    def __init__(self, role: str, chat_id: int, text: str, topic: str, kb) -> None:
        self.role = role
        self.chat_id = chat_id
        self.text = text
        self.topic = topic
        self.kb = kb

    @property
    def buttons(self) -> list[str]:
        if self.kb is None:
            return []
        return [b.callback_data for row in self.kb.inline_keyboard for b in row]

    def __repr__(self) -> str:
        head = self.text.replace("\n", " ⏎ ")[:90]
        return f"<{self.role}/{self.topic}: {head}>"


class FakeRegistry:
    """Реестр, который никуда не ходит."""

    def __init__(self) -> None:
        self.sent: list[Say] = []
        # Роутер спрашивает usernames для явных упоминаний.
        self.me = {r: f"lily_cf_{r}_bot" for r in
                   ("assistant", "research", "strategy", "editor",
                    "reels", "design", "publisher")}

    async def say(self, role, chat_id, text, *, topic="general", kb=None,
                  with_label=True):
        self.sent.append(Say(role, chat_id, text, topic, kb))
        return None

    def clear(self) -> None:
        self.sent.clear()

    def last(self) -> Say | None:
        return self.sent[-1] if self.sent else None

    def last_in(self, topic: str) -> Say | None:
        """Последнее сообщение в названном топике.

        Карточку плана нельзя брать последним сообщением: следом за ней в
        General уходит строка-окрик, и `last()` возвращает её.
        """
        for s in reversed(self.sent):
            if s.topic == topic:
                return s
        return None

    def texts(self) -> str:
        return "\n---\n".join(s.text for s in self.sent)


# ── проверки ──────────────────────────────────────────────────────────

FAILS: list[str] = []
CHECKS = [0]


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS[0] += 1
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(f"{name}: {detail}" if detail else name)
    return ok


def report() -> int:
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО {len(FAILS)} из {CHECKS[0]}:")
        for f in FAILS:
            print(f"  · {f}")
        return 1
    print(f"Все {CHECKS[0]} проверок прошли.")
    return 0
