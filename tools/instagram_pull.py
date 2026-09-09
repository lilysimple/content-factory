"""Профили Instagram из списка источников — в кэш папки бренда.

    ./.venv/bin/python tools/instagram_pull.py --login      вход руками, один раз
    ./.venv/bin/python tools/instagram_pull.py              все профили из sources.md
    ./.venv/bin/python tools/instagram_pull.py --profile x  один профиль
    ./.venv/bin/python tools/instagram_pull.py --show       что лежит в кэше

Зачем отдельный сборщик, а не строка в `sources.fetch`. Telegram отдаёт
ленту бесплатно и полно, Instagram — нет: страница приезжает пустой
оболочкой, `api/v1` без залогиненной сессии отвечает пустотой или капчей,
а IP завода после десятка запросов уходит в бан. Поэтому сеть развязана с
чтением: сюда ходит человек своим браузером, завод читает то, что осталось
на диске (`orchestrator/instagram.py`).

Чем это оплачено: **сбор ручной**. Кэш не обновляется сам, стареет молча,
и возраст называется дырой в сводке, а не подразумевается свежим. Это
честнее, чем фоновый парсер, который живёт до первой смены вёрстки.

Chrome берётся свой, с отдельным профилем в `~/.content-factory`, а не
рабочий: рабочий держит свой каталог занятым, и второй Chrome на нём
просто не поднимется. Логинишься туда один раз, дальше сессия живёт в
профиле.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import cfg                                            # noqa: E402
from orchestrator import cdp, design, instagram, research         # noqa: E402
from storage.brand import Brand                                   # noqa: E402

PROFILE_DIR = Path.home() / ".content-factory" / "chrome-instagram"

# Ключ веб-клиента Instagram. Не секрет и не чужой доступ: его посылает
# сама страница в каждом своём запросе, и без него `api/v1` отвечает
# «Bad Request» даже залогиненному.
APP_ID = "936619743392459"

POSTS = 12          # столько отдаёт первая страница профиля; больше — пагинация
SETTLE = 2.0        # пауза после навигации, чтобы страница успела ожить


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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_chrome(port: int, *, url: str = "about:blank") -> subprocess.Popen:
    """Свой Chrome на своём профиле. Видимый: headless Instagram узнаёт."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [design.chrome(),
         f"--user-data-dir={PROFILE_DIR}",
         f"--remote-debugging-port={port}",
         "--no-first-run", "--no-default-browser-check",
         "--disable-features=Translate", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):                       # до тридцати секунд на старт
        try:
            cdp.targets(port, timeout=1.0)
            return proc
        except Exception:                     # noqa: BLE001  порт ещё не слушает
            if proc.poll() is not None:
                sys.exit("Chrome не поднялся — закрой окна этого профиля")
            time.sleep(0.5)
    proc.terminate()
    sys.exit("Chrome не открыл порт отладки за тридцать секунд")


FETCH_JS = """(async () => {
  try {
    const r = await fetch(
      '/api/v1/users/web_profile_info/?username=%s',
      {headers: {'x-ig-app-id': '%s'}, credentials: 'include'});
    if (!r.ok) return {error: 'ответ ' + r.status};
    return await r.json();
  } catch (e) { return {error: String(e)}; }
})()"""


