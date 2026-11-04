"""Анимированный клип в карточку панели: Higgsfield по тексту расшифровки.

    ./.venv/bin/python tools/higgsfield_pull.py --models
    ./.venv/bin/python tools/higgsfield_pull.py \
        --video tmp/head.mp4 --blocks tmp/blocks.json --dry-run
    ./.venv/bin/python tools/higgsfield_pull.py \
        --video tmp/head.mp4 --blocks tmp/blocks.json --words tmp/words.json
    ./.venv/bin/python tools/higgsfield_pull.py \
        --prompt "slow drift over a dark server hall" --out tmp/clip.mp4

Что он делает. Берёт тот же `blocks.json`, по которому собирается сплит
(`tools/avatar_split.py`), снимает расшифровку с самой головы и для
каждой карточки знает **что в этот момент звучит**: от её слова до слова
следующей. Этот кусок речи и становится запросом к генерации — не
дословно, а картинкой: русскую фразу в английский визуальный запрос
переводит модель одним вызовом на все карточки сразу (Claude Code по
подписке, `orchestrator/cli.py`, денег не стоит). Готовый клип ложится
файлом и прописывается карточке полем `video` — дальше сплит собирается
обычной командой.

Порядок именно такой — сначала клипы, потом сплит, — потому что поле
`video` сплит читает на входе, а не досыпает по ходу.

**Запрос показывается до оплаты.** `--dry-run` печатает, что уйдёт в
генерацию, и в сеть не идёт: Higgsfield считает генерации, и запрос,
собранный по неверно разложенным карточкам, оплачивается целиком.

**Одна такая карточка отменяет дорожку всей панели.** Сорт `media`
дорожку HyperFrames не покрывает (`frames.track_spec`), и остальные
карточки собираются поодиночке, как до 14.10: переходов между ними не
будет, фон прервётся на стыке. Это цена движения в одной карточке, и
инструмент называет её строкой каждый раз.

Ключ платный и зовётся руками — автоматического вызова из роли нет, как
у стока, трендов и HeyGen.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import avatar_split                                               # noqa: E402
from orchestrator import cli, footage, higgsfield                 # noqa: E402

# Запросы собирает модель, и роли у этого вызова нет: промпт держится
# здесь целиком. Роль завела бы файл в `roles/`, субагента и адаптер —
# ради одного перевода фразы в кадр это дороже самой работы.
SYSTEM = """Ты оператор, который подбирает кадр под звучащую фразу.

На входе список карточек панели вертикального ролика: что написано на
карточке и что в этот момент говорит человек в кадре по-русски. На
выходе — запрос к генератору видео на каждую карточку.

Правила кадра:
- Запрос на английском, одной фразой, 20–40 слов.
- Это фон под слова, а не иллюстрация к ним буквально: атмосфера,
  фактура, движение камеры. Названную вещь показывай метафорой среды, а
  не предметом с подписью.
- Обязательно назови движение: медленный наезд, дрейф, пролёт, течение.
  Статичный кадр в панели читается как картинка, а не как видео.
- Вертикальный кадр 9:16, кинематографический свет, спокойный темп.
- Никакого текста, букв, цифр, интерфейсов с подписями, логотипов: свои
  слова панель кладёт сверху сама, а сгенерированные буквы приезжают
  кривыми.
- Крупного лица в кадре не бывает: человек уже есть в нижней половине
  ролика, второе лицо спорит с ним.

Отвечай строго по схеме, по одному запросу на каждую карточку входа, в
том же порядке."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["clips"],
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["n", "prompt"],
                "properties": {
                    "n": {"type": "integer"},
                    "prompt": {"type": "string"},
                },
            },
        },
    },
}

TRACK_NOTE = ("карточка с клипом отменяет дорожку всей панели: остальные "
              "соберутся поодиночке, без переходов между ними")


def said(words: list[dict], start: float, end: float) -> str:
    """Что звучит между секундами. Это и есть вход для запроса."""
    return " ".join(w["text"] for w in words
                    if float(w["start"]) >= start - 0.2
                    and float(w["start"]) < end).strip()


