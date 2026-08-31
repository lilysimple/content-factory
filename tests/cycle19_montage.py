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

import harness
from harness import CHAT, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import footage, grab, montage                   # noqa: E402
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
    cut = footage.cut_words(words, tl)
    check("слово из паузы выброшено", len(cut) == 2,
          " ".join(str(w["text"]) for w in cut))
    check("слово после паузы сдвинуто",
          abs(float(cut[1]["start"]) - 7.4) < 0.01, str(cut[1]))

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
    frag = montage.reels.Fragment(0.0, 30.0, "хук куска", "заголовок", "зачем")
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


main()
raise SystemExit(report())
