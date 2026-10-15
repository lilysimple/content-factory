"""Цикл 31: публикация в Instagram.

Модель здесь не участвует, как и во всём Публикаторе. Проверяется то, что
дороже всего ошибается у второй площадки:

- потолки площадки ловятся **до** кнопки, а не отказом Meta после: превью,
  которое соврало, — это пост, которого человек не ждал;
- порядок трёх запросов. Контейнер, ожидание, публикация — и у карусели
  контейнеров столько же, сколько карточек, плюс один общий. Подпись
  едет на общем, а не на каждой карточке: на карточке Instagram её молча
  выбрасывает;
- одна картинка это пост, а не карусель из одной. `CAROUSEL` с
  единственным ребёнком Instagram не принимает вовсе;
- дубль. Защита общая с Telegram (`posts.external_id UNIQUE`), и
  проверяется она здесь заново: префикс `ig:` новый, и ошибка в нём
  означала бы второй пост в профиле;
- телеграмные мерки на Instagram не применяются. 1024 знака завернули бы
  подпись, которой положено 2200.

Сеть подделана на уровне `_call`: проверяется протокол, а не httpx.
"""
from __future__ import annotations

import asyncio
import struct
import zlib

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import desk, instapost, publisher, strategy     # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

IG = "17841400000000000"


# ── подделки ──────────────────────────────────────────────────────────

class FakeFile:
    def __init__(self, path): self.file_path = path


class FakeDoc:
    def __init__(self, fid): self.file_id = fid


class FakeMsg:
    def __init__(self, mid, name):
        self.message_id = mid
        self.document = FakeDoc(f"file-{name}")


class FakeBot:
    """Бот, который отдаёт файловый адрес и помнит удаления."""
    token = "111:AAA"

    def __init__(self):
        self.deleted = []

    async def get_file(self, file_id):
        return FakeFile(f"documents/{file_id}")

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


class Reg(FakeRegistry):
    def __init__(self):
        super().__init__()
        self._bot = FakeBot()
        self.files = []

    def bot(self, role): return self._bot

    async def send_file(self, role, chat_id, blob, name, **kw):
        self.files.append((name, kw.get("topic")))
        return FakeMsg(500 + len(self.files), name)


class Wire:
    """Поддельный Graph API: помнит, что спросили, и отвечает по порядку."""

    def __init__(self, *, limit_used=0, status="FINISHED"):
        self.calls = []
        self.limit_used = limit_used
        self.status = status
        self.n = 0

    async def __call__(self, method, path, **params):
        self.calls.append((method, path, params))
        if path.endswith("content_publishing_limit"):
            return {"data": [{"quota_usage": self.limit_used,
                              "config": {"quota_total": 100}}]}
        if path.endswith("/media"):
            self.n += 1
            out = {"id": f"c{self.n}"}
            if params.get("upload_type") == "resumable":
                out["uri"] = (instapost.RUPLOAD
                              + f"/ig-api-upload/v23.0/c{self.n}")
            return out
        if path.endswith("media_publish"):
            return {"id": "media-777"}
        if path == "refresh_access_token":
            return {"access_token": "token-new", "expires_in": 5184000}
        if "fields" in params and "status_code" in str(params["fields"]):
            return {"status_code": self.status, "status": "готово"}
        if params.get("fields") == "permalink":
            return {"permalink": "https://www.instagram.com/p/ABC/"}
        return {}

    def made(self):
        """Пути запросов подряд, без параметров."""
        return [f"{m} {p}" for m, p, _ in self.calls]


