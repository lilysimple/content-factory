"""Стенд: гоняем завод без Telegram и без боевой базы.

Реестр подделан — say() складывает сообщения в список вместо отправки.
База и папка брендов копируются во временную директорию, поэтому прогон
ничего не портит.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Пути выводятся от самого файла: стенд не должен знать, где лежит
# репозиторий, иначе он работает ровно на одной машине.
REPO = Path(__file__).resolve().parents[1]
BRANDS = REPO.parent / "content-factory-brands"
TMP = Path(__file__).parent / ".sandbox"

CHAT = -1003990495505


def setup() -> None:
    """Свежая песочница на каждый прогон."""
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
    for folder in ("posts", "plans"):
        path = TMP / "brands" / "lily-space" / folder
        if path.is_dir():
            for f in path.iterdir():
                if f.is_file():
                    f.unlink()

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
