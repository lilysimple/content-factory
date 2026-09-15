"""Цикл 25: автопубликация по времени.

Проверяется то, что дороже всего ошибается у автомата: порог включения,
граница окна, публикация задним числом и молчание. Автомат, который
опубликовал тихо, и автомат, который тихо не опубликовал, одинаково
плохи — и то и другое человек узнаёт из канала, а не из чата.

Модель здесь не участвует: роль детерминирована целиком.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime, timedelta

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import desk, publisher                          # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

TODAY = desk.today(CHAT)


class FakeMsg:
    def __init__(self, mid): self.message_id = mid


class FakeBot:
    def __init__(self): self.calls = []

    async def send_message(self, **kw):
        self.calls.append(("text", kw))
        return FakeMsg(2000 + len(self.calls))

    async def send_photo(self, **kw):
        self.calls.append(("photo", kw))
        return FakeMsg(2000 + len(self.calls))

    async def send_video(self, **kw):
        self.calls.append(("video", kw))
        return FakeMsg(2000 + len(self.calls))


class Reg(FakeRegistry):
    def __init__(self):
        super().__init__()
        self._bot = FakeBot()

    def bot(self, role): return self._bot

    async def send_file(self, role, chat_id, blob, name, **kw):
        pass


def wipe() -> None:
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        c.execute("DELETE FROM posts WHERE chat_id = ?", (CHAT,))


def seed_theme(tid: str, day: str, status: str = "ready",
               text: str = "Готовый текст поста.") -> str:
    b = desk.brand(CHAT)
    for old in b.path("posts").glob(f"{tid}-*"):
        old.unlink()
    asset = None
    if text:
        b.artifact(f"posts/{tid}.md", f"<!-- {tid} -->\n\n{text}")
        asset = f"posts/{tid}.md"
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO themes (id, chat_id, date, plat, "
                  "format, status, title, asset) VALUES (?,?,?,'telegram',"
                  "'пост',?,'Тема',?)", (tid, CHAT, day, status, asset))
    return tid


def seed_approvals(n: int, days_ago: int) -> None:
    """n опубликованных постов, первый — days_ago дней назад."""
    first = (date.fromisoformat(TODAY) - timedelta(days=days_ago)).isoformat()
    with db.tx() as c:
        for i in range(n):
            tid = f"hist-{i:02d}"
            c.execute("INSERT OR REPLACE INTO posts (theme_id, chat_id, "
                      "platform, state, external_id, published_at) VALUES "
                      "(?,?,'telegram','pub',?,?)",
                      (tid, CHAT, f"hist:{i}", f"{first} 10:00:00"))


def at_moment(at: str, shift_min: int = 0, day: str = ""):
    h, m = (int(x) for x in at.split(":"))
    base = datetime.combine(date.fromisoformat(day or TODAY),
                            datetime.min.time()).replace(hour=h, minute=m)
    return base + timedelta(minutes=shift_min)


async def main() -> None:
    object.__setattr__(cfg, "publish_channel", "@test_channel")

    # ── 1. разбор времени ─────────────────────────────────────────────
    print("\n1. Время слота")
    check("«9:30» понимается", publisher.parse_at("9:30") == "09:30")
    check("«09.30» тоже", publisher.parse_at("09.30") == "09:30")
    check("«25:00» отказ", publisher.parse_at("25:00") is None)
    check("«утром» отказ", publisher.parse_at("утром") is None)
    check("время по умолчанию есть",
          publisher.settings(CHAT)[1] == publisher.AUTO_AT_DEFAULT)

    # ── 2. гейт ───────────────────────────────────────────────────────
    print("\n2. Порог автомата")
    wipe()
    db.set_tenant(CHAT, auto=0, auto_at=None)
    blockers = publisher.gate(CHAT)
    check("на пустой истории автомат закрыт", bool(blockers), str(blockers))
    check("отказ называет счёт утверждений",
          any("утверждений 0" in b for b in blockers), str(blockers))
    check("включить не даёт", bool(publisher.arm(CHAT, True)))
    check("флаг не поднялся", publisher.settings(CHAT)[0] is False)

    seed_approvals(publisher.AUTO_MIN_PUBS, publisher.AUTO_MIN_DAYS - 1)
    check("десять утверждений, но две недели не прошли",
          any("дней" in b for b in publisher.gate(CHAT)),
          str(publisher.gate(CHAT)))

    seed_approvals(publisher.AUTO_MIN_PUBS - 1, publisher.AUTO_MIN_DAYS + 3)
    with db.tx() as c:
        c.execute("DELETE FROM posts WHERE theme_id = ?",
                  (f"hist-{publisher.AUTO_MIN_PUBS - 1:02d}",))
    check("девять утверждений — всё ещё нет",
          any("утверждений" in b for b in publisher.gate(CHAT)),
          str(publisher.gate(CHAT)))

    seed_approvals(publisher.AUTO_MIN_PUBS, publisher.AUTO_MIN_DAYS + 3)
    check("порог пройден", publisher.gate(CHAT) == [], str(publisher.gate(CHAT)))
    check("включение прошло", publisher.arm(CHAT, True, "10:00") == [])
    on, at = publisher.settings(CHAT)
    check("флаг и время записались", on and at == "10:00", f"{on} {at}")

    # Канал сняли — гейт закрывается обратно.
    object.__setattr__(cfg, "publish_channel", "")
    check("без канала автомат закрыт",
          any("канал" in b for b in publisher.gate(CHAT)))
    object.__setattr__(cfg, "publish_channel", "@test_channel")

    # ── 3. окно ───────────────────────────────────────────────────────
    print("\n3. Окно слота")
    seed_theme("2026-09-15-telegram-01", TODAY)
    ready, late = publisher.due_now(CHAT, at_moment("10:00", -1))
    check("до времени не публикуем", not ready and not late)
    ready, late = publisher.due_now(CHAT, at_moment("10:00", 0))
    check("ровно в слот — пора", len(ready) == 1 and not late)
    ready, late = publisher.due_now(CHAT, at_moment("10:00", 30))
    check("через полчаса ещё пора", len(ready) == 1)
    ready, late = publisher.due_now(
        CHAT, at_moment("10:00", publisher.AUTO_GRACE_MIN + 1))
    check("после окна — опоздание, а не публикация",
          not ready and len(late) == 1)

    # Instagram автомата не касается: там публикует человек руками, и
    # ежедневная строка «комплект не готов» была бы неправдой.
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO themes (id, chat_id, date, plat, "
                  "format, status, title, asset) VALUES "
                  "(?,?,?,'instagram','карусель','ready','Тема',NULL)",
                  ("2026-09-15-instagram-01", CHAT, TODAY))
    ready, _ = publisher.due_now(CHAT, at_moment("10:00", 5))
    check("чужая площадка в автомат не попадает",
          [t["id"] for t in ready] == ["2026-09-15-telegram-01"],
          str([t["id"] for t in ready]))
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE id = ?", ("2026-09-15-instagram-01",))

    # ── 4. публикация ─────────────────────────────────────────────────
    print("\n4. Автомат публикует")
    reg = Reg()
    links = await publisher.autopost(reg, CHAT, at_moment("10:00", 5))
    check("ушло в канал", len(links) == 1, str(links))
    check("отправлено текстом", reg._bot.calls[0][0] == "text")
    row = db.one("SELECT * FROM themes WHERE id = ?", "2026-09-15-telegram-01")
    check("тема в pub", row["status"] == "pub", row["status"])
    post = db.one("SELECT * FROM posts WHERE theme_id = ?",
                  "2026-09-15-telegram-01")
    check("публикация записана", post["state"] == "pub", post["state"])
    check("человеку сказано", any("Опубликовала сама" in s.text
                                  for s in reg.sent), reg.texts()[:120])
    check("сказано в очередь", any(s.topic == "queue" for s in reg.sent))

    # ── 5. второй тик ─────────────────────────────────────────────────
    print("\n5. Второй тик")
    reg2 = Reg()
    links = await publisher.autopost(reg2, CHAT, at_moment("10:00", 6))
    check("дубля нет", links == [], str(links))
    check("и молчит", reg2.sent == [], reg2.texts()[:120])

    # ── 6. задним числом ──────────────────────────────────────────────
    print("\n6. Вчерашний слот")
    wipe()
    seed_approvals(publisher.AUTO_MIN_PUBS, publisher.AUTO_MIN_DAYS + 3)
    yesterday = (date.fromisoformat(TODAY) - timedelta(days=1)).isoformat()
    seed_theme("2026-09-14-telegram-01", yesterday)
    reg3 = Reg()
    links = await publisher.autopost(reg3, CHAT, at_moment("10:00", 5))
    check("вчерашнее автомат не публикует", links == [], str(links))
    check("и в канал ничего не ушло", reg3._bot.calls == [])

    # ── 7. неполный комплект ──────────────────────────────────────────
    print("\n7. Комплект не готов")
    wipe()
    seed_approvals(publisher.AUTO_MIN_PUBS, publisher.AUTO_MIN_DAYS + 3)
    # Комплект есть, но текст не влезает в сообщение: это и есть
    # типичная неготовность — тема `ready`, а публиковать нельзя.
    seed_theme("2026-09-15-telegram-02", TODAY,
               text="Очень длинный текст. " * 300)
    reg4 = Reg()
    links = await publisher.autopost(reg4, CHAT, at_moment("10:00", 5))
    check("без текста не публикует", links == [])
    check("и говорит почему", any("не готов" in s.text for s in reg4.sent),
          reg4.texts()[:200])
    reg5 = Reg()
    await publisher.autopost(reg5, CHAT, at_moment("10:00", 6))
    check("второй раз не повторяется", reg5.sent == [], reg5.texts()[:120])

    # ── 8. опоздание называется ───────────────────────────────────────
    print("\n8. Пропущенный слот")
    wipe()
    seed_approvals(publisher.AUTO_MIN_PUBS, publisher.AUTO_MIN_DAYS + 3)
    seed_theme("2026-09-15-telegram-03", TODAY)
    reg6 = Reg()
    links = await publisher.autopost(
        reg6, CHAT, at_moment("10:00", publisher.AUTO_GRACE_MIN + 10))
    check("после окна не публикует", links == [])
    check("но пропуск называет", any("пропущен" in s.text for s in reg6.sent),
          reg6.texts()[:200])

    # ── 9. выключенный автомат ────────────────────────────────────────
    print("\n9. Выключено")
    publisher.arm(CHAT, False)
    check("выключается без порога", publisher.settings(CHAT)[0] is False)
    wipe()
    seed_theme("2026-09-15-telegram-04", TODAY)
    reg7 = Reg()
    links = await publisher.autopost(reg7, CHAT, at_moment("10:00", 5))
    check("выключенный автомат молчит и не публикует",
          links == [] and reg7.sent == [] and reg7._bot.calls == [])

    # ── 10. колонки в старой базе ─────────────────────────────────────
    print("\n10. Миграция")
    old = harness.TMP / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE tenants (chat_id INTEGER PRIMARY KEY, tz TEXT)")
    con.commit()
    con.row_factory = sqlite3.Row
    added = db._add_columns(con)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(tenants)")}
    check("колонки дописались", {"auto", "auto_at"} <= cols, str(added))
    check("повторный проход ничего не делает", db._add_columns(con) == [])
    con.close()

    raise SystemExit(report())


asyncio.run(main())
