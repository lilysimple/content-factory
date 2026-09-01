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

# Центр изменившегося скачет сам по себе. Замер на настоящем материале
# (запись экрана и съёмка с камеры, по сорок секунд): между соседними
# отсчётами он прыгает в среднем на 0,09 кадра, а в каждом десятом — на
# треть кадра и больше. Ехать за таким центром напрямую значит трясти
# картинку там, где на экране ничего не происходит.
#
# Поэтому цель считается не по одному отсчёту, а по окну в две секунды, и
# кадр идёт к ней с ограниченной скоростью. Три правила подряд:
#
# 1. окно усредняет случайные всплески (WINDOW);
# 2. мёртвая зона не даёт трогаться с места ради мелочи (DEADZONE);
# 3. потолок скорости не даёт догонять цель рывком (MAX_STEP).
#
# Плюс финальное сглаживание уже готового трека: между отсчётами
# композиция интерполирует линейно, и без него на каждом отсчёте виден
# излом.
WINDOW = 20              # четыре секунды
QUIET_SAMPLE = 200       # отсчёт легче — это шум, в окно его не берём
LIVE_SAMPLES = 6         # столько живых отсчётов нужно, чтобы цель считалась
START_ZONE = 0.15        # трогаемся, только если действие ушло далеко
STOP_ZONE = 0.05         # и едем, пока не подойдём вплотную
MAX_STEP = 0.004         # доля кадра за отсчёт: две сотых в секунду
SMOOTH_SPAN = 5          # ±секунда на сглаживание готового трека


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

    if total <= 0:
        return (-1.0, -1.0, 0)
    return (sx / total / (GRID_W - 1), sy / total / (GRID_H - 1), total)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _target(window: list[tuple[float, float, int]]) -> tuple[float, float] | None:
    """Куда смотрит активность за последние четыре секунды.

    Медиана, а не среднее со взвешиванием: одна вспышка на пол-кадра
    (переключили окно, махнули рукой перед камерой) утаскивает среднее к
    себе, а медиану — нет. На настоящем материале среднее давало треть
    кадра блуждания там, где на экране ничего не менялось.
    """
    live = [(x, y) for x, y, w in window if w >= QUIET_SAMPLE]
    if len(live) < LIVE_SAMPLES:
        return None
    return (_median([x for x, _ in live]), _median([y for _, y in live]))


def _smooth(track: list[Focus], span: int = SMOOTH_SPAN) -> list[Focus]:
    """Скользящее среднее по готовому треку: убирает изломы на отсчётах."""
    if span < 1 or len(track) < 3:
        return track
    out: list[Focus] = []
    for i, f in enumerate(track):
        lo, hi = max(0, i - span), min(len(track), i + span + 1)
        part = track[lo:hi]
        out.append(Focus(f.t,
                         round(sum(p.x for p in part) / len(part), 4),
                         round(sum(p.y for p in part) / len(part), 4),
                         f.w))
    return out


async def pan(video: Path) -> list[Focus]:
    """Трек «куда смотреть» по исходному времени дубля."""
    raw = await _gray(video)
    size = GRID_W * GRID_H
    frames = [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]
    if len(frames) < 2:
        return []

    track: list[Focus] = []
    window: list[tuple[float, float, int]] = []
    # Первую цель занимаем сразу, а не едем к ней от середины холста:
    # эта поездка ничего не показывает, а на видео читается как отъезд
    # кадра в первые секунды.
    fx = fy = None
    first: tuple[float, float] | None = None
    moving = False

    for n in range(1, len(frames)):
        cx, cy, weight = _scan(frames[n - 1], frames[n])
        if cx >= 0:
            window.append((cx, cy, weight))
        else:
            window.append((0.5, 0.5, 0))
        if len(window) > WINDOW:
            window.pop(0)

        # Гистерезис: тронуться дорого, поэтому порог на старт большой, а
        # на остановку маленький. Без него кадр «дышит» вокруг цели —
        # шагнул, попал в зону, замер, цель чуть уползла, шагнул снова.
        goal = _target(window)
        if goal is not None and fx is None:
            fx, fy = goal
            first = goal
        elif goal is not None:
            dx, dy = goal[0] - fx, goal[1] - fy
            far = max(abs(dx), abs(dy))
            if not moving and far > START_ZONE:
                moving = True
            elif moving and far < STOP_ZONE:
                moving = False
            if moving:
                fx += max(-MAX_STEP, min(MAX_STEP, dx))
                fy += max(-MAX_STEP, min(MAX_STEP, dy))

        track.append(Focus(n / SAMPLE_FPS,
                           round(0.5 if fx is None else fx, 4),
                           round(0.5 if fy is None else fy, 4),
                           float(weight)))

    # Пока цель не нашлась, трек стоял в середине холста — это не выбор,
    # а отсутствие данных. Задним числом ставим туда же, где кадр
    # оказался в первый раз: иначе ролик открывается рывком из центра.
    if first is not None:
        for f in track:
            if f.x == 0.5 and f.y == 0.5:
                f.x, f.y = round(first[0], 4), round(first[1], 4)
            else:
                break
    return _smooth(track)


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


