"""Цикл 29: анимированные карточки панели и обложка (HyperFrames).

Рендер в стенд не входит: он поднимает Chrome и ffmpeg и стоит минуты.
Проверяется то, что ошибается молча и доезжает до готового ролика:

* **кого отдаём HyperFrames.** Запись из альбома и картинку по теме —
  никогда: их двигает шаблон Remotion своим наездом. Отдать их значит
  получить панель, на которой вместо записи экрана стоит пустая карточка.
* **секунды внутри задания.** Пункт списка привязан к слову в секундах
  готового ролика, а клип живёт своей длиной: не вычесть начало блока
  значит собрать клип, где список выезжает после его конца.
* **отпечаток.** Правка шаблона обязана обесценить кэш, иначе старые
  клипы приедут вперемешку с новыми, и заметит это человек.
* **откат.** Нет тулчейна — блоки рисуются как раньше, а причина уходит
  строкой. Молчаливый откат неотличим от поломки.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import harness
from harness import check, report

harness.setup()

from orchestrator import frames, montage                          # noqa: E402

COLORS = {"panelColor": "#F8F5F1", "accentColor": "#C6DE48",
          "textColor": "#1F1F1F", "cardColor": "rgba(31,31,31,0.06)"}
PANEL = (1080, 576)


class Brand:
    """Папка бренда под кэш клипов. Своя, не настоящая: кэш — файлы, и
    чистить их в папке клиента стенду незачем."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, rel: str) -> Path:
        out = self.root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        return out


def _other(like: Path) -> Path:
    """Второй файл под тем же именем в соседней папке: так приходят
    скрины разных страниц в один и тот же `panel-0.png`."""
    out = like.parent / "other" / like.name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes("\x89PNG\r\n\x1a\nдругая страница".encode())
    return out


def block(**kw) -> dict:
    """Блок в той же форме, в какой он уезжает в Remotion."""
    out = {"start": 2.0, "end": 6.0, "kind": "card", "lines": ["Строка"],
           "items": [], "full": False}
    out.update(kw)
    return out


