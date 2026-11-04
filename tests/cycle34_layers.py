"""Цикл 34: слои поверх дубля — второй формат монтажа.

Формат пришёл с референса 22.10 и устроен наоборот сплиту: кадр не
делится вовсе, дубль идёт во весь экран, а поверх ложатся заголовок,
плашка площадки и снимок страницы с ползущим маркером. Модель здесь
только называет фразы и строки; всё, что проверяется ниже, — код.

Что дороже всего ошибается у этого формата:

- **слой стоит на слове, а не на секунде**. Та же граница, что у панели,
  и по той же причине: секунды готового ролика — это секунды после
  выброшенных пауз, и модель их не видела;
- **конец у каждого слоя свой**. Блок панели живёт до следующего блока,
  потому что панель всегда чем-то занята. Слои друг друга не сменяют —
  они лежат поверх кадра и пересекаются, и «до следующего» растянуло бы
  заголовок на весь ролик;
- **заголовок проверяется по строке, а не по длине всего текста**. Его
  растягивает на всю ширину шаблон, и длинная строка уезжает кеглем
  вниз — плашкой она быть перестаёт;
- **снимок без страницы это не слой словами, а пустое место**. `card`
  панели без картинки остаётся строками, `proof` рисовать нечем;
- **строка, которой на странице нет, не роняет монтаж**. Страница чужая
  и живая, её переписывают без нас, и отказ означал бы, что правка
  одного абзаца в чужой справке стоит человеку ролика;
- **форматы взаимно исключены**. Кадр либо делится швом, либо нет, и
  выбор делается до минут рендера, а не после;
- **просьба словами сильнее молчаливого выбора по альбому**. Иначе
  присланная к дублю картинка молча отменяла бы названный формат.

Сеть и Chrome сюда не ходят: снимок подделан путём и размером.
"""
from __future__ import annotations

import harness
from harness import check, report

harness.setup()

from orchestrator import cut, footage, montage                     # noqa: E402

TMP = harness.TMP / "layers"
TMP.mkdir(parents=True, exist_ok=True)


def _reel() -> montage.Reel:
    """Дубль на двадцать секунд с разобранной речью."""
    reel = montage.Reel(theme={"id": "t-lay"}, video=TMP / "нет.mp4")
    reel.probe = footage.Probe(duration=20.0, width=1080, height=1920,
                               fps=30.0, has_audio=True)
    reel.cuts = footage.timeline(20.0, [])
    said = [("они", 1.0), ("выкатили", 1.3), ("новое", 1.7),
            ("обновление", 2.1),
            ("а", 6.0), ("теперь", 6.2), ("смотрите", 6.6),
            ("что", 7.0), ("написано", 7.3),
            ("рекомендуем", 10.0), ("вам", 10.4), ("контент", 10.7),
            ("ищите", 14.0), ("настройку", 14.4), ("алгоритмов", 14.9),
            ("вот", 18.0), ("и", 18.2), ("всё", 18.4)]
    reel.subs = [{"text": t, "start": s, "end": s + 0.3} for t, s in said]
    return reel


