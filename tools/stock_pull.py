"""Сток в фотобанк бренда: найти по запросу, ужать, записать источник.

    ./.venv/bin/python tools/stock_pull.py "рабочий стол ноутбук" -n 3
    ./.venv/bin/python tools/stock_pull.py "текстура бумаги" --landscape
    ./.venv/bin/python tools/stock_pull.py "кофе" --dry-run

Источник — Pexels: лицензия разрешает коммерческое использование и
правку, атрибуция не обязательна. Ключ бесплатный, берётся на
pexels.com/api и кладётся в `.env` как `PEXELS_API_KEY`.

**Сток не для обложки поста.** Фон обложки в личном бренде — сам
человек, а безликая картинка со стока читается ровно как AI-контент,
против которого продукт и построен. Сток нужен там, где автора в кадре
быть не должно: предметка и абстракция в карусели, фоны раздач.
Поэтому файлы приезжают с префиксом `stock-`, и `design._pick_photo`
берёт их **только** если человек вписал имя в `design/photos.md`: в
слепую ротацию сток не попадает.

Кто автор каждого файла — в `design/assets/stock-credits.md`. Лицензия
атрибуции не требует, но знать, откуда взялась картинка в папке
клиента, надо: без этого через полгода не отличить сток от съёмки.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import cfg                                            # noqa: E402
from tools.photos_pull import LONG_SIDE, QUALITY, brand_dir, convert, slug  # noqa: E402

API = "https://api.pexels.com/v1/search"
UA = "content-factory/1.0 (photo pull for brand assets)"

# Слова, после которых со стока приезжает чужой человек в кадре. В
# личном бренде это худший сток из возможных: постановку узнают быстрее,
# чем прочитают текст, и доверие к автору падает разом. Отказом это не
# делается — клиенту может быть нужен именно человек, — но сказать надо.
PEOPLE = ("woman", "man", "person", "people", "girl", "boy", "team",
          "portrait", "female", "male", "model", "businessman")
PREFIX = "stock-"
CREDITS = "stock-credits.md"
CREDITS_HEAD = """# Сток: откуда какой файл

Заполняет `tools/stock_pull.py`. Лицензия Pexels атрибуции не требует,
таблица нужна для другого: отличить сток от своей съёмки через полгода.

| Файл | Автор | Источник | Запрос | Когда |
|---|---|---|---|---|
"""


def search(key: str, query: str, n: int, orientation: str) -> list[dict]:
    url = f"{API}?" + urllib.parse.urlencode({
        "query": query, "per_page": n, "orientation": orientation})
    # User-Agent обязателен: без него перед API стоит Cloudflare и
    # отвечает 403, хотя ключ верный. Ошибка выглядит как «ключ не
    # приняли», и искать её начинаешь не там.
    req = urllib.request.Request(url, headers={
        "Authorization": key, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("photos", [])
    except urllib.error.HTTPError as e:
        detail = {401: "ключ не принят",
                  403: "ключ не принят или не активирован",
                  429: "лимит запросов исчерпан"}.get(e.code, str(e.reason))
        sys.exit(f"Pexels ответил {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Pexels недоступен: {e.reason}")


def fetch(url: str, dst: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r, dst.open("wb") as f:
        f.write(r.read())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query", help="что искать, словами")
    ap.add_argument("-n", type=int, default=3, help="сколько взять (до 20)")
    ap.add_argument("--brand", help="slug папки бренда")
    ap.add_argument("--landscape", action="store_true",
                    help="горизонталь под превью YouTube; по умолчанию вертикаль")
    ap.add_argument("--dry-run", action="store_true", help="только показать")
    args = ap.parse_args()

    key = cfg.pexels_key
    if not key:
        sys.exit("нет PEXELS_API_KEY в .env — ключ берётся на pexels.com/api")

    brand = brand_dir(args.brand)
    images = brand / "design" / "assets" / "images"
    images.mkdir(parents=True, exist_ok=True)

    # Теги на Pexels английские, а русский запрос он переводит сам и
    # мимо: на «минимализм стол ноутбук» приезжает инжир на тарелке.
    # Сказать об этом дешевле, чем разбирать выдачу глазами.
    words = set(re.findall(r"[a-z]+", args.query.lower()))
    if words & set(PEOPLE):
        print("В запросе человек. Со стока приедет чужое лицо в узнаваемой "
              "постановке — в личном бренде это видно сразу. Фон под текст "
              "ищется текстурой и предметкой.\n")

    if re.search(r"[а-яё]", args.query, re.I):
        print("Запрос кириллицей: Pexels ищет по английским тегам и "
              "переводит грубо. Английский запрос точнее.\n")

    found = search(key, args.query, min(args.n, 20),
                   "landscape" if args.landscape else "portrait")
    if not found:
        sys.exit(f"по запросу «{args.query}» на Pexels ничего нет")

    base = f"{PREFIX}{slug(args.query) or 'photo'}"
    taken = {f.name for f in images.iterdir() if f.is_file()}
    today = dt.date.today().isoformat()
    rows, added = [], []

    with tempfile.TemporaryDirectory(prefix="stock-pull-") as tmp:
        for i, ph in enumerate(found, 1):
            name, n = f"{base}-{i:02d}.jpg", i
            while name in taken:
                n += 1
                name = f"{base}-{n:02d}.jpg"
            author = str(ph.get("photographer") or "—")
            if args.dry_run:
                print(f"  + {name}  ← {author}, {ph.get('url')}")
                taken.add(name)
                continue

            raw = Path(tmp) / f"raw-{i}.jpg"
            fetch(ph["src"]["original"], raw)
            convert(raw, Path(tmp) / name)
            (images / name).write_bytes((Path(tmp) / name).read_bytes())
            taken.add(name)
            added.append(name)
            rows.append(f"| `{name}` | {author} | {ph.get('url')} | "
                        f"{args.query} | {today} |\n")

    if rows:
        credits = brand / "design" / "assets" / CREDITS
        if not credits.is_file():
            credits.write_text(CREDITS_HEAD, encoding="utf-8")
        with credits.open("a", encoding="utf-8") as f:
            f.writelines(rows)

    print(f"\nВзято с Pexels: {len(added)}")
    for name in added:
        print(f"  {name}")
    if added:
        print(f"\nСток в слепую ротацию не идёт. Чтобы Дизайнер его "
              f"поставил, впиши имя файла в {brand / 'design' / 'photos.md'} "
              "под нужную цель.")


if __name__ == "__main__":
    main()