def main() -> int:
    # ── 1. кого отдаём HyperFrames ────────────────────────────────────
    print("1. Кого отдаём HyperFrames")
    spec = frames.panel_spec(block(), size=PANEL, fps=30, colors=COLORS)
    check("карточка словами идёт в анимацию", spec is not None)

    for kind in ("media", "art"):
        check(f"«{kind}» остаётся Remotion",
              frames.panel_spec(block(kind=kind), size=PANEL, fps=30,
                                colors=COLORS) is None)

    check("карточка со скрином без файла остаётся Remotion",
          frames.panel_spec(block(imagePath="panel-1.png"), size=PANEL,
                            fps=30, colors=COLORS) is None,
          "имя внутри public/ — не путь, по нему файла не найти")

    shot = harness.TMP / "frames-shot.png"
    shot.write_bytes("\x89PNG\r\n\x1a\nпусть будет файл".encode())
    spec = frames.panel_spec(block(imagePath="panel-1.png"), size=PANEL,
                             fps=30, colors=COLORS, image=shot)
    check("карточка со скрином и файлом идёт в окно браузера",
          spec is not None and spec["kind"] == frames.SHOT, spec)
    check("скрин едет своим шаблоном",
          frames.template_of(spec) == "shot.html")
    check("отпечаток держит сам файл, а не имя",
          spec["imageMark"] != frames.panel_spec(
              block(imagePath="panel-1.png"), size=PANEL, fps=30,
              colors=COLORS, image=_other(shot))["imageMark"])
    check("знак продукта без файла остаётся Remotion",
          frames.panel_spec(block(kind="icon", imagePath="logo.png"),
                            size=PANEL, fps=30, colors=COLORS) is None)
    check("блок с записью остаётся Remotion",
          frames.panel_spec(block(videoPath="panel-1.mp4"), size=PANEL,
                            fps=30, colors=COLORS) is None)
    check("короткий блок не рендерится",
          frames.panel_spec(block(start=2.0, end=2.4), size=PANEL, fps=30,
                            colors=COLORS) is None)
    check("пустой блок не рендерится",
          frames.panel_spec(block(lines=[], items=[]), size=PANEL, fps=30,
                            colors=COLORS) is None)

    # ── 2. секунды внутри задания ─────────────────────────────────────
    print("\n2. Секунды задания идут от начала блока")
    spec = frames.panel_spec(
        block(start=12.0, end=16.0,
              items=[{"text": "первый", "at": 12.5},
                     {"text": "второй", "at": 14.0}],
              kind="counter", value=12, valueAt=13.0),
        size=PANEL, fps=30, colors=COLORS)
    check("длина клипа это длина блока", spec["duration"] == 4.0,
          spec["duration"])
    check("пункт отсчитывается от начала блока",
          [i["at"] for i in spec["items"]] == [0.5, 2.0], spec["items"])
    check("число набирается от начала блока", spec["valueAt"] == 1.0,
          spec["valueAt"])
    check("холст задания это холст панели",
          (spec["width"], spec["height"]) == PANEL)
    check("цвета панели приехали целиком",
          spec["panelColor"] == COLORS["panelColor"]
          and spec["accentColor"] == COLORS["accentColor"])

    # ── 3. отпечаток ──────────────────────────────────────────────────
    print("\n3. Отпечаток держит кэш")
    one = frames.panel_spec(block(), size=PANEL, fps=30, colors=COLORS)
    two = frames.panel_spec(block(lines=["Другая строка"]), size=PANEL,
                            fps=30, colors=COLORS)
    check("разные задания — разный отпечаток",
          frames._digest(one, "panel.html") != frames._digest(two, "panel.html"))
    check("одно задание — один отпечаток",
          frames._digest(one, "panel.html") == frames._digest(one, "panel.html"))
    check("шаблон входит в отпечаток",
          frames._digest(one, "panel.html") != frames._digest(one, "cover.html"))

    # ── 4. кэш не зовёт рендер ────────────────────────────────────────
    print("\n4. Кэш отдаёт готовый файл")
    b = Brand(harness.TMP / "frames-brand")
    mark = frames._digest(one, "panel.html")
    ready = b.path(f"{frames.FRAMES}/card-{mark[:16]}.mp4")
    ready.write_bytes(b"not really a video, but a file")
    got = asyncio.run(frames.clip(b, one))
    check("второй раз берётся из кэша", got == ready, got)

    # ── 5. откат, когда тулчейна нет ──────────────────────────────────
    print("\n5. Нет тулчейна — карточки рисует Remotion")
    was = frames.ROOT
    try:
        frames.ROOT = harness.TMP / "no-such-hyperframes"
        clips, notes = asyncio.run(frames.panel_clips(
            b, [block()], size=(1080, 1920), fps=30, colors=COLORS,
            split=0.30))
        check("клипов нет", clips == {}, clips)
        check("причина названа строкой",
              len(notes) == 1 and "не установлен" in notes[0], notes)
    finally:
        frames.ROOT = was

    # ── 6. клип доезжает до props Remotion ────────────────────────────
    print("\n6. Клип доезжает до props")
    names = montage._clip_files("2026-10-14-instagram-01", {0: ready, 2: ready})
    check("имена по индексу блока",
          names == {0: "clip-2026-10-14-instagram-01-0.mp4",
                    2: "clip-2026-10-14-instagram-01-2.mp4"}, names)

    bl = montage.Block(start=2.0, end=6.0, lines=["Строка"], kind="card")
    props = montage._block_props(bl, None, None, "clip-x-0.mp4")
    check("props несёт имя клипа", props["clipPath"] == "clip-x-0.mp4", props)
    check("нарисованное шаблоном остаётся в props",
          props["lines"] == ["Строка"], props)
    check("без клипа поля нет",
          "clipPath" not in montage._block_props(bl, None, None, None))

    # ── 7. цвета панели одни на оба пути ──────────────────────────────
    print("\n7. Цвета панели одни на оба пути")
    colors = montage.panel_colors()
    check("фон панели общий", colors["panelColor"] == montage.REEL_PANEL_BG)
    check("акцент общий", colors["accentColor"] == montage.REEL_ACCENT)
    check("текст общий", colors["textColor"] == montage.REEL_PANEL_TEXT)

    # ── 8. обложка: задание из тех же чисел ───────────────────────────
    print("\n8. Обложка движением")
    spec = frames.cover_spec(
        size=(1080, 1920), fps=30, seconds=1.8, lines=[],
        anchor="bottom", inset=211, focus=(0.5, 0.35), scrim=0.0,
        blur=False, back="#111111", image=Path("/tmp/cover-x.png"))
    check("клип обложки длиной в первый кадр", spec["duration"] == 1.8)
    check("картинка приезжает именем файла",
          spec["imageSrc"] == "cover-x.png", spec["imageSrc"])
    check("точка кропа та же, что у неподвижной обложки",
          spec["focus"] == {"x": 0.5, "y": 0.35}, spec["focus"])
    check("задание сериализуется целиком",
          json.loads(json.dumps(spec))["width"] == 1080)

    return report()


raise SystemExit(main())
