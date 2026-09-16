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
import shutil

import harness
from harness import CHAT, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import (agent, album, cut, desk, footage, grab,      # noqa: E402
                          montage, panelshot)
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

    print("\n9б. Обложка Дизайнера идёт первым кадром целиком")
    # Она уже свёрстана: фото из фотобанка, рубрика, заголовок. Свои
    # строки монтаж клал бы поверх чужих слов — до 07.09 так и было.
    png("reel-01-cover.png", 1080, 1920)
    dressed = montage.Reel(
        theme={"id": "reel-01", "plat": "instagram", "format": "reels"},
        video=posts / "нет.mov", hook="Я перестала писать посты руками")
    asyncio.run(montage._intro(dressed, _brand(), (1080, 1920)))
    check("обложка взята", dressed.cover is not None
          and dressed.cover.name == "reel-01-cover.png", str(dressed.cover))
    check("своих строк на неё не кладём", dressed.lines == [],
          str(dressed.lines))
    check("и заголовка тоже", dressed.dressed)
    check("человеку сказано, чья обложка",
          any("Дизайнер" in f for f in dressed.findings),
          str(dressed.findings))

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


    # ── 41. сплит ─────────────────────────────────────────────────────
    print("\n41. Панель сплита привязана к речи, а не к таймеру")
    words = [{"text": "Просто", "start": 1.0, "end": 1.4},
             {"text": "опиши,", "start": 1.5, "end": 2.0},
             {"text": "что", "start": 2.1, "end": 2.3},
             {"text": "нужно", "start": 2.4, "end": 2.9},
             {"text": "—", "start": 3.0, "end": 3.05},
             {"text": "опиши", "start": 3.1, "end": 3.5}]
    check("фраза находит свою секунду",
          footage.anchor(words, "опиши что") == 1.5,
          str(footage.anchor(words, "опиши что")))
    check("знак препинания поиску не мешает",
          footage.anchor(words, "нужно") == 2.4, str(footage.anchor(words, "нужно")))
    check("берётся первое вхождение, а не последнее",
          footage.anchor(words, "опиши") == 1.5, str(footage.anchor(words, "опиши")))
    check("несказанного нет", footage.anchor(words, "ремоушен") is None)
    check("дубль без расшифровки не ломает поиск",
          footage.anchor([], "опиши") is None)

    print("\n42. Props сплита собираются кодом")
    split_reel = montage.Reel(theme={"id": "t-split"},
                              video=harness.TMP / "нет.mp4",
                              color="#1F1F1F", accent="#C6DE48",
                              text="#F8F5F1", cta="Подписывайтесь")
    split_reel.probe = footage.Probe(duration=20.0, width=1920, height=1080,
                                     fps=30.0, has_audio=True)
    split_reel.cuts = footage.timeline(20.0, [(8.0, 11.0)])
    split_reel.subs = words
    shot = harness.TMP / "panel.png"
    shot.write_bytes(b"png")
    blocks = [
        montage.Block(start=0.0, end=3.0, kicker="БЕЗ КОДА", full=True,
                      lines=["Google Opal"]),
        montage.Block(start=3.0, end=9.0, image=shot,
                      items=[("опиши что", montage.speak_at(split_reel, "опиши что")),
                             ("этого не говорили", None)]),
    ]
    props = montage.motion_props(split_reel, blocks, (1080, 1920))

    check("композиция получает имя дубля, а не путь",
          props["videoPath"] == "input-t-split.mp4", props["videoPath"])
    check("куски те же, что у ролика",
          len(props["segments"]) == len(split_reel.cuts.keep),
          str(props["segments"]))
    check("скрин панели назван по номеру блока",
          props["blocks"][1]["imagePath"] == "panel-t-split-1.png",
          str(props["blocks"][1]))
    check("блок без картинки её и не получает",
          "imagePath" not in props["blocks"][0], str(props["blocks"][0]))
    # Дефолты схемы Remotion подставляет в props целиком, а не внутрь
    # элементов массива: пропущенный список приезжал `undefined`, и
    # рендер падал на первом же блоке без пунктов.
    check("пустой список едет пустым списком, а не пропадает",
          props["blocks"][0]["items"] == [], str(props["blocks"][0]))
    check("пункт списка встаёт на секунду своего слова",
          props["blocks"][1]["items"][0]["at"] == 1.5,
          str(props["blocks"][1]["items"]))
    check("несказанный пункт едет с началом блока, а не пропадает",
          props["blocks"][1]["items"][1]["at"] == 3.0,
          str(props["blocks"][1]["items"]))
    check("панель говорит цветами бренда",
          props["panelColor"] == "#1F1F1F" and props["accentColor"] == "#C6DE48"
          and props["textColor"] == "#F8F5F1", str(props["panelColor"]))
    check("свечение взято прозрачностью от акцента",
          props["glowColor"] == "rgba(198, 222, 72, 0.13)", props["glowColor"])
    check("аутро есть только под CTA",
          props["outroSeconds"] == 1.8
          and montage.motion_props(
              montage.Reel(theme={"id": "t2"}, video=harness.TMP / "нет.mp4",
                           probe=split_reel.probe, cuts=split_reel.cuts),
              [], (1080, 1920))["outroSeconds"] == 0.0,
          str(props["outroSeconds"]))

    try:
        montage.motion_props(split_reel, blocks, (1080, 1920), head="сбоку")
        check("голова бывает только сверху или снизу", False, "прошло")
    except ValueError as e:
        check("голова бывает только сверху или снизу", "сбоку" in str(e), str(e))

    check("кривой цвет не режется молча на куски",
          montage._rgba("#12345", 0.1) == "#12345", montage._rgba("#12345", 0.1))

    print("\n43. Блок говорит, чем он занят")
    rich = [montage.Block(start=0.0, end=4.0, kind="counter", value=3,
                          value_at=1.5, lines=["бизнес-модели"]),
            montage.Block(start=4.0, end=8.0, kind="scale",
                          items=[("low — быстро", 4.4),
                                 ("high — рассуждение", 6.2)]),
            montage.Block(start=8.0, end=9.0, kind="counter", value=5)]
    rp = montage.motion_props(split_reel, rich, (1080, 1920))["blocks"]
    check("тип блока доезжает до шаблона",
          [b["kind"] for b in rp] == ["counter", "scale", "counter"], str(rp))
    check("счётчик набирается на своём слове",
          rp[0]["value"] == 3 and rp[0]["valueAt"] == 1.5, str(rp[0]))
    check("счётчик без слова набирается с началом блока",
          rp[2]["valueAt"] == 8.0, str(rp[2]))
    check("обычный блок остаётся карточкой",
          props["blocks"][0]["kind"] == "card", str(props["blocks"][0]["kind"]))
    try:
        montage.motion_props(split_reel,
                             [montage.Block(start=0.0, end=1.0, kind="диаграмма")],
                             (1080, 1920))
        check("неизвестный тип блока — отказ, а не молчание", False, "прошло")
    except ValueError as e:
        check("неизвестный тип блока — отказ, а не молчание",
              "диаграмма" in str(e), str(e))


