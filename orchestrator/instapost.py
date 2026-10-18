"""Транспорт Instagram: контейнер, ожидание, публикация.

Вторая площадка Публикатора. Модель здесь не зовётся, как и в Telegram:
собрать комплект, прогнать потолки и отправить — работа детерминированная.

Отправка идёт в три хода, и это не наша выдумка, а устройство API:

1. `POST /{ig}/media` заводит контейнер;
2. `GET /{container}?fields=status_code` ждёт `FINISHED`;
3. `POST /{ig}/media_publish` публикует контейнер.

## Два пути входа, и выбирает их ролик

У Meta два набора прав на одну и ту же публикацию, и различает их не
удобство, а **ролик**.

`facebook` — Instagram API with Facebook Login, хост
`graph.facebook.com`, права `instagram_basic`, `instagram_content_publish`,
`pages_read_engagement`, `pages_show_list`. Требует, чтобы аккаунт
Instagram был связан со страницей Facebook. Даёт **загрузку байтами**:
`upload_type=resumable` отдаёт адрес на `rupload.facebook.com`, туда
уходит сам файл, и публичный хост не нужен вовсе. Потолок ролика — 300 МБ.

`instagram` — Instagram API with Instagram Login, хост
`graph.instagram.com`, права `instagram_business_basic` и
`instagram_business_content_publish`. Страница Facebook не нужна, и этим
путь хорош для аккаунта, у которого её нет. Но ролик на нём берётся
**только по публичной ссылке**: `upload_type=resumable` Meta отдаёт лишь
фейсбучному входу.

Дом по умолчанию — `facebook`, и причина в замере: смонтированные ролики
бренда весят от 14 до 275 МБ, а публичного хоста у завода нет. Ссылку на
этом пути может дать Telegram, но `getFile` отдаёт файлы **до 20 МБ** —
то есть основной формат площадки через него не проходит почти никогда.
Поменять путь значит поменять `IG_LOGIN`, остальной код развилку не
разбирает.

Ревью приложения ни на одном из путей для своего аккаунта не нужно: Meta
отдаёт Standard Access тем, кто публикует в аккаунты своего же владельца.
Ревью начнётся на втором клиенте коробки.

## Картинка всё равно уходит ссылкой

Загрузка байтами в API только для видео. Карусель и пост берутся по
`image_url` на обоих путях, и ссылку даёт Telegram: файл уезжает
документом в 📤 Очередь, `getFile` отдаёт адрес, адрес уходит Meta,
сообщение убирается после. Для макетов это работает — PNG Дизайнера весят
сотни килобайт, — но цена названа вслух: **в адресе едет токен бота**, он
уходит на серверы Meta и ложится в их логи. Осознанный размен на
отсутствие своего хоста; заменить хостинг значит заменить `host()`.

Документом, а не картинкой: `send_photo` пережимает PNG и меняет пиксели,
а в профиль должен уйти тот самый макет, который человек утвердил.

Картинка переводится в JPEG перед отправкой: других форматов Instagram не
принимает вовсе, а Дизайнер рендерит PNG. Конвертирует системный `sips` —
Pillow в зависимостях завода нет, и тащить его ради одного вызова дороже.

Дубль ловится там же, где у Telegram: `posts.external_id UNIQUE` плюс
статус `sending` до отправки. Идентификатор несёт префикс `ig:` — одна и
та же тема на двух площадках это две записи, а не одна.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx

from config import cfg
from storage import db

log = logging.getLogger("instapost")

# Хост на каждый путь входа. Выбирает `IG_LOGIN`, дом по умолчанию
# `facebook`: на нём ролик уходит байтами, а ролик здесь основной формат.
HOSTS = {"facebook": "https://graph.facebook.com",
         "instagram": "https://graph.instagram.com"}

# Куда уходит сам файл видео на фейсбучном пути. Адрес Meta отдаёт сама,
# в ответе на создание контейнера; константа нужна только затем, чтобы
# узнать его в логе и не ходить на чужой хост по чужой строке.
RUPLOAD = "https://rupload.facebook.com"


def login() -> str:
    """Какой путь входа настроен."""
    want = (cfg.ig_login or "facebook").strip().lower()
    return want if want in HOSTS else "facebook"


def host_api() -> str:
    return HOSTS[login()]


# Версию можно не называть: оба хоста принимают путь без неё. Вшитая
# версия устаревает молча — запрос начинает отвечать отказом через год
# после того, как её вписали, и причину ищут где угодно, кроме константы.
# Адрес загрузки видео версию несёт, но его отдаёт сама Meta.
VERSION = (cfg.ig_api_version or "").strip("/")


# ── потолки площадки ──────────────────────────────────────────────────
#
# Все до кнопки. Отказ Meta в момент отправки значит показанное человеку
# превью, которое врало, — ту же ошибку завод уже проходил на 50 МБ
# Telegram и больше не повторяет.

CAPTION = 2200               # знаков в подписи
HASHTAGS = 30                # решёток в подписи
MENTIONS = 20                # упоминаний через @
CAROUSEL = (2, 10)           # карточек в карусели: меньше двух не карусель
IMG_MB = 8                   # потолок картинки
VIDEO_MB = 300               # потолок ролика на стороне Meta
REEL_SEC = (3.0, 15 * 60)    # длина ролика
RATIO = (4 / 5, 1.91)        # пропорция картинки: от 4:5 до 1.91:1
POSTS_24H = 100              # публикаций через API за сутки

# Потолок скачивания у Bot API. Он наш, а не Instagram: ссылку на медиа
# даёт Telegram, и всё, что тяжелее, он просто не отдаст.
HOST_MB = 20

# Сколько ждём обработки контейнера. Meta советует опрашивать раз в
# минуту и не дольше пяти — это про картинку. Ролик на двести мегабайт
# она перекодирует дольше, и отказ по своему таймауту на уже загруженном
# файле означал бы «не опубликовалось» про пост, который вот-вот выйдет.
POLL_SEC = 5
POLL_IMAGE = 300
POLL_VIDEO = 900

HASH_RX = re.compile(r"#\w", re.UNICODE)
AT_RX = re.compile(r"(?<![\w.])@\w", re.UNICODE)


class NoAccount(RuntimeError):
    """Аккаунт не настроен: публиковать некуда."""


class Refused(RuntimeError):
    """Instagram отказал. Текст отказа идёт человеку как есть."""


# ── доступ ────────────────────────────────────────────────────────────

def token() -> str:
    """Действующий долгий токен.

    Дом токену — база: продление отдаёт новое значение, и записать его в
    `.env` код не может. Окружение это первый завод, не хранилище.
    """
    row = db.one("SELECT token FROM ig_token WHERE id = 1")
    if row and (row["token"] or "").strip():
        return row["token"].strip()
    env = (cfg.ig_token or "").strip()
    if env:
        # Перенос из окружения в базу делается при первом чтении, и
        # сорваться он имеет право: `ready()` спрашивают и оттуда, где
        # база открыта только на чтение. Токен при этом известен, и
        # отвечать «аккаунта нет» из-за неудавшейся записи нельзя.
        try:
            save_token(env)
        except Exception as e:                               # noqa: BLE001
            log.warning("токен из окружения не лёг в базу: %s", e)
    return env


def save_token(value: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO ig_token (id, token) VALUES (1, ?) "
                  "ON CONFLICT(id) DO UPDATE SET token = excluded.token, "
                  "refreshed_at = datetime('now')", (value.strip(),))


def token_age_days() -> float | None:
    """Сколько суток токену. `None` — токена нет вовсе."""
    row = db.one("SELECT julianday('now') - julianday(refreshed_at) AS d "
                 "FROM ig_token WHERE id = 1")
    return float(row["d"]) if row and row["d"] is not None else None


def ready() -> bool:
    """Есть ли куда и чем публиковать.

    Автомат спрашивает это, а не надеется: площадка без аккаунта в
    `AUTO_PUBLISH` означала бы слот, который каждый день получает строку
    «не готово» — неправду, комплект как раз готов.
    """
    return bool((cfg.ig_user_id or "").strip() and token())


def _url(path: str) -> str:
    base = host_api()
    return f"{base}/{VERSION}/{path}" if VERSION else f"{base}/{path}"


async def _call(method: str, path: str, **params: Any) -> dict[str, Any]:
    """Один запрос к Graph API. Отказ поднимается текстом Meta.

    Текст не переписывается своими словами намеренно: «Media type not
    supported» человеку бесполезнее, чем нам, но выдуманная замена вроде
    «что-то пошло не так» бесполезна обоим.
    """
    params = {k: v for k, v in params.items() if v is not None}
    params["access_token"] = token()
    async with httpx.AsyncClient(timeout=120) as http:
        r = await (http.post(_url(path), data=params) if method == "POST"
                   else http.get(_url(path), params=params))
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or "error" in body:
        err = body.get("error") or {}
        msg = err.get("error_user_msg") or err.get("message") or r.text[:300]
        raise Refused(str(msg))
    return body


async def refresh() -> str | None:
    """Продлить долгий токен ещё на шестьдесят дней.

    Зовётся часами завода раз в сутки. Meta продлевает токен старше
    суток и не продлевает истёкший — то есть пропущенное продление
    чинится только руками, заново через браузер. Поэтому продлеваем
    задолго до срока, а не в последний день.
    """
    if not token():
        return None
    if login() == "facebook":
        # Фейсбучный путь живёт на токене страницы, выведенном из долгого
        # токена пользователя: такой не истекает, и продлевать его нечем —
        # `ig_refresh_token` это грант инстаграмного пути. Отвалится он
        # только от смены пароля или снятия прав, и чинится это руками.
        return None
    age = token_age_days()
    if age is not None and age < 1:
        return None                      # младше суток Meta не продлит
    body = await _call("GET", "refresh_access_token",
                       grant_type="ig_refresh_token")
    new = str(body.get("access_token") or "").strip()
    if new:
        save_token(new)
        log.info("токен Instagram продлён, ещё %s с", body.get("expires_in"))
    return new or None


async def left_today() -> int | None:
    """Сколько публикаций осталось в сутках. `None` — Meta не ответила.

    Счётчик спрашивается у Meta, а не считается по своей базе: сутки тут
    скользящие, и свой счёт разошёлся бы с чужим молча.
    """
    try:
        body = await _call("GET", f"{cfg.ig_user_id}/content_publishing_limit",
                           fields="quota_usage,config")
    except Refused as e:
        log.warning("лимит публикаций не прочитан: %s", e)
        return None
    rows = body.get("data") or [{}]
    used = rows[0].get("quota_usage")
    cap = (rows[0].get("config") or {}).get("quota_total") or POSTS_24H
    return None if used is None else max(0, int(cap) - int(used))


# ── медиа ─────────────────────────────────────────────────────────────

def _pixels(path: Path) -> tuple[int, int] | None:
    """Размер картинки в пикселях. `None` — `sips` не прочитал."""
    try:
        out = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    got = dict(re.findall(r"(pixelWidth|pixelHeight):\s*(\d+)", out))
    if "pixelWidth" not in got or "pixelHeight" not in got:
        return None
    return int(got["pixelWidth"]), int(got["pixelHeight"])


def jpeg(src: Path, folder: Path) -> Path:
    """PNG в JPEG рядом, под своим именем. Уже JPEG — отдаём как есть.

    Instagram принимает только JPEG, и расширенные формы вроде MPO тоже
    не берёт. `sips` системный: Pillow в зависимостях завода нет, а
    тянуть его ради одной конвертации дороже, чем вызвать то, что есть.
    """
    if src.suffix.lower() in (".jpg", ".jpeg"):
        return src
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{src.stem}.jpg"
    r = subprocess.run(["sips", "-s", "format", "jpeg", "-s",
                        "formatOptions", "92", str(src), "--out", str(out)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not out.exists():
        raise Refused(f"{src.name} не перевёлся в JPEG: "
                      f"{(r.stderr or '').strip()[:200]}")
    return out


async def host(reg, chat_id: int, path: Path) -> tuple[str, Any]:
    """Публичная ссылка на файл и сообщение, которым он загружен.

    Instagram приходит за медиа сам, поэтому файл сначала уезжает в
    Telegram документом — документом, а не картинкой: `send_photo`
    пережимает PNG и меняет пиксели, а в Instagram должен уйти тот самый
    макет, который человек утвердил.
    """
    blob = path.read_bytes()
    mb = len(blob) / 1024 / 1024
    if mb > HOST_MB:
        raise Refused(f"{path.name} весит {mb:.0f} МБ, а ссылку на медиа "
                      f"даёт Telegram, и он отдаёт файлы до {HOST_MB} МБ")
    msg = await reg.send_file("publisher", chat_id, blob, path.name,
                              caption="техническая загрузка для Instagram",
                              topic="queue")
    doc = getattr(msg, "document", None) or (getattr(msg, "video", None))
    if doc is None:
        raise Refused(f"{path.name} загрузился, но Telegram не назвал файл")
    bot = reg.bot("publisher")
    info = await bot.get_file(doc.file_id)
    return (f"https://api.telegram.org/file/bot{bot.token}/{info.file_path}",
            msg)


async def unhost(reg, chat_id: int, messages: list[Any]) -> None:
    """Убрать технические загрузки из чата.

    Зовётся после того, как Instagram медиа забрал. Ссылка на файл живёт
    у Telegram дольше сообщения, поэтому удаление контейнеру не вредит, а
    топик очереди остаётся читаемым: человек приходит туда за решениями,
    а не за служебными файлами.
    """
    bot = reg.bot("publisher")
    for msg in messages:
        try:
            await bot.delete_message(chat_id, msg.message_id)
        except Exception as e:                               # noqa: BLE001
            log.warning("техническая загрузка осталась в чате: %s", e)


async def upload(container: str, uri: str, video: Path) -> None:
    """Отдать файл ролика Meta байтами. Фейсбучный путь.

    Публичный хост не нужен: Meta отдаёт адрес на `rupload.facebook.com` в
    ответе на создание контейнера, и файл уходит туда телом запроса. Для
    завода это главный способ публикации ролика — смонтированные ролики
    весят десятки и сотни мегабайт, и ни один из них не прошёл бы через
    файловый адрес Telegram с его двадцатью.

    Адрес берём **из ответа**, а не собираем сами: в нём уже стоит версия
    API и идентификатор контейнера, и собранный вручную разошёлся бы с
    настоящим на первой же смене версии. Подстраховка одна — префикс
    проверяется, чтобы файл не уехал на хост, которого мы не ждали.

    Файл читается потоком, а не в память: 275 МБ, прочитанные целиком,
    на восьми гигабайтах памяти соседствуют с Chrome и рендером.
    """
    if not uri.startswith(RUPLOAD):
        raise Refused(f"Meta отдала незнакомый адрес загрузки: {uri[:60]}")
    size = video.stat().st_size
    headers = {"Authorization": f"OAuth {token()}",
               "offset": "0",
               "file_size": str(size)}
    log.info("ролик %s, %.0f МБ, уходит в контейнер %s",
             video.name, size / 1024 / 1024, container)
    # Потолок времени щедрый намеренно: на медленном канале 200 МБ едут
    # минутами, а оборванная загрузка это не только время, но и
    # контейнер, который придётся заводить заново.
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=1800,
                                                       write=1800)) as http:
        with video.open("rb") as f:
            r = await http.post(uri, headers=headers, content=f)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or not body.get("success"):
        err = (body.get("debug_info") or {}).get("message") \
            or (body.get("error") or {}).get("message") or r.text[:300]
        raise Refused(f"ролик не загрузился: {err}")


# ── потолки до кнопки ─────────────────────────────────────────────────

def findings(pkg) -> list[str]:
    """Чем комплект не годится для Instagram. Пусто — годится.

    Проверки лежат здесь, а не в `publisher.collect`, по той же причине,
    по какой транспорт вынесен в отдельный файл: потолки у площадок
    разные, и общий список превратился бы в набор `if plat ==`.

    Функция синхронная намеренно: её зовёт `collect`, а `collect` зовут и
    стенд, и очередь, и панель. Всё, что требует чтения самого файла
    видео, живёт отдельно в `video_findings` — иначе синхронность пришлось
    бы ломать во всех вызовах ради двух проверок одной площадки.
    """
    out: list[str] = []
    if not ready():
        out.append("аккаунт Instagram не настроен: нет IG_USER_ID или токена")

    text = pkg.text or ""
    if len(text) > CAPTION:
        out.append(f"подпись {len(text)} знаков, потолок Instagram {CAPTION}")
    if len(HASH_RX.findall(text)) > HASHTAGS:
        out.append(f"решёток {len(HASH_RX.findall(text))}, потолок {HASHTAGS}")
    if len(AT_RX.findall(text)) > MENTIONS:
        out.append(f"упоминаний {len(AT_RX.findall(text))}, "
                   f"потолок {MENTIONS}")

    if pkg.video:
        mb = pkg.video.stat().st_size / 1024 / 1024
        # Потолок у ролика свой на каждом пути входа, и разница в
        # пятнадцать раз. На фейсбучном файл уходит байтами, и мерка это
        # потолок Meta; на инстаграмном ролик берётся по ссылке, ссылку
        # даёт Telegram, и мерка его — двадцать мегабайт.
        if login() == "facebook":
            if mb > VIDEO_MB:
                out.append(f"ролик {mb:.0f} МБ, потолок Instagram "
                           f"{VIDEO_MB} МБ")
        elif mb > HOST_MB:
            out.append(f"ролик {mb:.0f} МБ, а на пути `instagram` он "
                       f"берётся по ссылке, и ссылку даёт Telegram с "
                       f"потолком {HOST_MB} МБ — ролику нужен путь "
                       f"`facebook`, там файл уходит байтами")
        return out

    if not pkg.images:
        out.append("картинок нет, а Instagram без медиа не публикует")
        return out
    if len(pkg.images) > CAROUSEL[1]:
        out.append(f"карточек {len(pkg.images)}, в карусель входит "
                   f"{CAROUSEL[1]}")
    for img in pkg.images:
        mb = img.stat().st_size / 1024 / 1024
        if mb > IMG_MB:
            out.append(f"{img.name} весит {mb:.1f} МБ, потолок {IMG_MB} МБ")
        size = _pixels(img)
        if size is None:
            out.append(f"{img.name} не прочитался, пропорцию не проверить")
            continue
        ratio = size[0] / size[1] if size[1] else 0
        if not RATIO[0] <= ratio <= RATIO[1]:
            out.append(f"{img.name} это {size[0]}×{size[1]}, Instagram берёт "
                       f"от 4:5 до 1.91:1")
    return out


async def video_findings(pkg) -> list[str]:
    """То же про ролик, но прочитав сам файл: длина и ширина кадра.

    Отдельно от `findings` из-за асинхронности `footage.probe`. Зовётся в
    двух местах — перед показом кнопки и перед самой отправкой: кнопка
    может появиться раньше, чем файл перечитан, а публиковать без
    проверки нельзя.
    """
    if not pkg.video:
        return []
    out: list[str] = []
    try:
        from orchestrator import footage
        pr = await footage.probe(pkg.video)
    except Exception as e:                                   # noqa: BLE001
        return [f"ролик не прочитался, длину и кадр не проверить: "
                f"{str(e)[:120]}"]
    if not REEL_SEC[0] <= pr.duration <= REEL_SEC[1]:
        out.append(f"ролик {pr.duration:.0f} с, Instagram берёт от "
                   f"{REEL_SEC[0]:.0f} с до {REEL_SEC[1] / 60:.0f} мин")
    if pr.width > 1920:
        out.append(f"кадр {pr.width} px по ширине, потолок Instagram 1920")
    return out


# ── публикация ────────────────────────────────────────────────────────

async def _wait(container: str, limit: int = POLL_IMAGE) -> None:
    """Дождаться, пока Instagram обработает контейнер."""
    waited = 0
    while waited < limit:
        body = await _call("GET", container, fields="status_code,status")
        code = body.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise Refused(f"Instagram не принял медиа: "
                          f"{body.get('status') or code}")
        await asyncio.sleep(POLL_SEC)
        waited += POLL_SEC
    raise Refused(f"Instagram не обработал медиа за {limit // 60} мин")


async def publish(reg, chat_id: int, pkg) -> tuple[str, str]:
    """Опубликовать комплект. Возвращает пару «id медиа, ссылка».

    Запись в `posts` делает `publisher.send` — до отправки, статусом
    `sending`. Здесь только сеть: транспорт не знает про базу, иначе
    второй площадки пришлось бы учить и её.
    """
    if not ready():
        raise NoAccount("аккаунт Instagram не настроен")

    ig = (cfg.ig_user_id or "").strip()
    left = await left_today()
    if left == 0:
        raise Refused("сутки исчерпаны: Instagram берёт через API "
                      f"{POSTS_24H} публикаций за скользящие сутки")

    if not pkg.video and not pkg.images:
        raise Refused("медиа нет, а Instagram без медиа не публикует")

    bad = findings(pkg) + await video_findings(pkg)
    if bad:
        raise Refused("; ".join(bad))

    tmp = Path(pkg.video or pkg.images[0]).parent / ".ig"
    uploads: list[Any] = []
    try:
        if pkg.video and login() == "facebook":
            # Байтами. Контейнер заводится пустым, файл уходит следом, и
            # никакого публичного адреса в цепочке нет вовсе.
            body = await _call("POST", f"{ig}/media", media_type="REELS",
                               upload_type="resumable",
                               caption=pkg.text or None)
            container = str(body["id"])
            uri = str(body.get("uri") or "")
            if not uri:
                raise Refused("Meta не отдала адрес загрузки ролика: путь "
                              "`facebook` требует связанной страницы и прав "
                              "instagram_content_publish")
            await upload(container, uri, pkg.video)
            await _wait(container, POLL_VIDEO)
        elif pkg.video:
            url, msg = await host(reg, chat_id, pkg.video)
            uploads.append(msg)
            body = await _call("POST", f"{ig}/media", media_type="REELS",
                               video_url=url, caption=pkg.text or None)
            container = str(body["id"])
            await _wait(container, POLL_VIDEO)
        else:
            # Одна картинка это пост, а не карусель из одной: контейнер
            # CAROUSEL с единственным ребёнком Instagram не принимает.
            children: list[str] = []
            for img in pkg.images[:CAROUSEL[1]]:
                url, msg = await host(reg, chat_id, jpeg(img, tmp))
                uploads.append(msg)
                single = len(pkg.images) == 1
                body = await _call(
                    "POST", f"{ig}/media", image_url=url,
                    caption=pkg.text or None if single else None,
                    is_carousel_item=None if single else "true")
                children.append(str(body["id"]))
            for child in children:
                await _wait(child)
            if len(children) == 1:
                container = children[0]
            else:
                body = await _call("POST", f"{ig}/media",
                                   media_type="CAROUSEL",
                                   children=",".join(children),
                                   caption=pkg.text or None)
                container = str(body["id"])
                await _wait(container)

        done = await _call("POST", f"{ig}/media_publish", creation_id=container)
        media_id = str(done["id"])
    finally:
        await unhost(reg, chat_id, uploads)

    link = await permalink(media_id)
    log.info("опубликовано в Instagram %s → %s", pkg.theme.get("id"), link)
    return media_id, link


async def permalink(media_id: str) -> str:
    """Ссылка на опубликованное. Отказ — не беда: публикация уже есть.

    Ссылку спрашиваем отдельным запросом, потому что `media_publish`
    отдаёт только id. Упасть здесь значит сказать человеку «не
    опубликовалось» про пост, который уже лежит в профиле.
    """
    try:
        body = await _call("GET", media_id, fields="permalink")
        return str(body.get("permalink") or "")
    except Refused as e:
        log.warning("ссылка на %s не прочитана: %s", media_id, e)
        nick = (cfg.instagram_profile or "").lstrip("@")
        return f"https://www.instagram.com/{nick}/" if nick else ""