class Browser:
    """Одна вкладка на весь прогон, с переподключением.

    Переход между сайтами меняет процесс вкладки, и Chrome при этом рвёт
    сокет отладки. Само по себе это не поломка: адрес вкладки остаётся
    тем же, надо просто подключиться заново. Без этого сборщик падал на
    первом же профиле — `ConnectionResetError` посреди навигации.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.sock = self._open()

    def _open(self) -> cdp.Socket:
        sock = cdp.Socket(cdp.page_socket(self.port))
        sock.call("Page.enable")
        return sock

    def reconnect(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = self._open()

    def call(self, method: str, params: dict | None = None) -> dict:
        try:
            return self.sock.call(method, params)
        except cdp.CDPError:
            self.reconnect()
            return self.sock.call(method, params)

    def evaluate(self, js: str):
        try:
            return self.sock.evaluate(js)
        except cdp.CDPError:
            self.reconnect()
            return self.sock.evaluate(js)

    def close(self) -> None:
        self.sock.close()


def pull(br: Browser, name: str) -> dict:
    """Профиль одним запросом изнутри страницы. Ошибка — словарь с `error`.

    Запрос идёт со страницы самого профиля, а не с пустой вкладки: у
    `api/v1` проверяется источник, и с `about:blank` он отвечает отказом.
    """
    br.call("Page.navigate", {"url": f"https://www.instagram.com/{name}/"})
    time.sleep(SETTLE)
    # Без входа Instagram через пару секунд уводит на стену логина, и
    # `api/v1` оттуда отвечает пустым профилем. Причина в этом, а не в
    # профиле — говорим её прямо, иначе человек пойдёт искать опечатку
    # в нике.
    here = br.evaluate("location.href") or ""
    if "/accounts/login" in str(here):
        return {"error": "Instagram увёл на вход — сессия разлогинилась, "
                         "запусти с --login"}
    raw = br.evaluate(FETCH_JS % (name, APP_ID))
    if not isinstance(raw, dict):
        return {"error": "страница вернула не JSON"}
    if err := raw.get("error"):
        # 401 и 403 это не поломка запроса, а отсутствие входа: с
        # разлогиненной сессией `api/v1` отвечает ими всегда. Называем
        # причину, а не код.
        if any(code in str(err) for code in ("401", "403")):
            return {"error": f"{err} — сессии нет, запусти с --login"}
        return {"error": str(err)}
    user = ((raw.get("data") or {}).get("user")) or {}
    if not user:
        return {"error": "профиля в ответе нет — закрыт, переименован "
                         "или сессия разлогинилась (--login)"}
    return user


def _caption(node: dict) -> str:
    edges = ((node.get("edge_media_to_caption") or {}).get("edges")) or []
    return (edges[0].get("node", {}).get("text", "") if edges else "").strip()


def _likes(node: dict) -> int | None:
    for key in ("edge_liked_by", "edge_media_preview_like"):
        count = (node.get(key) or {}).get("count")
        if isinstance(count, int):
            return count
    return None


def shape(user: dict, name: str) -> dict:
    """Ответ Instagram → форма кэша. Чего нет — прочерк, а не ноль."""
    media = ((user.get("edge_owner_to_timeline_media") or {}).get("edges")) or []
    posts = []
    for edge in media[:POSTS]:
        node = edge.get("node") or {}
        stamp = node.get("taken_at_timestamp")
        posts.append({
            "code": node.get("shortcode") or "",
            "text": _caption(node),
            "date": (datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                     if isinstance(stamp, (int, float)) else None),
            "likes": _likes(node),
            "comments": (node.get("edge_media_to_comment") or {}).get("count"),
            "views": node.get("video_view_count"),
            "video": bool(node.get("is_video")),
        })
    return {
        "profile": name,
        "title": user.get("full_name") or f"@{name}",
        "bio": user.get("biography") or "",
        "followers": (user.get("edge_followed_by") or {}).get("count"),
        "pulled": datetime.now().astimezone().isoformat(timespec="seconds"),
        "posts": posts,
    }


def wanted(b: Brand, arg: str | None) -> list[str]:
    """Кого снимаем: названный профиль или все из `research/sources.md`."""
    if arg:
        name = instagram.handle(arg)
        if not name:
            sys.exit(f"это не похоже на профиль Instagram: {arg}")
        return [name]
    names = [instagram.handle(u) for u in research.watchlist(b)
             if instagram.is_profile(u)]
    if not names:
        sys.exit("в `research/sources.md` нет ни одной ссылки на профиль "
                 "Instagram — добавь строкой `- instagram.com/ник — зачем "
                 "смотрим`")
    return names


def show(b: Brand) -> None:
    folder = b.path(instagram.CACHE)
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if not files:
        print(f"кэш пуст: {folder}")
        return
    for f in files:
        src, gap = instagram.read(b, f.stem)
        print(f"- {src.summary()}" + (f"\n  ⚠ {gap}" if gap else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brand", help="slug папки бренда")
    ap.add_argument("--profile", help="один профиль вместо списка источников")
    ap.add_argument("--login", action="store_true",
                    help="открыть Chrome и войти руками, один раз")
    ap.add_argument("--show", action="store_true", help="что лежит в кэше")
    args = ap.parse_args()

    path = brand_dir(args.brand)
    b = Brand(path.name, path)

    if args.show:
        show(b)
        return

    if args.login:
        print(f"Открываю Chrome на профиле {PROFILE_DIR}.\n"
              "Войди в Instagram руками, потом закрой окно — сессия "
              "останется в этом профиле.")
        proc = start_chrome(free_port(), url="https://www.instagram.com/")
        proc.wait()
        print("Готово. Дальше гоняй без --login.")
        return

    names = wanted(b, args.profile)
    port = free_port()
    proc = start_chrome(port)
    saved, failed = [], []
    br = Browser(port)
    try:
        for name in names:
            print(f"@{name} … ", end="", flush=True)
            try:
                user = pull(br, name)
            except cdp.CDPError as e:
                print(f"не вышло: {e}")
                failed.append(f"@{name}: {e}")
                continue
            if err := user.get("error"):
                print(f"не вышло: {err}")
                failed.append(f"@{name}: {err}")
                continue
            data = shape(user, name)
            instagram.stash(b, data)
            dated = [p["date"] for p in data["posts"] if p["date"]]
            print(f"{len(data['posts'])} постов"
                  + (f", свежий {max(dated)[:10]}" if dated else ""))
            saved.append(name)
    finally:
        br.close()
        proc.terminate()

    print(f"\nВ кэш легло профилей: {len(saved)}. Папка: "
          f"{b.path(instagram.CACHE)}")
    if failed:
        print("Не собралось:")
        for line in failed:
            print(f"- {line}")
        print("Сессия могла разлогиниться — тогда `--login` и ещё раз.")
    sys.exit(1 if failed and not saved else 0)


if __name__ == "__main__":
    main()
