#!/usr/bin/env python3
"""Снимает референс голоса из снятого дубля: кусок 3–10 секунд и его
расшифровку.

Почему так, а не «отдай движку весь файл»: OmniVoice принимает референс с
приложенной расшифровкой только до 20 секунд, а работает хорошо на 3–10.
Сам он расшифровать кусок не может — для этого нужна его ASR-модель,
которой у завода нет и ставить её незачем: `faster-whisper` уже стоит в
venv и им же работает монтаж.

Расшифровка обязана совпадать со звуком дословно, поэтому кусок режется не
по секундам, а по паузе между словами: граница в середине слова даёт на
выходе клон, который глотает окончания.

    ./.venv/bin/python .claude/skills/voicestudio-voiceover/scripts/make_ref.py \
        incoming/pending.mov --out voice/lily

Кладёт рядом `lily.wav` и `lily.txt`. Этой парой дальше работает speak.py.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
# Бандловый ffmpeg из Remotion: системного на машине может не быть. Его
# dylib'ы подтягиваются относительным путём, поэтому запускать только с
# cwd внутри папки компоновщика — иначе dyld не найдёт libavdevice.
FFMPEG_DIR = next(iter(sorted(
    (ROOT / "tools" / "remotion-montage" / "node_modules" / "@remotion")
    .glob("compositor-*"))), None)


def _ffmpeg(*args: str) -> None:
    if FFMPEG_DIR is None:
        sys.exit("Не найден бандловый ffmpeg в tools/remotion-montage/node_modules.")
    r = subprocess.run([str(FFMPEG_DIR / "ffmpeg"), "-hide_banner", "-v", "error", *args],
                       cwd=FFMPEG_DIR, capture_output=True)
    if r.returncode != 0:
        sys.exit(f"ffmpeg не справился: {r.stderr.decode()[:400]}")


def _clean(text: str) -> str:
    """Убирает артефакты токенизации whisper, из-за которых расшифровка
    расходится со звуком на письме, хотя на слух совпадает."""
    text = re.sub(r"\s+-\s*(то|либо|нибудь|ка)\b", r"-\1", text)
    return text.strip().rstrip(" ,.—-")


def main() -> None:
    ap = argparse.ArgumentParser(description="Референс голоса из дубля")
    ap.add_argument("source", help="видео или аудио, где человек говорит один")
    ap.add_argument("--out", required=True,
                    help="путь без расширения: рядом лягут .wav и .txt")
    ap.add_argument("--skip", type=float, default=20.0,
                    help="сколько секунд пропустить с начала (по умолчанию 20: "
                         "в начале дубля обычно раскачка и шум установки)")
    ap.add_argument("--target", type=float, default=9.0,
                    help="желаемая длина куска в секундах, 3–10")
    ap.add_argument("--model", default="small",
                    help="модель whisper; ниже small русский разваливается на слоги")
    args = ap.parse_args()

    if not 3.0 <= args.target <= 10.0:
        sys.exit("Длина куска должна быть от 3 до 10 секунд: движок отказывает длиннее 20, "
                 "а на коротком клон стабильнее.")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = out.with_suffix(".raw.wav")

    # Берём запас вдвое длиннее цели: внутри него ищем границу по паузе.
    _ffmpeg("-ss", str(args.skip), "-t", str(args.target * 2 + 6),
            "-i", args.source, "-vn", "-ac", "1", "-ar", "24000",
            "-c:a", "pcm_s16le", "-y", str(raw))

    from faster_whisper import WhisperModel  # тяжёлый импорт, лениво
    wm = WhisperModel(args.model, device="cpu", compute_type="int8")
    segs, _ = wm.transcribe(str(raw), language="ru",
                            word_timestamps=True, vad_filter=True)
    words = [w for s in segs for w in s.words]
    if not words:
        sys.exit("В этом куске не слышно речи. Возьмите другой отрезок через --skip.")

    # Граница — самая длинная пауза в окне вокруг цели. Режем по ней, а не
    # по ровной секунде: обрыв на середине слова портит клон.
    lo, hi = args.target * 0.65, args.target * 1.15
    best = None
    for a, b in zip(words, words[1:]):
        if lo <= a.end <= hi:
            gap = b.start - a.end
            if best is None or gap > best[0]:
                best = (gap, a.end)
    if best is None:
        best = (0.0, min(words[-1].end, args.target))
    gap, cut = best

    keep = [w for w in words if w.end <= cut]
    text = _clean(" ".join(w.word.strip() for w in keep))

    _ffmpeg("-i", str(raw), "-t", f"{cut + gap / 2:.3f}", "-c", "copy",
            "-y", str(out.with_suffix(".wav")))
    out.with_suffix(".txt").write_text(text + "\n", encoding="utf-8")
    raw.unlink(missing_ok=True)

    print(f"Референс: {out.with_suffix('.wav')} — {cut + gap / 2:.1f} с, "
          f"пауза на границе {gap:.2f} с")
    print(f"Расшифровка: {text}")
    print("\nСверьте расшифровку со звуком на слух: она должна совпадать дословно. "
          "Разошлась — поправьте .txt руками, это дешевле пересъёмки.")


if __name__ == "__main__":
    main()
