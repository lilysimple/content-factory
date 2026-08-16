"""Цикл 10: живой Дизайнер.

Редактор пишет текст настоящей моделью, Дизайнер верстает по нему макет
и рендерит PNG. Проверяется то, что подменой не поймать: соблюдает ли
модель ТЗ, попадает ли в холст, берёт ли переменные вместо цветов.
"""
from __future__ import annotations

import asyncio
import logging

import harness
from harness import CHAT, FakeRegistry, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import design, desk, editor                         # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)
logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

SENT = []


class Reg(FakeRegistry):
    async def send_file(self, role, chat_id, blob, name, **kw):
        SENT.append((name, len(blob)))


COPY = ("Я пришла в AI из аудита и проектов. Кода не писала ни строчки.\n\n"
        "В консалтинге моя работа была разобрать процесс клиента и найти "
        "место, где он рвётся.\n\n"
        "Сейчас я собираю такие же процессы. Исполнители в них это "
        "роли-агенты.")


def seed():
    b = desk.brand(CHAT)
    tid = "2026-08-15-telegram-01"
    b.artifact(f"posts/{tid}.md", f"<!-- {tid} -->\n\n{COPY}")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                  "rubric, status, title, hook, asset) VALUES "
                  "(?,?,'2026-08-15','telegram','пост','Путь','ready',?,?,?)",
                  (tid, CHAT, "Аудит, проекты, теперь AI",
                   "Кода не писала ни строчки", f"posts/{tid}.md"))
    return tid


async def main() -> None:
    reg = Reg()
    tid = seed()
    b = desk.brand(CHAT)

    lay = await design.build(CHAT, "сделай обложку")

    check("макет собран", len(lay.cards) >= 1, f"карточек {len(lay.cards)}")
    check("HTML и PNG оба на месте",
          len(lay.htmls) == len(lay.pngs) == len(lay.cards),
          f"html {len(lay.htmls)}, png {len(lay.pngs)}")

    html = lay.htmls[0].read_text(encoding="utf-8")
    png = lay.pngs[0]

    check("PNG весит как картинка", png.stat().st_size > 100_000,
          f"{png.stat().st_size} байт")
    check("ни одного абсолютного пути",
          design.ABSOLUTE.search(html) is None,
          str(design.ABSOLUTE.search(html)))
    check("токены подключены", "../design/tokens.css" in html)
    check("фото из папки бренда",
          any(p in html for p in design._photos(b)),
          "фото не подставлено")
    check("цвета переменными",
          len(design.LITERAL_COLOR.findall(html)) <= 6,
          f"литералов {len(design.LITERAL_COLOR.findall(html))}")
    check("холст 1080×1350",
          "width:1080px" in html.replace(" ", "") and
          "height:1350px" in html.replace(" ", ""),
          "холст не заявлен")
    check("код замечаний не нашёл", not lay.findings, "; ".join(lay.findings[:4]))

    print(f"\n  акцент: {lay.accent}")
    print(f"  файлы: {[f.name for f in lay.files]}")
    print(f"  PNG: {png.stat().st_size // 1024} КБ")

    # Копия для просмотра. В песочнице, чтобы не сорить в репозитории.
    out = harness.TMP / "live-cover.png"
    out.write_bytes(png.read_bytes())
    print(f"  копия: {out}")


asyncio.run(main())
raise SystemExit(report())