# ── лицо в кадре ──────────────────────────────────────────────────────
#
# Обложка рилса должна быть кадром с человеком, а не случайной секундой
# записи: плитку в сетке профиля листают по лицу, а не по интерфейсу. И
# заголовок не должен ложиться человеку на лицо — это первое, что видно,
# и испорчено оно бывает молча.
#
# «На глаз» такой кадр не находится. Признаки, которые пробовали и
# померили на настоящем материале, не разделяют съёмку и запись экрана
# (разбор — в ТЗ обложки бренда). Поэтому спрашиваем детектор лиц macOS:
# framework Vision уже стоит в системе, новой библиотеки в venv не
# заводит и модели на диск не кладёт. Скрипт — `tools/facebox.swift`,
# один запуск на все кадры сразу: swift компилирует его при каждом
# вызове, и три секунды на кадр вместо трёх на пачку — плохая сделка.
#
# Детектора нет (нет Xcode CLT, не тот Mac) — это **не** поломка монтажа:
# обложка собирается по-старому, самым спокойным кадром, и человеку об
# этом говорится строкой. Молчаливая подмена хуже неполной обложки.

FACE_SCRIPT = ROOT / "tools" / "facebox.swift"
FACE_TIMEOUT = 180
FACE_MIN_CONF = 0.4
FACE_MIN_SIDE = 0.05      # доля высоты кадра: меньше — человек в толпе,
                          # а не в кадре, и обложку на нём не строят
FACE_SHOTS = 8            # столько кадров-кандидатов снимаем на пробу


class NoVision(RuntimeError):
    """Детектор лиц не отозвался."""


@dataclass
class Face:
    x: float                # доли кадра, начало в левом верхнем углу
    y: float
    w: float
    h: float
    conf: float = 0.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def _face_of(raw: dict) -> list[Face]:
    out = []
    for f in raw.get("faces") or []:
        face = Face(float(f.get("x") or 0), float(f.get("y") or 0),
                    float(f.get("w") or 0), float(f.get("h") or 0),
                    float(f.get("conf") or 0))
        if face.conf >= FACE_MIN_CONF and face.h >= FACE_MIN_SIDE:
            out.append(face)
    return out


