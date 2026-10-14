"""Сплит из готовой говорящей головы: видео плюс список карточек → ролик.

    ./.venv/bin/python tools/avatar_split.py --video tmp/head.mp4 \
        --blocks tmp/blocks.json --out tmp/reel.mp4

Зачем отдельный вход. Сплит (`Motion`) завод собирает одним путём — от
снятого дубля: человек присылает файл в 🎬 Reels, Монтажёр ставит
границы, код режет, панель набирается по сценарию. У говорящей головы
из HeyGen (`tools/heygen_pull.py`) границы ставить не на чем: пауз в ней
нет, оговорок нет, резать нечего. Остаётся ровно одно — разложить
карточки панели по словам и позвать рендер.

Темы в базе при этом не заводится. Это сознательно: карточки темы у
такого ролика нет, и правка пойдёт тем же кругом — поправить
`blocks.json` и позвать команду заново. Цена круга здесь секунды
рендера, а не вызов модели.

Секунды блоков снимает whisper с самого файла (`footage.captions`), а не
суфлёр: тайминг суфлёра считается от скорости чтения, а аватар говорит
в своём темпе. Поэтому карточка привязывается к **слову**, а не к
секунде: в `blocks.json` пишется `at` фразой («Пятый»), и код ищет её в
расшифровке. Секунду можно поставить и руками (`start`), но тогда она
переживёт следующую перегенерацию головы молча.

**Недокачанный mp4 выглядит целым.** Браузер отдаёт файл из папки
`…mp4.download`, ffprobe называет его длину по заголовку, а данных в нём
меньше: 14.10 расшифровка молча кончилась на 35-й секунде из 51, и
пятый блок встал по оценке. Поэтому код сверяет конец расшифровки с
длиной дорожки и говорит расхождение строкой, а не продолжает.

Формат `blocks.json` — список карточек сверху вниз:

    [
      {"kind": "counter", "value": 5, "at": "пять",
       "lines": ["плагинов", "работают каждый день"]},
      {"kicker": "ПЕРВЫЙ", "at": "Первый",
       "lines": ["brand-content-pipeline", "голос и план бренда"]}
    ]

`kind` по умолчанию `icon`: имя крупно пружиной, пояснение мельче.
Остальные сорта — те же, что у монтажа (`montage.MOTION_KINDS`).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import footage, montage                         # noqa: E402

# Воздух между карточками: панель гаснет до того, как человек назвал
# следующую. Без него карточка подменяется на полуслове, и глаз читает
# это как сбой, а не как смену.
GAP = 0.30

# Хвост без панели. Короче — ролик кончается швом посередине кадра;
# в заводе та же граница и то же число (`montage.SOLO_SECONDS`).
SOLO = montage.SOLO_SECONDS

# Насколько расшифровка может не дотянуть до конца дорожки, пока это
# тишина в конце, а не оборванный файл.
TAIL_SLACK = 2.5


class NoBlocks(RuntimeError):
    """Карточки панели не разложились по записи."""


def _spoken(phrase: str, words: list[dict], what: str) -> float:
    at = footage.anchor(words, phrase)
    if at is None:
        raise NoBlocks(f"{what}: фразы «{phrase}» в записи нет")
    return round(at, 2)


def _starts(cards: list[dict], words: list[dict]) -> list[float]:
    """Секунда каждой карточки: по слову из записи или поставленная рукой.

    `start` сильнее `at`: у первой карточки они расходятся нарочно —
    счётчик стоит на панели с нулевой секунды, а число набирается позже,
    на слове.
    """
    out: list[float] = []
    for i, card in enumerate(cards):
        if card.get("start") is not None:
            out.append(float(card["start"]))
            continue
        phrase = str(card.get("at") or "").strip()
        if not phrase:
            raise NoBlocks(f"карточка {i + 1}: нет ни «at», ни «start»")
        out.append(_spoken(phrase, words, f"карточка {i + 1}"))
    if out != sorted(out):
        raise NoBlocks("карточки идут не по порядку записи: "
                       + ", ".join(str(s) for s in out))
    return out


def _blocks(cards: list[dict], words: list[dict],
            total: float) -> list[dict]:
    starts = _starts(cards, words)
    ends = [s - GAP for s in starts[1:]] + [max(0.0, total - SOLO)]
    out: list[dict] = []
    for card, start, end in zip(cards, starts, ends):
        if end - start < montage.BLOCK_MIN:
            raise NoBlocks(f"«{(card.get('lines') or ['?'])[0]}»: на панели "
                           f"{round(end - start, 1)} с, короче "
                           f"{montage.BLOCK_MIN} с — это мигание")
        block: dict = {
            "start": round(start, 2), "end": round(end, 2),
            "kind": card.get("kind", "icon"),
            "lines": list(card.get("lines") or []),
            "items": [], "full": False,
        }
        if card.get("kicker"):
            block["kicker"] = str(card["kicker"])
        if card.get("value") is not None:
            block["value"] = int(card["value"])
            phrase = str(card.get("at") or "").strip()
            # Число набирается на своём слове, даже когда карточка стоит
            # на панели раньше. Счётчик, тронувшийся не на том слове,
            # хуже неподвижного числа.
            block["valueAt"] = (_spoken(phrase, words, "счётчик")
                                if phrase and card.get("start") is not None
                                else round(start, 2))
        out.append(block)
    return out


def props(video_name: str, probe: footage.Probe, blocks: list[dict],
          pages: list[dict], *, fps: int = 30,
          head: str = "bottom", split: float = montage.MOTION_SPLIT) -> dict:
    """JSON для композиции `Motion`. Палитра и доли — те же, что у монтажа:
    разъехавшись, они дадут второй фирменный стиль у той же пары роликов."""
    return {
        "videoPath": video_name,
        "videoWidth": probe.width, "videoHeight": probe.height,
        "segments": [{"from": 0.0, "to": round(probe.duration, 3)}],
        "pan": [], "headAnchor": head, "split": split,
        "panelColor": montage.REEL_PANEL_BG,
        "glowColor": montage._rgba(montage.REEL_ACCENT, montage.GLOW_ALPHA),
        "cardColor": montage._rgba(montage.REEL_PANEL_TEXT, montage.CARD_ALPHA),
        "accentColor": montage.REEL_ACCENT,
        "textColor": montage.REEL_PANEL_TEXT,
        "captionTextColor": montage.REEL_CAPTION_TEXT,
        "blocks": blocks, "pages": pages,
        "soloSeconds": SOLO, "introSeconds": 0.0, "outroSeconds": 0.0,
        "width": 1080, "height": 1920, "fps": fps,
    }


async def build(video: Path, cards: list[dict], out: Path, *,
                words_cache: Path | None = None, fps: int = 30,
                head: str = "bottom",
                split: float = montage.MOTION_SPLIT) -> list[str]:
    """Собрать ролик. Возвращает строки «что не сошлось», файл пишет сам."""
    notes: list[str] = []
    probe = await footage.probe(video)

    if words_cache and words_cache.exists():
        words = json.loads(words_cache.read_text(encoding="utf-8"))
        notes.append(f"расшифровка взята из {words_cache.name}, "
                     "заново не снималась")
    else:
        raw = await footage.captions(video)
        words = [{"text": w.text, "start": round(w.start, 2),
                  "end": round(w.end, 2)} for w in raw]
        if words_cache:
            words_cache.write_text(
                json.dumps(words, ensure_ascii=False, indent=1),
                encoding="utf-8")
    if not words:
        raise NoBlocks("в записи не разобрано ни одного слова")

    # Оборванный файл виден здесь и только здесь: ffprobe читает длину из
    # заголовка, а whisper — из данных.
    heard = words[-1]["end"]
    if probe.duration - heard > TAIL_SLACK:
        raise NoBlocks(
            f"расшифровка кончилась на {heard:.1f} с, а дорожка идёт "
            f"{probe.duration:.1f} с: файл скачан не целиком")

    blocks = _blocks(cards, words, probe.duration)
    pages = footage.pages(words)

    name = f"avatar-{out.stem}{video.suffix}"
    payload = props(name, probe, blocks, pages, fps=fps, head=head,
                    split=split)
    frames = max(1, round(probe.duration * fps))

    await montage._ready()
    with montage._staged({name: video}):
        made = await montage._render("Motion", payload, key=out.stem,
                                     frames=frames)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(made), out)
    return notes


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Сплит из готовой говорящей головы.")
    ap.add_argument("--video", required=True, help="mp4 с аватаром")
    ap.add_argument("--blocks", required=True,
                    help="json со списком карточек панели")
    ap.add_argument("--out", required=True, help="куда положить ролик")
    ap.add_argument("--words", default="",
                    help="json расшифровки: есть — берётся оттуда, "
                         "нет — снимется и ляжет туда")
    ap.add_argument("--head", default="bottom",
                    choices=list(montage.MOTION_HEADS),
                    help="где в кадре человек")
    ap.add_argument("--split", type=float, default=montage.MOTION_SPLIT,
                    help="доля холста под панель")
    args = ap.parse_args()

    video = Path(args.video).expanduser()
    if not video.exists():
        print(f"файла {video} нет", file=sys.stderr)
        return 2
    cards = json.loads(Path(args.blocks).expanduser().read_text(
        encoding="utf-8"))
    if not isinstance(cards, list) or not cards:
        print("в json с карточками ожидается непустой список",
              file=sys.stderr)
        return 2

    out = Path(args.out).expanduser()
    try:
        notes = asyncio.run(build(
            video, cards, out,
            words_cache=Path(args.words).expanduser() if args.words else None,
            head=args.head, split=args.split))
    except (NoBlocks, footage.NoWhisper, footage.NoFfmpeg,
            montage.NoRenderer, montage.NotInstalled) as e:
        print(str(e), file=sys.stderr)
        return 1

    print(out)
    for note in notes:
        print(note, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
