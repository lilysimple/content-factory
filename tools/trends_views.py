"""Просмотры чужих рилсов из сетки профиля — в уже снятый кэш ниши.

    ./.venv/bin/python tools/trends_views.py              все цели-профили
    ./.venv/bin/python tools/trends_views.py --target @x  одна цель
    ./.venv/bin/python tools/trends_views.py --show       что уже покрыто
    ./.venv/bin/python tools/trends_views.py --dry        куда бы сходил

Зачем отдельный сборщик, а не ещё одна ступень в `trends_pull.py`.

Замер 23.10 показал дыру, из-за которой весь контур выбросов ехал не на
том числе: **прогон Apify по сетке профиля отдаёт `videoPlayCount` и
`videoViewCount` пустыми у всех записей** — 453 рилса из 453 по
четырнадцати аккаунтам. Покрытие ноль, поэтому `trends._metric` всегда
выбирает лайки, а ветка «мерить просмотрами» не выбирается никогда.

Закрыть дыру платно можно только поштучно, по прямому адресу ролика
(`trends_pull.deepen`), и это даёт показы у верхушки, а не у партии. Для
медианы такие показы не годятся принципиально: медиана по верхушке — это
медиана выбросов, после неё выбросов не остаётся вовсе. Отсюда граница в
`trends._value`, и отсюда же отдельное поле `plays`.

А сетка Reels отдаёт просмотры **у всех роликов сразу и бесплатно**: они
лежат прямо в разметке, рядом со ссылкой на ролик. Снимаются одним
выражением, без API, без ключа и без оплаты. Это единственный известный
способ получить покрытие, при котором `VIEWS_ENOUGH` наконец достижим и
медиана едет на просмотрах, как и задумано.

Поэтому ходит сюда тот же человек и тот же Chrome, что за профилями
(`tools/instagram_pull.py`): свой профиль в `~/.content-factory`, вход
один раз, дальше сессия живёт. Завод в сеть по-прежнему не ходит.

Чем это оплачено: **числа округлены ровно так, как их печатает
Instagram.** «5,6 млн» это 5 600 000, а на самом деле 5 661 655.

Ошибка замерена сверкой с точными показами того же прогона Apify:
0,02 % на «970 тыс.» (970 166), 0,55 % на «5,7 млн» (5 731 626), худшее —
3,93 % на «1,2 млн» (1 249 077). Чем короче мантисса, тем грубее: «1,2»
несёт два знака, «970» — три.

Для медианы и кратности это безразлично: и то и другое считается
отношениями, а не разностями, и четыре процента не двигают ни порог
`HOT`, ни порядок роликов. Там, где нужно точное число, его добирает
платный `--deep`, и оно ложится в `plays` отдельно. Мелкие значения не
округляются вовсе: «4 559» приезжает как есть.

Разбор подписей проверен на 216 живых счётчиках со страниц десяти
профилей: разобрались все 216.

Порядок работы: сначала `trends_pull.py` (снимает партию), потом этот
сборщик (дописывает просмотры). Наоборот бессмысленно — дописывать
нечего, кэша ещё нет.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import trends                                   # noqa: E402
from storage.brand import Brand                                   # noqa: E402
from tools.instagram_pull import (Browser, brand_dir, free_port,  # noqa: E402
                                  start_chrome)

# Сколько раз прокрутить сетку. Каждая прокрутка подгружает следующий
# ряд; двенадцать закрывают около сорока роликов, то есть больше, чем
# приезжает в партию (`trends_pull.LIMIT` упирается в потолок источника
# задолго до этого).
SCROLLS = 12
SETTLE = 3.0          # пауза после навигации: сетка грузится не мгновенно
STEP = 1.2            # пауза между прокрутками

# Выражение, которым снимается пара «шорткод → подпись с просмотрами».
#
# Опора на `a[href*="/reel/"]`, а не на классы: классы у Instagram
# генерируются сборкой и меняются каждый релиз, а адрес ролика — часть
# продукта. Это самая долгоживущая зацепка, какая на странице есть.
#
# `innerText`, а не разбор вложенных узлов: в ссылке лежит и счётчик, и
# иногда значок закрепления, и читать это структурно значит привязаться
# к вёрстке. Текст разбирает Python, где ошибку видно.
GRAB_JS = """(() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href*="/reel/"]')) {
    const m = (a.getAttribute('href') || '').match(/\\/reel\\/([^/?#]+)/);
    if (m) out.push([m[1], (a.innerText || '').replace(/\\n/g, '|')]);
  }
  return JSON.stringify(out);
})()"""

# «5,6 млн», «86,3 тыс.», «79 тыс.», «4 559», «276».
NUM = re.compile(r"(\d+(?:[.,]\d+)?)\s*(млн|тыс)?", re.I)


def parse_count(text: str) -> int | None:
    """Подпись счётчика → число. Не разобралось — прочерк, а не ноль.

    Прочерк здесь важнее аккуратности: ноль вместо непонятой подписи
    утащил бы медиану источника вниз и сделал выбросом всё подряд.
    """
    if not text:
        return None
    # Пробелы у Instagram бывают неразрывными и узкими, и обычный
    # `strip()` их не берёт.
    clean = re.sub(r"[\s   ]+", " ", text).strip()
    # Тысячи разделены пробелом: «4 559», «1 249 077». Без склейки
    # выражение останавливается на пробеле и возвращает первую группу —
    # «4» вместо 4559, то есть занижает счётчик в тысячу раз и делает
    # ролик мусором в медиане. Склеиваются только цифры с цифрами, так
    # что «79 тыс.» не задевается.
    clean = re.sub(r"(?<=\d) +(?=\d)", "", clean)
    for chunk in clean.split("|"):
        m = NUM.search(chunk)
        if not m:
            continue
        value = float(m.group(1).replace(",", "."))
        suffix = (m.group(2) or "").lower()
        if suffix == "млн":
            return int(value * 1_000_000)
        if suffix == "тыс":
            return int(value * 1_000)
        # Без суффикса дробной части у счётчика не бывает: «4 559» это
        # тысячи, разделённые пробелом, а не «4,559».
        if "," in m.group(1) or "." in m.group(1):
            continue
        return int(value)
    return None


def grab(br: Browser, user: str) -> dict[str, int]:
    """Сетка Reels одного профиля → «шорткод: просмотры»."""
    br.call("Page.navigate",
            {"url": f"https://www.instagram.com/{user}/reels/"})
    time.sleep(SETTLE)

    here = str(br.evaluate("location.href") or "")
    if "/accounts/login" in here:
        raise RuntimeError("Instagram увёл на вход — сессия разлогинилась, "
                           "запусти `instagram_pull.py --login`")

    # Возрастное ограничение отдаёт страницу тем же кодом, но без сетки.
    # Замер 23.10: `@sunlower` так не открылся ни анонимно, ни актором.
    # Без этой проверки цель вернёт ноль роликов и будет выглядеть как
    # «просто не нашлось», уведя человека чинить не то.
    body = str(br.evaluate("document.body.innerText.slice(0, 400)") or "")
    if "Ограниченный профиль" in body or "Restricted profile" in body:
        raise RuntimeError("профиль с возрастным ограничением — сетка не "
                           "отдаётся даже залогиненным, цель бесполезна")

    for _ in range(SCROLLS):
        br.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        time.sleep(STEP)

    raw = br.evaluate(GRAB_JS)
    try:
        pairs = json.loads(str(raw or "[]"))
    except ValueError:
        raise RuntimeError("сетка вернула не JSON — вёрстка сменилась, "
                           "проверь `GRAB_JS`") from None

    found: dict[str, int] = {}
    for code, text in pairs:
        if (n := parse_count(text)) is not None:
            found[str(code)] = n
    return found


def apply(b: Brand, t: trends.Target, found: dict[str, int]) -> tuple[int, int]:
    """Дописать просмотры в кэш цели. Возвращает «дописано, всего».

    Пишет в `views`, а не в `plays`, и это ровно та разница, ради которой
    сборщик написан: сетка покрывает партию целиком, значит по этим
    числам можно считать медиану. Поштучный платный добор покрывает
    верхушку, и его место — `plays`.

    Партию не пересобирает: читает файл, меняет одно поле, кладёт назад
    через `trends.stash`. Форму кэша держит стэш, а не этот сборщик.
    """
    path = trends.cache_path(b, t.name)
    if not path.exists():
        raise RuntimeError("кэша нет — сначала сними партию "
                           f"`tools/trends_pull.py --target {t.human}`")
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") or []
    hit = 0
    for it in items:
        if (n := found.get(str(it.get("code") or ""))) is not None:
            it["views"] = n
            hit += 1
    data["items"] = items
    trends.stash(b, data)
    return hit, len(items)


def user_targets(b: Brand, arg: str | None) -> list[trends.Target]:
    """Цели, у которых есть сетка. Хэштег сюда не годится.

    У выдачи по тегу страницы профиля нет вовсе, счётчики там не лежат, и
    снимать нечего. Это не ошибка цели, а свойство источника, поэтому
    такая цель просто называется и пропускается.
    """
    if arg:
        t = trends.target(arg)
        if t is None:
            sys.exit(f"цель не разобралась: {arg!r} — пиши `@профиль`")
        if t.kind != "user":
            sys.exit(f"{t.human}: это тег, у него нет сетки с просмотрами")
        return [t]
    found, gaps = trends.targets(b)
    for gap in gaps:
        print(f"дыра: {gap}")
    users = [t for t in found if t.kind == "user"]
    for t in found:
        if t.kind != "user":
            print(f"  {t.human}: тег, сетки нет — пропускаю")
    return users


def show(b: Brand) -> None:
    """Покрытие просмотрами по каждой цели: от него зависит метрика."""
    found, _ = trends.targets(b)
    for t in found:
        batch, gap = trends.read(b, t)
        if not batch.items:
            print(f"  {t.human}: партии нет" + (f" · {gap}" if gap else ""))
            continue
        seen = sum(1 for r in batch.items if r.views is not None)
        share = seen / len(batch.items)
        enough = "хватает на просмотры" if share >= trends.VIEWS_ENOUGH \
            else "мало, метрика будет по лайкам"
        deep = sum(1 for r in batch.items if r.plays is not None)
        print(f"  {t.human}: просмотры у {seen} из {len(batch.items)} "
              f"({share * 100:.0f} %, {enough}), показов добрано {deep}, "
              f"метрика сейчас «{batch.metric or '—'}»")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand")
    ap.add_argument("--target", help="одна цель: @профиль")
    ap.add_argument("--show", action="store_true",
                    help="какое покрытие просмотрами уже есть")
    ap.add_argument("--dry", action="store_true", help="куда бы сходил")
    args = ap.parse_args()

    path = brand_dir(args.brand)
    b = Brand(path.name, path)

    if args.show:
        show(b)
        return

    targets = user_targets(b, args.target)
    if not targets:
        sys.exit("целей с сеткой нет")

    if args.dry:
        for t in targets:
            print(f"{t.human} → https://www.instagram.com/{t.value}/reels/, "
                  f"прокруток {SCROLLS}")
        return

    print(f"Целей {len(targets)}. Прогон бесплатный, но ручной: сетку "
          "открывает свой Chrome, вход должен быть живым.")
    port = free_port()
    proc = start_chrome(port, url="https://www.instagram.com/")
    br = Browser(port)
    total = 0
    try:
        for t in targets:
            try:
                found = grab(br, t.value)
                hit, all_items = apply(b, t, found)
                total += hit
                print(f"  {t.human}: в сетке {len(found)} счётчиков, "
                      f"дописано {hit} из {all_items} роликов партии")
            except RuntimeError as e:
                print(f"  {t.human}: не снялось — {e}")
    finally:
        br.close()
        proc.terminate()
    print(f"Дописано просмотров: {total}")
    print("Проверь метрику: `tools/trends_views.py --show`")


if __name__ == "__main__":
    main()
