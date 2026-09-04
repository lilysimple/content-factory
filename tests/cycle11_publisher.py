"""Цикл 11: Публикатор.

Модель здесь не участвует вовсе — роль детерминирована. Проверяется то,
что дороже всего ошибается: двойная публикация, публикация задним числом,
неполный комплект и молчаливый пропуск.
"""
from __future__ import annotations

import asyncio

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import desk, publisher                              # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

SENT = []


class FakeMsg:
    def __init__(self, mid): self.message_id = mid


class FakeBot:
    """Канал, который всё принимает и возвращает номер сообщения."""
    def __init__(self): self.calls = []

    async def send_message(self, **kw):
        self.calls.append(("text", kw))
        return FakeMsg(1000 + len(self.calls))

    async def send_photo(self, **kw):
        self.calls.append(("photo", kw))
        return FakeMsg(1000 + len(self.calls))

    async def send_video(self, **kw):
        self.calls.append(("video", kw))
        return FakeMsg(1000 + len(self.calls))


class BrokenBot(FakeBot):
    async def send_message(self, **kw):
        raise RuntimeError("chat not found")


class Reg(FakeRegistry):
    def __init__(self, bot=None):
        super().__init__()
        self._bot = bot or FakeBot()
        self.files = []          # (имя, подпись, кнопки, фотографией ли)

    def bot(self, role): return self._bot

    async def send_file(self, role, chat_id, blob, name, **kw):
        SENT.append(name)
        self.files.append((name, kw.get("caption") or "", kw.get("kb"),
                           kw.get("as_photo", False),
                           kw.get("as_video", False)))

    def clear(self):
        super().clear()
        self.files.clear()


