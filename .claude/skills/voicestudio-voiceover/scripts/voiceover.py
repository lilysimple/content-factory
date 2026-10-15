#!/usr/bin/env python3
"""Озвучивает видео по тексту голосом бренда.

На входе видео и текст, на выходе то же видео с новой звуковой дорожкой.
Синтез идёт в локальном VoiceStudio (`127.0.0.1:3900`), наружу ничего не
уходит.

    ./.venv/bin/python .claude/skills/voicestudio-voiceover/scripts/voiceover.py \
        clip.mp4 --text script.md --voice voice/lily --out clip-voiced.mp4

Про укладку в длину. По умолчанию текст читается в своём темпе, и скрипт
честно называет расхождение с длиной картинки. Это сделано нарочно: у
движка есть поле `duration`, которым можно впихнуть любой текст в любой
хронометраж, но впихнутая вдвое речь звучит как скороговорка, и на слух
это хуже, чем подрезанная картинка. Хотите впихнуть — скажите `--fit`
явно, и скрипт предупредит, если натяжение больше четверти.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
BACKEND = "http://127.0.0.1:3900"
FFMPEG_DIR = next(iter(sorted(
    (ROOT / "tools" / "remotion-montage" / "node_modules" / "@remotion")
    .glob("compositor-*"))), None)

# Расхождение темпа, после которого речь перестаёт быть похожей на живую.
STRETCH_WARN = 0.25


def _ff(binary: str, *args: str, capture: bool = False) -> str:
    """Бандловый ffmpeg/ffprobe. cwd обязателен: dylib'ы лежат рядом и
    подтягиваются относительным путём."""
    if FFMPEG_DIR is None:
        sys.exit("Не найден бандловый ffmpeg в tools/remotion-montage/node_modules.")
    r = subprocess.run([str(FFMPEG_DIR / binary), "-hide_banner", "-v", "error", *args],
                       cwd=FFMPEG_DIR, capture_output=True)
    if r.returncode != 0:
        sys.exit(f"{binary} не справился: {r.stderr.decode()[:400]}")
    return r.stdout.decode().strip() if capture else ""


def _duration(path: Path) -> float:
    return float(_ff("ffprobe", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", str(path), capture=True))


def _alive() -> bool:
    try:
        with urllib.request.urlopen(f"{BACKEND}/health", timeout=10) as r:
            return json.load(r).get("status") == "ok"
    except Exception:
        return False


def _plain(text: str) -> str:
    """Суфлёрный текст ломается на строки под вдох, а движку нужна проза:
    перевод строки он читает как точку и делает лишнюю паузу. Блоки JSON и
    заголовки из `script.md` в озвучку не идут."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    out = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block or block.startswith("#"):
            continue
        out.append(" ".join(line.strip() for line in block.splitlines() if line.strip()))
    return "\n\n".join(out)


