"""Цикл 24: профили Instagram — кэш, окно недели, лайки вместо просмотров.

Три границы, ради которых цепочка разведена на сборщик и читателя.

Кэш стареет молча: файл недельной давности выглядит как снятый утром, и
разницу видно только по дате сбора. Возраст проверяет код и называет
дырой — иначе сводка выдаст прошлую неделю за эту.

Лайки не просмотры. Сетка отдаёт лайки у всего, просмотры только у видео,
и подписать одно другим значит увести число в план как охват.

В сеть за профилями завод не ходит вовсе: `sources.fetch` отвечает
отказом со ссылкой на сборщик, а `snapshot` не пускает профили в
`fetch_all` — иначе каждая строка вернулась бы дырой «не открылось», в
которой виновата якобы сеть.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta

import harness
from harness import check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import instagram, research, sources             # noqa: E402
from storage.brand import Brand                                   # noqa: E402


def brand() -> Brand:
    path = cfg.brands_path / "lily-space"
    return Brand("lily-space", path)


def post(day: date, likes: int, text: str, views=None, comments=7) -> dict:
    return {"code": f"C{likes}", "text": text,
            "date": datetime(day.year, day.month, day.day, 12).isoformat(),
            "likes": likes, "comments": comments, "views": views,
            "video": views is not None}


async def main() -> None:
    b = brand()
    week = research.Window(date(2026, 8, 24), date(2026, 8, 30), "2026-W35")

    # ── адрес профиля ────────────────────────────────────────────────
    check("ник из ссылки с хвостом",
          instagram.handle("https://www.instagram.com/lily_space/reels/")
          == "lily_space")
    check("голое @имя это Telegram, а не профиль",
          not instagram.is_profile("@lilyspace"))
    check("ссылка на профиль узнаётся",
          instagram.is_profile("instagram.com/lilyspace"))
    check("канал Telegram профилем не считается",
          not instagram.is_profile("https://t.me/s/addmeto"))

    # ── кэша нет — это дыра, а не отказ ──────────────────────────────
    src, gap = instagram.read(b, "nobody")
    check("без кэша профиль не открылся", not src.ok, str(src.ok))
    check("дыра называет сборщик", "instagram_pull" in gap, gap)

    # ── запись и чтение ──────────────────────────────────────────────
    instagram.stash(b, {
        "profile": "lilyspace", "title": "Lily Space", "followers": 4200,
        # Дата сбора здесь относительная, а не вшитая: `read` меряет
        # возраст от сегодня, и вшитый день молча переезжает за порог
        # восьми суток — тест начинает падать по календарю, а не по коду.
        "pulled": (datetime.now() - timedelta(days=2)).isoformat(),
        "posts": [
            post(date(2026, 8, 25), 310, "Карусель про голос бренда"),
            post(date(2026, 8, 27), 120, "Ролик про монтаж", views=9100),
            post(date(2026, 8, 29), 205, "Пост про профиль клиента"),
            post(date(2026, 8, 20), 900, "Старый пост вне окна"),
            {"code": "Cnone", "text": "", "date": None,
             "likes": 50, "comments": 0, "views": None, "video": False},
        ]})
    src, gap = instagram.read(b, "lilyspace")
    check("кэш прочитался", src.ok, src.error)
    check("кадр без подписи в срез не идёт", len(src.posts) == 4,
          str(len(src.posts)))
    check("подписчики подхватились", "4200" in src.subscribers, src.subscribers)
    check("свежий кэш дырой не считается", gap == "", gap)

    # ── окно недели и метрика ────────────────────────────────────────
    st = research.measure(src, window=week, metric="likes")
    check("окно оставило три поста", st.posts == 3, str(st.posts))
    check("старый пост посчитан вне окна", st.outside == 1, str(st.outside))
    check("медиана по лайкам", st.median == 205, str(st.median))
    check("медиана подписана лайками", st.metric == "лайков", st.metric)

    # Та же арифметика по просмотрам даёт другое число и другую подпись:
    # видео в окне одно, и выдать его за медиану профиля нельзя.
    by_views = research.measure(src, window=week, metric="views")
    check("по просмотрам считается только видео", by_views.with_views == 1,
          str(by_views.with_views))

    # ── возраст кэша ─────────────────────────────────────────────────
    stale = instagram._aged("lilyspace", datetime(2026, 8, 20).isoformat(),
                            today=date(2026, 9, 7))
    check("старый кэш назван дырой", "не попали" in stale, stale)
    fresh = instagram._aged("lilyspace", datetime(2026, 9, 5).isoformat(),
                            today=date(2026, 9, 7))
    check("свежий кэш молчит", fresh == "", fresh)
    check("кэш без даты сбора свежим не считается",
          "нет даты" in instagram._aged("lilyspace", None))

    # ── завод в сеть за профилем не ходит ────────────────────────────
    net = await sources.fetch("https://instagram.com/lilyspace")
    check("сеть за профилем не идёт", not net.ok, str(net.ok))
    check("отказ показывает дорогу", "instagram_pull" in net.error, net.error)

    # ── срез в snapshot ──────────────────────────────────────────────
    b.artifact("research/sources.md",
               "# За чем следим\n\n"
               "- instagram.com/lilyspace — свой профиль\n"
               "- instagram.com/nobody — не снят\n")
    watch = research.watchlist(b)
    check("профили попали в список источников", len(watch) == 2, str(watch))

    async def no_net(urls, *, limit=40):
        raise AssertionError("за профилями ходили в сеть")
    sources.fetch_all = no_net

    text, gaps = await research.snapshot(b, window=week)
    check("раздел профилей собран", "## Instagram-профили" in text, text[:80])
    check("медиана в таблице по лайкам", "Медиана лайков" in text)
    check("пост недели в срезе", "Карусель про голос бренда" in text)
    check("старый пост в срез не попал", "Старый пост вне окна" not in text)
    check("несобранный профиль назван дырой",
          any("nobody" in g and "кэша нет" in g for g in gaps), str(gaps))
    check("лайки подписаны словом", "лайк." in text)


asyncio.run(main())
raise SystemExit(report())