def shape() -> None:
    print("\n1. Что код не пускает в кадр")

    good, lost = cut._layers([
        # Заголовок целиком: строки, рукописный акцент, стрелка.
        {"phrase": "они выкатили новое", "until": "а теперь смотрите",
         "kind": "title", "lines": ["новое", "обновление"],
         "script": "Instagram", "arrow": "down", "url": "", "marks": [],
         "open_phrase": ""},
        # Строка длиннее потолка: шаблон тянет её на всю ширину.
        {"phrase": "вторая тема", "until": "", "kind": "title",
         "lines": ["это очень длинная строка заголовка"], "script": "",
         "arrow": "none", "url": "", "marks": [], "open_phrase": ""},
        # Стрелка без акцента: ей неоткуда начаться.
        {"phrase": "третья тема", "until": "", "kind": "title",
         "lines": ["шаги"], "script": "", "arrow": "left", "url": "",
         "marks": [], "open_phrase": ""},
        # Снимок с адресом и строками подсветки.
        {"phrase": "что написано", "until": "ищите настройку",
         "kind": "proof", "lines": [], "script": "", "arrow": "none",
         "url": "help.example.com/algorithms",
         "marks": ["рекомендуем вам контент"], "open_phrase": ""},
        # Второй снимок: страница занимает кадр целиком, читать некогда.
        {"phrase": "и ещё страница", "until": "", "kind": "proof",
         "lines": [], "script": "", "arrow": "none", "url": "example.com",
         "marks": [], "open_phrase": ""},
        # Адрес, по которому завод не ходит.
        {"phrase": "локальная страница", "until": "", "kind": "proof",
         "lines": [], "script": "", "arrow": "none",
         "url": "http://localhost/secret", "marks": [], "open_phrase": ""},
        # Плашка площадки: имя и подпись.
        {"phrase": "ищите настройку алгоритмов", "until": "",
         "kind": "chrome",
         "lines": ["Настройка алгоритмов", "Узнайте, как настроить"],
         "script": "", "arrow": "none", "url": "", "marks": [],
         "open_phrase": ""},
        # Сорта такого нет.
        {"phrase": "что-то ещё", "until": "", "kind": "sticker",
         "lines": ["а"], "script": "", "arrow": "none", "url": "",
         "marks": [], "open_phrase": ""},
    ])

    kinds = [o.kind for o in good]
    check("в кадр прошли только годные слои",
          kinds == ["title", "title", "proof", "chrome"], str(kinds))
    check("строка длиннее потолка выбрасывает заголовок",
          any("длиннее 18 знаков" in n for n in lost), str(lost))
    check("стрелка без рукописного акцента снята, а слой остался",
          good[1].arrow == "none"
          and any("неоткуда начаться" in n for n in lost), str(lost))
    check("второй снимок страницы не берём",
          any("второй читать некогда" in n for n in lost), str(lost))
    check("адрес внутри сети в кадр не идёт",
          any("не годится" in n for n in lost), str(lost))
    check("сорта, которого нет, не бывает",
          any("слоя «sticker» не бывает" in n for n in lost), str(lost))
    check("отброшенное названо человеку, а не выброшено молча",
          len(lost) >= 5, str(lost))

    # Плашка стоит в чужом интерфейсе, и её текст читается как слова
    # площадки. Потолки тут не про красоту: строка без переноса, и то,
    # что не влезло, молча исчезло бы под многоточием.
    _, long = cut._layers([
        {"phrase": "плашка", "until": "", "kind": "chrome",
         "lines": ["и" * (cut.CHROME_HEAD + 1)], "script": "",
         "arrow": "none", "url": "", "marks": [], "open_phrase": ""}])
    check("текст плашки, который не влезает в строку, не берём",
          any("не влезает в строку" in n for n in long), str(long))


def laying() -> None:
    print("\n2. Слой встаёт на слово, а конец у него свой")

    reel = _reel()
    overlays = [
        cut.Overlay(phrase="выкатили новое обновление",
                    until="а теперь смотрите", kind="title",
                    lines=["новое", "обновление"], script="Instagram",
                    arrow="down"),
        cut.Overlay(phrase="ищите настройку алгоритмов", kind="chrome",
                    lines=["Настройка алгоритмов"]),
        cut.Overlay(phrase="этого никто не говорил", kind="title",
                    lines=["мимо"]),
    ]
    layers, lost = montage.lay_layers(reel, overlays)

    check("слой встал на секунду своей фразы",
          len(layers) == 2 and layers[0]["start"] == 1.3,
          str([(l["kind"], l["start"], l["end"]) for l in layers]))
    check("конец слоя это его собственная фраза, а не следующий слой",
          layers[0]["end"] == 6.0, str(layers[0]["end"]))
    check("слой без названного конца держится своим сроком",
          layers[1]["end"] == min(14.0 + montage.LAYER_HOLD["chrome"], 20.0),
          str(layers[1]["end"]))
    check("ненайденная фраза выбрасывает слой и называет его",
          any("в записи не нашлось" in n for n in lost), str(lost))

    # Пустых секунд тут бояться не надо: в них видно лицо, ради которого
    # дубль и снят. Поэтому первый слой к нулю не тянется — в отличие от
    # блока панели, где пустая панель читается поломкой.
    check("первый слой не тянется к нулю: пустой кадр это лицо",
          layers[0]["start"] > 0, str(layers[0]["start"]))

    print("\n3. Слой, который не успеть прочесть")
    short = montage.lay_layers(_reel(), [
        cut.Overlay(phrase="а теперь смотрите", until="что написано",
                    kind="title", lines=["новое"])])
    check("слой короче порога выброшен и назван",
          not short[0] and any("слой короче" in n for n in short[1]),
          str(short))

    back = montage.lay_layers(_reel(), [
        cut.Overlay(phrase="ищите настройку", until="они выкатили",
                    kind="title", lines=["шаги"])])
    check("конец раньше начала — слой выброшен, а не переставлен",
          not back[0] and any("раньше его начала" in n for n in back[1]),
          str(back))


