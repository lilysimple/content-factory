"""Чужие рилсы по нише — в кэш папки бренда.

    ./.venv/bin/python tools/trends_pull.py                 все цели из trends.md
    ./.venv/bin/python tools/trends_pull.py --target "#ai"  одна цель
    ./.venv/bin/python tools/trends_pull.py --show          что лежит в кэше
    ./.venv/bin/python tools/trends_pull.py --dry           что бы запросил

Зачем платный сборщик, а не свой Chrome, как у профилей. `instagram_pull`
снимает профиль, который человек уже назвал: двенадцать последних постов,
один запрос на аккаунт, и этого хватает для сводки недели. Тренд ниши так
не снять — нужен поиск по хэштегу и десятки роликов на цель, а на таком
числе запросов аккаунт уходит в 429 и дальше в бан. Поэтому за ними ходит
Apify, по ключу и за деньги.

Чем это оплачено: **прогон стоит денег и зовётся руками**. Фонового опроса
здесь нет и не будет — ни по часам завода, ни в момент прогона роли.
Тренд живёт неделями, а счёт растёт с каждого запроса, и человек должен
видеть, за что платит. Нет ключа — Trend Watcher работает на том кэше,
что уже лежит, и называет его возраст дырой.

Форму кэша держит не этот файл, а `orchestrator.trends.stash`: сборщик
ходит в чужой API и меняется чаще всех, читатель у кэша один.

**Схема актора сверена 14.10** с опубликованным билдом
`apify/instagram-scraper` (`shu8hvrXbJbY3Eb9W`), по публичному
`api/v2/acts/.../builds/default`: оттуда взяты и имена полей входа, и
имена полей датасета — `videoPlayCount`, `videoViewCount`, `likesCount`,
`commentsCount`, `ownerUsername`, `videoDuration`, `productType`,
`shortCode`, `timestamp`, `type`, `followersCount`.

**Живым прогоном выдача проверена 23.10**, и опасение подтвердилось:
схема обещает поле, но не то, что оно приедет заполненным. По сетке
профиля `videoPlayCount`, `videoViewCount` и `videoDuration` приезжают
пустыми у **всех** записей — 453 рилса из 453 по четырнадцати аккаунтам.
Тот же актор по прямому адресу ролика отдаёт их заполненными: проверено
на 27 ссылках, приехали все 27. Отсюда вторая ступень (`deepen`), она же
единственный способ получить показы и длину.

Поэтому `convert` берёт число из первого поля, которое отдали, и ничего
не додумывает. Последние ключи в каждой цепочке (`playCount`,
`takenAtTimestamp`, `ownerFollowersCount`) в схеме отсутствуют и оставлены
догадкой на случай смены имени: не приехало ничего — в кэше прочерк,
партия посчитается по лайкам и скажет это вслух. Молча нулём не станет
ничего.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import cfg                                            # noqa: E402
from orchestrator import trends                                   # noqa: E402
from storage.brand import Brand                                   # noqa: E402

API = "https://api.apify.com/v2"
LIMIT = 50            # роликов на цель: хватает на медиану с запасом

# За какой срок брать ролики. Без границы в партию по хэштегу приезжает
# всё, что под ним лежало годами, и медиана источника считается по
# позапрошлому лету: ролик этой недели оказывается выбросом просто
# потому, что сравнили его с мёртвым хвостом. Актор умеет резать это сам
# (`onlyPostsNewerThan`), и дешевле отрезать на его стороне, чем платить
# за выдачу, которую выбросим.
#
# Девяносто, а не тридцать: живой прогон 14.10 показал, что за месяц у
# соседа выходит пять-семь рилсов, а партия меньше восьми не считается
# вовсе. Три месяца его собственной отдачи — честная база для медианы, и
# выброс всё равно считается относительно неё, а не абсолютным числом.
DAYS = 90
POLL = 5.0            # пауза между опросами статуса прогона
WAIT = 600            # потолок ожидания одной цели, секунды

# Скольких кандидатов добирать вторым запросом, поштучно по ссылке.
#
# Зачем второй запрос вообще. Замер 23.10: прогон по профилю отдаёт
# `videoPlayCount` и `videoViewCount` **пустыми у всех записей** — 453
# рилса из 453 по четырнадцати аккаунтам, ни одно поле цепочки не
# приехало заполненным. Значит показов у партии нет вовсе, медиана всегда
# считается по лайкам, а длительность (`videoDuration`) приезжает пустой
# тем же порядком.
#
# Тот же актор по **прямому адресу ролика** отдаёт и показы, и
# длительность: проверено на 27 ссылках, приехали все 27, показы сошлись
# с числами на сетке до округления (5 731 626 против «5,7 млн»). Отсюда
# две ступени: первая берёт сетку и шорткоды, вторая добирает кандидатов.
#
# Двенадцать, а не вся партия: второй запрос платный и считается по
# записям, а показы нужны не для медианы (в неё они не идут, см.
# `trends._value`), а чтобы роль прочитала отклик у тех роликов, которые
# она и так разбирает. Выбросов в выдачу уходит меньше десятка, так что
# двенадцать закрывают верхушку с запасом.
DEEP = 12

# Прогон кончился, но не удачей. `ABORTED` сюда входит намеренно: его
# ставит человек из кабинета Apify, и для сборщика это такой же конец.
DEAD = {"FAILED", "TIMED-OUT", "TIMING-OUT", "ABORTED", "ABORTING"}


class NoTrends(RuntimeError):
    """Снять не удалось: нет ключа, нет сети, прогон не задался."""


def brand_dir(arg: str | None) -> Path:
    root = cfg.brands_path
    if arg:
        path = root / arg
        if not path.is_dir():
            sys.exit(f"нет папки бренда {path}")
        return path
    found = sorted(p for p in root.iterdir() if (p / "core.md").is_file())
    if len(found) != 1:
        sys.exit("брендов несколько или ни одного — назови нужный: --brand")
    return found[0]


def _call(method: str, path: str, body: dict | None = None,
          timeout: int = 60) -> dict | list:
    url = f"{API}/{path}{'&' if '?' in path else '?'}token={cfg.apify_token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        hint = {401: "ключ не принят", 402: "на счету Apify нет средств",
                404: "актора нет или он переименован",
                429: "лимит запросов Apify исчерпан"}.get(e.code, e.reason)
        raise NoTrends(f"Apify ответил {e.code}: {hint}") from e
    except urllib.error.URLError as e:
        raise NoTrends(f"Apify недоступен: {e.reason}") from e


def actor_of(t: trends.Target) -> str:
    """Какой актор снимает цель такого рода.

    Разводится здесь, а не в настройке: это свойство цели, а не вкус.
    Сетку профиля отдаёт `instagram-scraper`, а по тегу тот же актор
    отдаёт фото и карусели без единого видео (замер 14.10). Рилсы по тегу
    и по ключевому слову отдаёт `instagram-hashtag-scraper` —
    `resultsType: reels`, профили ему не нужны вовсе.
    """
    return cfg.apify_actor_tag if t.kind == "tag" else cfg.apify_actor


def actor_input(t: trends.Target, limit: int, days: int = DAYS) -> dict:
    """Вход актора под одну цель. Имена полей из схемы билда, не на слух.

    Форма входа у двух акторов разная, и это главное, что здесь есть.

    **Тег и ключевое слово** идут в хэштег-скрапер полем `hashtags`, с
    `resultsType: reels`. Это единственный известный путь к залетевшим
    рилсам **без привязки к профилям**.

    **Профиль** идёт в `instagram-scraper` прямым адресом сетки и
    `resultsType: posts`; рилсы отбираются после, по типу записи.
    Поле `search` того актора не используется вовсе: живой прогон 14.10
    показал, что поиск тега идёт у него **через Google**
    (`searchSource: google`) и на `#claudeai` вернул одну запись — огрызок
    страницы чужого тега (`wifisensing`, `postsCount: 0`), ни одного
    поста. Страница тега прямым адресом тоже не годится: 21 запись, все
    фото и карусели, ни одного видео.

    `resultsType: posts`, а не `reels`, хотя `reels` в схеме есть. Рилсы
    отбираются после, по типу записи (`is_reel`): для `reels` схема
    требует адрес вида `/reel/`, то есть конкретный ролик, а нам нужна
    выдача по тегу. Проверять это предположение платным прогоном сейчас
    нечем — ключа нет.
    """
    body: dict = {"resultsLimit": limit}
    if t.kind == "tag":
        # На входе хэштег-скрапера всего четыре поля, и окна по дате среди
        # них нет вовсе. Лишнее поле — повод для отказа, а не для игнора,
        # поэтому его тут и не появляется: окно партии всё равно режет
        # читатель (`trends._window`), и для тега это единственный рубеж.
        return {**body, "hashtags": [t.value], "resultsType": "reels"}

    # Поля `instagram-scraper`.
    body |= {"resultsType": "posts", "addParentData": False}
    if days > 0:
        # Явной датой, а не относительной строкой («30 days»): схема
        # допускает обе, но разбор относительной живёт на стороне актора,
        # и его правка сдвинула бы окно партии молча. Закреплённые ролики
        # этот фильтр всё равно не держит — отсюда рубеж у читателя.
        since = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        body["onlyPostsNewerThan"] = since.isoformat()
    return {**body,
            "directUrls": [f"https://www.instagram.com/{t.value}/"]}


def _run(actor: str, payload: dict, hint: str) -> list[dict]:
    """Запустить прогон, дождаться, забрать записи.

    Асинхронно, с опросом статуса, а не ручкой `run-sync-get-dataset-items`:
    у синхронной свой потолок в пять минут, и партия по хэштегу в него
    упирается — ответ приходит пустой, а деньги за прогон уже списаны.

    `hint` дописывается к сообщению о таймауте: у сетки и у поштучного
    добора разные способы ужаться, и общий совет «снизь --limit» для
    второго неверен.
    """
    run = _call("POST", f"acts/{actor}/runs", payload)
    data = (run or {}).get("data") or {}
    run_id, dataset = data.get("id"), data.get("defaultDatasetId")
    if not run_id or not dataset:
        raise NoTrends(f"Apify не вернул прогон {actor}: {str(run)[:200]}")

    waited = 0.0
    while True:
        status = str(((_call("GET", f"actor-runs/{run_id}") or {})
                      .get("data") or {}).get("status") or "")
        if status == "SUCCEEDED":
            break
        if status in DEAD:
            raise NoTrends(f"прогон {run_id} кончился со статусом {status}")
        if waited >= WAIT:
            raise NoTrends(
                f"прогон {run_id} не уложился в {WAIT} секунд (статус "
                f"{status}) — {hint}")
        time.sleep(POLL)
        waited += POLL

    items = _call("GET", f"datasets/{dataset}/items?clean=true&format=json")
    return [x for x in (items or []) if isinstance(x, dict)]


def run_actor(t: trends.Target, limit: int, days: int = DAYS) -> list[dict]:
    """Первая ступень: сетка профиля или выдача по тегу."""
    return _run(actor_of(t), actor_input(t, limit, days),
                "снизь --limit или забери позже из кабинета")


def run_direct(codes: list[str]) -> list[dict]:
    """Вторая ступень: те же ролики, но поштучно по прямому адресу.

    Актор тот же (`instagram-scraper`), отличается только вход: вместо
    адреса сетки идут адреса роликов. По ним он отдаёт `videoPlayCount` и
    `videoDuration` заполненными, чего прогон по сетке не делает вовсе.

    `resultsLimit: 1` — на один адрес приходится одна запись, и больше
    просить не за что.
    """
    if not codes:
        return []
    return _run(cfg.apify_actor, {
        "directUrls": [f"https://www.instagram.com/reel/{c}/" for c in codes],
        "resultsType": "posts",
        "resultsLimit": 1,
        "addParentData": False,
    }, f"снизь --deep (сейчас {len(codes)}) или забери позже из кабинета")


def _first(row: dict, *keys):
    """Первое поле, которое актор отдал. Ни одного — прочерк, не ноль."""
    for k in keys:
        if (v := row.get(k)) is not None:
            return v
    return None


def is_reel(row: dict) -> bool:
    """Ролик, а не карточка. Карусель с видео внутри рилсом не считается."""
    return (str(row.get("productType") or "").lower() == "clips"
            or str(row.get("type") or "").lower() == "video")


def convert(row: dict) -> dict:
    """Запись актора → строка кэша. Числа не выдумываются."""
    code = str(_first(row, "shortCode", "shortcode", "code") or "")
    return {
        "code": code,
        "url": str(_first(row, "url") or
                   (f"https://www.instagram.com/reel/{code}/" if code else "")),
        "owner": str(_first(row, "ownerUsername", "username") or ""),
        "caption": str(_first(row, "caption", "text") or ""),
        "date": _first(row, "timestamp", "takenAtTimestamp"),
        "views": _first(row, "videoPlayCount", "videoViewCount", "playCount"),
        "likes": _first(row, "likesCount", "likes"),
        "comments": _first(row, "commentsCount", "comments"),
        "seconds": _first(row, "videoDuration", "duration"),
        "followers": _first(row, "ownerFollowersCount", "followersCount"),
        # Заполняется не здесь, а вторым запросом (`deepen`). Ключ стоит
        # в форме всегда, чтобы кэш не менял схему от прогона к прогону:
        # читателю прочерк и отсутствие ключа одинаковы, а человеку,
        # который откроет файл, — нет.
        "plays": None,
    }


def deepen(reels: list[dict], count: int = DEEP) -> int:
    """Добрать показы и длительность у верхушки партии, поштучно.

    Кандидаты берутся по лайкам, потому что показов на этой ступени ещё
    нет ни у кого (см. `DEEP`), а чем-то отбирать надо. Это не отбор
    выбросов: выбросы считает читатель и по своей медиане. Здесь задача
    проще — накрыть ту верхушку, которую читатель потом и разберёт.

    Пишет только в `plays` и `seconds`, и только там, где актор что-то
    отдал. В `views` не пишет ничего: добранные показы есть лишь у части
    партии, и попади они в поле, по которому считается медиана, медианой
    стала бы медиана верхушки. Эту границу держит `trends._value`, но
    нарушить её легче всего именно отсюда.

    Возвращает, скольким роликам что-то дописали.
    """
    if count <= 0 or not reels:
        return 0
    ranked = sorted(reels, key=lambda r: r.get("likes") or 0, reverse=True)
    pick = [r for r in ranked[:count] if r.get("code")]
    if not pick:
        return 0

    by_code = {r["code"]: r for r in pick}
    rows = run_direct(list(by_code))
    touched = 0
    for row in rows:
        code = str(_first(row, "shortCode", "shortcode", "code") or "")
        target = by_code.get(code)
        if target is None:
            continue
        plays = _first(row, "videoPlayCount", "videoViewCount", "playCount")
        secs = _first(row, "videoDuration", "duration")
        if plays is not None:
            target["plays"] = plays
        if secs is not None and target.get("seconds") is None:
            target["seconds"] = secs
        if plays is not None or secs is not None:
            touched += 1
    return touched


def pull(b: Brand, t: trends.Target, limit: int, days: int = DAYS,
         deep: int = DEEP) -> int:
    rows = run_actor(t, limit, days)
    reels = [convert(r) for r in rows if is_reel(r)]

    # Вторая ступень. Идёт до записи в кэш, чтобы на диск лёг один
    # законченный файл: партия, дописанная вторым прогоном после того,
    # как читатель уже прочёл первую, разошлась бы с ней молча.
    #
    # Своей ошибкой добор партию не роняет. Сетка уже снята и оплачена,
    # и терять её из-за того, что второй запрос не задался, глупо:
    # показов в кэше просто не будет, а это прочерк, с которым читатель
    # умеет работать.
    deepened = 0
    if deep > 0 and reels:
        try:
            deepened = deepen(reels, deep)
        except NoTrends as e:
            print(f"  {t.human}: показы не добрались — {e}")

    # Окно уезжает в кэш, потому что режет по нему читатель: актор свой
    # же фильтр не держит — закреплённые ролики приходят мимо него.
    since = ((datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
             if days > 0 else None)
    trends.stash(b, {
        "target": t.human,
        "source": f"apify:{actor_of(t)}",
        "pulled": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since,
        "items": reels,
    })
    skipped = len(rows) - len(reels)
    print(f"  {t.human}: {len(reels)} роликов"
          + (f", {skipped} записей не рилсы — не взяты" if skipped else "")
          + (f", показы добраны у {deepened}" if deepened else ""))

    # Пустая партия у профиля это чаще всего не «ничего не вышло», а
    # закрытая дверь: возрастное ограничение отдаёт страницу без постов и
    # тем же кодом 200. Замер 23.10: `@sunlower` так не открылся ни
    # анонимным браузером, ни актором — вернулась одна пустая запись.
    # Читатель назовёт это «роликов меньше пяти», что уведёт человека
    # чинить не то.
    if not reels and t.kind == "user":
        print(f"  {t.human}: пусто. Проверь профиль руками: закрытый или "
              "с возрастным ограничением отдаёт страницу без постов, и "
              "это не чинится ни ключом, ни лимитом.")
    return len(reels)


def show(b: Brand) -> None:
    found, gaps = trends.targets(b)
    for gap in gaps:
        print(f"  дыра: {gap}")
    for t in found:
        batch, gap = trends.read(b, t)
        med = f"{batch.median:,.0f}".replace(",", " ") if batch.ok else "—"
        print(f"  {t.human}: роликов {len(batch.items)}, медиана {med}"
              f" ({batch.metric or '—'}), выбросов {len(batch.hot)}"
              + (f" · {gap}" if gap else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand")
    ap.add_argument("--target", help="одна цель: #хэштег или @профиль")
    ap.add_argument("--limit", type=int, default=LIMIT)
    ap.add_argument("--days", type=int, default=DAYS,
                    help=f"за сколько дней брать ролики, по умолчанию {DAYS}; "
                         "0 — без границы")
    ap.add_argument("--deep", type=int, default=DEEP,
                    help=f"скольким кандидатам добрать показы и длину "
                         f"вторым запросом, по умолчанию {DEEP}; 0 — не "
                         "добирать (дешевле, но показов в кэше не будет)")
    ap.add_argument("--show", action="store_true", help="что лежит в кэше")
    ap.add_argument("--dry", action="store_true", help="что бы запросил")
    args = ap.parse_args()

    path = brand_dir(args.brand)
    b = Brand(path.name, path)

    if args.show:
        show(b)
        return

    if args.target:
        t = trends.target(args.target)
        if t is None:
            sys.exit(f"цель не разобралась: {args.target!r} — "
                     "пиши `#хэштег` или `@профиль`")
        found, gaps = [t], []
    else:
        found, gaps = trends.targets(b)
    for gap in gaps:
        print(f"дыра: {gap}")
    if not found:
        sys.exit("снимать нечего")

    if args.dry:
        for t in found:
            print(f"{t.human} → {actor_of(t)}: "
                  + json.dumps(actor_input(t, args.limit, args.days),
                               ensure_ascii=False))
        return

    if not cfg.apify_token:
        sys.exit("нет APIFY_TOKEN — заведи ключ в `.env`, иначе снимать нечем")

    print(f"Целей {len(found)}, до {args.limit} записей на каждую, "
          + (f"за последние {args.days} дней" if args.days > 0 else "без границы по дате")
          + (f", плюс добор показов у {args.deep} на цель" if args.deep > 0
             else ", без добора показов")
          + ". Прогон платный.")
    total = 0
    for t in found:
        try:
            total += pull(b, t, args.limit, args.days, args.deep)
        except NoTrends as e:
            print(f"  {t.human}: не снялось — {e}")
    print(f"Снято роликов: {total}")


if __name__ == "__main__":
    main()