def spans(cards: list[dict], words: list[dict],
          total: float) -> list[tuple[float, float, str]]:
    """Границы и речь каждой карточки.

    Секунды считает `avatar_split._starts` — тот же код, которым их
    считает сплит. Второй счёт тех же чисел разошёлся бы с первым молча,
    а стоит он здесь оплаченного клипа не на своём месте.
    """
    starts = avatar_split._starts(cards, words)
    ends = ([s - avatar_split.GAP for s in starts[1:]]
            + [max(0.0, total - avatar_split.SOLO)])
    return [(s, e, said(words, s, e)) for s, e in zip(starts, ends)]


def slug(text: str) -> str:
    flat = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return flat[:28] or "clip"


async def ask_prompts(items: list[dict]) -> dict[int, str]:
    """Один вызов на все карточки. Возвращает запрос по номеру."""
    lines = []
    for it in items:
        card = " / ".join(it["lines"]) or "без слов"
        lines.append(f"{it['n']}. карточка: «{card}»\n"
                     f"   звучит: «{it['said'] or 'тишина'}»")
    raw = await cli.ask(role="higgsfield", system=SYSTEM,
                        prompt="\n".join(lines), schema=SCHEMA)
    data = json.loads(raw)
    out = {int(c["n"]): str(c["prompt"]).strip()
           for c in data.get("clips") or []}
    missed = [it["n"] for it in items if not out.get(it["n"])]
    if missed:
        raise higgsfield.NoClip(
            "модель не дала запрос для карточек: "
            + ", ".join(str(n) for n in missed))
    return out


async def one(prompt: str, out: Path, *, want: float, name: str,
              gate: asyncio.Semaphore) -> tuple[Path, str]:
    # Сеть здесь синхронная (`urllib`, как у HeyGen), поэтому поток: три
    # клипа по две минуты подряд — это шесть минут ожидания на ровном
    # месте, а Higgsfield считает их параллельно сам.
    async with gate:
        return await asyncio.to_thread(
            higgsfield.clip, prompt, out, name=name, want=want)


async def fill(video: Path, blocks: Path, *, out_dir: Path, name: str,
               words_cache: Path | None, only: set[int], dry: bool,
               parallel: int) -> list[str]:
    """Сгенерировать клипы и прописать их в `blocks.json`."""
    notes: list[str] = []
    cards = json.loads(blocks.read_text(encoding="utf-8"))
    if not isinstance(cards, list) or not cards:
        raise higgsfield.NoClip("в json с карточками ожидается непустой "
                                "список")

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
        raise higgsfield.NoClip("в записи не разобрано ни одного слова")

    # Тот же оборванный файл, что ловит сплит: ffprobe читает длину из
    # заголовка, whisper — из данных. Здесь это дороже: по короткой
    # расшифровке карточки встанут не на своих словах, и оплаченные
    # клипы уйдут под чужую речь.
    heard = words[-1]["end"]
    if probe.duration - heard > avatar_split.TAIL_SLACK:
        raise higgsfield.NoClip(
            f"расшифровка кончилась на {heard:.1f} с, а дорожка идёт "
            f"{probe.duration:.1f} с: файл скачан не целиком")

    laid = spans(cards, words, probe.duration)
    items = []
    for i, (start, end, text) in enumerate(laid):
        n = i + 1
        if only and n not in only:
            continue
        if cards[i].get("video") and not only:
            notes.append(f"карточка {n}: клип уже прописан, не трогаем")
            continue
        items.append({"n": n, "i": i, "lines": [str(s) for s in
                                                cards[i].get("lines") or []],
                      "said": text, "want": max(0.0, end - start)})
    if not items:
        notes.append("генерировать нечего: у всех карточек клип уже есть")
        return notes

    asked = await ask_prompts(items)
    model = higgsfield.model(name)
    for it in items:
        secs, why = higgsfield.seconds(model, it["want"])
        it["prompt"], it["secs"] = asked[it["n"]], secs
        if why:
            notes.append(f"карточка {it['n']}: {why}")

    if dry:
        for it in items:
            print(f"\nкарточка {it['n']} · {it['secs']} с · "
                  f"звучит «{it['said'][:60]}»\n  {it['prompt']}")
        notes.append(f"{len(items)} запросов показаны, в сеть не ходили")
        notes.append(TRACK_NOTE)
        return notes

    gate = asyncio.Semaphore(max(1, parallel))
    jobs = [one(it["prompt"], out_dir / f"{higgsfield.PREFIX}{it['n']:02d}-"
                f"{slug(it['prompt'])}.mp4",
                want=it["want"], name=name, gate=gate) for it in items]
    done = await asyncio.gather(*jobs, return_exceptions=True)

    made = 0
    for it, got in zip(items, done):
        if isinstance(got, BaseException):
            notes.append(f"карточка {it['n']} без клипа — {got}")
            continue
        path, why = got
        if why:
            notes.append(f"карточка {it['n']}: {why}")
        cards[it["i"]]["video"] = str(path)
        higgsfield.credit(out_dir, path, name or higgsfield.DEFAULT,
                          it["secs"], it["prompt"])
        made += 1

    if made:
        blocks.write_text(json.dumps(cards, ensure_ascii=False, indent=1),
                          encoding="utf-8")
        notes.append(f"{made} клипов прописаны в {blocks.name} — сплит "
                     "собирается обычной командой")
        notes.append(TRACK_NOTE)
    return notes


