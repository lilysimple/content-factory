"""Видео по ссылке: скачать чужой файл на диск и забрать его субтитры.

Нужен ровно для одного: нарезка (`montage.split`) умеет работать только
с файлом на диске, а записи эфиров живут на YouTube. Скачиваем `yt-dlp`,
кладём рядом с профилем бренда — дальше монтаж не различает, откуда файл
взялся.

Субтитры забираем той же командой и не зря: у автоматических субтитров
YouTube есть **пословные** тайминги (формат `json3`), а это ровно то, что
нужно и для выбора кусков, и для караоке. Час эфира расшифровывается
локальным whisper минут пятнадцать; здесь то же самое приезжает вместе с
видео за секунды. Whisper остаётся для файлов, которые человек снял сам:
у них субтитров нет и взять их неоткуда.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from orchestrator.footage import FFMPEG_DIR, Word

log = logging.getLogger("grab")

URL_RX = re.compile(r"https?://\S+")
HOSTS = ("youtube.com", "youtu.be")

DOWNLOAD_TIMEOUT = 1800
MAX_HEIGHT = 1080          # выше не нужно: холст рилса всё равно 1080 по ширине


class NoVideo(RuntimeError):
    """Скачать не вышло."""


def link(text: str) -> str | None:
    """Ссылка на видео в просьбе человека, если она там есть."""
    for m in URL_RX.finditer(text or ""):
        url = m.group().rstrip(".,;)»")
        if any(h in url for h in HOSTS):
            return url
    return None


@dataclass
class Fetched:
    video: Path
    words: list[Word]
    title: str = ""


def _from_json3(raw: str) -> list[Word]:
    """Пословный транскрипт из автоматических субтитров YouTube.

    В `json3` каждое событие несёт куски `segs` со сдвигом от начала
    события — это и есть слова. Пустые куски (перевод строки, пробел)
    пропускаем: слово без букв караоке не нужно.
    """
    data = json.loads(raw)
    out: list[Word] = []
    for event in data.get("events") or []:
        base = float(event.get("tStartMs") or 0) / 1000
        for seg in event.get("segs") or []:
            text = str(seg.get("utf8") or "").strip()
            if not text:
                continue
            start = base + float(seg.get("tOffsetMs") or 0) / 1000
            if out and start < out[-1].start:      # накатывающиеся титры
                continue
            if out:
                out[-1] = Word(out[-1].text, out[-1].start,
                               min(out[-1].end, start))
            out.append(Word(text, start, start + 0.6))
    return out


async def fetch(url: str, dest: Path, *, lang: str = "ru") -> Fetched:
    """Скачать видео и его субтитры в папку `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("pending.*"):
        old.unlink(missing_ok=True)

    # Свести видео и звук в один файл yt-dlp может только ffmpeg-ом, а
    # системного на машине нет — и не должно быть. Отдаём ему тот же
    # бандловый, которым работает весь монтаж. Без этого он честно
    # скачивает две дорожки, не сводит их, и в папке остаётся звук без
    # картинки: «No video stream found».
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ffmpeg-location", str(FFMPEG_DIR),
        "-f", f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]/b",
        "--merge-output-format", "mp4",
        "--write-auto-subs", "--sub-langs", lang, "--sub-format", "json3",
        "--no-playlist", "--no-progress",
        "-o", str(dest / "pending.%(ext)s"),
        "--print", "after_move:%(title)s",
        url,
    ]
    # Бандловый ffmpeg сам по себе не запускается: свои dylib он ищет
    # рядом с собой, а не по системным путям, и без подсказки падает на
    # `Library not loaded: libavdevice.dylib`. Remotion зовёт его со своим
    # окружением, а yt-dlp про это не знает — подсказываем мы.
    env = dict(os.environ)
    env["DYLD_LIBRARY_PATH"] = str(FFMPEG_DIR)
    env["DYLD_FALLBACK_LIBRARY_PATH"] = str(FFMPEG_DIR)
    env["LD_LIBRARY_PATH"] = str(FFMPEG_DIR)          # на случай не-macOS

    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(),
                                          timeout=DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise NoVideo(f"скачивание не уложилось в "
                      f"{DOWNLOAD_TIMEOUT // 60} минут") from e

    # Сведённый файл берём первым: рядом с ним могут лежать недобитые
    # куски отдельных дорожек, и звуковая по имени от видео не отличима.
    merged = [p for p in dest.glob("pending.mp4")]
    videos = merged or [p for p in dest.glob("pending.*")
                        if p.suffix.lower() in (".mkv", ".webm", ".mov")]
    for junk in dest.glob("pending.*"):
        if junk not in videos and junk.suffix.lower() != ".json3":
            junk.unlink(missing_ok=True)
    if proc.returncode != 0 or not videos:
        tail = err.decode(errors="replace").strip().splitlines()[-3:]
        raise NoVideo("не скачалось: " + " | ".join(tail))

    words: list[Word] = []
    subs = sorted(dest.glob("pending.*.json3")) or sorted(dest.glob("pending.*.json"))
    if subs:
        try:
            words = _from_json3(subs[0].read_text(encoding="utf-8"))
        except (ValueError, KeyError) as e:
            log.warning("субтитры не разобрались: %s", e)
        for f in subs:
            f.unlink(missing_ok=True)

    title = out.decode(errors="replace").strip().splitlines()[-1] if out else ""
    log.info("скачано %s (%s слов субтитров)", videos[0].name, len(words))
    return Fetched(videos[0], words, title)
