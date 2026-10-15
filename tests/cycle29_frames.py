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

    check("«media» остаётся Remotion",
          frames.panel_spec(block(kind="media"), size=PANEL, fps=30,
                            colors=COLORS) is None,
          "запись из альбома двигает шаблон наездом на слово")
    check("«art» без файла остаётся Remotion",
          frames.panel_spec(block(kind="art"), size=PANEL, fps=30,
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
    cover = harness.TMP / "cover-x.png"
    cover.write_bytes("\x89PNG\r\n\x1a\nкадр обложки".encode())
    spec = frames.cover_spec(
        size=(1080, 1920), fps=30, seconds=1.8, lines=[],
        anchor="bottom", inset=211, focus=(0.5, 0.35), scrim=0.0,
        blur=False, back="#111111", image=cover)
    check("клип обложки длиной в первый кадр", spec["duration"] == 1.8)
    check("картинка приезжает именем файла",
          spec["imageSrc"] == "cover-x.png", spec["imageSrc"])
    check("точка кропа та же, что у неподвижной обложки",
          spec["focus"] == {"x": 0.5, "y": 0.35}, spec["focus"])
    check("задание сериализуется целиком",
          json.loads(json.dumps(spec))["width"] == 1080)

    # ── 9. шрифт: именем из ТЗ, чужое имя не подменяется молча ───────
    print("\n9. Шрифт обложки")
    check("шрифт из ТЗ площадки берётся как есть",
          frames.face("Unbounded") == "Unbounded.css")
    check("шрифт с пробелом в имени сходится с файлом",
          frames.face("Golos Text") == "GolosText.css")
    check("незнакомый шрифт падает в Manrope, а не в системный",
          frames.face("Чужая Гарнитура") == "Manrope.css")
    for name in frames.FACES:
        check(f"{name} лежит в репозитории",
              (frames.ROOT / frames.FONTS / f"{name}.css").exists())

    # ── 10. фото и скрин идут разными шаблонами ──────────────────────
    print("\n10. Фото и скрин — разные шаблоны")
    photo = frames.panel_spec(block(kind="art", imagePath="art-1.png"),
                              size=PANEL, fps=30, colors=COLORS, image=shot)
    check("картинка по теме идёт карточкой-фото",
          photo is not None and photo["kind"] == frames.PHOTO, photo)
    check("фото собирается своим шаблоном",
          frames.template_of(photo) == "photo.html")
    page = frames.panel_spec(
        block(imagePath="panel-1.png"),
        size=PANEL, fps=30, colors=COLORS, image=shot)
    check("снимок страницы идёт в окно браузера",
          page is not None and page["kind"] == frames.SHOT, page)

    # ── 11. дорожка панели: один клип вместо шести ──────────────────
    print("\n11. Дорожка панели")
    track = [block(start=2.0, end=6.0, lines=["Первая"]),
             block(start=7.0, end=11.0, kind="counter", value=12,
                   valueAt=7.5, lines=["минуты"])]
    spec = frames.track_spec(track, size=PANEL, fps=30, colors=COLORS,
                             band=115)
    check("дорожка начинается с первого блока", spec["offset"] == 2.0, spec)
    check("дорожка длится до конца последнего",
          spec["duration"] == 9.0, spec["duration"])
    check("сцены отсчитываются от начала дорожки",
          [(sc["start"], sc["end"]) for sc in spec["scenes"]]
          == [(0.0, 4.0), (5.0, 9.0)], spec["scenes"])
    check("число набирается от начала своей сцены",
          spec["scenes"][1]["valueAt"] == 0.5, spec["scenes"][1])
    check("дорожка идёт своим шаблоном",
          frames.template_of(spec) == "track.html")

    check("блок во весь кадр отменяет дорожку",
          frames.track_spec(track + [block(start=12.0, end=16.0, full=True)],
                            size=PANEL, fps=30, colors=COLORS,
                            band=115) is None,
          "у такого блока другой холст — панель разворачивается на кадр")
    check("запись из альбома отменяет дорожку",
          frames.track_spec(track + [block(start=12.0, end=16.0,
                                           kind="media",
                                           videoPath="panel-2.mp4")],
                            size=PANEL, fps=30, colors=COLORS,
                            band=115) is None,
          "её двигает Remotion, и в дорожке на её месте была бы дыра")
    check("без блоков дорожки нет",
          frames.track_spec([], size=PANEL, fps=30, colors=COLORS,
                            band=115) is None)

    page = frames.track_spec(
        [block(imagePath="panel-0.png"),
         block(start=7.0, end=11.0, kind="art", imagePath="art-0.png")],
        size=PANEL, fps=30, colors=COLORS, band=115,
        images={0: shot, 1: _other(shot)})
    check("снимок страницы в дорожке идёт окном",
          page["scenes"][0]["frame"] == "browser", page["scenes"][0])
    check("картинка по теме в дорожке идёт кадром",
          page["scenes"][1]["frame"] == "photo", page["scenes"][1])
    check("у картинок дорожки свои отпечатки",
          page["scenes"][0]["imageMark"] != page["scenes"][1]["imageMark"])

    # ── 12. значки и пункты по словам ───────────────────────────────
    print("\n12. Значки и разбор пункта")
    from orchestrator import cut                                   # noqa: E402

    it = montage.point("[папка] папка с исходниками — любой дубль")
    check("значок снят с начала пункта", it["icon"] == "папка", it)
    check("показывается текст без значка и пояснения",
          it["text"] == "папка с исходниками", it)
    check("пояснение отделено", it["note"] == "любой дубль", it)
    check("в записи ищется только фраза",
          it["phrase"] == "папка с исходниками", it)
    plain = montage.point("стабилизировать тряску")
    check("пункт без разметки остаётся собой",
          (plain["text"], plain["icon"], plain["note"])
          == ("стабилизировать тряску", "", ""), plain)

    check("значок из набора находится", bool(frames.icon("Ножницы")))
    check("значка не из набора нет", frames.icon("единорог") == "")

    good, lost = cut._slides([
        {"phrase": "сначала папка", "kind": "flow",
         "items": ["[папка] папка", "[искра] Клод"]},
        {"phrase": "одна вещь", "kind": "grid", "items": ["только одна"]},
        {"phrase": "подсветить фразу", "kind": "mark", "lines": []},
        {"phrase": "значок чужой", "kind": "card", "icon": "единорог",
         "lines": ["строка"]},
    ])
    kinds = [(g.kind, g.icon) for g in good]
    check("поток с двумя шагами взят", ("flow", "") in kinds, str(kinds))
    check("плитки с одним пунктом отброшены",
          any("пунктов 1" in n for n in lost), str(lost))
    check("подсветка без фразы отброшена",
          any("строк нет" in n for n in lost), str(lost))
    check("чужой значок снят, а карточка осталась",
          ("card", "") in kinds and any("единорог" in n for n in lost),
          str(lost))

    # ── 13. столбики: число из речи, а не из головы ─────────────────
    print("\n13. Столбики")
    bar = montage.point("[часы] вручную — 180")
    check("число пояснения стало высотой", bar["value"] == 180.0, bar)
    check("подпись столбика это имя", bar["text"] == "вручную", bar)
    check("у обычного пункта числа нет",
          "value" not in montage.point("заменить кадр — любой дубль"))

    good, lost = cut._slides([
        {"phrase": "час против минуты", "kind": "bars",
         "items": ["вручную — 180", "с ним — 20"]},
        {"phrase": "столбики без чисел", "kind": "bars",
         "items": ["раз", "два"]},
    ])
    check("столбики с числами взяты",
          [g.kind for g in good] == ["bars"], str(good))
    check("столбики без чисел отброшены и названы",
          any("нет числа" in n for n in lost), str(lost))

    return report()


raise SystemExit(main())
