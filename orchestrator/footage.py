"""Разбор отснятого дубля: что в нём есть, где он молчит, куда смотреть.

Три прохода по видео, все детерминированные и все локальные:

- `probe` — длительность, кадр, есть ли звук;
- `silences` → `Timeline` — где человек молчит и что из этого выбросить;
- `pan` — куда едет кадр при кропе 9:16, чтобы активная зона (на записи
  экрана это курсор и печать) не уезжала за край;
- `captions` — пословный транскрипт через whisper.cpp, вход для караоке.

Модель здесь не зовётся ни разу. Это продолжение той же границы, что у
`publisher.py`: посчитать тишину и центр движения — арифметика, а не
рассуждение, и стоить она не должна ничего.

Весь ffmpeg берётся бандловый, из `@remotion/compositor-*` внутри
`tools/remotion-montage/node_modules`. Системного ffmpeg на машине может
не быть вовсе, и ставить его отдельно ради монтажа не нужно.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from config import ROOT

log = logging.getLogger("footage")

TOOLS = ROOT / "tools" / "remotion-montage"
FFMPEG = TOOLS / "node_modules" / ".bin" / "remotion"

# Папка с бандловыми ffmpeg и ffprobe внутри пакета компоновщика. Нужна
# тем, кто зовёт ffmpeg сам, а не через `remotion ffmpeg` — например
# `yt-dlp`, когда сводит видео и звук в один файл.
# Имя пакета зависит от платформы (compositor-darwin-arm64 и так далее),
# поэтому ищем, а не вписываем: репозиторий не должен работать ровно на
# одной машине.
FFMPEG_DIR = next(iter(sorted(
    (TOOLS / "node_modules" / "@remotion").glob("compositor-*"))), TOOLS)


class NoFfmpeg(RuntimeError):
    """Бандловый ffmpeg не отозвался."""


async def _run(*cmd: str, timeout: int = 600) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(TOOLS),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise NoFfmpeg(f"{cmd[0]} не уложился в {timeout} с") from e
    return proc.returncode, out, err


# ── что за файл прислали ──────────────────────────────────────────────

@dataclass
class Probe:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


async def probe(video: Path) -> Probe:
    code, out, err = await _run("node", str(TOOLS / "scripts" / "probe.mjs"),
                                str(video), timeout=120)
    if code != 0 or not out.strip():
        raise NoFfmpeg(f"не прочитал {video.name}: "
                       f"{err.decode(errors='replace').strip()[:200]}")
    d = json.loads(out.decode())
    return Probe(float(d["durationInSeconds"]), int(d["width"]),
                 int(d["height"]), float(d["fps"]), bool(d["hasAudio"]))


# ── тишина и нарезка ──────────────────────────────────────────────────
#
# Порог тишины считается от самой записи, а не берётся константой:
# комната, микрофон и голос у каждого свои, и -32 дБ, годные для одной
# записи, режут речь на другой. `loudnorm` в режиме JSON измеряет
# громкость по EBU R128 и отдаёт `input_thresh` — уровень, ниже которого
# звук уже не считается голосом. Его и передаём в `silencedetect`.
# Так советует делать сам Remotion (skills/remotion-best-practices,
# remotion-markup/silence-detection.md), и это ровно тот случай, когда
# «настройка по умолчанию» — это измерение, а не вкус.
#
# Полсекунды — граница между вдохом и паузой. Ниже неё нарезка начинает
# рубить речь на слоги, и ролик звучит как склейка, а не как человек.

SILENCE_MIN = 0.55
SILENCE_FALLBACK_DB = -32   # если громкость измерить не вышло
KEEP_PAD = 0.20          # воздух вокруг реплики, чтобы не глотать начало
MIN_PIECE = 0.35         # огрызок короче — мусор, а не кусок дубля

SILENCE_RX = re.compile(
    r"silence_start:\s*(-?[\d.]+)|silence_end:\s*(-?[\d.]+)")


THRESH_RX = re.compile(r'"input_thresh"\s*:\s*"?(-?[\d.]+|-inf)"?')


async def loudness(video: Path) -> float:
    """Порог тишины этой записи в дБ. Не измерилось — запасной."""
    code, _, err = await _run(
        str(FFMPEG), "ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
        "-map", "0:a", "-af", "loudnorm=print_format=json",
        "-f", "null", "-", timeout=900)
    if code != 0:
        return SILENCE_FALLBACK_DB
    m = THRESH_RX.search(err.decode(errors="replace"))
    if not m or m.group(1) == "-inf":
        return SILENCE_FALLBACK_DB
    return float(m.group(1))


async def silences(video: Path,
                   threshold: float | None = None) -> list[tuple[float, float]]:
    """Куски, где дубль молчит. Без звуковой дорожки — пустой список."""
    if threshold is None:
        threshold = await loudness(video)
    code, _, err = await _run(
        str(FFMPEG), "ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
        "-vn", "-af", f"silencedetect=noise={threshold}dB:d={SILENCE_MIN}",
        "-f", "null", "-", timeout=900)
    if code != 0:
        raise NoFfmpeg("silencedetect не отработал: "
                       + err.decode(errors="replace").strip()[-200:])

    out: list[tuple[float, float]] = []
    start: float | None = None
    for m in SILENCE_RX.finditer(err.decode(errors="replace")):
        if m.group(1) is not None:
            start = float(m.group(1))
        elif start is not None:
            out.append((start, float(m.group(2))))
            start = None
    return out


@dataclass
class Timeline:
    """Что оставили от дубля и как исходное время ложится на готовое.

    Одна точка правды на нарезку: и субтитры, и трек панорамы считаются
    в исходном времени, а в ролик едут в готовом. Пока пересчёт лежал бы
    в двух местах, любая правка порога тишины разводила бы их молча — и
    рассинхрон нашёлся бы уже на смонтированном видео.
    """
    keep: list[tuple[float, float]]
    dropped: float

    @property
    def total(self) -> float:
        return sum(b - a for a, b in self.keep)

    def shift(self, by: float) -> "Timeline":
        """Те же куски, но отсчитанные от другой точки.

        Нужна, когда фрагмент вырезан в отдельный файл: внутри него
        время идёт с нуля, а найденные паузы записаны в секундах
        исходной записи.
        """
        return Timeline([(a - by, b - by) for a, b in self.keep], self.dropped)

    def at(self, t: float) -> float | None:
        """Исходная секунда → секунда в готовом ролике. Вырезано — None."""
        clock = 0.0
        for a, b in self.keep:
            if t < a:
                return None
            if t <= b:
                return clock + (t - a)
            clock += b - a
        return None


def window(tl: Timeline, start: float, end: float) -> Timeline:
    """Та же нарезка, но только внутри куска дубля.

    Нужна, когда из одной длинной записи режется несколько роликов:
    паузы ищутся по всему файлу один раз, а каждому ролику достаётся
    своя часть найденного. Считать тишину заново на каждом куске значит
    получить разные пороги на соседних секундах одной записи.
    """
    keep = [(max(a, start), min(b, end)) for a, b in tl.keep
            if b > start and a < end]
    keep = [(a, b) for a, b in keep if b - a >= MIN_PIECE]
    if not keep:
        return Timeline([(start, end)], 0.0)
    return Timeline(keep, (end - start) - sum(b - a for a, b in keep))


def timeline(duration: float, quiet: list[tuple[float, float]]) -> Timeline:
    """Из тишины — куски, которые остаются в ролике."""
    if not quiet:
        return Timeline([(0.0, duration)], 0.0)

    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in quiet:
        end = min(a + KEEP_PAD, duration)
        if end - cursor >= MIN_PIECE:
            keep.append((cursor, end))
        cursor = max(cursor, min(b - KEEP_PAD, duration))
    if duration - cursor >= MIN_PIECE:
        keep.append((cursor, duration))

    if not keep:                       # дубль молчит целиком — не наше дело
        return Timeline([(0.0, duration)], 0.0)
    return Timeline(keep, duration - sum(b - a for a, b in keep))


# ── куда смотрит кадр ─────────────────────────────────────────────────
#
# Позиции курсора взять неоткуда: он нарисован в пикселях, трека рядом
# нет. Зато на записи экрана меняется ровно то место, где курсор и
# печать, — поэтому центр берётся по разнице соседних кадров.
#
# Считается на сетке 48×32 в сером: две тысячи байт на кадр, чистый
# питон справляется без numpy, а новая зависимость ради арифметики,
# которая укладывается в двадцать строк, — плохая сделка.

GRID_W, GRID_H = 48, 32
SAMPLE_FPS = 5
NOISE = 14               # ниже — шум кодека, а не движение
QUIET_FRAME = 40         # суммарный вес тише — считаем, что ничего не было
SMOOTH = 0.22            # инерция кадра: рывок за курсором тошнотворен
DEADZONE = 0.02          # мельче — дрожание, а не движение


@dataclass
class Focus:
    t: float
    x: float
    y: float
    w: float = 0.0        # сколько всего изменилось: вес движения в кадре


async def _gray(video: Path) -> bytes:
    # ffmpeg у Remotion собран урезанным: фильтра `fps` в нём нет вовсе, а
    # мукcера `rawvideo` нет тоже — частота задаётся выходным `-r`, а кадры
    # льются через `image2pipe`. Обычные рецепты из интернета здесь просто
    # не запускаются, и это не наша ошибка, а чужая сборка.
    code, out, err = await _run(
        str(FFMPEG), "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-an", "-r", str(SAMPLE_FPS),
        "-vf", f"scale={GRID_W}:{GRID_H},format=gray",
        "-c:v", "rawvideo", "-f", "image2pipe", "-", timeout=900)
    if code != 0:
        raise NoFfmpeg("не снял кадры для панорамы: "
                       + err.decode(errors="replace").strip()[-200:])
    return out


def _scan(prev: bytes, cur: bytes) -> tuple[float, float, int]:
    """Центр изменившегося между двумя кадрами, нормализованный 0..1."""
    total = sx = sy = 0
    for i in range(GRID_W * GRID_H):
        d = cur[i] - prev[i]
        if d < 0:
            d = -d
        if d < NOISE:
            continue
        total += d
        sx += d * (i % GRID_W)
        sy += d * (i // GRID_W)

    if total < QUIET_FRAME:
        return (-1.0, -1.0, total)
    return (sx / total / (GRID_W - 1), sy / total / (GRID_H - 1), total)


async def pan(video: Path) -> list[Focus]:
    """Трек «куда смотреть» по исходному времени дубля."""
    raw = await _gray(video)
    size = GRID_W * GRID_H
    frames = [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]
    if len(frames) < 2:
        return []

    track: list[Focus] = []
    fx, fy = 0.5, 0.5
    for n in range(1, len(frames)):
        cx, cy, weight = _scan(frames[n - 1], frames[n])
        if cx >= 0 and (abs(cx - fx) > DEADZONE or abs(cy - fy) > DEADZONE):
            fx += (cx - fx) * SMOOTH
            fy += (cy - fy) * SMOOTH
        track.append(Focus(n / SAMPLE_FPS, round(fx, 4), round(fy, 4),
                           float(weight)))
    return track


# ── кадр на обложку ───────────────────────────────────────────────────
#
# Обложка берётся из самого дубля, а не рисуется заново: монтаж модель не
# зовёт, и придумывать картинку ему нечем. Годится не любой кадр —
# середина прокрутки или взмаха рукой смазана, и заголовок ложится на
# кашу. Поэтому берётся самый спокойный кадр: тот, где между соседними
# кадрами изменилось меньше всего.

STILL_WINDOW = 6.0        # ищем в начале: обложка должна быть про начало
STILL_SKIP = 1.0          # первая секунда почти всегда пустая заставка


def calm_at(track: list[Focus], window: float = STILL_WINDOW) -> float:
    """Секунда самого спокойного кадра в начале дубля.

    Первая секунда пропускается намеренно: дубль обычно открывается
    неподвижным экраном перед тем, как человек начнёт говорить и делать,
    и по метрике «меньше всего изменилось» побеждала бы именно она — то
    есть кадр, на котором ещё ничего не произошло.
    """
    if not track:
        return 0.0
    head = [f for f in track if STILL_SKIP <= f.t <= window]
    if not head:
        head = [f for f in track if f.t <= window] or track[:1]
    return min(head, key=lambda f: f.w).t


async def clip(video: Path, start: float, end: float, out: Path) -> Path:
    """Вырезать кусок записи в отдельный файл.

    Рендерер читает видео покадрово и перематывает к нужному кадру сам.
    На получасовой записи это его убивает: Chrome падал на восьмисотом
    кадре и потом не укладывался в тридцать секунд на одну перемотку к
    четырнадцатой минуте. Кусок в шестьдесят секунд он читает спокойно.

    Перекодируем, а не режем по ключевым кадрам: `-c copy` начинает файл
    с ближайшего ключевого кадра, и ролик открывается замороженной
    картинкой на секунду-две. Об этом прямо предупреждает и документация
    Remotion (remotion-markup/ffmpeg.md).
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    code, _, err = await _run(
        str(FFMPEG), "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{end - start:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-y", str(out), timeout=900)
    if code != 0 or not out.exists():
        raise NoFfmpeg("не вырезал кусок записи: "
                       + err.decode(errors="replace").strip()[-200:])
    return out