def table() -> str:
    rows = ["модель            вход    секунды"]
    for key, m in higgsfield.MODELS.items():
        span = (", ".join(str(s) for s in m.steps) if m.steps
                else f"{m.lo}–{m.hi}")
        mark = " (по умолчанию)" if key == higgsfield.DEFAULT else ""
        rows.append(f"{key:<17} {m.kind:<7} {span}{mark}")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Клип в карточку панели генерацией Higgsfield.")
    ap.add_argument("--models", action="store_true",
                    help="показать таблицу моделей и выйти")
    ap.add_argument("--video", default="", help="mp4 с головой")
    ap.add_argument("--blocks", default="",
                    help="json с карточками панели: читается и правится")
    ap.add_argument("--words", default="",
                    help="json расшифровки: есть — берётся оттуда, "
                         "нет — снимется и ляжет туда")
    ap.add_argument("--dir", default="",
                    help="куда класть клипы (по умолчанию рядом с blocks)")
    ap.add_argument("--only", default="",
                    help="номера карточек через запятую: только они, "
                         "даже если клип у них уже есть")
    ap.add_argument("--model", default="", help="имя из --models")
    ap.add_argument("--parallel", type=int, default=2,
                    help="сколько генераций держать разом")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать запросы и не ходить в сеть")
    ap.add_argument("--prompt", default="",
                    help="один клип по своему запросу, без карточек")
    ap.add_argument("--out", default="", help="куда положить клип к --prompt")
    ap.add_argument("--seconds", type=float, default=5.0,
                    help="длина клипа к --prompt")
    args = ap.parse_args()

    if args.models:
        print(table())
        return 0

    if not higgsfield.creds() and not args.dry_run:
        print("Пусто в HIGGSFIELD_KEY_ID и HIGGSFIELD_KEY_SECRET — в сеть "
              "не идём. Пара берётся в консоли higgsfield.ai и "
              "показывается один раз.", file=sys.stderr)
        return 1

    try:
        if args.prompt:
            if not args.out:
                print("к --prompt нужен --out", file=sys.stderr)
                return 2
            path, why = higgsfield.clip(
                args.prompt, Path(args.out).expanduser(),
                name=args.model, want=args.seconds)
            print(path)
            if why:
                print(why, file=sys.stderr)
            return 0

        if not (args.video and args.blocks):
            print("нужны --video и --blocks (или --prompt с --out)",
                  file=sys.stderr)
            return 2
        video = Path(args.video).expanduser()
        if not video.exists():
            print(f"файла {video} нет", file=sys.stderr)
            return 2
        blocks = Path(args.blocks).expanduser()
        if not blocks.exists():
            print(f"файла {blocks} нет", file=sys.stderr)
            return 2

        notes = asyncio.run(fill(
            video, blocks,
            out_dir=(Path(args.dir).expanduser() if args.dir
                     else blocks.parent / "clips"),
            name=args.model,
            words_cache=Path(args.words).expanduser() if args.words else None,
            only={int(n) for n in args.only.replace(" ", "").split(",") if n},
            dry=args.dry_run, parallel=args.parallel))
    except (higgsfield.NoClip, avatar_split.NoBlocks, cli.CliError,
            footage.NoWhisper, footage.NoFfmpeg) as e:
        print(str(e), file=sys.stderr)
        return 1

    for note in notes:
        print(note, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
