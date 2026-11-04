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

Весь ffmpeg идёт через шим `tools/ffmpeg-shim`. Системного ffmpeg на
машине может не быть вовсе: шим тогда возьмёт полную сборку из
`imageio_ffmpeg`, а если нет и её — бандловую из `@remotion/compositor-*`
внутри `tools/remotion-montage/node_modules`. Ставить ffmpeg отдельно
ради монтажа по-прежнему не нужно, но с полной сборкой здесь доступны
фильтры, которых у бандловой нет вовсе.
"""
from __future__ import annotations

import array
import asyncio
import bisect
import base64
import binascii
import contextlib
import difflib
import json
import logging
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

from config import ROOT

log = logging.getLogger("footage")

TOOLS = ROOT / "tools" / "remotion-montage"
# Зовём ffmpeg через шим `tools/ffmpeg-shim`, а не через `remotion ffmpeg`.
# Шим берёт полную сборку, если она на машине есть, и бандловую только
# запасной: у бандловой 50 фильтров против 492, и чего в ней нет —
# написано в NOTES.md. Путь вызова это не меняет, меняется только то,
# какой бинарь окажется на том конце.
FFMPEG = ROOT / "tools" / "ffmpeg-shim" / "ffmpeg"

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


@dataclass
class Loud:
    """Громкость записи по EBU R128, как её измерил `loudnorm`.

    Пять чисел, а не одно: порог тишины нужен нарезке, а интегральная
    громкость и истинный пик — выравниванию выхода. Мерятся они одним
    проходом, и разводить их по двум вызовам ffmpeg значило бы платить
    минутами за одно и то же.
    """
    i: float         # интегральная громкость, LUFS
    tp: float        # истинный пик, dBTP
    lra: float       # разброс громкости
    thresh: float    # уровень, ниже которого звук уже не речь
    offset: float    # поправка самого измерения


def _loud_field(text: str, name: str) -> float | None:
    """Поле замера из вывода `loudnorm`. Кавычки у чисел ставят не все
    сборки ffmpeg, поэтому читается и так, и так."""
    m = re.search(rf'"{name}"\s*:\s*"?(-?[\d.]+|-?inf)"?', text)
    if m is None:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


async def measure(video: Path) -> Loud | None:
    """Замер громкости одним проходом. Нет звука или не сошлось — None."""
    code, _, err = await _run(
        str(FFMPEG), "-hide_banner", "-nostats", "-i", str(video),
        "-map", "0:a", "-af", "loudnorm=print_format=json",
        "-f", "null", "-", timeout=900)
    if code != 0:
        return None
    return read_loud(err.decode(errors="replace"))


def read_loud(text: str) -> Loud | None:
    """Вывод `loudnorm` → пять чисел. Пустая дорожка даёт -inf, и это не
    замер: на тишине выравнивать нечего, а гейн из -inf уедет в NaN."""
    got = {k: _loud_field(text, f"input_{k}")
           for k in ("i", "tp", "lra", "thresh")}
    got["offset"] = _loud_field(text, "target_offset") or 0.0
    if any(v is None or v != v or v in (float("-inf"), float("inf"))
           for v in got.values()):
        return None
    return Loud(**got)                                  # type: ignore[arg-type]


async def loudness(video: Path) -> float:
    """Порог тишины этой записи в дБ. Не измерилось — запасной."""
    loud = await measure(video)
    return SILENCE_FALLBACK_DB if loud is None else loud.thresh


# ── громкость выхода ──────────────────────────────────────────────────
#
# Ролик уходит в канал с той громкостью, с какой человек его снял, и
# снятое ей не управляет: микрофон, расстояние до него и голос в тот
# день свои у каждого дубля. Замер двенадцати опубликованных роликов
# (04.11) дал разброс **11,7 дБ** — от -17,6 до -29,3 LUFS. На слух это
# не «чуть тише»: один ролик идёт нормально, следующий человек не
# слышит вовсе и уходит, не дослушав хук.
#
# Поэтому выход выравнивается, и выравнивается **двумя проходами**:
# первый измеряет, второй применяет измеренное. Один проход `loudnorm`
# работает динамически — он догоняет цель на ходу и слышно качает
# громкость внутри фразы. На ролике в сорок секунд это слышно сразу.
#
# Цель -14 LUFS: Instagram приводит звук примерно к ней сам, и отдать
# ему уже выровненное лучше, чем дать выровнять его компрессору. Telegram
# не нормирует вовсе — там наша цифра и остаётся конечной.
#
# Картинка при этом не пересчитывается: `-c:v copy`. Пережимать видео
# ради звука значило бы платить минутами рендера и терять качество на
# ровном месте.

TARGET_LUFS = -14.0      # громкость выхода
TARGET_TP = -1.0         # запас до клиппинга
TARGET_LRA = 11.0        # допустимый разброс внутри ролика
LEVEL_TOL = 0.5          # ближе этого к цели — не трогаем
AUDIO_BITRATE = "192k"   # на сорокасекундном ролике это сотни килобайт


def levelled(loud: Loud) -> bool:
    """Громкость уже на месте — второй прогон звука ничего не даст.

    Порог здесь не вкус, а защита от лишнего пережатия: AAC не
    беспотерьный, и прогонять через него ролик ради трёх десятых
    децибела значит терять качество без выигрыша.
    """
    return (abs(loud.i - TARGET_LUFS) < LEVEL_TOL
            and loud.tp <= TARGET_TP + LEVEL_TOL)


def loud_filter(loud: Loud) -> str:
    """Второй проход `loudnorm`: применить измеренное.

    `linear=true` просит сдвинуть громкость одним числом, не трогая
    динамику. Если такой сдвиг уткнётся в потолок пика, loudnorm сам
    перейдёт в динамический режим — это его решение, и спорить с ним
    нечем: иначе пришлось бы выбирать между клиппингом и недотягиванием.
    """
    return (f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA={TARGET_LRA}"
            f":measured_I={loud.i}:measured_TP={loud.tp}"
            f":measured_LRA={loud.lra}:measured_thresh={loud.thresh}"
            f":offset={loud.offset}:linear=true:print_format=summary")


async def normalize(video: Path) -> str:
    """Выровнять громкость готового файла на месте. Отдаёт строку
    человеку — пустую, если говорить нечего.

    Упавший проход оставляет файл как был: ролик без выровненного звука
    всё ещё ролик, а подменённый огрызком — уже нет. Поэтому результат
    пишется рядом и встаёт на место одним движением, а не поверх.
    """
    loud = await measure(video)
    if loud is None:
        return ("громкость ролика не выровняла: замер не сошёлся — "
                "в файле может не быть звука")
    if levelled(loud):
        return ""

    tmp = video.with_name(video.stem + ".level.mp4")
    tmp.unlink(missing_ok=True)
    code, _, err = await _run(
        str(FFMPEG), "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-i", str(video), "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "copy", "-af", loud_filter(loud),
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart", str(tmp), timeout=900)
    if code != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return ("громкость ролика не выровняла: "
                + err.decode(errors="replace").strip()[-160:])

    tmp.replace(video)
    return (f"громкость выровняла: было {loud.i:.1f}, "
            f"стало {TARGET_LUFS:.0f} LUFS")


async def silences(video: Path,
                   threshold: float | None = None) -> list[tuple[float, float]]:
    """Куски, где дубль молчит. Без звуковой дорожки — пустой список."""
    if threshold is None:
        threshold = await loudness(video)
    code, _, err = await _run(
        str(FFMPEG), "-hide_banner", "-nostats", "-i", str(video),
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

    def back(self, t: float) -> float:
        """Секунда готового ролика → секунда исходника.

        Обратная `at`. Нужна тем, кто считает по исходнику то, что задано
        по ролику: маски силуэта снимаются с записи, а окно маскота
        человек называет секундами готового видео.
        """
        clock = 0.0
        for a, b in self.keep:
            if t <= clock + (b - a):
                return a + (t - clock)
            clock += b - a
        return self.keep[-1][1] if self.keep else 0.0


def snap(words: list["Word"], start: float, end: float,
         pad: float = KEEP_PAD) -> tuple[float, float]:
    """Границы куска, отодвинутые с середины слова на его край.

    Монтажёр ставит границы по смыслу и называет их секундами, а секунда
    у него — на слух. На дубле 24.09 начало куска пришлось на 5,0 с,
    когда первое слово звучало с 4,69 по 5,41: ролик открывался
    обрубком «…ин файл». Здесь это правится арифметикой — расшифровка
    знает, где у слова край.

    Наружу, а не внутрь: съеденное слово человек слышит сразу, лишние
    две десятых воздуха — нет.
    """
    for w in words:
        if w.start < start < w.end:
            start = max(0.0, w.start - pad)
            break
    for w in words:
        if w.start < end < w.end:
            end = w.end + pad
            break
    return start, end


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


# ── мусор в середине: повторный заход и кашель ────────────────────────
#
# Паузы вырезает `silences` — там, где тихо. Кашель громкий, а повторный
# заход это нормальная речь, так что ни то ни другое тишиной не ловится:
# оба доезжают до готового ролика, и человек просит убрать их руками на
# каждом дубле.
#
# Монтажёру эта работа не отдана намеренно. Роль ставит границы куска и
# середину не трогает (`roles/cut.md`): дырку в середине ей пришлось бы
# называть секундами, которых она не видит, — те же секунды, что уже
# один раз выдумывались в блоках панели. Здесь вместо секунд есть
# расшифровка со словами и найденная тишина, и обе находки считаются по
# ним арифметикой.
#
# **Повтор узнаётся по тексту.** Человек сбился и начал фразу заново —
# в расшифровке один и тот же кусок идёт подряд дважды. В кадр идёт
# последний заход, предыдущий вырезается целиком. Это то же правило,
# которым роль обрезает начало дубля, только применённое к середине.
#
# **Кашель узнаётся по дырке.** Whisper пишет слова, а не звуки: кашель,
# вздох и «ммм» он не пишет вовсе. Значит между двумя словами остаётся
# заметная дырка, и если тишина её не нашла — там звучало что-то, что
# речью не является. Вырезаем дырку, а не слова вокруг неё.

JUNK_GAP = 0.8           # дырка между словами короче — это дыхание речи
JUNK_ISLE = 2.5          # звук длиннее — это речь, которую не расслышали
MIN_ISLE = 0.12          # короче — щелчок клавиши, его вырезание слышнее его
JUNK_EDGE = 0.15         # воздух у слова: начало реплики глотать нельзя
REPEAT_WORDS = 8         # длиннее куска повтор не ищем
REPEAT_MIN = 3           # короче — это «то есть», а не повторный заход
REPEAT_CHARS = 12        # и не меньше этого знаков подряд
REPEAT_REACH = 18.0      # дальше по записи это уже не заход заново
# Заход заново начинается после запинки: человек сбился, остановился и
# начал фразу сначала. Без этого условия под правило попадал перечень —
# «примеры постов из прошлых, есть примеры постов из соцсетей», — и
# вырезание ломало предложение (дубль 22.09).
REPEAT_BREAK = 1.0

_WORD_TRIM = re.compile(r"[^\w]+", re.UNICODE)


def _norm(text: str) -> str:
    """Слово для сравнения: без знаков, регистра и разницы «ё/е»."""
    return _WORD_TRIM.sub("", str(text).lower()).replace("ё", "е")


def repeats(words: list["Word"]) -> list[tuple[float, float]]:
    """Повторные заходы: тот же кусок сказан второй раз — первый вон.

    Заход не обязан начинаться сразу за предыдущим. Человек сбивается не
    на первом слове: «на первом шаге я бы посоветовала вам создать
    проект, загрузить туда... конечно. На первом шаге я бы посоветовала
    вам...» — между двумя началами лежит целый огрызок мысли, и вырезать
    надо его вместе с первым заходом. Поэтому второе начало ищется
    вперёд по записи, а не строго встык (замер на дубле 22.09: встык
    не нашлось ни одного повтора, а на слух их два).

    Ищем от длинного куска к короткому: совпадение по трём словам может
    оказаться случайным, по семи — уже нет.
    """
    norm = [_norm(w.text) for w in words]
    out: list[tuple[float, float]] = []
    i = 0
    while i < len(words):
        found = 0
        at = 0
        for n in range(min(REPEAT_WORDS, len(words) - i), REPEAT_MIN - 1, -1):
            head = norm[i:i + n]
            if not all(head) or sum(len(s) for s in head) < REPEAT_CHARS:
                continue
            for j in range(i + n, len(words) - n + 1):
                if words[j].start - words[i].start > REPEAT_REACH:
                    break
                if norm[j:j + n] != head:
                    continue
                # Встык — это заика на первом слове, и запинки там нет.
                # Дальше по записи повтор считается заходом заново только
                # после паузы: иначе это перечисление.
                if j > i + n and words[j].start - words[j - 1].end < REPEAT_BREAK:
                    continue
                found, at = n, j
                break
            if found:
                break
        if found:
            out.append((words[i].start, words[at].start))
            i = at
        else:
            i += 1
    return out


# Заход заново длиной в одно слово. `repeats` ищет повтор от трёх слов —
# и не находит самую частую запинку: человек назвал пункт, осёкся и начал
# его заново теми же словами. «Первое — это… [пауза] первое место — это
# знание проекта» (дубль 24.09) в готовый ролик уехало целиком.
#
# Порог в три слова здесь не снизить: совпадение по одному слову внутри
# связной речи это норма («заново и заново»). Отличает запинку не длина,
# а **пауза между двумя заходами**: тишину в этом месте уже нашёл
# `silences`, и она приходит сюда списком. Нет паузы — нет и запинки.

STUB_WORDS = 4           # огрызок длиннее — это мысль, а не заход заново
STUB_CHARS = 5           # слово короче — «это», «то»: совпадение случайно
STUB_QUIET = 1.0         # пауза между заходами короче — дыхание речи


def restarts(words: list["Word"], quiet: list[tuple[float, float]]
             ) -> list[tuple[float, float]]:
    """Короткий заход заново: огрызок до паузы и те же слова после неё."""
    if not quiet:
        return []
    norm = [_norm(w.text) for w in words]
    holes = sorted((a, b) for a, b in quiet if b - a >= STUB_QUIET)
    opens = [a for a, _ in holes]

    out: list[tuple[float, float]] = []
    for i, head in enumerate(norm):
        if len(head) < STUB_CHARS:
            continue
        for j in range(i + 1, min(i + STUB_WORDS + 1, len(words))):
            if norm[j] != head:
                continue
            # Между двумя заходами обязана лежать найденная пауза, и
            # строго между: после неё второй заход обязан ещё звучать.
            # Тайминг слова у whisper растянут на соседнюю тишину в обе
            # стороны, и без этого условия под правило попадал повтор
            # ради нажима — «перечитывается заново и заново», где пауза
            # начинается внутри последнего слова, а не перед ним.
            k = bisect.bisect_left(opens, words[i].end)
            if not any(b < words[j].end for _, b in holes[k:k + 4]):
                continue
            out.append((words[i].start, words[j].start))
            break
    return out


def envelope(path: Path, step: float = 0.05) -> list[float]:
    """Громкость записи по шагам. Чистая арифметика, без numpy.

    Восемь килогерц моно — этого хватает, чтобы отличить звук от тишины,
    а весь дубль на три минуты умещается в полтора миллиона отсчётов.
    """
    import wave

    with contextlib.closing(wave.open(str(path))) as wav:
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    data = array.array("h")
    data.frombytes(raw[:len(raw) // 2 * 2])
    span = max(1, int(rate * step))
    out: list[float] = []
    for i in range(0, max(0, len(data) - span), span):
        chunk = data[i:i + span]
        out.append((sum(x * x for x in chunk) / len(chunk)) ** 0.5)
    return out


async def loudness_track(video: Path, step: float = 0.05) -> list[float]:
    """Та же громкость, но от файла с видео. Не вышло — пустой список."""
    wav = TOOLS / f".audio-{video.stem}.wav"
    try:
        code, _, err = await _run(
            str(FFMPEG), "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-vn", "-ac", "1", "-ar", "8000", "-y",
            str(wav), timeout=900)
        if code != 0 or not wav.exists():
            raise NoFfmpeg(err.decode(errors="replace").strip()[-200:])
        return await asyncio.to_thread(envelope, wav, step)
    except (NoFfmpeg, OSError, EOFError) as e:
        log.warning("громкость не снялась: %s", e)
        return []
    finally:
        wav.unlink(missing_ok=True)


# Доля от медианной громкости речи, выше которой звук считается звуком, а
# не фоном комнаты. Порог относительный, как и у `silences`: микрофон и
# комната у каждого свои.
JUNK_LOUD = 0.35


def coughs(words: list["Word"], track: list[float],
           step: float = 0.05) -> list[tuple[float, float]]:
    """Звук там, где нет слова: кашель, вздох, стук, «ммм».

    Whisper пишет слова, а не звуки, поэтому кашля в расшифровке нет
    вовсе. `silences` его тоже не берёт: он громкий, и тишина вокруг него
    разрывается надвое — половина кашля переживала нарезку в воздухе
    `KEEP_PAD`, который положен началу реплики (дубль 22.09, пять таких
    островков из семи остались в готовом ролике).

    Поэтому ищем прямо по громкости: громко, а слова нет — значит, это
    не речь. Островок длиннее `JUNK_ISLE` не трогаем: столько звучит
    фраза, которую просто не расслышали, а выбросить речь хуже, чем
    оставить вздох.
    """
    if not track or not words:
        return []

    def voiced(t: float) -> bool:
        return any(w.start - JUNK_EDGE <= t <= w.end + JUNK_EDGE
                   for w in words)

    speech = sorted(v for i, v in enumerate(track) if voiced(i * step))
    if not speech:
        return []
    floor = speech[len(speech) // 2] * JUNK_LOUD

    out: list[tuple[float, float]] = []
    start: float | None = None
    for i, v in enumerate(track):
        t = i * step
        hot = v > floor and not voiced(t)
        if hot and start is None:
            start = t
        elif not hot and start is not None:
            if MIN_ISLE <= t - start <= JUNK_ISLE:
                out.append((start, t))
            start = None
    return out


def junk(words: list["Word"], track: list[float], step: float = 0.05,
         quiet: list[tuple[float, float]] | None = None
         ) -> tuple[list[tuple[float, float]], int, int]:
    """Что выбросить из середины. Плюс сколько чего нашлось — на строку.

    Повторных заходов два рода, и считаются они одним числом: длинный
    ищется по тексту (`repeats`), короткий — по тексту и паузе между
    заходами (`restarts`). Человеку в находках это одна строка: он
    слышал одну запинку, а не две её разновидности.
    """
    back = repeats(words) + restarts(words, quiet or [])
    noise = coughs(words, track, step)
    spans = sorted(back + noise)
    merged: list[tuple[float, float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged, len(back), len(noise)


def prune(tl: Timeline, spans: list[tuple[float, float]]) -> Timeline:
    """Та же нарезка минус названные куски. Границы точные, без воздуха.

    `timeline` добавляет `KEEP_PAD` вокруг реплики — там это правильно:
    тишина найдена с запасом, и без воздуха глоталось бы начало слова.
    Здесь наоборот: повтор вырезается ровно по словам, и оставленные
    поверху две десятых — это тот самый обрывок, ради которого всё.
    """
    keep = list(tl.keep)
    for a, b in spans:
        out: list[tuple[float, float]] = []
        for x, y in keep:
            if b <= x or a >= y:
                out.append((x, y))
                continue
            if x < a:
                out.append((x, a))
            if b < y:
                out.append((b, y))
        keep = out
    keep = [(a, b) for a, b in keep if b - a >= MIN_PIECE]
    if not keep:
        return tl
    return Timeline(keep, tl.dropped + tl.total - sum(b - a for a, b in keep))


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
    # Частота задаётся выходным `-r`, а кадры льются через `image2pipe`,
    # хотя обычный рецепт — фильтр `fps` и мукcер `rawvideo`. Так писалось
    # под бандловую сборку, где нет ни того, ни другого. На полной сборке
    # работает тоже, поэтому обход оставлен: он единственный, который
    # запускается на обоих бинарях, а бинарь выбирает шим.
    code, out, err = await _run(
        str(FFMPEG), "-hide_banner", "-loglevel", "error",
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
        str(FFMPEG), "-hide_banner", "-loglevel", "error",
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
        str(FFMPEG), "-hide_banner", "-loglevel", "error",
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


# ── автопроверка: три круга расшифровки и голос двух из трёх ───────────
#
# Одна расшифровка врёт предсказуемо и незаметно. Малая модель слышит
# «остановила» вместо «установила», «весит» вместо «бесит» и глотает
# окончание вместе с концом слова — слово при этом остаётся русским и
# ложится в караоке как ни в чём не бывало. Человек видит подмену уже на
# готовом ролике, после минут рендера.
#
# Словарь бренда (`relex`) такое не ловит и не должен: он знает пары
# имён — «клод» → Claude, — а не падежи. Вести в нём русский язык значит
# дописывать строку на каждую оговорку.
#
# Поэтому дубль слушается трижды, моделями разного размера, и слово
# берётся то, за которое два голоса из трёх. Модель здесь не зовётся:
# это те же локальные проходы whisper, то есть арифметика, а не
# рассуждение, — граница монтажа остаётся там же, где была.
#
# Цена честная и заметная: каждый круг это минуты, а большая модель
# качается один раз тремя гигабайтами. Круг, который не собрался,
# пропускается строкой в находках и монтаж не роняет: ролик с
# субтитрами от одного прохода полезнее отказа смонтировать уже снятое.
#
# Слабое место у голосования одно, и его надо знать: два малых круга,
# ошибившиеся одинаково, перебивают большой, который услышал верно.
# Поэтому тайминги и текст по умолчанию берутся у самого крупного
# прохода, а переголосованные слова называются числом — тихой правки за
# спиной человека здесь нет.

CHECK_MODELS = ("small", "medium", "large-v3")

# Дольше этого автопроверка не запускается: на получасовой записи три
# круга стоят больше, чем весь остальной монтаж, а резать её всё равно
# будут на куски (`montage.split`). Длинный дубль слушается одним
# кругом, и это называется строкой.
CHECK_MAX_SECONDS = 300.0


def _dress(src: str, dst: str) -> str:
    """Слово-победитель в одежде того, что стояло на его месте.

    Заглавная буква и знак на конце принадлежат месту во фразе, а не
    слову: круги рвут речь на предложения по-разному, и голос за слово
    не должен уносить с собой чужую запятую.
    """
    out = dst.strip()
    if not out:
        return src
    if src[:1].isupper() and out[:1].islower():
        out = out[0].upper() + out[1:]
    tail = _TAIL_RX.search(src)
    if tail and not _TAIL_RX.search(out):
        out += tail.group()
    return out


def _pairs(base: list[Word], other: list[Word]) -> dict[int, Word]:
    """Что другой круг поставил на место каждого слова базового.

    Сводить круги ни по номеру слова, ни по времени нельзя. По номеру —
    потому что где один услышал «какие-то», другой слышит два слова, и
    дальше расшифровки разъезжаются до конца записи. По времени — потому
    что метки у кругов разъезжаются на полслова: соседние слова базового
    выбирают одно и то же слово другого круга, и голосование за него
    размножает слово по ролику. Так и вышло на первом прогоне: 26.09
    «с чего бы я начала» приехало как «с чего бы бы начала», а из
    двухсот слов переголосованными оказались тридцать семь.

    Канву даёт `SequenceMatcher` по нормализованным словам: длинные
    совпадающие куски держат ряд, а спорное место остаётся коротким
    блоком между ними. Голос считается только там, где блок меняет слово
    на слово — вставку и пропуск словом не переголосуешь, и трогать их
    нельзя: там круги слышали разное число слов, а не разные слова.
    """
    a = [norm(w.text) for w in base]
    b = [norm(w.text) for w in other]
    out: dict[int, Word] = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            a=a, b=b, autojunk=False).get_opcodes():
        if tag != "replace" or (i2 - i1) != (j2 - j1):
            continue
        for k in range(i2 - i1):
            out[i1 + k] = other[j1 + k]
    return out


def vote(passes: list[list[Word]]) -> tuple[list[Word], int, int]:
    """Несколько расшифровок одного дубля → одна, по голосу двух из трёх.

    Тайминги и спорные места остаются за последним кругом: он идёт самой
    крупной моделью. Текст слова меняется только когда два других круга
    согласны между собой и оба против него — один несогласный это шум, а
    не голос.

    Вторым — сколько слов переголосовано, третьим — сколько мест, где
    круги разошлись все. И то и другое строка человеку: он смотрит
    субтитры там, а не по всему ролику.
    """
    live = [p for p in passes if p]
    if not live:
        return [], 0, 0
    base, others = live[-1], live[:-1]
    if len(others) < 2:
        return list(base), 0, 0

    said = [_pairs(base, p) for p in others]
    out: list[Word] = []
    voted = split = 0
    for i, w in enumerate(base):
        got = [d.get(i) for d in said]
        keys = [norm(o.text) if o else "" for o in got]
        if all(k and k == keys[0] for k in keys):
            # Одеваем в победителя из самого крупного из согласных кругов:
            # они сошлись по слову, но не обязательно по знакам.
            out.append(Word(_dress(w.text, got[-1].text), w.start, w.end))
            voted += 1
            continue
        if all(keys) and len(set(keys)) > 1:
            split += 1
        out.append(w)
    return out, voted, split


async def recheck(video: Path, *,
                  models: tuple[str, ...] = CHECK_MODELS,
                  seconds: float = 0.0) -> tuple[list[Word], list[str]]:
    """Расшифровка с автопроверкой. Вторым — что сказать человеку.

    Отказ отдельного круга это строка, а не исключение. `NoWhisper`
    поднимается только когда не собрался ни один: тогда субтитров нет
    вовсе, и обрабатывает это тот же обработчик, что и раньше.
    """
    plan = list(models) or [WHISPER_MODEL]
    notes: list[str] = []
    if seconds and seconds > CHECK_MAX_SECONDS:
        notes.append(
            f"дубль длиннее {CHECK_MAX_SECONDS / 60:.0f} минут — слушаю "
            "одним кругом вместо трёх: автопроверка на такой записи стоит "
            "дороже всего остального монтажа")
        plan = plan[:1]

    passes: list[list[Word]] = []
    failed: list[str] = []
    for model in plan:
        try:
            got = await captions(video, model=model)
        except NoWhisper as e:
            log.warning("круг %s не собрался: %s", model, e)
            failed.append(model)
            continue
        if got:
            passes.append(got)
        else:
            failed.append(model)

    if not passes:
        raise NoWhisper("ни один круг расшифровки не собрался: "
                        + ", ".join(plan))

    words, voted, split = vote(passes)
    if failed and len(plan) > 1:
        notes.append(f"дубль прослушан {len(passes)} раза из "
                     f"{len(plan)}: не собрались круги "
                     + ", ".join(failed)
                     + (" — слова остались от одного прохода, без голосования"
                        if len(passes) < 3 else ""))
    if voted:
        notes.append(f"автопроверка поправила слов: {voted} — там два круга "
                     "из трёх услышали одно, а третий другое")
    if split:
        notes.append(f"мест, где все три круга разошлись: {split} — "
                     "субтитры там от самой крупной модели, проверьте их "
                     "глазами")
    return words, notes



# ── правка субтитров целиком ───────────────────────────────────────────
#
# Заменами «как слышно -> как правильно» правится опечатка, но не текст.
# Человек, который смотрит готовый ролик, видит сразу всё: тут имя
# переврано, тут окончание, тут лишнее «вот». Выписывать это пятью
# строками замен он не станет — он перепишет абзац, как переписывают
# текст, и это правильная форма правки.
#
# Поэтому под карточкой есть второй путь: монтаж отдаёт субтитры одной
# строкой, человек присылает их обратно поправленными, а замены для
# словаря код выводит сам — сличая присланное с услышанным. Словарь при
# этом остаётся домом правки: следующий дубль приедет уже исправленным,
# а не потеряет правку вместе с этой карточкой.
#
# Границы здесь держит код, а не просьба. Замена длиной в полфразы в
# словарь не годится — она не повторится ни на одном другом дубле.
# Текст, изменённый больше чем на треть, это не правка, а другая речь:
# человек прислал не тот файл или переписал ролик заново, и молча
# завести двадцать бессмысленных правил хуже, чем отказаться.

# Больше этого в одно сообщение Telegram не влезет, а собирать
# присланное из нескольких — угадывать, где кончилась правка.
WHOLE_MAX = 3500
# Слов в одной замене. Длиннее — это не словарь, а другой текст.
WHOLE_RUN = 6
# Доля изменённых слов, после которой это уже не правка субтитров.
WHOLE_SHARE = 0.34

_EDGE = " \t,.!?;:«»\"\'…()–—"


def subtitle_text(words: list[dict]) -> str:
    """Субтитры ролика одной строкой — так, как человек их видит в кадре."""
    out = [str(w["text"]).strip() for w in words]
    return " ".join(t for t in out if t)


def _back(shown: list[dict], raw: list[dict]) -> list[tuple[int, int] | None]:
    """Слово субтитров → то, что на его месте услышал whisper.

    Левая часть правила обязана быть услышанным, а не прочитанным в
    кадре: словарь применяется к сырой расшифровке. Разница между этими
    списками — ровно то, что словарь уже переписал, и если переписал
    неверно, новая строка встанет на тот же ключ и перебьёт старую:
    правка человека, который только что смотрел ролик, сильнее прошлого
    правила.

    Поэтому сходятся не только совпавшие куски, но и заменённые: слово в
    кадре знает, из какого услышанного оно получилось. Не сходится один
    случай — правило, развернувшее одно слово в два: там на два слова
    кадра приходится один источник, и оба указывают на него целиком.
    """
    out: list[tuple[int, int] | None] = [None] * len(shown)
    a = [norm(str(w["text"])) for w in shown]
    b = [norm(str(w["text"])) for w in raw]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal" or (tag == "replace" and (i2 - i1) == (j2 - j1)):
            for k in range(i2 - i1):
                out[i1 + k] = (j1 + k, j1 + k + 1)
        elif tag == "replace":
            for i in range(i1, i2):
                out[i] = (j1, j2)
    return out


def _span(shown: list[dict], raw: list[dict],
          back: list[tuple[int, int] | None], i1: int, i2: int) -> str:
    """Левая часть правила: услышанное, если оно на месте, иначе показанное."""
    slots = [back[i] for i in range(i1, i2)]
    if all(s is not None for s in slots):
        lo, hi = slots[0][0], max(s[1] for s in slots)
        if hi - lo == len({j for s in slots for j in range(s[0], s[1])}):
            return " ".join(str(w["text"]).strip() for w in raw[lo:hi])
    return " ".join(str(shown[i]["text"]).strip() for i in range(i1, i2))


def whole_pairs(shown: list[dict], raw: list[dict], text: str
                ) -> tuple[list[tuple[str, str]], list[str]]:
    """Поправленный человеком текст субтитров → замены для словаря.

    `shown` — что человек видел в кадре, `raw` — что услышал whisper до
    словаря. Вторым возвращается то, что надо сказать человеку: пустой
    список значит «разобрано без остатка».
    """
    new = [t for t in (text or "").split() if t.strip(_EDGE)]
    if not shown:
        return [], ["субтитров у этого ролика нет — сличать не с чем"]
    if not new:
        return [], ["присланный текст пустой — словарь я не тронула"]

    a = [norm(str(w["text"])) for w in shown]
    b = [norm(t) for t in new]
    ops = [op for op in difflib.SequenceMatcher(
        a=a, b=b, autojunk=False).get_opcodes() if op[0] != "equal"]
    if not ops:
        return [], ["текст совпадает со субтитрами слово в слово — "
                    "править нечего"]

    # Знаки препинания в счёт изменённого не идут. Человек, переписывая
    # текст, ставит запятые по-своему, а whisper держит их приклеенными к
    # слову: считать их правкой значит упираться в порог на ровном месте.
    runs: list[tuple[int, int, int, int]] = []
    for _, i1, i2, j1, j2 in ops:
        if i1 == i2:
            # Вставка: сама по себе правилом не станет — искать нечего.
            # Цепляем её к соседнему слову, которое услышано так же.
            if i1 > 0:
                i1, j1 = i1 - 1, j1 - 1
            elif i2 < len(shown):
                i2, j2 = i2 + 1, j2 + 1
            else:
                continue
        left = " ".join(a[i1:i2])
        right = " ".join(b[j1:j2])
        if not left.strip() or left == right:
            continue
        runs.append((i1, i2, j1, j2))

    moved = sum(max(i2 - i1, j2 - j1) for i1, i2, j1, j2 in runs)
    if moved > len(shown) * WHOLE_SHARE:
        return [], [f"изменённых слов {moved} из {len(shown)} — это уже "
                    "другой текст, а не правка субтитров, и словарь я не "
                    "тронула. Пришлите субтитры ролика с правками."]

    back = _back(shown, raw)
    pairs: list[tuple[str, str]] = []
    notes: list[str] = []
    seen: set[str] = set()
    for i1, i2, j1, j2 in runs:
        src = _span(shown, raw, back, i1, i2).strip(_EDGE)
        dst = " ".join(new[j1:j2]).strip(_EDGE)
        if not norm(src) or norm(src) == norm(dst):
            continue
        if i2 - i1 > WHOLE_RUN or j2 - j1 > WHOLE_RUN:
            notes.append(f"«{src[:40]}» переписано целой фразой — в словарь "
                         "такое не пошло: правило длиной в полфразы не "
                         "повторится ни на одном другом дубле")
            continue
        if norm(src) in seen:
            continue                    # то же слово дважды в одном тексте
        seen.add(norm(src))
        pairs.append((src, dst))
    if not pairs and not notes:
        # Разошлись только знаки препинания: слова те же. Пересобирать
        # ролик незачем, а молчать нельзя — человек ждёт ответа на правку.
        notes.append("в словах ничего не изменилось — разошлись только "
                     "знаки, пересобирать ролик не из-за чего")
    return pairs, notes


# ── караоке: слова в страницы ─────────────────────────────────────────
#
# На экране держится не предложение, а два слова: строку целиком глаз не
# ловит. Страница набирается по числу слов и рвётся на длинной паузе —
# иначе фраза, разорванная вдохом, доедет до кадра склеенной.
#
# Было четыре — из соображения «одно слово читать не успеваешь». Замер
# 04.11 по пяти чужим роликам это соображение не подтвердил: у
# `aa.rashchupkina` в кадре стоит ровно одно слово, у `mironova.dii` —
# одно-два, и читаются они легче нашей горсти, потому что слово крупнее
# и держится дольше паузы между ними. Ставим два: одно — чужой почерк
# целиком, четыре — строка, которую не ловит глаз.

PAGE_WORDS = 2
PAGE_GAP = 0.7          # пауза длиннее — начинаем новую страницу
PAGE_MAX = 3.2          # и дольше этого страница не висит
# Знаков на страницу, с пробелами. Четыре длинных слова капсом давали
# три строки: «НАША ЗАДАЧА ПРОДОЛЖАЕТ ЗАКЛЮЧАТЬСЯ» наезжала на панель в
# сплите 16.09.
#
# Было 30 под кегль 0.032 высоты, в строку которого влезало около 22
# знаков. Кегль поднят вместе с `PAGE_WORDS` до 0.040 — в строку влезает
# около семнадцати, и прежние 30 снова дали бы две строки. Два длинных
# слова капсом это и есть потолок.
PAGE_CHARS = 20


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
            chars = sum(len(str(c["text"])) + 1 for c in cur) + len(str(w["text"]))
            if (len(cur) >= PAGE_WORDS or gap > PAGE_GAP or span > PAGE_MAX
                    or chars > PAGE_CHARS):
                flush()
        cur.append(w)
    flush()
    return out


# ── перекладка на готовую дорожку ─────────────────────────────────────

# Меньше этого от слова в ролике не остаётся слышно. Граница куска
# ставится по тишине и по просьбе, а тайминги слов приходят от whisper с
# точностью до десятых — поэтому слово то свисает за край куска, то
# целиком лежит за ним, начинаясь ровно на границе.
#
# Порог меряет только то, что свисает за край. Слово, целиком лежащее
# внутри куска, остаётся при любой длине: whisper отдаёт «и», «то», «все»
# длиной в две сотых, и общий порог выбрасывал их из середины фразы.
MIN_HEARD = 0.08


def cut_words(words: list[Word], tl: Timeline) -> list[dict[str, float | str]]:
    """Слова в время готового ролика. Попавшие в вырезанное — выброшены.

    Решает это **перекрытие** слова с куском, а не одна его метка начала.
    По метке начала терялись два слова разом и в обе стороны: «Как» и
    «Третий» звучали в ролике, а в субтитрах их не было — whisper
    поставил начало на две десятых раньше, чем кончилась тишина; а
    «Здесь» в субтитрах было, хотя в ролике не звучало вовсе — слово
    начиналось ровно на секунде, по которой кусок обрезан. И то и другое
    человек видит в кадре как ошибку расшифровки, а не как границу куска.
    """
    marks: list[tuple[float, float, float]] = []
    clock = 0.0
    for a, b in tl.keep:
        marks.append((a, b, clock))
        clock += b - a

    out = []
    for w in words:
        best = max(marks, key=lambda m: min(w.end, m[1]) - max(w.start, m[0]),
                   default=None)
        if best is None:
            continue
        a, b, base = best
        inside = w.start >= a and w.end <= b
        heard = min(w.end, b) - max(w.start, a)
        if not inside and heard < MIN_HEARD:
            continue
        start = base + max(w.start, a) - a
        end = base + min(w.end, b) - a
        out.append({"text": w.text, "start": round(start, 3),
                    "end": round(max(end, start + 0.12), 3)})
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


# ── привязка к слову ──────────────────────────────────────────────────
#
# Панель сплита меняется не по таймеру, а по речи: пункт списка выезжает
# в ту секунду, когда человек его назвал. Секунду даёт расшифровка, и
# ищется она по словам, а не по номеру слова — номер сдвинется на первом
# же перемонтаже, а фраза останется той же.

_FLAT_RX = re.compile(r"[^\w\s-]+", re.UNICODE)


def _flat(text: str) -> list[str]:
    """Слова без знаков и без «ё»: расшифровка их ставит не так, как
    человек, и точка после слова не должна ломать поиск."""
    return _FLAT_RX.sub(" ", (text or "").lower().replace("ё", "е")).split()


def span(words: list[dict], phrase: str) -> tuple[float, float] | None:
    """Где в записи лежит `phrase`: от начала первого слова до конца
    последнего. Не нашлось — `None`.

    Отличается от `anchor` только концом. Нужна тем, кто фразу вырезает,
    а не ставит на неё блок: вырезать по началу нечего.
    """
    need = _flat(phrase)
    if not need or not words:
        return None
    flat: list[tuple[str, float, float]] = []
    for w in words:
        for tok in _flat(str(w["text"])):
            flat.append((tok, float(w["start"]), float(w["end"])))
    for i in range(len(flat) - len(need) + 1):
        if [tok for tok, _, _ in flat[i:i + len(need)]] == need:
            return flat[i][1], flat[i + len(need) - 1][2]
    return None


# Человек называет кусок словами, а не секундами: «убери „но и в режиме
# такой“». Кавычки обязательны — без них просьба «убери кашель» съела бы
# слово «кашель» из речи. Глагол тоже: цитата в просьбе бывает и не про
# вырезание («поставь „Cowork“ на первый блок»).
CUT_VERB = re.compile(r"убер|вырез|выкин|удал|убрат", re.IGNORECASE)
CUT_QUOTE = re.compile(r"[«\"']([^«»\"']{4,120})[»\"']")


def asked_cuts(words: list[dict], ask: str) -> tuple[list[tuple[float, float]],
                                                     list[str]]:
    """Куски, которые человек попросил вырезать словами. И что не нашлось.

    Правило про повтор ловит заход, начатый теми же словами. Оборванный
    кусок, после которого человек продолжил другими словами, оно не
    ловит и ловить не должно — угадывать, где мысль брошена, нечем.
    Зато человек это слышит сразу, и сказать словами ему дешевле, чем
    объяснять секунды.
    """
    if not ask or not CUT_VERB.search(ask):
        return [], []
    spans: list[tuple[float, float]] = []
    lost: list[str] = []
    for m in CUT_QUOTE.finditer(ask):
        phrase = m.group(1).strip()
        found = span(words, phrase)
        if found is None:
            lost.append(f"«{phrase[:40]}» в записи не нашлось — не вырезано")
            continue
        spans.append(found)
    return spans, lost


def anchor(words: list[dict], phrase: str) -> float | None:
    """Секунда, с которой в дубле звучит `phrase`. Не нашлось — `None`.

    Слова на входе уже во времени готового ролика (`cut_words`), в нём же
    и ответ. Берётся **первое** вхождение: панель ведёт речь сверху вниз,
    и второй раз то же слово звучит уже под следующим блоком.

    Не нашлось — это не поломка: человек сказал своими словами, а не
    теми, что в сценарии. Решает за это вызывающий, здесь только ответ.
    """
    need = _flat(phrase)
    if not need or not words:
        return None
    # Одно слово расшифровки распадается на два токена («что-то»), и
    # обратное тоже бывает. Поэтому сравниваем плоским списком токенов,
    # помня за каждым секунду того слова, из которого он вышел.
    flat: list[tuple[str, float]] = []
    for w in words:
        for tok in _flat(str(w["text"])):
            flat.append((tok, float(w["start"])))
    for i in range(len(flat) - len(need) + 1):
        if [tok for tok, _ in flat[i:i + len(need)]] == need:
            return flat[i][1]
    return None


# Короче этого кусок фразы не ищется: по двум словам блок встаёт на
# случайное место записи, и это хуже, чем блок, который не встал вовсе.
LOOSE_MIN = 3


def loose_anchor(words: list[dict], phrase: str) -> float | None:
    """То же, что `anchor`, но прощает роли неточную цитату.

    Точное совпадение важнее: сперва ищем фразу целиком. Не нашлось —
    ищем самый длинный её кусок подряд, не короче трёх слов, и берём
    первый, который в записи есть. Кусок, а не обрезанные края: роль
    промахивается и серединой тоже — «Висплатной версии Claude тоже»
    против «висплатной версии клода тоже», где словарь бренда поправил
    имя уже после того, как роль его прочитала.

    Меньше трёх слов не ищем вовсе: по двум блок встанет на случайное
    место, а это хуже, чем блок, который не встал.

    Нужна материалам из альбома: запись экрана, потерянная из-за
    неточной цитаты, — это материал, который человек снял и не увидел
    (дубль 22.09, запись про проекты).
    """
    exact = anchor(words, phrase)
    if exact is not None:
        return exact
    parts = str(phrase).split()
    for length in range(len(parts) - 1, LOOSE_MIN - 1, -1):
        for i in range(0, len(parts) - length + 1):
            at = anchor(words, " ".join(parts[i:i + length]))
            if at is not None:
                return at
    return None


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
    # Наклон головы в радианах, как его отдаёт Vision. `pitch` растёт,
    # когда лицо опускается: замер на дубле 18.09 — 0,17 в камеру, 0,55
    # на экран ноутбука. Знак проверен кадрами, а не выведен из
    # документации: у Apple он нигде не назван.
    # `None` — старая ревизия детектора, и это не поломка: рамка лица
    # работает без углов, обложка собирается как прежде.
    pitch: float | None = None
    yaw: float | None = None
    roll: float | None = None

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def _face_of(raw: dict) -> list[Face]:
    out = []
    for f in raw.get("faces") or []:
        angle = lambda k: (None if f.get(k) is None else float(f[k]))
        face = Face(float(f.get("x") or 0), float(f.get("y") or 0),
                    float(f.get("w") or 0), float(f.get("h") or 0),
                    float(f.get("conf") or 0),
                    angle("pitch"), angle("yaw"), angle("roll"))
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


# ── слова на экране ───────────────────────────────────────────────────
#
# Скриншот на панели показывает окно целиком, а речь идёт про одну строку
# в нём: «память», «проекты», «скиллы». Подчеркнуть нужное слово нельзя,
# не зная, где оно лежит, — и узнаётся это тем же Vision, который считает
# лица и силуэт (`tools/ocrbox.swift`). Ни новой библиотеки в venv, ни
# модели на диск.
#
# Распознавателя нет или слово не нашлось — это **не** поломка: блок
# остаётся записью экрана, как был до 22.09, и человеку об этом строка.

OCR_SCRIPT = ROOT / "tools" / "ocrbox.swift"
OCR_TIMEOUT = 300
OCR_MIN_CONF = 0.3


class NoOcr(RuntimeError):
    """Распознаватель текста не отозвался."""


@dataclass
class Spot:
    """Кусок текста на кадре: доли кадра от левого верхнего угла."""
    text: str
    x: float
    y: float
    w: float
    h: float
    conf: float = 0.0
    # Строка целиком или слово внутри неё. Подчёркивать можно и то и
    # другое, а показывать роли — только строки: список, где рядом со
    # строкой лежат её же слова по одному, читается мусором.
    whole: bool = True


def _spots_of(raw: dict) -> list[Spot]:
    out: list[Spot] = []
    for line in raw.get("lines") or []:
        words = [Spot(str(w.get("text") or ""), float(w.get("x") or 0),
                      float(w.get("y") or 0), float(w.get("w") or 0),
                      float(w.get("h") or 0), float(w.get("conf") or 0),
                      False)
                 for w in (line.get("words") or [])]
        out.append(Spot(str(line.get("text") or ""), float(line.get("x") or 0),
                        float(line.get("y") or 0), float(line.get("w") or 0),
                        float(line.get("h") or 0), float(line.get("conf") or 0)))
        out.extend(words)
    return [s for s in out if s.text.strip() and s.conf >= OCR_MIN_CONF
            and s.w > 0 and s.h > 0]


async def words_on(shots: list[Path]) -> list[list[Spot]]:
    """Текст на каждом кадре: строки целиком и слова по отдельности."""
    if not shots:
        return []
    if not OCR_SCRIPT.exists():
        raise NoOcr(f"нет {OCR_SCRIPT.name}")
    proc = await asyncio.create_subprocess_exec(
        "swift", str(OCR_SCRIPT), *[str(p) for p in shots],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(),
                                          timeout=OCR_TIMEOUT)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise NoOcr("распознаватель текста не ответил") from e
    if proc.returncode != 0:
        raise NoOcr(err.decode(errors="replace").strip()[-200:]
                    or "swift вернул ошибку")

    found: dict[str, list[Spot]] = {}
    for line in out.decode(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        found[str(raw.get("path") or "")] = _spots_of(raw)
    return [found.get(str(p), []) for p in shots]


def find_spot(spots: list[Spot], mark: str) -> Spot | None:
    """Где на кадре лежит названное слово. Не нашлось — `None`.

    Сначала точное совпадение слова, потом строка, которая его содержит:
    «Generate memory from chats» подчёркивается целиком, если роль
    назвала строку, и одним словом, если назвала слово.
    """
    key = _norm(mark)
    if not key:
        return None
    exact = [s for s in spots if _norm(s.text) == key]
    if exact:
        return max(exact, key=lambda s: s.conf)
    inside = [s for s in spots if key in _norm(s.text)]
    if not inside:
        return None
    # Самый тесный из подходящих: строка целиком хуже слова в ней.
    return min(inside, key=lambda s: s.w * s.h)


# ── взгляд вниз ───────────────────────────────────────────────────────
#
# «Убери, где я смотрю в экран» — это не тишина. Человек читает свой же
# текст с ноутбука и при этом говорит: нарезка по паузам такой кусок
# оставляет, а в кадре остаётся макушка.
#
# Считать это по картинке не нужно: ревизия 3 детектора лиц отдаёт наклон
# головы сама, одним проходом, уже оплаченным обложкой. Порог снят с
# материала, а не назначен: на дубле 18.09 лицо в камеру давало `pitch`
# 0,17 (медиана), взгляд в ноутбук — 0,30 и выше. 0,28 это середина
# зазора, и она проверена кадрами с обеих сторон.
#
# Порог остаётся порогом одной съёмки: другая камера, другая высота
# стола — другой ноль. Поэтому он аргументом, а не вшит в `looking_down`
# намертво, и тот, кто его двигает, обязан посмотреть кадры.
#
# Поэтому же порог по умолчанию **относительный**, а не абсолютный
# (правка 24.09, после дубля `IMG_8077`). Там камера стояла выше, и весь
# дубль уехал в минус: медиана −0,25, максимум 0,21, то есть абсолютный
# 0,28 не поймал ни одного кадра — а глазами видно, что в камеру человек
# смотрит на −0,62, а вниз на 0,21. Шкала работала, ноль был чужой.
#
# Свой ноль у съёмки — медиана её же наклонов: голова чаще смотрит туда,
# куда человек говорит. Зазор до «опущен» взят с той съёмки, где его
# мерили кадрами: 0,28 − 0,17 = 0,11. Абсолютный порог остаётся входом
# для того, кто посмотрел кадры и знает число.

GAZE_FPS = 5              # чаще незачем: голова не дёргается покадрово
GAZE_DOWN = 0.28          # pitch выше — лицо опущено (замер 18.09)
GAZE_LIFT = 0.11          # насколько выше своего нуля — уже вниз
GAZE_LEAST = 0.4          # окно короче — моргнул, а не отвёл глаза
GAZE_PAD = 0.1            # воздух по краям окна: срез впритык видно


async def head_angles(video: Path, work: Path, *,
                      fps: int = GAZE_FPS) -> list[tuple[float, Face]]:
    """Наклон головы по всему дублю: секунда и лицо на ней.

    Кадры снимаются мелкими (480 по ширине): детектору хватает, а
    трёхсот полноразмерных JPEG на минуту записи не хватит никому.
    """
    work.mkdir(parents=True, exist_ok=True)
    for old in work.glob("*.jpg"):
        old.unlink()
    code, _, err = await _run(
        str(FFMPEG), "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-an", "-r", str(fps),
        "-vf", "scale=480:-2", "-q:v", "4",
        str(work / "%04d.jpg"), timeout=900)
    if code != 0:
        raise NoFfmpeg("не снял кадры для взгляда: "
                       + err.decode(errors="replace").strip()[-200:])

    shots = sorted(work.glob("*.jpg"))
    found = await faces(shots)
    out: list[tuple[float, Face]] = []
    for i, (shot, seen) in enumerate(zip(shots, found)):
        if not seen:
            continue                      # лица нет — и судить не о чем
        out.append((i / fps, max(seen, key=lambda f: f.conf)))
    return out


def merge_spans(spans: list[tuple[float, float]],
                gap: float = 0.0) -> list[tuple[float, float]]:
    """Слить пересекающиеся отрезки в один список без нахлёстов."""
    out: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if out and a - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def gaze_level(points: list[tuple[float, Face]], *,
               lift: float = GAZE_LIFT) -> float | None:
    """Порог «взгляд вниз» для этой съёмки. Углов нет — `None`.

    Ноль у каждой съёмки свой (см. выше), и берётся он из неё самой:
    медиана наклонов плюс зазор.
    """
    vals = [f.pitch for _, f in points if f.pitch is not None]
    if not vals:
        return None
    return statistics.median(vals) + lift


def looking_down(points: list[tuple[float, Face]], *,
                 thresh: float | None = None, least: float = GAZE_LEAST,
                 pad: float = GAZE_PAD, lift: float = GAZE_LIFT,
                 fps: int = GAZE_FPS) -> list[tuple[float, float]]:
    """Отрезки, где лицо опущено. Углов нет — пустой список.

    `thresh` не назван — порог считается от самой съёмки
    (`gaze_level`). Названный идёт как есть: его ставит тот, кто
    посмотрел кадры.

    Отдаётся в той же форме, что и тишина, и сливается с ней одним
    списком: `timeline` не должна знать, почему кусок выброшен.
    """
    if thresh is None:
        thresh = gaze_level(points, lift=lift)
        if thresh is None:
            return []
    step = 1 / fps
    runs: list[tuple[float, float]] = []
    start: float | None = None
    last = 0.0
    for t, face in points:
        down = face.pitch is not None and face.pitch > thresh
        if down and start is None:
            start = t
        elif not down and start is not None:
            runs.append((start, last + step))
            start = None
        last = t
    if start is not None:
        runs.append((start, last + step))

    return merge_spans([(max(0.0, a - pad), b + pad)
                        for a, b in runs if b - a >= least])


# ── силуэт человека ───────────────────────────────────────────────────
#
# Маскот за спиной требует того, чего нарезка и панорама не требовали
# никогда: знать, где в кадре человек. Делает это Vision — тот же
# системный framework, что считает лица, — и потому матирование здесь
# стоит один проход по кадрам, а не чужую модель на диске.
#
# Маски снимаются **со всего дубля**, а не с оставшихся кусков: кадр
# ролика знает своё время в исходнике, и маска ищется по нему. Считать их
# по нарезке значит пересчитывать всё заново после каждой правки границ.
#
# Разрешение маски вдвое меньше кадра. Край от этого мягче на полпикселя,
# и это не потеря, а подарок: жёсткий срез по контуру выдаёт монтаж
# сильнее, чем мягкий.

MASK_SCRIPT = ROOT / "tools" / "personmask.swift"
MASK_FPS = 30             # кадр ролика ищет свою маску по номеру
MASK_WIDTH = 540
MASK_TIMEOUT = 1800
# Сетка замера перекрытия. Те же числа стоят в `personmask.swift`: клетку
# считает он, потому что силуэт уже у него в руках, а прочесть маски
# вторым проходом нечем: на бандловой сборке нет ни `alphaextract`, ни
# мукcера `rawvideo`, Pillow в этом проекте не живёт, а полная сборка
# есть не на всякой машине — шим её только предпочитает, не обещает.
MASK_COLS = 64
MASK_ROWS = 114


async def person_masks(video: Path, work: Path, *,
                       fps: int = MASK_FPS,
                       width: int = MASK_WIDTH,
                       seconds: float | None = None
                       ) -> tuple[Path, int, int, list[tuple[int, bytes]]]:
    """Силуэт человека на каждом кадре дубля.

    Отдаёт папку с масками, сколько их вышло, на скольких кадрах человека
    не нашлось и силуэт крупной клеткой на каждый удавшийся кадр. Имя
    маски — номер кадра при `fps`, считая от нуля: `mask-00042.png` это
    42/fps секунды исходника.

    `seconds` обрезает проход по времени исходника. Маскот живёт первые
    секунды ролика, и маски дальше его ухода не нужны никому: на дубле
    18.09 окно в девять секунд стоило 285 масок вместо 1762.
    """
    if not MASK_SCRIPT.exists():
        raise NoVision(f"нет {MASK_SCRIPT.name}")

    shots = work / "shots"
    masks = work / "masks"
    for folder in (shots, masks):
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.png"):
            old.unlink()
        for old in folder.glob("*.jpg"):
            old.unlink()

    code, _, err = await _run(
        str(FFMPEG), "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-an", "-r", str(fps),
        *(["-t", f"{seconds:.3f}"] if seconds else []),
        "-vf", f"scale={width}:-2", "-q:v", "3",
        str(shots / "%05d.jpg"), timeout=1800)
    if code != 0:
        raise NoFfmpeg("не снял кадры для маски: "
                       + err.decode(errors="replace").strip()[-200:])

    # ffmpeg нумерует с единицы, а ролик считает кадры с нуля. Сдвиг
    # делается здесь, один раз, а не в шаблоне на каждом кадре.
    pairs: list[str] = []
    for shot in sorted(shots.glob("*.jpg")):
        n = int(shot.stem) - 1
        pairs += [str(shot), str(masks / f"mask-{n:05d}.png")]

    proc = await asyncio.create_subprocess_exec(
        "swift", str(MASK_SCRIPT), *pairs,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(),
                                          timeout=MASK_TIMEOUT)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise NoVision(f"маски не уложились в {MASK_TIMEOUT} с") from e
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip().splitlines()[-2:]
        raise NoVision("маски не собрались: " + " | ".join(tail))

    grid: list[tuple[int, bytes]] = []
    missed = 0
    cell = MASK_COLS * MASK_ROWS
    for line in out.decode(errors="replace").splitlines():
        if line.startswith("fail"):
            missed += 1
            continue
        parts = line.split(" ")
        if len(parts) < 3:
            continue                      # маска есть, клетки нет — не беда
        with contextlib.suppress(ValueError, binascii.Error):
            raw = base64.b64decode(parts[2], validate=True)
            if len(raw) == cell:
                grid.append((int(Path(parts[1]).stem) - 1, raw))
    return masks, len(list(masks.glob("*.png"))), missed, grid


def clear_of_speech(spans: list[tuple[float, float]],
                    words: list["Word"],
                    pad: float = 0.12) -> list[tuple[float, float]]:
    """Те же отрезки, но без тех мест, где человек говорит.

    Взгляд вниз выбрасывается ради картинки, и картинка не стоит слова:
    на дубле 18.09 два окна из семи пришлись на речь, и срез по ним унёс
    «Если» и «То» вместе со звуком — фраза в кадре начиналась с середины.
    Поэтому речь сильнее взгляда: где человек говорит глядя вниз, кусок
    остаётся, и об этом говорится строкой, а не молчанием.
    """
    said = merge_spans([(w.start - pad, w.end + pad) for w in words])
    out: list[tuple[float, float]] = []
    for a, b in spans:
        cur = a
        for sa, sb in said:
            if sb <= cur or sa >= b:
                continue
            if sa > cur:
                out.append((cur, min(sa, b)))
            cur = max(cur, sb)
            if cur >= b:
                break
        if cur < b:
            out.append((cur, b))
    return [(a, b) for a, b in out if b - a > 0]


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


FACE_TRACK_SHOTS = 10     # кадров на весь дубль: голова ходит медленно


async def face_points(video: Path, duration: float, work: Path, *,
                      n: int = FACE_TRACK_SHOTS) -> list[tuple[float, Face]]:
    """Где лицо по ходу дубля: секунда записи и самое крупное лицо в кадре.

    Нужно сплиту. Трек активности (`pan`) едет за движением, а в
    говорящей голове движутся руки: в сплите 15.09 окно головы стояло на
    груди, и лицо было срезано по глаза весь ролик. Кадр без лица просто
    пропускается — пустой список значит, что лица не нашлось нигде.
    """
    start = STILL_SKIP
    end = max(start, duration - 0.5)
    times = ([round(start + (end - start) * (i + 0.5) / n, 2) for i in range(n)]
             if end > start else [round(duration / 2, 2)])
    work.mkdir(parents=True, exist_ok=True)
    probes: list[Path] = []
    try:
        for i, t in enumerate(times):
            probes.append(await still(video, t, work / f".face-{i}.png"))
        found = await faces(probes)
    finally:
        for p in probes:
            p.unlink(missing_ok=True)
    return [(t, max(fs, key=lambda f: f.w * f.h))
            for t, fs in zip(times, found) if fs]


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


def relex_heard(words: list[Word], rules: list[tuple[str, str]]) -> list[Word]:
    """Услышанное по словарю бренда, теми же объектами.

    `relex` работает со словарями — это форма субтитров. Ролям же нужно
    услышанное с исходными метками, а оно живёт в `Word`, поэтому тут
    перекладка туда и обратно, а правило замены остаётся одно.
    """
    fixed, _ = relex([{"text": w.text, "start": w.start, "end": w.end}
                      for w in words], rules)
    return [Word(str(w["text"]), float(w["start"]), float(w["end"]))
            for w in fixed]


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
        # Знаки препинания `norm` не видит, поэтому в совпадение затекает
        # и одинокое тире, стоящее словом: whisper отбивает им паузу.
        # Приклеенное к замене, оно выходит в кадр как «CLAUDE– ЭТО».
        # Тире — соседнее слово, а не хвост этого, и идёт своим ходом.
        while n > 1 and not _WORD_RX.search(str(words[i + n - 1]["text"])):
            n -= 1
        out.extend(_respan(words[i:i + n], dst))
        fixed += n
        i += n
    return out, fixed