async def still(video: Path, at: float, out: Path) -> Path:
    """Снять один кадр в PNG. Нужен и обложке, и разбору глазами."""
    out.parent.mkdir(parents=True, exist_ok=True)
    code, _, err = await _run(
        str(FFMPEG), "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1", "-y",
        str(out), timeout=300)
    if code != 0 or not out.exists():
        raise NoFfmpeg("не снял кадр на обложку: "
                       + err.decode(errors="replace").strip()[-200:])
    return out


# ── субтитры ──────────────────────────────────────────────────────────
#
# Локально и бесплатно: faster-whisper в том же venv, модель качается
# один раз в кеш HuggingFace. Дубль наружу не уходит — ни ключа, ни
# запроса, ни чужого сервера.
#
# Почему не whisper.cpp, которым это делает сам Remotion: его установка
# собирается из исходников и требует cmake, которого на машине нет и
# который тянет за собой Homebrew с паролем. faster-whisper ставится
# готовым колесом одной командой pip.
#
# Тайминги берутся отсюда, а не из `-script-notes.md`: там они посчитаны
# от скорости чтения суфлёра (два слова в секунду), а человек на камере
# говорит в своём темпе. Субтитры по чужой оценке — это рассинхрон,
# выданный за фичу; ровно поэтому в v1 их не было вовсе.

