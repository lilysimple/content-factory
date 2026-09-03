"""Цикл 22: живой монтаж — whisper и Remotion на этой машине.

Цикл 19 считает арифметику монтажа и намеренно не трогает тяжёлые
проходы. Здесь ровно они: настоящая расшифровка речи и настоящий рендер
через Remotion. Всё, что ниже, ни разу не проверялось стендом, а
ломается тише всего — бинарник не тот, модель не скачалась, кодек не
собрался, и человек узнаёт об этом на своём отснятом дубле.

Дубль синтезируем: `say` начитывает текст голосом Milena, бандловый
ffmpeg из Remotion склеивает речь с фотографией бренда. Камера тут не
нужна — проверяется путь, а не композиция кадра.

Прогон идёт минуты и в офлайн-стенд не входит: `tests/run.py --live`.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import harness
from harness import CHAT, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import montage                                  # noqa: E402
from storage import db                                            # noqa: E402
from storage.brand import Brand                                   # noqa: E402

db.init(cfg.db_path)

TID = "2026-09-03-instagram-01"

SPEECH = ("Я перестала объяснять себя заново в каждом новом чате. "
          "Теперь весь контекст лежит в одном файле, я называю его ядро. "
          "Клод код открывает его сам, а ролик собирает ремоушен.")


def _brand() -> Brand:
    return Brand("lily-space", harness.TMP / "brands" / "lily-space")


def _script(b) -> None:
    """Сценарий на диске в том виде, в каком его пишет Редактор Reels."""
    b.artifact(f"posts/{TID}-script.md",
               f"<!-- {TID} · instagram · reels · 18 сек · 42 слов -->\n\n"
               + SPEECH + "\n")
    b.artifact(f"posts/{TID}-script-notes.md", "\n".join([
        f"# Разбор сценария {TID}", "",
        "Одна мысль: контекст живёт в одном файле, а не в каждом чате.", "",
        "Хронометраж 18 сек, слов 42.", "",
        "## Блоки", "",
        "### Хук · 0–3 · 9 слов", "",
        "Я перестала объяснять себя заново в каждом новом чате.", "",
        "### Суть · 3–14 · 24 слов", "",
        "Теперь весь контекст лежит в одном файле, я называю его ядро.", "",
        "### CTA · 14–18 · 9 слов", "",
        "Соберите свой файл сегодня.", "",
        "## Запасные хуки", "", "- нет", "",
    ]))


def _seed(b) -> None:
    _script(b)
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE chat_id = ?", (CHAT,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                  "status, title, hook, asset) VALUES "
                  "(?,?,?,'instagram','reels','ready',?,?,?)",
                  (TID, CHAT, "2026-09-03", "Файл ЯДРО",
                   "Я перестала объяснять себя заново",
                   f"posts/{TID}-script.md"))


def _dub(b) -> Path | None:
    """Синтетический дубль: речь Milena поверх фотографии бренда."""
    if not shutil.which("say"):
        return None
    photo = b.path("design/assets/images/author.jpg")
    if not photo.exists():
        return None

    # WAV, а не aiff по умолчанию: бандловый ffmpeg из Remotion собран
    # урезанным и aiff от `say` не открывает вовсе.
    wav = harness.TMP / "speech.wav"
    subprocess.run(["say", "-v", "Milena", "-o", str(wav),
                    "--data-format=LEI16@22050", SPEECH],
                   check=True, capture_output=True)

    out = montage.incoming_dir(b) / "pending.mp4"
    subprocess.run([
        str(montage.footage.FFMPEG), "ffmpeg", "-hide_banner", "-y",
        "-loglevel", "error",
        "-loop", "1", "-framerate", "30", "-i", str(photo),
        "-i", str(wav),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-vf", "scale=1080:-2", "-c:a", "aac", "-shortest", str(out),
    ], check=True, capture_output=True)
    return out


async def main() -> None:
    b = _brand()
    _seed(b)

    print("\n1. Дубль")
    dub = _dub(b)
    if dub is None:
        check("дубль собран", False, "нет `say` или фотографии бренда")
        return
    check("дубль собран", dub.exists() and dub.stat().st_size > 0,
          f"{dub.stat().st_size} байт")

    print("\n2. Словарь субтитров")
    rules = montage.lexicon(b)
    check("словарь прочитан без падения", isinstance(rules, list),
          str(type(rules)))
    if not rules:
        print("     · у бренда нет montage/subtitles.md — имена поедут "
              "так, как слышно")

    print("\n3. Монтаж целиком")
    said: list[str] = []
    reel = await montage.build(CHAT, TID, say=lambda t: said.append(t) or
                               asyncio.sleep(0))
    check("ролик отрендерен", reel.out is not None and reel.out.exists(),
          str(reel.out))
    check("файл не пустой", reel.out.stat().st_size > 100_000,
          f"{reel.out.stat().st_size} байт")
    check("сказано, что монтируем", any("Монтирую" in s for s in said),
          str(said[:1]))

    print("\n4. Расшифровка")
    words = [w["text"] for p in reel.pages for w in p["words"]]
    check("речь расслышана", len(words) > 10, f"{len(words)} слов")
    check("караоке разбито на страницы", len(reel.pages) > 1,
          f"{len(reel.pages)} страниц")
    check("страницы идут по возрастанию времени",
          all(p["start"] <= p["end"] for p in reel.pages)
          and all(a["end"] <= b["start"] + 0.01
                  for a, b in zip(reel.pages, reel.pages[1:])),
          str([(round(p["start"], 2), round(p["end"], 2))
               for p in reel.pages[:4]]))
    body = reel.cuts.total if reel.cuts else 0.0
    check("субтитры укладываются в отснятое",
          reel.pages[-1]["end"] <= body + 1.0,
          f'{reel.pages[-1]["end"]:.2f} при {body:.2f} с речи')
    print("     · услышано: " + " ".join(words[:14]))

    print("\n5. Что не сошлось")
    for f in reel.findings:
        print(f"     · {f}")
    check("роль назвала расхождения списком", isinstance(reel.findings, list))


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(report())