def _speak(text: str, ref_wav: Path, ref_text: str, dest: Path,
           duration: float | None, num_step: int, seed: int | None) -> None:
    """Зовёт /generate multipart. Через OpenAI-совместимый /v1/audio/speech
    это не работает: он не находит расшифровку референса и падает 500.
    Профиль, заведённый в приложении, расшифровку движку тоже не отдаёт —
    поэтому референс и текст кладутся в каждый вызов."""
    boundary = "----voiceover"
    fields: list[tuple[str, str]] = [
        ("text", text), ("ref_text", ref_text), ("language", "Russian"),
        ("num_step", str(num_step)),
    ]
    if duration is not None:
        fields.append(("duration", f"{duration:.2f}"))
    if seed is not None:
        fields.append(("seed", str(seed)))

    body = b""
    for name, value in fields:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                 f"{value}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"ref_audio\"; "
             f"filename=\"ref.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode()
    body += ref_wav.read_bytes() + b"\r\n" + f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{BACKEND}/generate", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        # Счёт идёт примерно восьмикратно медленнее реального времени,
        # поэтому потолок щедрый: минута речи это около восьми минут счёта.
        with urllib.request.urlopen(req, timeout=3600) as r:
            dest.write_bytes(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500]
        if "clone_ref_too_long" in detail:
            sys.exit("Референс длиннее 20 секунд. Пересоберите его make_ref.py: "
                     "с приложенной расшифровкой движок берёт 3–10 секунд.")
        sys.exit(f"Движок отказал ({e.code}): {detail}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Озвучка видео по тексту голосом бренда")
    ap.add_argument("video", help="видео, которое озвучиваем")
    ap.add_argument("--text", required=True, help="файл с текстом или сам текст")
    ap.add_argument("--voice", required=True,
                    help="путь без расширения к паре референса: рядом .wav и .txt "
                         "(делает make_ref.py)")
    ap.add_argument("--out", help="куда класть; по умолчанию рядом с исходным, -voiced")
    ap.add_argument("--fit", action="store_true",
                    help="уложить речь ровно в длину видео, пожертвовав темпом")
    ap.add_argument("--keep-original", type=float, metavar="GAIN", default=None,
                    help="оставить исходный звук фоном с этой громкостью, например 0.12")
    ap.add_argument("--num-step", type=int, default=16,
                    help="шагов движка; 32 даёт качество выше ценой вдвое большего счёта")
    ap.add_argument("--seed", type=int, default=None,
                    help="зерно: с ним повтор вызова даёт ту же читку")
    args = ap.parse_args()

    if not _alive():
        sys.exit("VoiceStudio не отвечает на 127.0.0.1:3900.\n"
                 "Запустите приложение: open -a VoiceStudio — и дайте бэкенду подняться "
                 "(первый запуск после перезагрузки занимает около минуты).")

    video = Path(args.video)
    if not video.exists():
        sys.exit(f"Нет файла: {video}")

    ref_wav = Path(args.voice).with_suffix(".wav")
    ref_txt = Path(args.voice).with_suffix(".txt")
    if not ref_wav.exists() or not ref_txt.exists():
        sys.exit(f"Нет пары референса {ref_wav} и {ref_txt}. Снимите голос:\n"
                 f"  make_ref.py <дубль> --out {args.voice}")

    src = Path(args.text)
    text = _plain(src.read_text(encoding="utf-8") if src.exists() else args.text)
    if not text:
        sys.exit("После чистки текста не осталось ничего, что можно прочитать.")

    out = Path(args.out) if args.out else video.with_name(f"{video.stem}-voiced{video.suffix}")
    speech = out.with_name(f".{out.stem}-speech.wav")
    video_len = _duration(video)

    print(f"Картинка {video_len:.1f} с, в тексте {len(text)} знаков. Считаю озвучку "
          f"(примерно {len(text) / 15 * 8 / 60:.0f} мин, движок медленнее реального времени)…")

    _speak(text, ref_wav, ref_txt.read_text(encoding="utf-8").strip(), speech,
           video_len if args.fit else None, args.num_step, args.seed)

    speech_len = _duration(speech)
    stretch = abs(speech_len - video_len) / video_len
    print(f"Озвучка {speech_len:.1f} с против картинки {video_len:.1f} с "
          f"({'длиннее' if speech_len > video_len else 'короче'} на {stretch:.0%}).")
    if args.fit and stretch > STRETCH_WARN:
        print("  Текст пришлось натянуть больше чем на четверть — послушайте, "
              "не стало ли скороговоркой.")
    if not args.fit and stretch > STRETCH_WARN:
        print("  Расхождение заметное. Либо подрежьте картинку, либо сократите текст, "
              "либо позовите заново с --fit.")

    # Собираем дорожку обратно. Картинку не перекодируем: она уже отрендерена,
    # второй проход только теряет качество и время.
    mix = ["-map", "0:v:0", "-map", "1:a:0"]
    filt: list[str] = []
    if args.keep_original is not None:
        filt = ["-filter_complex",
                f"[0:a]volume={args.keep_original}[bg];[bg][1:a]amix=inputs=2:duration=longest[a]"]
        mix = ["-map", "0:v:0", "-map", "[a]"]
    _ff("ffmpeg", "-i", str(video), "-i", str(speech), *filt, *mix,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-y", str(out))
    speech.unlink(missing_ok=True)

    print(f"\nГотово: {out}")
    print("Послушайте прежде, чем публиковать: клон ошибается на числах и "
          "на латинских названиях, а поймать это может только ухо.")


if __name__ == "__main__":
    main()