def proving() -> None:
    print("\n4. Снимок страницы: подсветка, разворот, отсутствие страницы")

    reel = _reel()
    shot = TMP / "proof.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    о = cut.Overlay(phrase="что написано", until="вот и всё", kind="proof",
                    url="https://help.example.com/a",
                    marks=["рекомендуем вам контент", "чего там нет"])
    о.image, о.image_size = shot, (1100, 4200)
    о.spots = [("рекомендуем вам контент", 0.1, 0.2, 0.5, 0.02),
               ("чего там нет", 0.1, 0.6, 0.4, 0.02)]
    layers, lost = montage.lay_layers(reel, [о])

    check("снимок встал слоем", len(layers) == 1, str(layers))
    marks = layers[0]["marks"]
    check("подсветка поехала на своём слове",
          marks[0]["at"] == 10.0, str(marks))
    check("строка, которой в речи нет, разложена по ходу слоя, а не "
          "брошена", marks[1]["at"] is not None
          and layers[0]["start"] < marks[1]["at"] < layers[0]["end"],
          str(marks))
    check("разворот во весь кадр едет на первой подсветке",
          layers[0]["openAt"] == 10.0, str(layers[0].get("openAt")))
    check("размер снимка доехал: без него нечем считать прокрутку",
          layers[0]["imageWidth"] == 1100
          and layers[0]["imageHeight"] == 4200, str(layers[0]))
    check("фраза подсветки в кадр не едет: рисуется прямоугольник",
          "phrase" not in marks[0], str(marks[0]))

    # `card` панели без картинки остаётся строками. `proof` рисовать
    # нечем: это пустое место в кадре, а не слой словами.
    naked = cut.Overlay(phrase="что написано", kind="proof",
                        url="https://help.example.com/a", marks=["а"])
    gone, why = montage.lay_layers(_reel(), [naked])
    check("снимок без страницы — слоя нет, и причина названа",
          not gone and any("страница не снялась" in n for n in why),
          str(why))


def choosing() -> None:
    print("\n5. Формат выбирается до рендера, а не после")

    check("слои просят словами", montage.wants_layers("смонтируй слоями"),
          "")
    check("референс тоже просьба про слои",
          montage.wants_layers("сделай как на референсе"), "")
    check("сплит словами про слои не поднимается",
          not montage.wants_layers("смонтируй сплитом с панелью"), "")
    # Обе просьбы живут в одном топике, и пересечься их спискам значит
    # отдать человеку не ту работу после минут рендера.
    both = [w for w in montage.LAYER_WORDS if w in montage.MOTION_WORDS]
    check("слова слоёв и слова сплита не пересекаются", not both, str(both))
    cross = [w for w in montage.LAYER_WORDS if w in montage.INSERT_WORDS]
    check("слова слоёв и слова накладок не пересекаются", not cross,
          str(cross))

    print("\n6. Страница чужая и живая")
    # Отказ тут означал бы, что правка одного абзаца в чужой справке
    # роняет монтаж целиком.
    half = cut.Overlay(phrase="что написано", until="вот и всё",
                       kind="proof", url="https://help.example.com/a",
                       marks=["есть", "нет такого"])
    shot = TMP / "proof.png"
    half.image, half.image_size = shot, (1100, 3000)
    half.spots = [("есть", 0.1, 0.2, 0.3, 0.02)]      # вторая не нашлась
    kept, _ = montage.lay_layers(_reel(), [half])
    check("ненайденная на странице строка не роняет слой",
          len(kept) == 1 and len(kept[0]["marks"]) == 1, str(kept))


shape()
laying()
proving()
choosing()
raise SystemExit(report())