def seed(tid="2026-08-14-telegram-01", date="2026-08-14", status="ready",
         text="Готовый текст поста.", png=False):
    b = desk.brand(CHAT)
    # Артефакты прошлого прогона живут на диске и подмешиваются в комплект:
    # тема без картинки внезапно получает обложку и потолок подписи 1024.
    for old in b.path("posts").glob(f"{tid}-*"):
        old.unlink()
    b.artifact(f"posts/{tid}.md", f"<!-- {tid} -->\n\n{text}")
    if png:
        b.artifact(f"posts/{tid}-cover.png", b"\x89PNG\r\n\x1a\n" + b"0" * 900)
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        c.execute("DELETE FROM posts WHERE chat_id = ?", (CHAT,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                  "status, title, asset) VALUES (?,?,?,'telegram','пост',?,"
                  "'Тема',?)", (tid, CHAT, date, status, f"posts/{tid}.md"))
    return tid


def seed_reel(tid="2026-08-14-instagram-01", date="2026-08-14",
              status="ready", text="", mb=1, asset=None):
    """Тема, закрытая смонтированным роликом.

    `asset` пустой — это норма второй цепи: дубль сняли из головы, темы в
    плане не было, подписи никто не писал. Ролик при этом готов.
    """
    b = desk.brand(CHAT)
    for old in b.path("posts").glob(f"{tid}-*"):
        old.unlink()
    b.artifact(f"posts/{tid}-reel.mp4",
               b"\x00\x00\x00\x18ftypmp42" + b"\xff\xfe" * (mb * 524_288))
    if text:
        b.artifact(f"posts/{tid}.md", f"<!-- {tid} -->\n\n{text}")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        c.execute("DELETE FROM posts WHERE chat_id = ?", (CHAT,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                  "status, title, asset) VALUES (?,?,?,'telegram','reels',?,"
                  "'Дубль',?)", (tid, CHAT, date, status, asset))
    return tid


async def main() -> None:
    cfg_channel = "@test_channel"
    object.__setattr__(cfg, "publish_channel", cfg_channel)

    # ── 1. комплект собирается ────────────────────────────────────────
    print("\n1. Комплект")
    tid = seed()
    pkg = publisher.collect(CHAT, dict(db.one("SELECT * FROM themes WHERE id = ?", tid)))
    check("текст подобран", "Готовый текст" in pkg.text, pkg.text[:40])
    check("проблем нет", not pkg.problems, str(pkg.problems))
    check("площадка автоматическая", pkg.auto is True)

    # ── 2. превью показывает пост как есть ────────────────────────────
    print("\n2. Превью")
    p = publisher.preview(pkg)
    check("в превью сам текст", "Готовый текст поста." in p)
    check("в превью id и площадка", tid in p and "telegram" in p)
    check("видно, что без картинки", "без картинки" in p)

    # ── 3. публикация ─────────────────────────────────────────────────
    print("\n3. Публикация")
    reg = Reg()
    link = await publisher.send(reg, CHAT, pkg)
    check("ушло в канал", len(reg._bot.calls) == 1, str(reg._bot.calls))
    check("ушло текстом", reg._bot.calls[0][0] == "text")
    check("в канал, а не в чат",
          reg._bot.calls[0][1]["chat_id"] == cfg_channel,
          str(reg._bot.calls[0][1]["chat_id"]))
    check("ссылка построена", link.startswith("https://t.me/test_channel/"), link)

    row = db.one("SELECT * FROM posts WHERE theme_id = ?", tid)
    check("состояние pub", row["state"] == "pub", row["state"])
    check("external_id записан", bool(row["external_id"]), str(row["external_id"]))
    check("тема в pub",
          db.one("SELECT status FROM themes WHERE id = ?", tid)["status"] == "pub")

    # ── 4. двойная публикация ─────────────────────────────────────────
    print("\n4. Двойная публикация")
    before = len(reg._bot.calls)
    try:
        await publisher.send(reg, CHAT, publisher.collect(
            CHAT, dict(db.one("SELECT * FROM themes WHERE id = ?", tid))))
        check("вторая публикация отбита", False, "отправил повторно")
    except publisher.NotReady as e:
        check("вторая публикация отбита", "уже опубликовано" in str(e), str(e))
    check("в канал ничего не ушло", len(reg._bot.calls) == before)

    # ── 5. картинка идёт фотографией ──────────────────────────────────
    print("\n5. С картинкой")
    tid = seed(png=True)
    reg = Reg()
    pkg = publisher.collect(CHAT, dict(db.one("SELECT * FROM themes WHERE id = ?", tid)))
    check("картинка найдена", len(pkg.images) == 1, str(pkg.images))
    await publisher.send(reg, CHAT, pkg)
    check("ушло фотографией", reg._bot.calls[0][0] == "photo")
    check("текст стал подписью", "caption" in reg._bot.calls[0][1])

    # ── 5б. превью показывает пост фотографией ────────────────────────
    # Пост с картинкой выходит одним сообщением: фото и подпись. Превью,
    # где текст отдельно от картинки, показывало бы пост, которого не
    # будет, и утверждать в нём нечего.
    print("\n5б. Превью с картинкой")
    tid = seed(png=True, text="Готовый текст поста.")
    reg = Reg()
    await publisher.run(reg, CHAT, f"комплект {tid}")
    check("картинка ушла фотографией",
          any(f[3] for f in reg.files), str(reg.files))
    photo = next((f for f in reg.files if f[3]), None)
    check("текст поста стал подписью",
          bool(photo) and photo[1] == "Готовый текст поста.",
          str(photo[1] if photo else None))
    check("кнопки на фотографии, а не над ней",
          bool(photo) and photo[2] is not None
          and any(b.callback_data.startswith("pub:go:")
                  for row in photo[2].inline_keyboard for b in row),
          str(photo[2] if photo else None))
    check("текста поста нет вторым разом",
          "Готовый текст поста." not in reg.texts(), reg.texts()[:160])
    check("шапка про слот осталась", tid in reg.texts(), reg.texts()[:160])

    # Подпись не вмещается — возвращаемся к тексту, иначе превью соврёт.
    tid = seed(png=True, text="я" * 1500)
    reg = Reg()
    await publisher.run(reg, CHAT, f"комплект {tid}")
    check("длинный текст показан текстом, а не подписью",
          "так выйдет" in reg.texts(), reg.texts()[:160])
    check("про потолок сказано", "потолок 1024" in reg.texts(),
          reg.texts()[:200])

    # ── 6. потолок подписи ────────────────────────────────────────────
    print("\n6. Потолки")
    tid = seed(text="я" * 1500, png=True)
    pkg = publisher.collect(CHAT, dict(db.one("SELECT * FROM themes WHERE id = ?", tid)))
    check("длинная подпись поймана",
          any("потолок 1024" in p for p in pkg.problems), str(pkg.problems))

    tid = seed(text="я" * 1500)
    pkg = publisher.collect(CHAT, dict(db.one("SELECT * FROM themes WHERE id = ?", tid)))
    check("без картинки 1500 знаков проходят", not pkg.problems, str(pkg.problems))

    tid = seed(text="я" * 5000)
    pkg = publisher.collect(CHAT, dict(db.one("SELECT * FROM themes WHERE id = ?", tid)))
    check("текст длиннее 4096 пойман",
          any("потолок 4096" in p for p in pkg.problems), str(pkg.problems))

    # ── 7. неполный комплект не публикуется ───────────────────────────
    print("\n7. Неполный комплект")
    reg = Reg()
    try:
        await publisher.send(reg, CHAT, pkg)
        check("длинный текст не ушёл", False, "опубликовал через потолок")
    except publisher.NotReady:
        check("длинный текст не ушёл", True)
    check("канал не тронут", not reg._bot.calls)

    # ── 8. канал не настроен ──────────────────────────────────────────
    print("\n8. Канала нет")
    object.__setattr__(cfg, "publish_channel", "")
    tid = seed()
    reg = Reg()
    pkg = publisher.collect(CHAT, dict(db.one("SELECT * FROM themes WHERE id = ?", tid)))
    try:
        await publisher.send(reg, CHAT, pkg)
        check("без канала не публикует", False, "отправил в пустоту")
    except publisher.NoChannel:
        check("без канала не публикует", True)
    check("в превью сказано про канал",
          "PUBLISH_CHANNEL" in publisher.preview(pkg))
    object.__setattr__(cfg, "publish_channel", cfg_channel)

    # ── 9. отказ Telegram ─────────────────────────────────────────────
    print("\n9. Telegram отказал")
    tid = seed()
    reg = Reg(BrokenBot())
    pkg = publisher.collect(CHAT, dict(db.one("SELECT * FROM themes WHERE id = ?", tid)))
    try:
        await publisher.send(reg, CHAT, pkg)
        check("отказ поднят наверх", False, "проглотил ошибку")
    except publisher.NotReady as e:
        check("отказ поднят наверх", "Telegram отказал" in str(e), str(e))
    row = db.one("SELECT state, attempts FROM posts WHERE theme_id = ?", tid)
    check("состояние failed", row["state"] == "failed", row["state"])
    check("попытка посчитана", row["attempts"] == 1, str(row["attempts"]))
    check("тема не помечена опубликованной",
          db.one("SELECT status FROM themes WHERE id = ?", tid)["status"] == "ready")

    # ── 10. просроченные слоты ────────────────────────────────────────
    print("\n10. Просроченное")
    seed(tid="2020-01-01-telegram-01", date="2020-01-01", status="ready")
    late = publisher.overdue(CHAT)
    check("просроченный слот виден", len(late) == 1, str(len(late)))
    reg = Reg()
    await publisher.run(reg, CHAT, "что в очереди")
    check("про просрочку сказано", "дата прошла" in reg.texts(),
          reg.texts()[:120])
    check("задним числом не публикует", "не публикую" in reg.texts())

    # ── 11. пропуск требует причины ───────────────────────────────────
    print("\n11. Пропуск")
    tid = seed()
    reg = Reg()
    await publisher.on_callback(reg, CHAT, f"skip:{tid}")
    check("ждём причину", publisher.wants_reason(CHAT) is True)
    check("сказано, зачем причина", "статистике" in reg.texts(), reg.texts()[:150])

    reg.clear()
    await publisher.take_reason(reg, CHAT, "событие отменилось")
    check("флаг снят", publisher.wants_reason(CHAT) is False)
    row = db.one("SELECT status, skip_reason FROM themes WHERE id = ?", tid)
    check("тема в skip", row["status"] == "skip", row["status"])
    check("причина записана", row["skip_reason"] == "событие отменилось",
          str(row["skip_reason"]))

    # ── 12. кнопка публикации ─────────────────────────────────────────
    print("\n12. Кнопки")
    tid = seed()
    reg = Reg()
    await publisher.run(reg, CHAT, "что в очереди")
    btns = [b for s in reg.sent for b in s.buttons]
    check("кнопка публикации есть", any(b.startswith("pub:go:") for b in btns),
          str(btns))
    check("id темы в кнопке", any(b.endswith(tid) for b in btns), str(btns))

    reg.clear()
    await publisher.on_callback(reg, CHAT, f"go:{tid}")
    check("опубликовал по кнопке", "Опубликовано" in reg.texts(),
          reg.texts()[:120])
    check("сказал про метрики честно", "не подключён" in reg.texts())

    # ── 13. комплект по id ────────────────────────────────────────────
    # Так передаёт Дизайнер после принятого макета: спрашивают про
    # конкретный пост, а не про очередь целиком.
    print("\n13. Комплект по id")
    tid = seed()
    reg = Reg()
    await publisher.run(reg, CHAT, f"свёрстан макет по теме {tid}")
    check("показан именно он", tid in reg.texts(), reg.texts()[:120])
    check("список просрочки не мешался",
          "Задним числом" not in reg.texts(), reg.texts()[:200])

    reg = Reg()
    await publisher.run(reg, CHAT, "2026-08-14-telegram-99")
    check("несуществующий id назван", "нет" in reg.texts().lower(),
          reg.texts()[:120])

    # Слот из будущего показываем, но кнопки публикации не даём:
    # выйти раньше срока так же плохо, как выйти задним числом.
    future = seed(tid="2026-12-31-telegram-01", date="2026-12-31")
    reg = Reg()
    await publisher.run(reg, CHAT, f"комплект {future}")
    btns = [b for s in reg.sent for b in s.buttons]
    check("превью будущего слота показано", future in reg.texts(),
          reg.texts()[:120])
    check("кнопки публикации у него нет",
          not any(b.startswith("pub:go:") for b in btns), str(btns))
    check("сказано, что дата не наступила",
          "не наступила" in reg.texts(), reg.texts()[-200:])

    # ── 14. комплект с видео ──────────────────────────────────────────
    #
    # Вторая цепь завода — от снятого, а не от плана — упиралась ровно
    # сюда. `collect` собирал только PNG, поля под ролик не было, а текст
    # был обязателен: «В очередь» на рилсе отвечало «текста нет». Хуже
    # того, монтаж клал путь к mp4 в `themes.asset`, и `b.read()` падал
    # `UnicodeDecodeError` внутри необёрнутого колбэка — человек нажимал
    # кнопку и не получал вообще ничего.
    print("\n14. Комплект с видео")
    tid = seed_reel()
    theme = dict(db.one("SELECT * FROM themes WHERE id = ?", tid))
    pkg = publisher.collect(CHAT, theme)
    check("ролик найден", pkg.video is not None and pkg.video.exists(),
          str(pkg.video))
    check("макеты к ролику не подмешаны", not pkg.images, str(pkg.images))
    check("без подписи это не проблема", not pkg.problems, str(pkg.problems))
    check("в шапке видно, что без подписи",
          "без подписи" in publisher.card(pkg), publisher.card(pkg))

    reg = Reg()
    await publisher.send(reg, CHAT, pkg)
    check("ушло видео, а не документ", reg._bot.calls[0][0] == "video",
          str(reg._bot.calls[0][0]))
    check("подписи нет, а не пустая строка",
          reg._bot.calls[0][1]["caption"] is None,
          str(reg._bot.calls[0][1]["caption"]))
    check("тема в pub",
          db.one("SELECT status FROM themes WHERE id = ?", tid)["status"] == "pub")

    # ── 14б. подпись под роликом ──────────────────────────────────────
    print("\n14б. Подпись под роликом")
    tid = seed_reel(text="Подпись под видео.", asset=None)
    b = desk.brand(CHAT)
    with db.tx() as c:
        c.execute("UPDATE themes SET asset = ? WHERE id = ?",
                  (f"posts/{tid}.md", tid))
    pkg = publisher.collect(CHAT, dict(
        db.one("SELECT * FROM themes WHERE id = ?", tid)))
    check("подпись подобрана", pkg.text == "Подпись под видео.", pkg.text)
    reg = Reg()
    await publisher.send(reg, CHAT, pkg)
    check("подпись уехала под видео",
          reg._bot.calls[0][1]["caption"] == "Подпись под видео.")

    # Потолок у подписи 1024, а не 4096: под видео это подпись.
    tid = seed_reel(text="я" * 1100)
    with db.tx() as c:
        c.execute("UPDATE themes SET asset = ? WHERE id = ?",
                  (f"posts/{tid}.md", tid))
    pkg = publisher.collect(CHAT, dict(
        db.one("SELECT * FROM themes WHERE id = ?", tid)))
    check("длинная подпись поймана до отправки",
          any("потолок 1024" in p for p in pkg.problems), str(pkg.problems))
    check("названа причина потолка",
          any("из-за видео" in p for p in pkg.problems), str(pkg.problems))

    # ── 14в. mp4 в asset не роняет сборку ─────────────────────────────
    #
    # Так писал монтаж до починки. Живые базы это переживут: комплект
    # должен сказать словами, а не упасть на первом нетекстовом байте.
    print("\n14в. mp4 в asset")
    tid = seed_reel()
    with db.tx() as c:
        c.execute("UPDATE themes SET asset = ? WHERE id = ?",
                  (f"posts/{tid}-reel.mp4", tid))
    pkg = publisher.collect(CHAT, dict(
        db.one("SELECT * FROM themes WHERE id = ?", tid)))
    check("сборка не упала", pkg.video is not None)
    check("сказано словами, а не исключением",
          any("вместо текста" in p for p in pkg.problems), str(pkg.problems))

    # ── 14г. потолок загрузки ─────────────────────────────────────────
    print("\n14г. Потолок Bot API")
    tid = seed_reel(mb=publisher.TG_VIDEO_MB + 2)
    pkg = publisher.collect(CHAT, dict(
        db.one("SELECT * FROM themes WHERE id = ?", tid)))
    check("тяжёлый ролик пойман до кнопки",
          any("потолок Bot API" in p for p in pkg.problems), str(pkg.problems))

    reg = Reg()
    await publisher.run(reg, CHAT, f"комплект {tid}")
    btns = [b for s in reg.sent for b in s.buttons]
    check("кнопки публикации у него нет",
          not any(b.startswith("pub:go:") for b in btns), str(btns))

    # ── 14д. ролик без текста доходит до очереди ──────────────────────
    #
    # `due()` фильтровал по `asset IS NOT NULL`, и тема, заведённая по
    # факту съёмки, в очередь не попадала вовсе.
    print("\n14д. Очередь видит ролик")
    tid = seed_reel(date="2026-08-14")
    check("ролик без текста в очереди",
          any(t["id"] == tid for t in publisher.due(CHAT)),
          str([t["id"] for t in publisher.due(CHAT)]))

    reg = Reg()
    await publisher.run(reg, CHAT, "что в очереди")
    check("превью показано видео",
          any(f[4] for f in reg.files), str(reg.files))
    # Кнопки едут на самом видео, а не отдельным сообщением: человек
    # утверждает то, что смотрит.
    kbs = [f[2] for f in reg.files if f[2] is not None]
    data = [btn.callback_data for kb in kbs for row in kb.inline_keyboard
            for btn in row]
    check("кнопка публикации на ролике",
          any(d.startswith("pub:go:") for d in data), str(data))


asyncio.run(main())
raise SystemExit(report())