WHISPER_MODEL = "small"        # ниже — русский разваливается на слоги


class NoWhisper(RuntimeError):
    """Транскрипт не собрался."""


@dataclass
class Word:
    text: str
    start: float
    end: float


def _transcribe(video: Path, model: str) -> list[Word]:
    from faster_whisper import WhisperModel          # тяжёлый импорт, лениво

    wm = WhisperModel(model, device="cpu", compute_type="int8")
    segments, _ = wm.transcribe(str(video), language="ru",
                                word_timestamps=True, vad_filter=True)
    out: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                out.append(Word(text, float(w.start), float(w.end)))
    return out


async def captions(video: Path, *, model: str = WHISPER_MODEL) -> list[Word]:
    """Пословный транскрипт дубля."""
    try:
        return await asyncio.to_thread(_transcribe, video, model)
    except Exception as e:                                    # noqa: BLE001
        raise NoWhisper(f"{type(e).__name__}: {e}") from e


# ── караоке: слова в страницы ─────────────────────────────────────────
#
# На экране держится не слово и не предложение, а горсть слов: одно
# читать не успеваешь, строку целиком глаз не ловит. Страница набирается
# по числу слов и рвётся на длинной паузе — иначе фраза, разорванная
# вдохом, доедет до кадра склеенной.

