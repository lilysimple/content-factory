"""Цикл 19: монтаж без ffmpeg и без Remotion.

Проверяется арифметика, на которой стоит ролик: где резать паузы, как
исходное время ложится на готовое, как слова собираются в страницы
караоке и откуда берутся цвета бренда. Всё это считает код, и ошибка тут
не падает, а тихо приезжает на смонтированном видео — рассинхроном
субтитров или белым текстом по белому фону.

Тяжёлые проходы (ffmpeg, whisper, рендер) сюда не входят намеренно:
минуты работы и чужие бинарники в стенде, который гоняется перед каждым
коммитом, — плохая сделка. Их проверяет живой прогон.
"""
from __future__ import annotations

import asyncio

import harness
from harness import CHAT, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import agent, cut, desk, footage, grab, montage                   # noqa: E402
from storage import db                                            # noqa: E402
from storage.brand import Brand                                   # noqa: E402

db.init(cfg.db_path)


def _brand() -> Brand:
    return Brand("lily-space", harness.TMP / "brands" / "lily-space")


def main() -> None:
    # ── 1. нарезка пауз ───────────────────────────────────────────────
    print("\n1. Паузы вырезаются, речь остаётся")
    tl = footage.timeline(30.0, [(5.0, 8.0), (20.0, 23.0)])
    check("кусков стало три", len(tl.keep) == 3, str(tl.keep))
    check("вырезано около шести секунд", 5.0 < tl.dropped < 5.3,
          f"{tl.dropped:.2f}")
    check("итог короче исходного", 24.5 < tl.total < 25.0, f"{tl.total:.2f}")
    check("вокруг реплики остался воздух", tl.keep[0][1] > 5.0,
          str(tl.keep[0]))

    print("\n2. Дубль без пауз не режется")
    whole = footage.timeline(12.0, [])
    check("кусок один", whole.keep == [(0.0, 12.0)], str(whole.keep))
    check("ничего не выброшено", whole.dropped == 0, str(whole.dropped))

    print("\n3. Молчащий дубль не превращается в пустоту")
    silent = footage.timeline(9.0, [(0.0, 9.0)])
    check("остался целиком", silent.total == 9.0, str(silent.total))

    # ── 4. пересчёт времени ───────────────────────────────────────────
    print("\n4. Исходное время ложится на готовое")
    check("до первой паузы время не двигается", tl.at(2.0) == 2.0, str(tl.at(2.0)))
    # 5,2 с речи до паузы плюс 2,2 с после неё: воздух вокруг реплики
    # (KEEP_PAD) входит в оба куска, и пересчёт обязан его учитывать.
    check("после паузы время сдвинуто назад",
          abs((tl.at(10.0) or 0) - 7.4) < 0.01, str(tl.at(10.0)))
    check("вырезанная секунда не существует", tl.at(6.5) is None, str(tl.at(6.5)))

    # ── 5. слова и панорама переезжают тем же пересчётом ──────────────
    print("\n5. Субтитры и панорама считаются одним пересчётом")
    words = [footage.Word("раз", 1.0, 1.4), footage.Word("тишина", 6.0, 6.4),
             footage.Word("два", 10.0, 10.5)]
    said = footage.cut_words(words, tl)
    check("слово из паузы выброшено", len(said) == 2,
          " ".join(str(w["text"]) for w in said))
    check("слово после паузы сдвинуто",
          abs(float(said[1]["start"]) - 7.4) < 0.01, str(said[1]))

    track = footage.cut_track(
        [footage.Focus(1.0, 0.2, 0.3), footage.Focus(6.5, 0.9, 0.9),
         footage.Focus(10.0, 0.7, 0.4)], tl)
    check("точка из паузы выброшена", len(track) == 2, str(track))
    check("точка после паузы сдвинута",
          abs(float(track[1]["t"]) - 7.4) < 0.01, str(track[1]))

    # ── 6. страницы караоке ───────────────────────────────────────────
    print("\n6. Караоке рвётся по паузе, а не только по счёту слов")
    flow = [{"text": f"с{i}", "start": i * 0.4, "end": i * 0.4 + 0.35}
            for i in range(8)]
    pages = footage.pages(flow)
    check("страницы по четыре слова", [len(p["words"]) for p in pages] == [4, 4],
          str([len(p["words"]) for p in pages]))

    torn = [{"text": "раз", "start": 0.0, "end": 0.4},
            {"text": "два", "start": 0.5, "end": 0.9},
            {"text": "три", "start": 3.0, "end": 3.4}]
    pages = footage.pages(torn)
    check("после паузы началась новая страница", len(pages) == 2, str(pages))
    check("страница помнит своё начало", pages[1]["start"] == 3.0, str(pages[1]))

    print("\n7. Дубль без речи не даёт страниц")
    check("страниц нет", footage.pages([]) == [], "непусто")

    # ── 8. цвета бренда ───────────────────────────────────────────────
    print("\n8. Цвет берётся по имени токена, а не по порядку строк")
    css = (":root {\n  --milk: #F8F5F1;\n  --graphite: #1F1F1F;\n"
           "  --terracotta: #C97C5D;\n}")
    check("фон это графит", montage._token(css, "graphite", "#000") == "#1F1F1F")
    check("акцент это терракота",
          montage._token(css, "terracotta", "#000") == "#C97C5D")
    check("первый hex в файле не выигрывает",
          montage._token(css, "graphite", "#000") != "#F8F5F1")
    check("без файла берётся запасной",
          montage._token("", "graphite", montage.DEFAULT_COLOR)
          == montage.DEFAULT_COLOR)

    # ── 9. обложка под этот холст или под чужой ───────────────────────
    print("\n9. Обложка чужого холста уходит в фон")
    posts = harness.TMP / "brands" / "lily-space" / "posts"
    posts.mkdir(parents=True, exist_ok=True)

    def png(name: str, w: int, h: int):
        path = posts / name
        head = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
                + w.to_bytes(4, "big") + h.to_bytes(4, "big"))
        path.write_bytes(head + b"\x08\x02\x00\x00\x00" + b"\x00" * 16)
        return path

    tall = png("t-cover.png", 1080, 1920)
    wide = png("w-cover.png", 1080, 1350)
    check("размер читается из заголовка", montage._png_size(tall) == (1080, 1920),
          str(montage._png_size(tall)))
    check("обложка 9:16 показывается как есть",
          montage._cover_fits(tall, (1080, 1920)))
    check("телеграмная 4:5 в рилс не годится",
          not montage._cover_fits(wide, (1080, 1920)))
    check("файла нет — не обложка, а не падение",
          montage._png_size(posts / "нет.png") is None)

    print("\n10. Порог тишины меряется по записи")
    js = '{ "input_i" : "-19.5", "input_thresh" : "-31.42" }'
    check("порог взят из loudnorm",
          abs(float(footage.THRESH_RX.search(js).group(1)) + 31.42) < 0.01)
    check("тишина без звука ловится как -inf",
          footage.THRESH_RX.search('{"input_thresh" : "-inf"}').group(1) == "-inf")

    print("\n11. Кадр на обложку берётся самый спокойный")
    track = [footage.Focus(1.0, 0.5, 0.5, 900.0),
             footage.Focus(2.0, 0.5, 0.5, 30.0),
             footage.Focus(3.0, 0.5, 0.5, 400.0),
             footage.Focus(30.0, 0.5, 0.5, 1.0)]
    check("выбран тихий кадр из начала", footage.calm_at(track, 6.0) == 2.0,
          str(footage.calm_at(track, 6.0)))
    check("кадр из конца не берётся", footage.calm_at(track, 6.0) != 30.0)
    check("пустой трек не роняет выбор", footage.calm_at([]) == 0.0)

    print("\n12. ТЗ обложки читается блоком, а не на глаз")
    b2 = _brand()
    spec, gap = montage._cover_spec(b2, "instagram", "reels")
    check("ТЗ бренда найдено", gap is None, str(gap))
    check("цвета взяты из ТЗ", spec["colors"].count("#") == 3, spec["colors"])
    spec_none, gap_none = montage._cover_spec(b2, "youtube", "shorts")
    check("без ТЗ работаем на дефолтах",
          spec_none["font"] == montage.COVER_DEFAULTS["font"])
    check("и говорим об этом строкой", bool(gap_none) and "ТЗ" in (gap_none or ""),
          str(gap_none))

    print("\n13. Хук ложится лесенкой, предлоги не висят в конце")
    lines = montage.cover_lines("Расскажи Claude всё о себе", (1080, 1920), spec)
    check("строк три", len(lines) == 3, str([l["text"] for l in lines]))
    check("цвета не повторяются подряд",
          lines[0]["color"] != lines[1]["color"] != lines[2]["color"],
          str([l["color"] for l in lines]))
    check("короткая строка крупнее длинной",
          max(lines, key=lambda l: l["size"])["text"]
          == min(lines, key=lambda l: len(l["text"]))["text"],
          str([(l["size"], l["text"]) for l in lines]))
    check("предлог не заканчивает строку",
          not any(l["text"].split()[-1].lower() in montage.GLUE for l in lines),
          str([l["text"] for l in lines]))
    check("блок влезает в отведённую высоту",
          sum(l["size"] * montage.LINE_HEIGHT for l in lines)
          <= 1920 * montage.BLOCK_SHARE + 1,
          str(sum(l["size"] for l in lines)))
    check("пустой хук не даёт строк",
          montage.cover_lines("", (1080, 1920), spec) == [])

    print("\n14. Размытие решает ТЗ, а не догадка кода")
    wide = montage.Reel(theme={"id": "t"}, video=posts / "нет.mov",
                        spec=dict(spec, blur="да"))
    wide.still = posts / "s.png"
    wide.cover = wide.still
    wide.probe = footage.Probe(10.0, 2940, 1912, 30.0, True)
    check("по умолчанию кадр из дубля размывается", montage._blur(wide))

    wide.spec = dict(spec, blur="нет")
    check("«нет» в ТЗ оставляет кадр резким", not montage._blur(wide),
          "снимаете на камеру — лицо важнее")

    wide.spec = dict(spec, blur="да")
    wide.cover = posts / "t-cover.png"      # обложка Дизайнера
    check("обложку Дизайнера не трогаем вовсе", not montage._blur(wide))


    print("\n14б. Кадр стоит на месте, пока действие не уехало")
    # Один выброс на пол-кадра посреди спокойной работы: среднее утащило
    # бы цель к нему, медиана — нет. Замер на настоящем материале дал
    # треть кадра блуждания именно на среднем.
    calm = [(0.30, 0.40, 500)] * 9 + [(0.95, 0.95, 4000)]
    goal = footage._target(calm)
    check("выброс не утаскивает цель", goal is not None and goal[0] < 0.4,
          str(goal))

    quiet = [(0.5, 0.5, 10)] * 20
    check("на шуме цели нет вовсе", footage._target(quiet) is None)
    check("нескольких живых отсчётов мало",
          footage._target([(0.3, 0.3, 500)] * 3) is None)

    jumpy = [footage.Focus(i / 5, 0.2 if i % 2 else 0.8, 0.5)
             for i in range(20)]
    smoothed = footage._smooth(jumpy)
    def swing(track):
        return sum(abs(track[i].x - track[i - 1].x)
                   for i in range(1, len(track)))
    check("сглаживание срезает дрожание",
          swing(smoothed) < swing(jumpy) / 5,
          f"{swing(smoothed):.2f} против {swing(jumpy):.2f}")
    check("сглаживание не двигает трек целиком",
          abs(sum(f.x for f in smoothed) / len(smoothed) - 0.5) < 0.05)

    print("\n15. Нарезка: кусок берёт свою часть найденных пауз")
    whole = footage.timeline(120.0, [(20.0, 24.0), (60.0, 66.0), (90.0, 95.0)])
    part = footage.window(whole, 50.0, 100.0)
    check("куски не выходят за окно",
          all(50.0 <= a and b <= 100.0 for a, b in part.keep), str(part.keep))
    check("пауза внутри окна вырезана", part.total < 50.0, str(part.total))
    check("время внутри куска считается от нуля", part.at(50.0) == 0.0,
          str(part.at(50.0)))
    empty = footage.window(whole, 21.0, 23.0)
    check("окно целиком в паузе не схлопывается в пустоту",
          empty.total > 0, str(empty.keep))

    shifted = part.shift(50.0)
    check("вырезанный в файл кусок считает время с нуля",
          shifted.keep[0][0] == 0.0, str(shifted.keep[:2]))
    check("сдвиг не меняет длительность",
          abs(shifted.total - part.total) < 0.001,
          f"{shifted.total} vs {part.total}")

    print("\n16. Время показывается человеку правильно")
    check("минуты делятся нацело, а не округляются",
          montage._clock(136) == "2:16", montage._clock(136))
    check("секунды с ведущим нулём", montage._clock(65) == "1:05",
          montage._clock(65))

    print("\n16б. Ссылка на видео узнаётся в просьбе")
    check("ссылка YouTube найдена",
          grab.link("нарежь https://youtu.be/abc123 на рилсы")
          == "https://youtu.be/abc123")
    check("точка в конце фразы не уезжает в ссылку",
          grab.link("вот https://www.youtube.com/watch?v=x.")
          == "https://www.youtube.com/watch?v=x")
    check("чужой хост не берём", grab.link("https://example.com/x.mp4") is None)
    check("без ссылки — ничего", grab.link("смонтируй ролик") is None)

    print("\n16в. Пословные тайминги из субтитров YouTube")
    raw = ('{"events":[{"tStartMs":1000,"segs":[{"utf8":"привет","tOffsetMs":0},'
           '{"utf8":" "},{"utf8":"мир","tOffsetMs":400}]},'
           '{"tStartMs":2000,"segs":[{"utf8":"дальше","tOffsetMs":0}]}]}')
    words = grab._from_json3(raw)
    check("пустые куски пропущены", [w.text for w in words]
          == ["привет", "мир", "дальше"], str([w.text for w in words]))
    check("время слова из сдвига события", abs(words[1].start - 1.4) < 0.01,
          str(words[1].start))
    check("слово кончается там, где начинается следующее",
          abs(words[0].end - 1.4) < 0.01, str(words[0].end))

    print("\n17. Нарезка узнаётся по просьбе")
    check("«нарежь на рилсы» это нарезка", montage.wants_split("нарежь на рилсы"))
    check("«разбей запись» это нарезка", montage.wants_split("разбей запись"))
    check("«смонтируй» это обычный монтаж",
          not montage.wants_split("смонтируй ролик"))

    print("\n18. Тема под кусок заводится без даты")
    frag = montage.cut.Fragment(0.0, 30.0, "хук куска", "заголовок", "зачем")
    t1 = montage._theme(CHAT, frag, "instagram", "reels")
    t2 = montage._theme(CHAT, frag, "instagram", "reels")
    check("id не повторяется", t1["id"] != t2["id"], f"{t1['id']} {t2['id']}")
    check("статус готовый", t1["status"] == "ready", str(t1["status"]))
    check("источник adhoc", t1["src"] == "adhoc", str(t1["src"]))
    check("слот в плане не занят", not t1["date"], str(t1["date"]))
    check("хук сохранён", t1["hook"] == "хук куска", str(t1["hook"]))

    # ── 19. хук и CTA берутся из разбора сценария ─────────────────────
    print("\n19. Хук и CTA приходят из файла Редактора Reels")
    b = _brand()
    b.artifact("posts/t1-script-notes.md",
               "# Разбор\n\n## Блоки\n\n### Хук · 0:00–0:03 · 5 слов\n\n"
               "Я перестала писать посты\n\n### CTA · 0:22–0:30 · 6 слов\n\n"
               "Приходите в канал\n")
    beats = montage._beats(b, "t1")
    check("хук найден", beats.get(montage.HOOK_TITLE) == "Я перестала писать посты",
          str(beats))
    check("CTA найден", beats.get(montage.CTA_TITLE) == "Приходите в канал",
          str(beats))
    check("лишних блоков не появилось", len(beats) == 2, str(list(beats)))

    # ── 20. лицо на обложке ───────────────────────────────────────────
    #
    # Требование бренда: кадр обложки — с человеком, и текст не ложится
    # ему на лицо. Сам детектор лиц тут не зовём (это swift и полсекунды
    # на кадр), проверяем арифметику вокруг него: кроп ведётся за лицом,
    # блок текста уходит от лица, и обе работы отдают то, что понимает
    # композиция.
    print("\n20. Кроп ведётся за лицом")
    canvas = (1080, 1920)
    # Кадр записи экрана 2940×1912: на холст 9:16 влезает по высоте, по
    # ширине теряет две трети. Лицо у левого края при кропе по центру
    # уехало бы за границу.
    left = footage.Face(0.08, 0.30, 0.10, 0.16)
    fx, fy, top, bottom = montage.cover_crop(left, (2940, 1912), canvas)
    check("кроп ушёл к левому краю", fx < 0.25, f"{fx:.3f}")
    check("по высоте кадр не двигали", abs(fy - 0.5) < 0.01, f"{fy:.3f}")
    check("лицо осталось в холсте", 0 < top < bottom < 1,
          f"{top:.3f}–{bottom:.3f}")

    mid = footage.Face(0.45, 0.30, 0.10, 0.16)
    fx2, _, _, _ = montage.cover_crop(mid, (2940, 1912), canvas)
    check("лицо по центру кроп не сдвигает", abs(fx2 - 0.5) < 0.06,
          f"{fx2:.3f}")

    vert = footage.Face(0.30, 0.10, 0.30, 0.20)
    fxv, fyv, topv, botv = montage.cover_crop(vert, (1080, 1920), canvas)
    check("вертикальный дубль не кропается",
          abs(fxv - 0.5) < 0.01 and abs(fyv - 0.5) < 0.01, f"{fxv} {fyv}")
    check("полоса лица совпала с рамкой",
          abs(topv - 0.10) < 0.01 and abs(botv - 0.30) < 0.01,
          f"{topv:.3f}–{botv:.3f}")

    print("\n21. Текст обложки уходит от лица")
    lines = montage.cover_lines("Я перестала писать посты руками",
                                canvas, dict(montage.COVER_DEFAULTS))
    block = montage._block_height(lines, 0, False)

    plain = montage.place_cover(lines, canvas, None)
    check("без лица блок остаётся внизу", plain["anchor"] == "bottom",
          str(plain["anchor"]))
    check("отступ по ТЗ", plain["inset"] == round(1920 * montage.COVER_INSET),
          str(plain["inset"]))

    # Лицо в верхней трети: низ свободен, блок остаётся внизу.
    high = montage.place_cover(lines, canvas, (0.10, 0.30))
    check("лицо сверху — текст внизу", high["anchor"] == "bottom",
          str(high["anchor"]))
    check("текст не задел лицо",
          1920 - high["inset"] - block > 0.30 * 1920,
          f"{high['inset']} {block:.0f}")

    # Лицо в нижней половине: внизу места нет, блок поднимается наверх.
    low = montage.place_cover(lines, canvas, (0.55, 0.95))
    check("лицо снизу — текст наверх", low["anchor"] == "top",
          str(low["anchor"]))
    check("блок кончается выше лица",
          low["inset"] + montage._block_height(low["lines"], 0, False)
          < 0.55 * 1920, f"{low['inset']}")
    check("на ужатие ушли не все строки", len(low["lines"]) == len(lines),
          str(len(low["lines"])))

    # Лицо во весь кадр: увести текст некуда, и это говорится строкой.
    huge = montage.place_cover(lines, canvas, (0.03, 0.99))
    check("лицо во весь кадр — честная строка", bool(huge["note"]),
          str(huge["note"]))

    print("\n22. Размытие «авто» слушает лицо")
    reel = montage.Reel(theme={"id": "t1"}, video=harness.TMP / "x.mp4")
    reel.still = harness.TMP / "still.png"
    reel.cover = reel.still
    reel.spec = dict(montage.COVER_DEFAULTS, blur="авто")
    reel.face = footage.Face(0.4, 0.2, 0.2, 0.2)
    check("кадр с лицом не размываем", not montage._blur(reel))
    reel.face = None
    check("кадр без лица размываем", montage._blur(reel))
    reel.spec = dict(montage.COVER_DEFAULTS, blur="нет")
    check("прямой запрет сильнее авто", not montage._blur(reel))

    print("\n23. Кадр-проба берётся из паузы, а не из речи")
    pauses = [(0.2, 1.4), (4.0, 5.2), (9.0, 9.6), (12.0, 14.0)]
    got = footage.quiet_times(pauses)
    check("проба поднята к первой секунде", 1.0 in got and 0.8 not in got,
          str(got))
    check("короткая пауза не годится", 9.3 not in got, str(got))
    check("пауза, кончающаяся до первой секунды, не годится",
          footage.quiet_times([(0.0, 1.1)]) == [],
          str(footage.quiet_times([(0.0, 1.1)])))
    check("проба стоит посередине паузы",
          footage.quiet_times([(10.0, 11.4)]) == [10.7],
          str(footage.quiet_times([(10.0, 11.4)])))
    check("середина длинной паузы взята", 13.0 in got, str(got))
    check("окно куска сужает список",
          footage.quiet_times(pauses, (10.0, 20.0)) == [13.0],
          str(footage.quiet_times(pauses, (10.0, 20.0))))
    check("без пауз список пуст", footage.quiet_times([]) == [])

    print("\n24. Рамки лиц разбираются из ответа детектора")
    raw = {"faces": [{"x": 0.3, "y": 0.2, "w": 0.2, "h": 0.25, "conf": 0.9},
                     {"x": 0.9, "y": 0.9, "w": 0.01, "h": 0.01, "conf": 0.9},
                     {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "conf": 0.1}]}
    got = footage._face_of(raw)
    check("лицо в кадре осталось одно", len(got) == 1, str(got))
    check("центр посчитан", abs(got[0].cx - 0.4) < 0.01, str(got[0].cx))
    check("детектор ищет по всему дублю",
          montage.footage._spread(
              [footage.Focus(t / 2, 0.5, 0.5, w=t) for t in range(0, 40)],
              20.0, 4)[-1] > 12.0)


    # ── 25. словарь субтитров ─────────────────────────────────────────
    #
    # Расшифровка слышит речь, а не имена: «клод» вместо Claude приезжает
    # на каждом дубле одинаково. Проверяем ровно арифметику замены —
    # whisper сюда не зовём.
    print("\n25. Словарь правит расслышанное")
    heard = [{"text": "Клод", "start": 0.0, "end": 0.5},
             {"text": "код,", "start": 0.5, "end": 1.0},
             {"text": "это", "start": 1.0, "end": 1.3},
             {"text": "ээ", "start": 1.3, "end": 1.5},
             {"text": "ремоушен.", "start": 1.5, "end": 2.0}]
    fixed, n = footage.relex(heard, [("клод код", "Claude Code"),
                                     ("ремоушен", "Remotion"), ("ээ", "")])
    said = [w["text"] for w in fixed]
    check("фраза целиком важнее слова",
          said[:2] == ["Claude", "Code,"], str(said))
    check("заглавная буква исходного слова осталась",
          said[0] == "Claude", said[0])
    check("запятая не пропала вместе с ошибкой", said[1].endswith(","),
          said[1])
    check("пустая замена выбрасывает слово", "ээ" not in said, str(said))
    check("поправленные слова посчитаны", n == 4, str(n))
    check("тайминги фразы не разъехались",
          fixed[0]["start"] == 0.0 and fixed[1]["end"] == 1.0, str(fixed[:2]))
    check("без словаря транскрипт не трогается",
          footage.relex(heard, []) == (heard, 0))
    check("«ё» и регистр сравнению не мешают",
          footage.relex([{"text": "Всё", "start": 0.0, "end": 0.4}],
                        [("все", "всё-таки")])[1] == 1)

    print("\n26. Правка человека разбирается в пары замен")
    check("стрелка", montage.fixes("клод -> Claude") == [("клод", "Claude")],
          str(montage.fixes("клод -> Claude")))
    check("стрелка знаком", montage.fixes("клод → Claude")
          == [("клод", "Claude")])
    check("словами в кавычках",
          montage.fixes("замени «ремоушен» на «Remotion»")
          == [("ремоушен", "Remotion")],
          str(montage.fixes("замени «ремоушен» на «Remotion»")))
    check("несколько строк сразу",
          len(montage.fixes("клод -> Claude\n- ээ ->")) == 2,
          str(montage.fixes("клод -> Claude\n- ээ ->")))
    check("пустая правая часть это выбросить",
          montage.fixes("ээ ->") == [("ээ", "")], str(montage.fixes("ээ ->")))
    check("обычная просьба заменой не считается",
          montage.fixes("подвинь заголовок на две строки ниже") == [],
          str(montage.fixes("подвинь заголовок на две строки ниже")))

    print("\n27о. Карточка ролика переживает рестарт")
    # Кнопка под пережившей рестарт карточкой поднимает работу из базы.
    # Ролик ищется **файлом**, тем же швом, каким его ищет Публикатор:
    # пока `_recover` смотрел в поле `asset`, куда `_save` намеренно
    # ничего не кладёт, любая кнопка после перезапуска завода отвечала
    # «этот монтаж уже неактуален» — и словарь субтитров поправить было
    # нельзя вовсе.
    fresh = desk.adhoc(CHAT, plat="instagram", fmt="reels",
                       title="Дубль из головы", hook="хук", why="",
                       status="ready")
    check("без файла поднимать нечего",
          montage._recover(CHAT, fresh["id"]) is None)
    _brand().artifact(f"posts/{fresh['id']}-reel.mp4", b"\x00")
    back = montage._recover(CHAT, fresh["id"])
    check("файл на диске — карточка поднялась", back is not None)
    check("ролик найден там, где его ищет Публикатор",
          back is not None and back.out is not None
          and back.out.name == f"{fresh['id']}-reel.mp4",
          str(back.out if back else None))
    check("поле asset для этого не нужно",
          back is not None and not back.theme.get("asset"),
          str(back.theme.get("asset") if back else None))
    check("дубля на столе нет, и это честно",
          back is not None and str(back.video) == "/dev/null",
          str(back.video if back else None))

    print("\n27. Словарь живёт в папке бренда")
    b3 = _brand()
    b3.path(montage.LEXICON).unlink(missing_ok=True)
    check("нет файла — нет замен", montage.lexicon(b3) == [])
    montage.remember(b3, [("клод", "Claude"), ("ээ", "")])
    montage.remember(b3, [("Клод", "Claude Code")])   # то же слово второй раз
    got = montage.lexicon(b3)
    check("замены записаны", ("клод", "Claude") in got, str(got))
    check("пустая замена пережила запись", ("ээ", "") in got, str(got))
    check("дубль слова не заводится дважды", len(got) == 2, str(got))
    check("шапка файла не читается как замена",
          all("Словарь" not in src for src, _ in got), str(got))

    print("\n28. Правка пересобирает субтитры, а не расшифровку")
    reel = montage.Reel(theme={"id": "t9"}, video=_brand().path("нет.mov"))
    reel.subs = list(heard)
    reel.findings = ["что-то не сошлось", montage.LEX_NOTE + "1"]
    montage._relex(reel, [("ремоушен", "Remotion")])
    check("страницы собраны из исправленных слов",
          "Remotion." in [w["text"] for p in reel.pages for w in p["words"]],
          str(reel.pages))
    check("услышанное на столе осталось", reel.subs == heard, str(reel.subs[:1]))
    check("прошлый счёт правок не задвоился",
          sum(f.startswith(montage.LEX_NOTE) for f in reel.findings) == 1,
          str(reel.findings))
    check("чужая находка не потерялась",
          "что-то не сошлось" in reel.findings, str(reel.findings))


    print("\n29. Замена словами не собирает ролик заново")
    from bots.router import resolve as _resolve
    check("«поправь субтитры» уходит монтажу, а не Редактору",
          _resolve("поправь субтитры: клод -> Claude", None, {}).role
          == "montage",
          _resolve("поправь субтитры: клод -> Claude", None, {}).role)
    montage.table.clear(CHAT)
    check("без карточки на столе это не правка",
          not montage.wants_relex(CHAT, "клод -> Claude"))
    montage.table.hold(CHAT, reel)
    check("с карточкой на столе замена это правка",
          montage.wants_relex(CHAT, "клод -> Claude"))
    check("«смонтируй» правкой не считается",
          not montage.wants_relex(CHAT, "смонтируй ролик"))
    montage.table.clear(CHAT)

    # ── 30. тема под монтаж ───────────────────────────────────────────
    # Дубль снят, сценария нет — это рабочий случай, а не ошибка: хук и
    # CTA украшают первый и последний кадр, а караоке и нарезка считаются
    # по записи. Но берём такую тему только по явному id: угадывать между
    # черновиками монтаж не должен, рендер стоит минут.
    print("\n30. Тема под монтаж")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                  "status, title) VALUES ('2026-09-05-instagram-01',?,"
                  "'2026-09-05','instagram','reels','idea','Сырой дубль')",
                  (CHAT,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                  "status, title) VALUES ('2026-09-06-telegram-01',?,"
                  "'2026-09-06','telegram','пост','idea','Не ролик')",
                  (CHAT,))

    got = montage._pick(CHAT, "смонтируй по теме 2026-09-05-instagram-01")
    check("тема без сценария берётся по id",
          got["id"] == "2026-09-05-instagram-01", got["id"])

    # Без id и без утверждённого сценария темы нет — и это не отказ, а
    # второй вход: человек снял дубль из головы. Черновик при этом всё
    # равно не берётся молча, угадывать между ними нельзя.
    check("без id черновик молча не берётся",
          montage._pick(CHAT, "смонтируй") is None)

    try:
        montage._pick(CHAT, "смонтируй по теме 2026-09-05-instagram-77")
        check("названный id, которого нет, это отказ", False, "смолчал")
    except desk.NoWork as e:
        check("названный id, которого нет, это отказ", "нет" in str(e), str(e))

    try:
        montage._pick(CHAT, "смонтируй по теме 2026-09-06-telegram-01")
        check("чужой формат не берётся", False, "смонтировал пост")
    except desk.NoWork as e:
        check("чужой формат не берётся", "не ролик" in str(e), str(e))

    with db.tx() as c:
        c.execute("UPDATE themes SET status = 'ready', asset = "
                  "'posts/2026-09-05-instagram-01-script.md' "
                  "WHERE id = '2026-09-05-instagram-01'")
    got = montage._pick(CHAT, "смонтируй")
    check("утверждённый сценарий берётся молча",
          got["id"] == "2026-09-05-instagram-01", got["id"])


    # ── 31. границы кусков ────────────────────────────────────────────
    # Работа Монтажёра: что он ответил — просьба, границы проверяет код.
    print("\n31. Границы кусков проверяет код, не промпт")

    class W:
        def __init__(self, text, start):
            self.text, self.start = text, start

    words = [W(f"с{i}", i * 0.5) for i in range(30)]
    text = cut.transcript(words, line=10)
    check("расшифровка идёт строками с меткой времени",
          text.startswith("[0] ") and "\n[5] " in text, text[:60])

    raw = [
        {"start": 0, "end": 30, "hook": "первый", "title": "т"},
        {"start": 25, "end": 55, "hook": "внахлёст", "title": "т"},
        {"start": 60, "end": 65, "hook": "короткий", "title": "т"},
        {"start": 70, "end": 140, "hook": "длинный", "title": "т"},
        {"start": 150, "end": 175, "hook": "", "title": "без хука"},
        {"start": 100, "end": 300, "hook": "за краем", "title": "т"},
        {"start": "ой", "end": 200, "hook": "не число", "title": "т"},
    ]
    good, lost = cut._fit(raw, 180)
    check("взят только годный кусок", [f.hook for f in good] == ["первый"],
          str([f.hook for f in good]))
    check("наезд отброшен", any("наезжает" in x for x in lost), str(lost))
    check("короткий отброшен", any("короче" in x for x in lost), str(lost))
    check("длинный отброшен", any("длиннее" in x for x in lost), str(lost))
    check("кусок без хука отброшен", any("без хука" in x for x in lost),
          str(lost))
    check("вышедший за длину записи отброшен",
          any("не помещается" in x for x in lost), str(lost))
    check("нечисловое время отброшено",
          any("не число" in x for x in lost), str(lost))
    check("каждое отбрасывание названо", len(lost) == 6, str(len(lost)))

    ok = [{"start": 10, "end": 40, "hook": "х", "title": "", "why": "п"}]
    good, _ = cut._fit(ok, 120)
    check("без title берётся хук", good[0].title == "х", good[0].title)
    check("длительность считается", good[0].seconds == 30, str(good[0].seconds))

    # ── 32. короткий дубль: своя вилка ────────────────────────────────
    # Вилка 20–60 придумана для выбора куска из длинной записи. Дубль на
    # восемнадцать секунд человек снял целиком, и отказать ему в монтаже
    # из-за чужой границы значит сломать работающий путь.
    print("\n32. У короткого дубля своя вилка")
    short = [{"start": 0.8, "end": 18.0, "hook": "х", "title": "т", "why": ""}]
    kept, _ = cut._fit(short, 19.0, lo=cut.WHOLE_MIN, hi=None)
    check("дубль короче двадцати секунд проходит", len(kept) == 1, str(kept))
    dropped, why = cut._fit(short, 19.0)
    check("в нарезке тот же кусок отбрасывается", not dropped, str(dropped))
    check("и это названо", any("короче" in x for x in why), str(why))

    long_one = [{"start": 0, "end": 95, "hook": "х", "title": "т", "why": ""}]
    kept, _ = cut._fit(long_one, 100.0, lo=cut.WHOLE_MIN, hi=None)
    check("длинный дубль монтируется целиком", len(kept) == 1, str(kept))

    # ── 33. обрезка краёв ─────────────────────────────────────────────
    print("\n33. Дубль обрезается по краям, а не по середине")
    reel = montage.Reel(theme={"id": "t"}, video=harness.TMP / "нет.mp4")
    reel.probe = footage.Probe(duration=60.0, width=1080, height=1920,
                               fps=30.0, has_audio=True)
    reel.cuts = footage.timeline(60.0, [(30.0, 33.0)])
    reel.focus = [footage.Focus(t=float(i), x=0.5, y=0.5) for i in range(60)]
    frag = cut.Fragment(4.0, 50.0, "хук", "заголовок", "")
    montage._trim(reel, frag, _brand())
    check("границы куска запомнены", reel.piece == (4.0, 50.0), str(reel.piece))
    check("пауза внутри куска осталась вырезанной",
          len(reel.cuts.keep) == 2, str(reel.cuts.keep))
    check("кусок не вылезает за границы",
          reel.cuts.keep[0][0] >= 4.0 and reel.cuts.keep[-1][1] <= 50.0,
          str(reel.cuts.keep))
    check("человеку сказано, сколько срезано",
          any("обрезан по краям" in f for f in reel.findings),
          str(reel.findings))

    # ── 34. промпт Монтажёра ──────────────────────────────────────────
    print("\n34. Монтажёр это отдельная роль")
    sys_cut = agent.system_text("cut", brand_name="Lily Space")
    check("подстановок не осталось", not agent.leftovers(sys_cut),
          str(agent.leftovers(sys_cut)))
    check("имя роли подставлено", "Ты Монтажёр" in sys_cut, sys_cut[:200])
    check("Монтажёру правила письма не едут",
          "Признаки машинного текста" not in sys_cut,
          "он ничего не сочиняет")
    check("хук берётся из сказанного", "из самого куска" in sys_cut)
    check("обе работы описаны",
          "## Длинная запись" in sys_cut and "## Короткий дубль" in sys_cut)

    sys_reels = agent.system_text("reels", brand_name="Lily Space")
    check("у Редактора Reels нарезки больше нет",
          "нарезка длинной записи" not in sys_reels.lower(),
          "секция осталась в промпте сценариста")
    check("Редактор Reels стал короче",
          len(sys_reels) < 12000, f"{len(sys_reels)} знаков")

    # ── 35. неудачный монтаж не оставляет тему готовой ────────────────
    # Тему под дубль из головы заводит монтаж. Упавший рендер оставил бы
    # её в `ready` без файла, и Публикатор взял бы её в очередь.
    print("\n35. Упавший монтаж не оставляет тему в очереди")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
    stub = cut.Fragment(0.0, 30.0, "хук", "Дубль из головы", "")
    made = montage._theme(CHAT, stub, "instagram", "reels")
    check("тема заведена готовой", made["status"] == "ready", made["status"])
    check("дня в плане не занимает", not made["date"], str(made["date"]))
    check("помечена как снятая, а не спланированная",
          made["src"] == "adhoc", made["src"])
    with db.tx() as c:
        c.execute("UPDATE themes SET status = 'failed', skip_reason = 'рендер' "
                  "WHERE id = ? AND chat_id = ?", (made["id"], CHAT))
    row = db.one("SELECT status FROM themes WHERE id = ?", made["id"])
    check("упавший монтаж уводит тему из очереди",
          row["status"] == "failed", row["status"])

    # ── 36. готовый ролик не затирает сценарий ────────────────────────
    #
    # `_save` писал путь к mp4 в `themes.asset`. Поле читается как текст
    # тремя местами (`publisher.collect`, `editor.revise`, `design.build`)
    # — все три делают `read_text(utf-8)` и падают на первом нетекстовом
    # байте, а колбэки ничем не обёрнуты: человек нажимал «В очередь» и
    # не получал ничего. Заодно терялась ссылка на суфлёр темы из плана.
    print("\n36. Готовый ролик не затирает сценарий")
    b = _brand()
    tid = "2026-09-05-instagram-01"
    script = f"posts/{tid}-script.md"
    b.artifact(script, "<!-- суфлёр -->\n\nПривет.")
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO themes (id, chat_id, plat, format) "
                  "VALUES (?,?,'instagram','reels')", (tid, CHAT))
        c.execute("UPDATE themes SET status = 'ready', asset = ? "
                  "WHERE id = ? AND chat_id = ?", (script, tid, CHAT))

    out = harness.TMP / "reel-out.mp4"
    out.write_bytes(b"\x00\x00\x00\x18ftypmp42\xff\xfe")
    reel = montage.Reel(theme={"id": tid, "chat_id": CHAT},
                        video=harness.TMP / "src.mp4", out=out)
    path = montage._save(b, reel)

    check("ролик лёг в папку бренда",
          path.name == f"{tid}-reel.mp4" and path.exists(), str(path))
    check("сценарий в базе остался на месте",
          db.one("SELECT asset FROM themes WHERE id = ?", tid)["asset"] == script,
          str(db.one("SELECT asset FROM themes WHERE id = ?", tid)["asset"]))
    check("статус не понижен до черновика",
          db.one("SELECT status FROM themes WHERE id = ?", tid)["status"] == "ready")

    # ── 37. видео из папки берётся с любым именем ─────────────────────
    #
    # `_footage` искал только `pending.*` — имя, которое даёт бот. Файл,
    # положенный в папку руками, назывался как назывался на камере и не
    # находился вовсе: отказ «видео ещё не пришло» на видео, которое
    # лежит в папке.
    print("\n37. Видео из папки берётся с любым именем")
    d = montage.incoming_dir(b)
    for f in d.iterdir():
        f.unlink()
    own = d / "IMG_4471.MOV"
    own.write_bytes(b"\x00" * 64)
    check("своё имя найдено", montage._footage(b) == own,
          str(montage._footage(b)))

    (d / "заметка.txt").write_text("не видео", encoding="utf-8")
    check("не-видео за дубль не принято", montage._footage(b) == own,
          str(montage._footage(b)))

    for f in d.iterdir():
        f.unlink()
    try:
        montage._footage(b)
        check("пустая папка это отказ словами", False, "смолчал")
    except montage.NoFootage as e:
        check("пустая папка это отказ словами", "ссылку" in str(e), str(e))

    # ── 38. просьба про несколько роликов узнаётся ────────────────────
    #
    # Список слов был уже фразы: «сделай из этого видео несколько рилс»
    # не попадало в него ничем и уходило монтировать запись целиком.
    print("\n38. Просьба про несколько роликов")
    for ask in ("сделай из этого видео несколько рилс",
                "нарежь клипы из эфира", "сделай пару рилсов из записи"):
        check(f"нарезка узнана: {ask[:34]}", montage.wants_split(ask), ask)
    check("одиночный монтаж не считается нарезкой",
          not montage.wants_split("смонтируй рилс"), "смонтируй рилс")

    # ── 38б. ссылка в топике Reels это материал, а не тема ────────────
    #
    # Слово «рилс» весит на Редактора Reels, поэтому «сделай из этого
    # видео несколько рилс <ссылка>» уходило писать суфлёр по теме,
    # которой нет, вместо того чтобы резать присланную запись. Голая
    # ссылка — туда же, топиком по умолчанию.
    print("\n38б. Ссылка в топике Reels")
    _r = lambda t: _resolve(t, "reels", {}).role       # noqa: E731
    for ask in ("сделай из этого видео несколько рилс https://youtu.be/abc",
                "смонтируй рилс https://youtu.be/abc",
                "https://youtu.be/abc"):
        check(f"ссылка уходит монтажу: {ask[:36]}", _r(ask) == "montage",
              _r(ask))
    check("сценарий без ссылки остаётся Редактору Reels",
          _r("напиши сценарий рилса про AI") == "reels",
          _r("напиши сценарий рилса про AI"))
    check("ссылка вне топика Reels монтаж не утаскивает",
          _resolve("посмотри https://youtu.be/abc и напиши пост",
                   "review", {}).role == "editor",
          _resolve("посмотри https://youtu.be/abc и напиши пост",
                   "review", {}).role)

    # ── 38в. перечитывание профиля дубль не перехватывает ─────────────
    #
    # `resolve` разводил ссылку правильно, а человек всё равно получал
    # Ресёрчера: `refresh.wants_refresh` видит **любую** ссылку и стоит в
    # `handlers` выше маршрутизации. Живой прогон 04.09, ссылка на эфир в
    # топике Reels уехала перечитывать профиль. Стережёт `is_footage`, и
    # проверяется здесь тот же порядок, что в обработчике.
    print("\n38в. Дубль важнее перечитывания профиля")
    from bots.router import is_footage                          # noqa: E402
    from orchestrator import sources                            # noqa: E402

    for ask in ("https://youtu.be/abc",
                "нарежь https://www.youtube.com/watch?v=abc на рилсы"):
        seen = bool(sources.extract_urls(ask))
        check(f"ссылка видна перечитыванию: {ask[:30]}", seen, ask)
        check(f"но дубль её забирает: {ask[:30]}",
              is_footage(ask, "reels"), ask)
    check("ссылка не в Reels остаётся перечитыванию",
          not is_footage("посмотри https://youtu.be/abc", "review"),
          "review")
    check("текст без ссылки дублем не считается",
          not is_footage("смонтируй уже", "reels"), "reels")

    # ── 39. референс обложки называется, когда ТЗ нет ─────────────────
    print("\n39. Референс обложки")
    _, note = montage._cover_spec(b, "telegram", "reels")
    check("отсутствие ТЗ названо", note and "ТЗ обложки нет" in note, str(note))
    check("сверить не с чем — сказано",
          note and "сверить не с чем" in note, str(note))
    _, note = montage._cover_spec(b, "instagram", "reels")
    check("при живом ТЗ лишнего не говорится", note is None, str(note))

    # ── 40. рендер сторожится движением, а не секундомером ────────────
    #
    # Ночь на 05.09: пять готовых роликов убиты за секунду до выхода
    # процесса, потому что бюджет секунд на кадр считался по замеру
    # двухнедельной давности, а машина в ту ночь была медленнее. Remotion
    # сюда не зовётся — сторож проверяется на подставных командах, как и
    # вся арифметика этого цикла.
    print("\n40. Сторож рендера")
    import sys as _sys                                            # noqa: E402
    PY = _sys.executable

    async def _watch(cmd, **kw):
        return await montage._watch([PY, "-c", cmd], ".", **kw)

    code, last, err = asyncio.run(_watch(
        "for i in range(1, 4): print(f'Rendered {i}/3', flush=True)",
        cap=30, stall=10))
    check("рендер, который дошёл до конца, не обрывается", code == 0, str(code))
    check("последний кадр виден", last == "Rendered 3/3", last)

    code, _, err = asyncio.run(_watch(
        "import sys; print('boom', file=sys.stderr); sys.exit(3)",
        cap=30, stall=10))
    check("чужая ошибка доезжает строкой", code == 3 and "boom" in err,
          f"{code} / {err}")

    # Медленный рендер это не мёртвый: пока идут кадры, его не трогают.
    slow = ("import time\n"
            "for i in range(1, 7):\n"
            "    print(f'Rendered {i}/6', flush=True)\n"
            "    time.sleep(0.4)\n")
    code, last, _ = asyncio.run(_watch(slow, cap=30, stall=2))
    check("медленный, но живой рендер доводится до конца",
          code == 0 and last == "Rendered 6/6", f"{code} / {last}")

    # А молчание это смерть, и внуки умирают вместе с ним: `npx` тут
    # обёртка, работают под ней node и пул браузеров.
    dead = ("import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(600)'])\n"
            "print('Rendered 5/100', flush=True)\n"
            "time.sleep(600)\n")
    try:
        asyncio.run(_watch(dead, cap=60, stall=2))
        check("молчащий рендер обрывается", False, "не оборвался")
    except montage.NoRenderer as e:
        check("молчащий рендер обрывается", "встал" in str(e), str(e))
        check("сказано, на каком кадре встал", "до 5 кадра из 100" in str(e),
              str(e))

    # Потолок остаётся предохранителем: кадры идут, конца не видно.
    endless = ("import time\n"
               "n = 0\n"
               "while True:\n"
               "    n += 1\n"
               "    print(f'Rendered {n}/999999', flush=True)\n"
               "    time.sleep(0.1)\n")
    try:
        asyncio.run(_watch(endless, cap=3, stall=30))
        check("бесконечный рендер обрывается потолком", False, "не оборвался")
    except montage.NoRenderer as e:
        check("бесконечный рендер обрывается потолком",
              "не уложился" in str(e), str(e))

    check("длинному ролику потолок растёт",
          montage._cap(20000) > montage._cap(2000), str(montage._cap(20000)))
    check("короткому ролику потолок не режется",
          montage._cap(1) == montage.RENDER_CAP_MIN, str(montage._cap(1)))


main()
raise SystemExit(report())