def panel() -> None:
    """44–45. Блоки панели: отбор у роли и посадка на слова."""
    print("\n44. Роль отдаёт блоки, код отбирает")
    raw = [
        {"phrase": "просто опиши что нужно", "kind": "card",
         "lines": ["Опиши задачу словами"], "items": ["первое", "второе"]},
        {"phrase": "", "kind": "card", "lines": ["без фразы"]},
        {"phrase": "три недели", "kind": "counter"},
        {"phrase": "три недели", "kind": "counter", "value": 3},
        {"phrase": "низкий уровень", "kind": "scale",
         "items": ["low — быстро"]},
        {"phrase": "низкий уровень", "kind": "scale",
         "items": ["low — быстро", "high — рассуждение"]},
        {"phrase": "пустая карточка", "kind": "card"},
        {"phrase": "диаграмма", "kind": "диаграмма", "lines": ["что-то"]},
        {"phrase": "первый разгон", "kind": "icon", "lines": ["Opal"],
         "full": True},
        {"phrase": "второй разгон", "kind": "icon", "lines": ["Claude"],
         "full": True},
    ]
    good, lost = cut._slides(raw)
    kinds = [(s.kind, s.phrase[:12]) for s in good]
    check("взяты только собранные блоки", len(good) == 5, str(kinds))
    check("блок без фразы отброшен: ставить не на что",
          any("без фразы" in n for n in lost), str(lost))
    check("счётчик без числа отброшен",
          any("счётчик без числа" in n for n in lost), str(lost))
    check("шкала из одной ступени отброшена",
          any("ступеней в шкале" in n for n in lost), str(lost))
    check("пустая карточка отброшена",
          any("пустая карточка" in n for n in lost), str(lost))
    check("выдуманный сорт блока отброшен, а не нарисован как карточка",
          any("не бывает" in n for n in lost), str(lost))
    # Второй блок на весь кадр это не разгон, а мигание: дубль уходит и
    # возвращается несколько раз за ролик.
    full = [s.full for s in good if s.kind == "icon"]
    check("на весь кадр остаётся один блок, второй остаётся на половине",
          full == [True, False], str(full))
    check("отобранное человеку названо, а не выброшено молча",
          len(lost) >= 5, str(lost))

    print("\n45. Блок встаёт на слово, а не на секунду")
    reel = montage.Reel(theme={"id": "t-lay"}, video=harness.TMP / "нет.mp4")
    reel.probe = footage.Probe(duration=20.0, width=1920, height=1080,
                               fps=30.0, has_audio=True)
    reel.cuts = footage.timeline(20.0, [])
    reel.subs = [{"text": "Просто", "start": 1.0, "end": 1.4},
                 {"text": "опиши,", "start": 1.5, "end": 2.0},
                 {"text": "что", "start": 2.1, "end": 2.3},
                 {"text": "нужно", "start": 2.4, "end": 2.9},
                 {"text": "за", "start": 9.0, "end": 9.2},
                 {"text": "три", "start": 9.3, "end": 9.6},
                 {"text": "недели", "start": 9.7, "end": 10.2},
                 {"text": "первое", "start": 12.0, "end": 12.4}]

    slides = [cut.Slide(phrase="опиши что", kind="card",
                        lines=["Опиши задачу"], items=["первое", "второго"]),
              cut.Slide(phrase="три недели", kind="counter", value=3,
                        value_phrase="три недели"),
              cut.Slide(phrase="этого никто не говорил", kind="card",
                        lines=["мимо"])]
    blocks, lost = montage.lay(reel, slides)

    check("блок встал на секунду своей фразы", len(blocks) == 2
          and blocks[1].start == 9.3, str([(b.start, b.end) for b in blocks]))
    check("первый блок тянется к нулю: пустая панель в начале — поломка",
          blocks[0].start == 0.0, str(blocks[0].start))
    check("конец блока это начало следующего",
          blocks[0].end == blocks[1].start, str(blocks[0].end))
    check("последний блок живёт до конца ролика",
          blocks[1].end == reel.cuts.total, str(blocks[1].end))
    check("ненайденная фраза выбрасывает блок и называет его",
          any("не нашлось" in n for n in lost), str(lost))
    check("ненайденный пункт остаётся без секунды, а не пропадает",
          blocks[0].items[1] == ("второго", None), str(blocks[0].items))
    # Шаблон отсчитывает пункт от начала блока: сказанный после его
    # конца не появился бы на панели вовсе.
    check("пункт, сказанный вне своего блока, едет с карточкой и назван",
          blocks[0].items[0] == ("первое", None)
          and any("вне своего блока" in n for n in lost), str(lost))
    check("счётчик набирается на своём слове",
          blocks[1].value_at == 9.3, str(blocks[1].value_at))

    # Порядок в ответе роли это её обещание, а не факт записи.
    shuffled = montage.lay(reel, [slides[1], slides[0]])[0]
    check("блоки идут по речи, а в каком порядке их назвали — неважно",
          [b.start for b in shuffled] == [0.0, 9.3],
          str([b.start for b in shuffled]))

    # Блок, которому сосед не оставил времени, нарисовать нельзя.
    tight = montage.lay(reel, [cut.Slide(phrase="опиши что", lines=["раз"]),
                               cut.Slide(phrase="нужно", lines=["два"])])
    check("блок короче потолка выброшен и назван",
          len(tight[0]) == 1 and any("короче" in n for n in tight[1]),
          str(tight[1]))

    print("\n45а. Картинки панели: адрес от роли, знак и скрин от кода")
    pics, pic_lost = cut._slides([
        {"phrase": "назвал супабейс", "kind": "icon", "lines": ["Supabase"],
         "url": "supabase.com"},
        {"phrase": "адрес внутрь сети", "kind": "card", "lines": ["мимо"],
         "url": "http://localhost:8000/admin"},
        {"phrase": "адрес у счётчика", "kind": "counter", "value": 5,
         "url": "https://example.com"},
    ])
    check("адрес продукта доезжает до слайда",
          pics[0].url == "supabase.com", str(pics[0]))
    check("адрес внутрь сети не пускается в Chrome и назван",
          pics[1].url == "" and any("не годится" in n for n in pic_lost),
          str(pic_lost))
    check("у счётчика адреса не бывает — рисовать некуда",
          pics[2].url == "", str(pics[2]))
    check("голый хост становится https-адресом",
          panelshot.safe_url("supabase.com") == "https://supabase.com/",
          str(panelshot.safe_url("supabase.com")))
    check("file://, IP и .local отбрасываются",
          not any(panelshot.safe_url(u) for u in
                  ("file:///etc/passwd", "http://192.168.1.5/", "printer.local",
                   "javascript:alert(1)")), "guard")

    html = """<head>
      <link rel="icon" href="/favicon.ico">
      <link rel="icon" sizes="32x32" href="/f32.png">
      <link rel="apple-touch-icon" sizes="180x180" href="/apple.png">
      <link rel="icon" type="image/svg+xml" href="https://cdn.x.com/logo.svg">
    </head>"""
    cands = panelshot.logo_candidates(html, "https://x.com/pricing")
    check("вектор первым, apple-touch-icon следом, мелкий favicon и .ico мимо",
          cands[:2] == ["https://cdn.x.com/logo.svg", "https://x.com/apple.png"]
          and "https://x.com/f32.png" not in cands
          and not any(c.endswith(".ico") for c in cands), str(cands))
    check("необъявленный apple-touch-icon и сервис знаков — в хвосте",
          cands[-2] == "https://x.com/apple-touch-icon.png"
          and cands[-1].endswith("domain=x.com"), str(cands))

    logo = harness.TMP / "logo.png"
    logo.write_bytes(b"png")
    reel_pic = [cut.Slide(phrase="опиши что", kind="icon", lines=["Opal"],
                          url="opal.google", image=logo)]
    laid = montage.lay(reel, reel_pic)[0]
    check("картинка слайда переезжает на блок и переживает пересборку",
          laid and laid[0].image == logo, str(laid))

    print("\n45е. Обложка сплита")
    lines = montage.plate_lines("Почему ИИ агенты бесполезны и что делать вместо команды из семи")
    check("заголовок режется на плашки по словам и не шире потолка",
          len(lines) <= montage.PLATE_LINES
          and all(len(x) <= montage.PLATE_CHARS for x in lines[:-1]),
          str(lines))
    check("хвост заголовка не теряется",
          " ".join(lines).split() ==
          "Почему ИИ агенты бесполезны и что делать вместо команды из семи".split(),
          str(lines))

    class _CB:
        def __init__(self, text):
            self.text = text
        def read(self, rel):
            return self.text
    check("знак бренда по умолчанию читается из файла",
          montage.cover_default(_CB("# c\nlogo: claude.ai\nname: Claude\n"))
          == ("claude.ai", "Claude"), "cover.md")
    check("файла нет — знака нет, не поломка",
          montage.cover_default(_CB("")) == ("", ""), "пусто")

    creel = montage.Reel(theme={"id": "t-cov"}, video=harness.TMP / "x.mp4")
    check("без обложки первого кадра нет",
          montage._cover_props(creel) == {"introSeconds": 0.0}, "нет")
    creel.split_cover = {"still": harness.TMP / "s.png", "focus": (0.4, 0.3),
                         "logo": harness.TMP / "l.svg", "name": "Claude",
                         "lines": ["Почему ИИ агенты"], "accent": ["ИИ"]}
    cp = montage._cover_props(creel)
    check("обложка едет в props с кадром, знаком и первым кадром",
          cp["introSeconds"] == montage.COVER_SECONDS
          and cp["coverPath"] == "cover-t-cov.png"
          and cp["coverLogoPath"] == "cover-logo-t-cov.svg"
          and cp["coverFocus"] == {"x": 0.4, "y": 0.3}, str(cp))

    print("\n45д. Лицо в кадре с первых секунд")
    firsts, why_full = montage.lay(reel, [
        cut.Slide(phrase="опиши что", kind="art", art="a cube", full=True,
                  image=harness.TMP / "a.png"),
        cut.Slide(phrase="три недели", kind="card", lines=["слова"],
                  full=True)])
    check("первый блок на весь кадр встаёт на половину и назван",
          not firsts[0].full and any("первые секунды" in n or
                                     "первых секунд" in n for n in why_full),
          str(why_full))
    check("слова на весь кадр не разворачиваются",
          not firsts[1].full, str(firsts[1]))
    later = montage.lay(reel, [
        cut.Slide(phrase="опиши что", kind="card", lines=["раз"]),
        cut.Slide(phrase="три недели", kind="art", art="a cube", full=True,
                  image=harness.TMP / "a.png")])[0]
    check("картинка не первым блоком на весь кадр остаётся",
          later[1].full, str(later[1]))

    print("\n45г. Окно головы сплита стоит по лицу")
    # Вертикальный дубль 1080×1920 в окне 1080×1060: видна 0.552 высоты.
    face = footage.Face(x=0.35, y=0.15, w=0.3, h=0.17, conf=0.9)
    track = montage.head_track([(2.0, face), (9.0, face)],
                               (1080, 1920), (1080, 1920))
    seen = 1920 * (1 - montage.MOTION_SPLIT) / 1920
    top = track[0].y - seen / 2
    at = (face.cy - top) / seen
    check("центр лица встаёт на заданную долю окна, а не на середину",
          abs(at - montage.HEAD_AT) < 0.01, f"{at:.3f}")
    check("по ширине окно идёт за лицом", track[0].x == round(face.cx, 4),
          str(track[0]))
    check("лиц нет — трека нет, остаётся движение",
          montage.head_track([], (1080, 1920), (1080, 1920)) == [], "пусто")

    print("\n45в. Границы дубля не знают про панель")
    # Нашёл живой монтаж 15.09: блок про материалы альбома уехал заменой
    # строки и в промпт границ, где переменной `materials` нет, — границы
    # падали на каждом дубле, и ролик шёл целиком без хука.
    seen: dict[str, str] = {}

    class _Stop(Exception):
        pass

    async def _peek(role, chat_id, prompt, **kw):
        seen["prompt"] = prompt
        raise _Stop

    real_ask, agent.ask = agent.ask, _peek
    try:
        asyncio.run(cut.fragments(CHAT, [], 30.0, whole=True))
        err = "модель не позвана"
    except _Stop:
        err = ""
    except Exception as e:                                   # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    finally:
        agent.ask = real_ask
    check("границы дубля доходят до модели без ошибки", not err, err)
    check("в промпте границ нет разделов панели",
          "Материалы из альбома" not in seen.get("prompt", "")
          and "Картинки по теме" not in seen.get("prompt", ""),
          seen.get("prompt", "")[-200:])

    print("\n45б. Альбом к дублю и картинки по теме")
    blocks_raw = [
        {"phrase": "просто пишешь ему", "kind": "media", "media": 1},
        {"phrase": "материала нет", "kind": "media", "media": 4},
        {"phrase": "стеклянный куб", "kind": "art",
         "art": "a translucent glass cube", "lines": ["Куб"]},
        *[{"phrase": f"образ {i}", "kind": "art", "art": f"thing {i}"}
          for i in range(2, cut.ART_MAX + 1)],
        {"phrase": "четвёртый образ", "kind": "art", "art": "a lock",
         "lines": ["Замок"]},
        {"phrase": "без брифа", "kind": "art", "art": ""},
    ]
    got, why = cut._slides(blocks_raw, materials=2, art=True)
    by = {s.phrase: s for s in got}
    check("материал альбома встаёт своим номером",
          by["просто пишешь ему"].media == 1, str(got))
    check("номер, которого в альбоме нет, отброшен и назван",
          "материала нет" not in by and any("№4" in n for n in why), str(why))
    check("картинок по теме не больше потолка: лишняя со словами — карточка",
          sum(s.kind == "art" for s in got) == cut.ART_MAX
          and by["четвёртый образ"].kind == "card", str([s.kind for s in got]))
    check("картинка без брифа и без слов выпадает",
          "без брифа" not in by, str(why))
    nokey, why2 = cut._slides(blocks_raw[2:3], art=False)
    check("нет ключа Nano Banana — блок словами и это названо",
          nokey[0].kind == "card" and any("ключа" in n for n in why2),
          str(why2))
    check("стиль картинки задаёт код: цвета бренда и запрет текста",
          "#C6DE48" in panelshot.art_prompt("a cube.", "#1F1F1F", "#C6DE48")
          and "no text" in panelshot.art_prompt("a cube", "#1F1F1F", "#C6DE48"),
          "стиль")

    class _B:
        root = harness.TMP / "album-brand"
        def path(self, rel):
            return self.root / rel
    ab = _B()
    shutil.rmtree(ab.root, ignore_errors=True)
    # Сообщения альбома приходят вразнобой: порядок — номер сообщения.
    album.stage(ab, "g1", 12, b"img", ".JPG", "результат")
    take, fresh = album.stage(ab, "g1", 10, b"take", ".mp4")
    album.stage(ab, "g1", 11, b"rec", ".mov", "терминал")
    check("первый файл альбома открывает его, остальные — нет",
          not fresh, "fresh")
    check("дубль альбома — первое видео по номеру сообщения",
          album.takes(ab) == [take], str(album.takes(ab)))
    mats = album.materials(ab, take)
    check("материалы — остальное по порядку, с подписями",
          [(m.kind, m.caption) for m in mats]
          == [("video", "терминал"), ("image", "результат")], str(mats))
    _, fresh2 = album.stage(ab, "g2", 20, b"take2", ".mp4")
    check("новый альбом стирает прежний: чужие материалы на панель не едут",
          fresh2 and not take.exists(), "стёрт")
    check("дубль не из альбома материалов не получает",
          album.materials(ab, harness.TMP / "pending.mp4") == [], "пусто")

    vid = harness.TMP / "rec.mov"
    vid.write_bytes(b"rec")
    mblock = montage.Block(start=0.0, end=4.0, kind="media", video=vid,
                           video_len=9.5, media_size=(1920, 1080))
    mp = montage._block_props(mblock, "panel-t-0.mov")
    check("запись из альбома едет видео с длиной и мерками, а не картинкой",
          mp.get("videoPath") == "panel-t-0.mov" and "imagePath" not in mp
          and mp["videoSeconds"] == 9.5 and mp["mediaWidth"] == 1920, str(mp))

    print("\n46. Из режима правки выводит просьба смонтировать")
    # Режим правки стоит раньше маршрутизации: пока он взведён, топик
    # разбирает сообщение как замену слов, а неразобранная замена
    # взводит его снова. Без своего выхода это ловушка — «смонтируй
    # сплитом» получал справку про формат правки на каждый повтор.
    montage.table.await_fix(-777, reel)
    check("режим правки взведён", montage.wants_fix(-777))
    check("просьба смонтировать сильнее режима правки",
          montage.wants_new("смонтируй сплитом 2026-09-05-instagram-06"),
          "выход есть")
    check("замена слов правкой и остаётся",
          not montage.wants_new("клод -> Claude"), "не выход")
    montage.leave_fix(-777)
    check("выход снимает режим правки", not montage.wants_fix(-777))
    check("карточка при этом остаётся на столе: кнопки под ней живые",
          montage.table.get(-777) is reel, "стол")
    montage.table.clear(-777)

    # Сплит и нарезка живут в одном топике, и путать их нельзя: одна
    # просьба даёт ролик с панелью, другая — пачку роликов.
    check("сплит просят своими словами",
          montage.wants_motion("смонтируй сплитом")
          and montage.wants_motion("собери с панелью")
          and not montage.wants_motion("смонтируй"),
          "триггер сплита")
    check("нарезка сплитом не становится",
          not montage.wants_split("смонтируй сплитом")
          and not montage.wants_motion("нарежь на рилсы"),
          "триггеры не пересекаются")


main()
panel()
raise SystemExit(report())