PAGE_WORDS = 4
PAGE_GAP = 0.7          # пауза длиннее — начинаем новую страницу
PAGE_MAX = 3.2          # и дольше этого страница не висит


def pages(words: list[dict]) -> list[dict]:
    """Слова с временем готового ролика → страницы караоке."""
    out: list[dict] = []
    cur: list[dict] = []

    def flush() -> None:
        if cur:
            out.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                        "words": list(cur)})
            cur.clear()

    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            span = w["end"] - cur[0]["start"]
            if len(cur) >= PAGE_WORDS or gap > PAGE_GAP or span > PAGE_MAX:
                flush()
        cur.append(w)
    flush()
    return out


# ── перекладка на готовую дорожку ─────────────────────────────────────

def cut_words(words: list[Word], tl: Timeline) -> list[dict[str, float | str]]:
    """Слова в время готового ролика. Попавшие в вырезанное — выброшены."""
    out = []
    for w in words:
        start = tl.at(w.start)
        if start is None:
            continue
        end = tl.at(w.end)
        if end is None or end <= start:
            end = start + max(0.12, w.end - w.start)
        out.append({"text": w.text, "start": round(start, 3),
                    "end": round(end, 3)})
    return out


def cut_track(track: list[Focus], tl: Timeline) -> list[dict[str, float]]:
    """Трек панорамы в время готового ролика."""
    out = []
    for f in track:
        t = tl.at(f.t)
        if t is None:
            continue
        out.append({"t": round(t, 3), "x": f.x, "y": f.y})
    return out