def png(w: int, h: int) -> bytes:
    """Настоящий PNG нужного размера.

    Байтовая заготовка вроде `b"\\x89PNG" + нули` для этого цикла не
    годится: пропорцию читает `sips`, и на подделке он отвечает отказом —
    проверка пропорции превратилась бы в проверку испорченного файла.
    """
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def seed(tid, *, date, text="Подпись к карусели.", fmt="карусель",
         images=(), video_mb=0, status="ready", keep=False):
    """Тема Instagram с макетами или роликом на диске.

    `date=None` — тема по факту съёмки: день пустой, как его оставляет
    `desk.adhoc`. `keep` оставляет прошлые темы в базе: занятость дня
    проверяется только тогда, когда в базе есть чем его занять.
    """
    b = desk.brand(CHAT)
    for old in b.path("posts").glob(f"{tid}*"):
        if old.is_file():
            old.unlink()
    asset = None
    if text:
        b.artifact(f"posts/{tid}.md", f"<!-- {tid} -->\n\n{text}")
        asset = f"posts/{tid}.md"
    for i, (w, h) in enumerate(images, 1):
        b.artifact(f"posts/{tid}-{i:02d}.png", png(w, h))
    if video_mb:
        b.artifact(f"posts/{tid}-reel.mp4",
                   b"\x00\x00\x00\x18ftypmp42"
                   + b"\xff\xfe" * (video_mb * 524_288))
    with db.tx() as c:
        if not keep:
            c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
            c.execute("DELETE FROM posts WHERE chat_id = ?", (CHAT,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                  "status, title, src, asset) VALUES (?,?,?,'instagram',?,?,"
                  "'Тема','adhoc',?)", (tid, CHAT, date, fmt, status, asset))
    return tid


def pkg_of(tid):
    return publisher.collect(CHAT, dict(
        db.one("SELECT * FROM themes WHERE id = ?", tid)))


async def main() -> None:
    today = desk.today(CHAT)

    # ── 1. без аккаунта площадка остаётся ручной ──────────────────────
    #
    # Это важнее, чем кажется: площадка, попавшая в автомат без
    # транспорта, каждый день получала бы строку «комплект не готов» —
    # неправду. Комплект как раз готов, выкладывает его человек.
    print("\n1. Транспорта нет")
    object.__setattr__(cfg, "ig_user_id", "")
    object.__setattr__(cfg, "ig_token", "")
    with db.tx() as c:
        c.execute("DELETE FROM ig_token")
    check("без ключей не готов", not instapost.ready())
    check("Instagram вне автомата",
          "instagram" not in strategy.auto_publish(),
          str(strategy.auto_publish()))

    tid = seed("2026-10-20-instagram-01", date=today, images=[(1080, 1350)])
    pkg = pkg_of(tid)
    check("комплект назвал причину",
          any("не настроен" in p for p in pkg.problems), str(pkg.problems))

    # ── 2. с аккаунтом площадка входит в автомат ──────────────────────
    print("\n2. Транспорт настроен")
    object.__setattr__(cfg, "ig_user_id", IG)
    object.__setattr__(cfg, "ig_token", "token-first")
    check("ключи прочитаны", instapost.ready())
    check("токен лёг в базу", instapost.token() == "token-first",
          instapost.token())
    check("Instagram в автомате",
          "instagram" in strategy.auto_publish(), str(strategy.auto_publish()))

    # ── 3. потолки площадки, а не чужие ───────────────────────────────
    print("\n3. Потолки")
    tid = seed("2026-10-20-instagram-02", date=today,
               text="я" * 1500, images=[(1080, 1350)])
    pkg = pkg_of(tid)
    check("1500 знаков под картинкой — норма для Instagram",
          not pkg.problems, str(pkg.problems))

    tid = seed("2026-10-20-instagram-03", date=today,
               text="я" * 2500, images=[(1080, 1350)])
    check("2500 знаков — отказ",
          any("2200" in p for p in pkg_of(tid).problems),
          str(pkg_of(tid).problems))

    tid = seed("2026-10-20-instagram-04", date=today,
               text="тема " + "#тег " * 31, images=[(1080, 1350)])
    check("31 решётка — отказ",
          any("решёток" in p for p in pkg_of(tid).problems),
          str(pkg_of(tid).problems))

    tid = seed("2026-10-20-instagram-05", date=today, images=[(1200, 300)])
    check("панорама вне пропорции — отказ",
          any("1.91" in p for p in pkg_of(tid).problems),
          str(pkg_of(tid).problems))

    tid = seed("2026-10-20-instagram-06", date=today, images=[(1080, 1350)] * 11)
    check("одиннадцать карточек — отказ",
          any("карточек" in p for p in pkg_of(tid).problems),
          str(pkg_of(tid).problems))

    tid = seed("2026-10-20-instagram-07", date=today, text="Подпись.")
    check("без медиа — отказ",
          any("картинок нет" in p for p in pkg_of(tid).problems),
          str(pkg_of(tid).problems))

    # Ролик на двадцать четыре мегабайта: для фейсбучного пути это норма,
    # он уходит байтами и упирается только в потолок Meta. Тот же ролик на
    # инстаграмном пути берётся по ссылке и не проходит — проверка ниже,
    # в разделе 8б.
    tid = seed("2026-10-20-instagram-08", date=today, text="", fmt="reels",
               video_mb=24)
    check("ролик 24 МБ байтами — норма",
          not pkg_of(tid).problems, str(pkg_of(tid).problems))

    # ── 4. PNG становится JPEG ────────────────────────────────────────
    print("\n4. JPEG")
    b = desk.brand(CHAT)
    src = b.path("posts") / "2026-10-20-instagram-01-01.png"
    out = instapost.jpeg(src, b.path("posts") / ".ig")
    check("получился jpg", out.suffix == ".jpg" and out.exists(), str(out))
    check("это настоящий JPEG", out.read_bytes()[:3] == b"\xff\xd8\xff",
          str(out.read_bytes()[:4]))

    # ── 5. карусель: контейнеры, подпись, порядок ─────────────────────
    print("\n5. Карусель")
    tid = seed("2026-10-21-instagram-01", date=today, text="Подпись карусели.",
               images=[(1080, 1350), (1080, 1350), (1080, 1350)])
    wire, reg = Wire(), Reg()
    instapost._call = wire
    link = await publisher.send(reg, CHAT, pkg_of(tid))

    media = [c for c in wire.calls if c[1].endswith("/media")]
    check("три карточки — три контейнера", len(media) == 4, str(len(media)))
    check("карточка помечена как элемент карусели",
          all(c[2].get("is_carousel_item") == "true" for c in media[:3]),
          str([c[2].get("is_carousel_item") for c in media[:3]]))
    check("на карточке подписи нет",
          all(not c[2].get("caption") for c in media[:3]),
          str([c[2].get("caption") for c in media[:3]]))
    check("общий контейнер это CAROUSEL",
          media[3][2].get("media_type") == "CAROUSEL", str(media[3][2]))
    check("дети перечислены по порядку",
          media[3][2].get("children") == "c1,c2,c3",
          str(media[3][2].get("children")))
    check("подпись на общем контейнере",
          media[3][2].get("caption") == "Подпись карусели.",
          str(media[3][2].get("caption")))
    check("публикация последним запросом",
          wire.made()[-2].endswith("media_publish"), str(wire.made()))
    check("публикуется общий контейнер",
          wire.calls[-2][2].get("creation_id") == "c4",
          str(wire.calls[-2][2]))
    check("ссылка вернулась", "instagram.com" in link, link)

    row = db.one("SELECT state, external_id, link FROM posts "
                 "WHERE theme_id = ?", tid)
    check("запись в posts с префиксом ig",
          row["state"] == "pub" and row["external_id"] == "ig:media-777",
          str(dict(row)))
    check("тема в pub",
          db.one("SELECT status FROM themes WHERE id = ?", tid)["status"]
          == "pub")

    # Технические загрузки уехали в очередь и убраны оттуда.
    check("файлы грузились в очередь",
          [t for _, t in reg.files] == ["queue"] * 3, str(reg.files))
    check("технические загрузки убраны",
          len(reg._bot.deleted) == 3, str(reg._bot.deleted))

    # ── 6. второй раз не публикуется ──────────────────────────────────
    print("\n6. Дубль")
    wire2 = Wire()
    instapost._call = wire2
    try:
        await publisher.send(reg, CHAT, pkg_of(tid))
        check("второй раз отказ", False, "опубликовал повторно")
    except publisher.NotReady as e:
        check("второй раз отказ", "уже опубликовано" in str(e), str(e))
    check("сеть при дубле не трогалась", not wire2.calls, str(wire2.made()))

    # ── 7. одна картинка это пост, а не карусель из одной ─────────────
    print("\n7. Одна картинка")
    tid = seed("2026-10-22-instagram-01", date=today, text="Один макет.",
               images=[(1080, 1350)])
    wire, reg = Wire(), Reg()
    instapost._call = wire
    await publisher.send(reg, CHAT, pkg_of(tid))
    media = [c for c in wire.calls if c[1].endswith("/media")]
    check("один контейнер", len(media) == 1, str(len(media)))
    check("без пометки карусели", not media[0][2].get("is_carousel_item"),
          str(media[0][2]))
    check("подпись на нём", media[0][2].get("caption") == "Один макет.",
          str(media[0][2].get("caption")))

    # ── 8. ролик уходит байтами, а не ссылкой ─────────────────────────
    #
    # Это главное в цикле: ролик основной формат площадки, а смонтированные
    # ролики бренда весят от 14 до 275 МБ. Ссылку на медиа у завода даёт
    # Telegram с потолком 20 МБ — то есть путь через ссылку для ролика не
    # работает почти никогда, и файл должен уходить байтами.
    print("\n8. Reels байтами")
    object.__setattr__(cfg, "ig_login", "facebook")
    tid = seed("2026-10-23-instagram-01", date=today, text="Подпись ролика.",
               fmt="reels", video_mb=60)

    async def ok_video(pkg):
        return []
    real_find = instapost.video_findings
    instapost.video_findings = ok_video

    sent = []

    async def fake_upload(container, uri, video):
        sent.append((container, uri, video.stat().st_size))
    real_upload = instapost.upload
    instapost.upload = fake_upload

    wire, reg = Wire(), Reg()
    instapost._call = wire
    await publisher.send(reg, CHAT, pkg_of(tid))

    media = [c for c in wire.calls if c[1].endswith("/media")]
    check("контейнер REELS один",
          len(media) == 1 and media[0][2].get("media_type") == "REELS",
          str(media))
    check("контейнер просит загрузку байтами",
          media[0][2].get("upload_type") == "resumable", str(media[0][2]))
    check("ссылки на видео в контейнере нет",
          not media[0][2].get("video_url"), str(media[0][2]))
    check("подпись при ролике",
          media[0][2].get("caption") == "Подпись ролика.",
          str(media[0][2].get("caption")))
    check("файл ушёл байтами", len(sent) == 1, str(sent))
    check("адрес загрузки от Meta, а не свой",
          sent and sent[0][1].startswith(instapost.RUPLOAD), str(sent))
    check("ролик 60 МБ прошёл: в Telegram он бы не влез",
          sent and sent[0][2] > 20 * 1024 * 1024, str(sent))
    check("медиа не грузилось в чат", not reg.files, str(reg.files))

    # Чужой адрес загрузки не принимается: файл не должен уехать на хост,
    # которого мы не ждали, даже если его назвала сама Meta.
    instapost.upload = real_upload
    try:
        await instapost.upload("c1", "https://example.com/upload",
                               desk.brand(CHAT).path("posts")
                               / f"{tid}-reel.mp4")
        check("чужой адрес загрузки — отказ", False, "ушёл на чужой хост")
    except instapost.Refused as e:
        check("чужой адрес загрузки — отказ", "адрес" in str(e), str(e))

    # ── 8б. на инстаграмном пути ролик упирается в потолок ссылки ─────
    print("\n8б. Reels ссылкой")
    object.__setattr__(cfg, "ig_login", "instagram")
    check("хост сменился",
          instapost.host_api().endswith("graph.instagram.com"),
          instapost.host_api())
    check("60 МБ названы отказом до кнопки",
          any("20 МБ" in p for p in pkg_of(tid).problems),
          str(pkg_of(tid).problems))
    check("отказ называет путь",
          any("facebook" in p for p in pkg_of(tid).problems),
          str(pkg_of(tid).problems))

    tid2 = seed("2026-10-23-instagram-02", date=today, text="Короткий ролик.",
                fmt="reels", video_mb=2)
    instapost.upload = fake_upload
    wire, reg = Wire(), Reg()
    instapost._call = wire
    await publisher.send(reg, CHAT, pkg_of(tid2))
    media = [c for c in wire.calls if c[1].endswith("/media")]
    check("лёгкий ролик уходит ссылкой",
          "api.telegram.org/file/bot" in str(media[0][2].get("video_url")),
          str(media[0][2]))
    check("загрузки байтами на этом пути нет", len(sent) == 1, str(sent))

    object.__setattr__(cfg, "ig_login", "facebook")
    instapost.upload = real_upload
    instapost.video_findings = real_find

    # ── 8в. ролик встаёт на день, иначе автомат его не видит ──────────
    #
    # Тема по факту съёмки заводится пустым днём, а автомат отбирает темы
    # по `date = сегодня`. Без занятого дня «автопостинг Instagram»
    # остался бы кнопкой: по времени ролик не вышел бы никогда.
    print("\n8в. День ролика")
    from orchestrator import montage                           # noqa: E402

    tid = seed("2026-10-23-instagram-03", date=None, text="Ролик дубля.",
               fmt="reels", video_mb=2)
    said = []

    async def say(text):
        said.append(text)

    check("день пуст после монтажа",
          db.one("SELECT date FROM themes WHERE id = ?", tid)["date"] is None)
    check("автомат такую тему не видит",
          tid not in [t["id"] for t in publisher.due_now(CHAT)[0]],
          str(publisher.due_now(CHAT)))

    await montage._take_slot(Reg(), CHAT, tid, "reels", say)
    day = db.one("SELECT date FROM themes WHERE id = ?", tid)["date"]
    check("день занят", bool(day), str(day))
    check("день не в прошлом", day >= today, f"{day} < {today}")
    check("человеку сказано, что дальше",
          said and "встал на" in said[-1], str(said))
    check("тема осталась готовой",
          db.one("SELECT status FROM themes WHERE id = ?",
                 tid)["status"] == "ready")

    # Второй ролик в тот же день на ту же площадку не встаёт: это тот же
    # запрет, что у Стратега, и держит его тот же код.
    with db.tx() as c:
        c.execute("UPDATE themes SET date = ? WHERE id = ?", (today, tid))
    tid2 = seed("2026-10-23-instagram-04", date=None, text="Второй дубль.",
                fmt="reels", video_mb=1, keep=True)
    said.clear()
    await montage._take_slot(Reg(), CHAT, tid2, "reels", say)
    day2 = db.one("SELECT date FROM themes WHERE id = ?", tid2)["date"]
    check("второй ролик встал в другой день", day2 != today, str(day2))

    # ── 9. отказ Instagram доходит словами ────────────────────────────
    print("\n9. Отказ")
    tid = seed("2026-10-24-instagram-01", date=today, text="Текст.",
               images=[(1080, 1350)])
    wire, reg = Wire(status="ERROR"), Reg()
    instapost._call = wire
    try:
        await publisher.send(reg, CHAT, pkg_of(tid))
        check("отказ поднялся", False, "промолчал")
    except publisher.NotReady as e:
        check("отказ поднялся", "Instagram" in str(e), str(e))
    check("тема осталась не опубликованной",
          db.one("SELECT status FROM themes WHERE id = ?", tid)["status"]
          != "pub")
    check("попытка помечена провалом",
          db.one("SELECT state FROM posts WHERE theme_id = ?",
                 tid)["state"] == "failed")
    check("файл из чата убран даже при отказе",
          len(reg._bot.deleted) >= 1, str(reg._bot.deleted))

    # ── 10. сутки исчерпаны ───────────────────────────────────────────
    #
    # Счётчик спрашивается у Meta, а не считается своей базой: сутки
    # скользящие, и свой счёт разошёлся бы с чужим молча.
    print("\n10. Потолок суток")
    tid = seed("2026-10-25-instagram-01", date=today, text="Текст.",
               images=[(1080, 1350)])
    wire, reg = Wire(limit_used=100), Reg()
    instapost._call = wire
    try:
        await publisher.send(reg, CHAT, pkg_of(tid))
        check("исчерпанные сутки — отказ", False, "опубликовал")
    except publisher.NotReady as e:
        check("исчерпанные сутки — отказ", "сутки" in str(e), str(e))
    check("медиа не грузилось", not reg.files, str(reg.files))

    # ── 11. токен продлевается и ложится в базу ───────────────────────
    #
    # Продление это инстаграмный путь: фейсбучный живёт на токене
    # страницы, выведенном из долгого токена пользователя, и такой не
    # истекает — продлевать его нечем, грант `ig_refresh_token` там чужой.
    print("\n11. Токен")
    wire = Wire()
    instapost._call = wire
    with db.tx() as c:
        c.execute("UPDATE ig_token SET refreshed_at = "
                  "datetime('now', '-40 days')")
    check("на фейсбучном пути не продлеваем",
          await instapost.refresh() is None)
    check("и сеть не трогаем", not wire.calls, str(wire.made()))

    object.__setattr__(cfg, "ig_login", "instagram")
    with db.tx() as c:
        c.execute("UPDATE ig_token SET refreshed_at = datetime('now')")
    check("младше суток не продлеваем", await instapost.refresh() is None)
    check("сеть не трогалась", not wire.calls, str(wire.made()))

    with db.tx() as c:
        c.execute("UPDATE ig_token SET refreshed_at = "
                  "datetime('now', '-40 days')")
    got = await instapost.refresh()
    check("на инстаграмном продлился", got == "token-new", str(got))
    check("новый токен в базе", instapost.token() == "token-new",
          instapost.token())
    check("возраст сброшен", (instapost.token_age_days() or 9) < 1,
          str(instapost.token_age_days()))
    object.__setattr__(cfg, "ig_login", "facebook")


asyncio.run(main())
raise SystemExit(report())