async def faces(shots: list[Path]) -> list[list[Face]]:
    """Лица на каждом кадре, в том же порядке. Пусто — лиц нет."""
    if not shots:
        return []
    if not FACE_SCRIPT.exists():
        raise NoVision(f"нет {FACE_SCRIPT.name}")
    proc = await asyncio.create_subprocess_exec(
        "swift", str(FACE_SCRIPT), *[str(p) for p in shots],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(),
                                          timeout=FACE_TIMEOUT)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise NoVision(f"детектор лиц не уложился в {FACE_TIMEOUT} с") from e
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip().splitlines()[-2:]
        raise NoVision("детектор лиц не запустился: " + " | ".join(tail))

    found: dict[str, list[Face]] = {}
    for line in out.decode(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        found[str(raw.get("path") or "")] = _face_of(raw)
    return [found.get(str(p), []) for p in shots]


def _spread(track: list[Focus], duration: float, n: int,
            window: tuple[float, float] | None = None) -> list[float]:
    """По самому спокойному кадру из каждого куска дубля.

    Кандидаты берутся со всей записи, а не из первых секунд: человек
    входит в кадр когда угодно, и требование «на обложке лицо» иначе не
    выполнить. Спокойный кадр внутри куска — по той же причине, что и у
    `calm_at`: середина взмаха рукой смазана.

    `window` сужает поиск до куска нарезки: обложка куска должна быть
    кадром этого куска, а не соседнего.
    """
    start, end = window or (STILL_SKIP, duration)
    start = max(start, STILL_SKIP if not window else start)
    if end <= start:
        return []
    n = max(1, n)
    step = (end - start) / n
    out: list[float] = []
    for i in range(n):
        lo = start + i * step
        hi = lo + step
        chunk = [f for f in track if lo <= f.t < hi]
        out.append(min(chunk, key=lambda f: f.w).t if chunk
                   else round(lo + step / 2, 2))
    return out


SUB_EDGE = 0.25           # отступ от краёв паузы: титр гаснет не мгновенно


def speech_gaps(words: list["Word"], duration: float) -> list[tuple[float, float]]:
    """Куски, где не звучит ни одного слова, — по пословному транскрипту.

    Точнее пауз из `silences`: тишиной там считается уровень звука, а
    вшитый субтитр держится ровно по словам. Хвост после последней
    реплики тишиной обычно не признаётся вовсе (дыхание, комната), а
    субтитра там уже нет — и это лучший кадр под обложку.
    """
    out: list[tuple[float, float]] = []
    cursor = 0.0
    for w in sorted(words, key=lambda w: w.start):
        if w.start > cursor:
            out.append((cursor, w.start))
        cursor = max(cursor, w.end)
    if duration > cursor:
        out.append((cursor, duration))
    return out


def quiet_times(quiet: list[tuple[float, float]],
                window: tuple[float, float] | None = None,
                min_len: float | None = None) -> list[float]:
    """Середины пауз — секунды, где на записи никто не говорит.

    Нужны обложке. Записи, снятые не в этом заводе, часто приходят с
    вшитыми субтитрами, и кадр посреди реплики уносит на обложку чужую
    строку поверх лица. Где человек молчит, титра нет — это факт
    записи, а не догадка по пикселям, и считается он бесплатно: паузы
    монтаж уже нашёл, чтобы их вырезать.
    """
    out: list[float] = []
    for a, b in quiet:
        # Отступаем от краёв паузы: титр гаснет не в ту же миллисекунду,
        # в которую человек замолчал.
        lo, hi = a + SUB_EDGE, b - SUB_EDGE
        if b - a < (SILENCE_MIN + 2 * SUB_EDGE if min_len is None else min_len):
            continue
        t = min(max((a + b) / 2, lo, STILL_SKIP), hi)
        if t < STILL_SKIP or (window and not (window[0] <= t <= window[1])):
            continue
        out.append(round(t, 2))
    return out


@dataclass
class Shot:
    """Кадр, который встанет на обложку."""
    at: float
    path: Path | None = None
    face: Face | None = None
    note: str | None = None      # что не сошлось, человеку строкой


async def cover_shot(video: Path, track: list[Focus], duration: float,
                     out: Path, *,
                     window: tuple[float, float] | None = None,
                     quiet: list[tuple[float, float]] | None = None,
                     words: list["Word"] | None = None) -> Shot:
    """Кадр под обложку: с лицом, в паузе, иначе самый спокойный.

    Возвращает и рамку лица — по ней монтаж уводит текст так, чтобы он не
    лёг человеку на лицо. `window` сужает поиск до куска нарезки,
    `quiet` — паузы дубля: кадр из паузы предпочтительнее, потому что на
    записи с вшитыми субтитрами там нет чужой строки.
    """
    if window:
        inside = [f for f in track if window[0] <= f.t <= window[1]] or track
        calm = calm_at(inside, window=window[1])
    else:
        calm = calm_at(track)
    # Транскрипт точнее тишины: субтитр держится по словам, а не по
    # уровню звука. Есть он — считаем по нему, нет — по паузам.
    if words:
        silent = quiet_times(speech_gaps(words, duration), window,
                             min_len=2 * SUB_EDGE)[:FACE_SHOTS]
    else:
        silent = quiet_times(quiet or [], window)[:FACE_SHOTS]
    times = _spread(track, duration, FACE_SHOTS, window)
    times = silent + [t for t in sorted({calm, *times}) if t not in silent]

    probes: list[Path] = []
    try:
        for i, t in enumerate(times):
            probes.append(await still(video, t, out.parent / f".probe-{i}.png"))
        found = await faces(probes)
    except NoFfmpeg:
        for p in probes:
            p.unlink(missing_ok=True)
        raise
    except NoVision as e:
        for p in probes:
            p.unlink(missing_ok=True)
        return Shot(calm, await still(video, calm, out), None,
                    f"лицо в кадре не искали ({e}) — на обложку взят самый "
                    f"спокойный кадр ({calm:.1f} с)")

    # Порядок предпочтения: сначала кадры из пауз (там нет вшитого
    # субтитра), внутри них — тот, где лицо крупнее. Обложка с человеком
    # в полный рост на плитке 1080×1920 читается хуже, чем портрет.
    def pick(among: list[float]) -> tuple[float, Face] | None:
        best: tuple[float, Face] | None = None
        for t, fs in zip(times, found):
            if t not in among or not fs:
                continue
            face = max(fs, key=lambda f: f.w * f.h)
            if best is None or face.w * face.h > best[1].w * best[1].h:
                best = (t, face)
        return best

    for p in probes:
        p.unlink(missing_ok=True)

    note = None
    best = pick(silent)
    if best is None:
        best = pick(times)
        if best is not None and silent:
            note = ("в паузах дубля лица не нашлось — кадр взят из речи, "
                    "и если на записи вшиты субтитры, строка попадёт "
                    "на обложку")
    if best is None:
        return Shot(calm, await still(video, calm, out), None,
                    f"лица в дубле не нашлось — на обложку взят самый "
                    f"спокойный кадр ({calm:.1f} с)")
    at, face = best
    return Shot(at, await still(video, at, out), face, note)


# ── словарь исправлений: что слышно → что сказано ─────────────────────
#
# Whisper слышит русскую речь, а бренд говорит именами: Claude, Remotion,
# Anthropic, названия своих продуктов. В словаре модели их нет, и в
# караоке приезжает «клод», «ремоушен», «антропик» — ошибка не случайная,
# а одна и та же на каждом дубле. Значит лечится она таблицей бренда, а
# не переслушиванием: модель здесь не зовут вовсе.
#
# Замена идёт по нормализованной форме (регистр, «ё», знаки препинания не
# считаются) и по фразе целиком, а не по одному слову: «клод код» это два
# слова транскрипта и одно имя. Тайминги фразы раскладываются поровну на
# слова замены, хвостовой знак препинания остаётся от исходного слова —
# иначе запятая в караоке пропадёт вместе с ошибкой.

_WORD_RX = re.compile(r"\w+", re.U)
_TAIL_RX = re.compile(r"[^\w]+$", re.U)
MIN_WORD = 0.12           # столько держится слово, если тайминги схлопнулись


def norm(text: str) -> str:
    """Форма для сравнения: без регистра, «ё» и знаков препинания."""
    return " ".join(_WORD_RX.findall(text.lower().replace("ё", "е")))


def _respan(run: list[dict], dst: str) -> list[dict]:
    """Слова замены на таймингах исходной фразы."""
    parts = dst.split()
    if not parts:
        return []                       # пустая замена — слово выброшено
    first = str(run[0]["text"])
    if first[:1].isupper() and parts[0][:1].islower():
        parts[0] = parts[0][0].upper() + parts[0][1:]
    tail = _TAIL_RX.search(str(run[-1]["text"]))
    if tail:
        parts[-1] += tail.group()

    start, end = float(run[0]["start"]), float(run[-1]["end"])
    step = (end - start) / len(parts)
    if step <= 0:
        step = MIN_WORD
    return [{"text": p, "start": round(start + i * step, 3),
             "end": round(start + (i + 1) * step, 3)}
            for i, p in enumerate(parts)]


def relex(words: list[dict],
          rules: list[tuple[str, str]]) -> tuple[list[dict], int]:
    """Пословный транскрипт → он же с исправлениями словаря.

    Вторым — сколько слов транскрипта поправлено: человеку это строка в
    карточке, а не тихая правка у него за спиной.
    """
    table: dict[str, str] = {}
    for src, dst in rules:
        key = norm(src)
        if key:
            table[key] = dst.strip()
    if not table or not words:
        return list(words), 0

    span = max(len(k.split()) for k in table)
    out: list[dict] = []
    fixed = 0
    i = 0
    while i < len(words):
        hit = None
        # Длинная фраза важнее короткой: «клод код» не должен разбираться
        # правилом про «клод», иначе второе слово останется как слышно.
        for n in range(min(span, len(words) - i), 0, -1):
            key = norm(" ".join(str(w["text"]) for w in words[i:i + n]))
            if key in table:
                hit = (n, table[key])
                break
        if hit is None:
            out.append(words[i])
            i += 1
            continue
        n, dst = hit
        out.extend(_respan(words[i:i + n], dst))
        fixed += n
        i += n
    return out, fixed
