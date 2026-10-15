"""Срез рилсов из ответа ChatPlace на список медиа.

Ответ `automations_triggers_list_instagram_media` — триста постов одной
строкой, 340 КБ: в контекст он не лезет и приезжает файлом. Здесь он
режется до того, что нужно для привязки ключа: свежие рилсы с их `id`
(это и есть media UUID для `mediaIds`), ссылкой и первой строкой подписи.

    python3 tools/chatplace_media.py <файл-ответа> [--limit 10] [--match текст] [--all]

`id` — UUID для ChatPlace, `mediaId` — числовой id Instagram, в триггеры
не годится. Пост от рилса отличается ссылкой: `/reel/` против `/p/`.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone


def load(path: str) -> list[dict]:
    raw = json.loads(open(path, encoding="utf-8").read())
    return raw if isinstance(raw, list) else raw.get("data", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="файл с ответом MCP")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--match", default="", help="подстрока подписи, регистр не важен")
    ap.add_argument("--all", action="store_true", help="не только рилсы")
    a = ap.parse_args()

    items = load(a.path)
    if not a.all:
        items = [x for x in items if "/reel/" in (x.get("permalink") or "")]
    if a.match:
        needle = a.match.lower()
        items = [x for x in items if needle in (x.get("caption") or "").lower()]
    items.sort(key=lambda x: x.get("timestamp") or 0, reverse=True)

    if not items:
        print("ничего не нашлось: проверь --match или сними список заново")
        return 1

    for x in items[: a.limit]:
        when = datetime.fromtimestamp(x["timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M")
        head = " ".join((x.get("caption") or "").split())[:70] or "— без подписи"
        print(f"{x['id']}  {when}  {x['permalink']}\n    {head}")
    print(f"\nвсего подходит: {len(items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
