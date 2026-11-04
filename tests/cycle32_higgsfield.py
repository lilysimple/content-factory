"""Цикл 32: анимированный клип в панель от Higgsfield.

Модель здесь участвует только как переводчик фразы в запрос, и подделана.
Проверяется то, что дороже всего ошибается у платной генерации:

- **ключа нет — в сеть не идём**. Та же граница, что у стока, трендов и
  HeyGen, и проверяется она первой: молчаливый поход в сеть без ключа
  кончается не отказом, а четырьмястами от чужого адреса;
- **запрос идёт с отпечатком**. Оборванный ответ выглядит как неудача, и
  повтор без `Idempotency-Key` оплатил бы тот же клип дважды;
- **длина клипа названа вслух**. Kling 2.5 Turbo даёт пять или десять
  секунд, блок панели — любую: совпало не совпало, человек должен это
  прочитать строкой, а не увидеть на готовом ролике;
- **оборванный файл ловится до генерации**. По короткой расшифровке
  карточки встанут не на своих словах, и оплаченные клипы уйдут под
  чужую речь — это дороже, чем у сплита, где круг стоит рендера;
- **секунды карточек считает один код**. Границы берутся у
  `avatar_split._starts`; второй счёт тех же чисел разошёлся бы молча;
- **отказ генерации это отказ, а не ожидание**. `failed` и `nsfw`
  Higgsfield отдаёт с возвратом кредитов, и ждать по ним нечего;
- **карточка с клипом отменяет дорожку панели**. Цена движения в одной
  карточке — остальные без переходов; инструмент называет её каждый раз.

Сеть подделана на уровне `_call`: проверяется протокол, а не urllib.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import harness
from harness import check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import footage, higgsfield                      # noqa: E402

spec = importlib.util.spec_from_file_location(
    "higgsfield_pull", harness.REPO / "tools" / "higgsfield_pull.py")
pull = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pull)

KEY = "id-1:secret-1"
TMP = harness.TMP / "higgs"
TMP.mkdir(parents=True, exist_ok=True)

CARDS = [
    {"kind": "counter", "value": 5, "at": "пять", "lines": ["плагинов"]},
    {"kicker": "ПЕРВЫЙ", "at": "первый", "lines": ["голос бренда"]},
    {"kicker": "ВТОРОЙ", "at": "второй", "lines": ["план недели"]},
]

SAID = ("у меня пять плагинов и каждый про своё первый держит голос "
        "бренда и ничего больше второй собирает план недели целиком "
        "и смотрит на статистику прошлой".split())


def words() -> list[dict]:
    return [{"text": w, "start": round(i * 0.5, 2),
             "end": round(i * 0.5 + 0.4, 2)} for i, w in enumerate(SAID)]


class Wire:
    """Подделка сети. Отдаёт заготовленное и помнит, о чём спросили."""

    def __init__(self, states: list[str] | None = None):
        self.calls: list[tuple[str, str, dict | None, str]] = []
        self.states = states or ["completed"]

    def __call__(self, method, url, key, payload=None, *, idem="",
                 timeout=90):
        self.calls.append((method, url, payload, idem))
        if method == "POST":
            return {"status": "queued", "request_id": "req-7",
                    "status_url": f"{higgsfield.API}/requests/req-7/status",
                    "cancel_url": "x"}
        state = self.states[min(len(
            [c for c in self.calls if c[0] == "GET"]) - 1,
            len(self.states) - 1)]
        out: dict = {"status": state, "request_id": "req-7"}
        if state == "completed":
            out["video"] = {"url": "https://example.test/out.mp4"}
        return out


def plug(wire: Wire) -> None:
    higgsfield._call = wire                                 # type: ignore


def plug_fetch() -> None:
    """Скачивание тоже подделано: адрес в ответе выдуманный."""
    def fake(url, out, *, timeout=300):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"0" * 20_000)
        return out
    higgsfield.fetch = fake                                 # type: ignore


# ── ключ и таблица моделей ────────────────────────────────────────────

def keys_and_models() -> None:
    object.__setattr__(cfg, "higgsfield_creds", "")
    object.__setattr__(cfg, "higgsfield_key_id", "")
    object.__setattr__(cfg, "higgsfield_key_secret", "")
    check("без ключа пара пустая", higgsfield.creds() == "")

    wire = Wire()
    plug(wire)
    try:
        higgsfield.clip("drift over a dark hall", TMP / "no.mp4")
        check("без ключа генерация отказала", False, "прошла молча")
    except higgsfield.NoClip as e:
        check("без ключа генерация отказала", "ключа Higgsfield нет" in str(e),
              str(e))
    check("без ключа в сеть не ходили", not wire.calls, str(wire.calls))

    object.__setattr__(cfg, "higgsfield_key_id", "id-1")
    object.__setattr__(cfg, "higgsfield_key_secret", "secret-1")
    check("пара собирается из двух полей", higgsfield.creds() == KEY,
          higgsfield.creds())
    object.__setattr__(cfg, "higgsfield_creds", "id-2:secret-2")
    check("одна строка сильнее двух полей",
          higgsfield.creds() == "id-2:secret-2", higgsfield.creds())
    object.__setattr__(cfg, "higgsfield_creds", "")

    try:
        higgsfield.model("kling-9000")
        check("неизвестная модель отказала", False, "прошла молча")
    except higgsfield.NoClip as e:
        check("неизвестная модель отказала", "в таблице нет" in str(e))
    check("по умолчанию текстовая модель",
          higgsfield.model("").kind == "text", higgsfield.model("").path)


# ── длина, тело запроса, отпечаток ────────────────────────────────────

def shape() -> None:
    text = higgsfield.MODELS["kling-3"]
    step = higgsfield.MODELS["kling-2.5-turbo"]

    secs, why = higgsfield.seconds(text, 7.2)
    check("целые секунды подгоняются под блок", secs == 8, str(secs))
    check("совпало — строки нет", why == "", why)

    secs, why = higgsfield.seconds(step, 7.2)
    check("закрытый список берёт ближайшее сверху", secs == 10, str(secs))
    check("несовпадение названо строкой", "ускорится" in why, why)

    secs, why = higgsfield.seconds(step, 12.0)
    check("длиннее потолка — потолок", secs == 10, str(secs))
    check("недобор назван хвостом", "хвост" in why, why)

    secs, _ = higgsfield.seconds(text, 1.0)
    check("короче нижней границы — граница", secs == text.lo, str(secs))

    payload = higgsfield.body(text, "slow drift", secs=8)
    check("вертикаль просится явно", payload["aspect_ratio"] == "9:16",
          str(payload))
    check("звук выключен: в сплите говорит голова",
          payload["sound"] == "off", str(payload))
    check("лишнее в кадре названо заранее",
          "text" in payload["negative_prompt"], str(payload))
    check("длина уехала числом", payload["duration"] == 8)

    try:
        higgsfield.body(step, "slow drift", secs=5)
        check("image-модель без кадра отказала", False, "прошла молча")
    except higgsfield.NoClip as e:
        check("image-модель без кадра отказала", "публичный адрес" in str(e),
              str(e))
    shot = higgsfield.body(step, "drift", secs=5,
                           image_url="https://example.test/a.jpg")
    check("кадр уехал адресом", shot["image_url"].endswith("a.jpg"))
    check("пропорции у image-модели не просим",
          "aspect_ratio" not in shot, str(sorted(shot)))

    one = higgsfield.idem_key(text, payload)
    check("отпечаток устойчив", one == higgsfield.idem_key(text, payload))
    other = higgsfield.body(text, "slow drift over a hall", secs=8)
    check("другой запрос — другой отпечаток",
          one != higgsfield.idem_key(text, other))


# ── постановка задачи и ожидание ──────────────────────────────────────

def waiting() -> None:
    object.__setattr__(cfg, "higgsfield_key_id", "id-1")
    object.__setattr__(cfg, "higgsfield_key_secret", "secret-1")

    wire = Wire(["queued", "in_progress", "completed"])
    plug(wire)
    plug_fetch()
    out, why = higgsfield.clip("slow drift", TMP / "clip.mp4", want=8.0,
                               sleep=lambda s: None)
    post = wire.calls[0]
    check("адрес модели собран из таблицы",
          post[1].endswith("kling-video/v3.0/std/text-to-video"), post[1])
    check("запрос ушёл с отпечатком", len(post[3]) == 32, post[3])
    check("ждали до готового", len([c for c in wire.calls
                                    if c[0] == "GET"]) == 3,
          str(len(wire.calls)))
    check("файл лёг на диск", out.exists() and out.stat().st_size > 10_000,
          str(out))
    check("совпавшая длина строки не дала", why == "", why)

    for dead in ("failed", "nsfw"):
        wire = Wire([dead])
        plug(wire)
        try:
            higgsfield.wait(KEY, "https://x/status", sleep=lambda s: None)
            check(f"{dead} — отказ, а не ожидание", False, "прошло молча")
        except higgsfield.NoClip as e:
            check(f"{dead} — отказ, а не ожидание",
                  "не состоялась" in str(e), str(e))
        check(f"{dead} не опрашивался повторно",
              len(wire.calls) == 1, str(len(wire.calls)))

    wire = Wire(["in_progress"])
    plug(wire)
    try:
        higgsfield.wait(KEY, "https://x/status", cap=12, every=6,
                        sleep=lambda s: None)
        check("потолок ожидания держится", False, "ждали вечно")
    except higgsfield.NoClip as e:
        check("потолок ожидания держится", "не собралось" in str(e), str(e))

    check("адрес файла виден в форме video",
          higgsfield.media_url({"video": {"url": "u"}}) == "u")
    check("адрес файла виден и в списке работ",
          higgsfield.media_url(
              {"jobs": [{"results": {"raw": {"url": "u2"}}}]}) == "u2",
          higgsfield.media_url({"jobs": [{"results":
                                          {"raw": {"url": "u2"}}}]}))
    check("готово без адреса — не готово",
          higgsfield.media_url({"status": "completed"}) == "")


# ── раскладка карточек по речи ────────────────────────────────────────

def laying() -> None:
    got = pull.spans(CARDS, words(), 14.0)
    check("карточек столько же, сколько границ", len(got) == 3, str(got))
    check("первая карточка стоит на своём слове",
          abs(got[0][0] - 1.0) < 0.01, str(got[0][0]))
    check("речь первой карточки кончается на слове второй",
          "первый" not in got[0][2], got[0][2])
    check("речь второй карточки своя",
          "голос" in got[1][2] and "план" not in got[1][2], got[1][2])
    check("последняя карточка доезжает до хвоста без панели",
          abs(got[2][1] - (14.0 - pull.avatar_split.SOLO)) < 0.01,
          str(got[2][1]))


# ── весь проход инструментом ──────────────────────────────────────────

def through() -> None:
    asked: list[str] = []

    async def fake_ask(*, role, system, prompt, **kw):
        asked.append(prompt)
        return json.dumps({"clips": [
            {"n": 1, "prompt": "slow drift over five glowing panels"},
            {"n": 2, "prompt": "camera glides through a quiet studio"},
            {"n": 3, "prompt": "wide pan across a calendar of light"},
        ]})

    made: list[tuple[str, Path, float]] = []

    def fake_clip(prompt, out, *, name="", want=5.0, **kw):
        made.append((prompt, out, want))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"0" * 20_000)
        return out, ""

    async def fake_probe(video):
        return footage.Probe(duration=14.0, width=1080, height=1920,
                             fps=30.0, has_audio=True)

    pull.cli.ask = fake_ask                                 # type: ignore
    pull.higgsfield.clip = fake_clip                        # type: ignore
    pull.footage.probe = fake_probe                         # type: ignore

    head = TMP / "head.mp4"
    head.write_bytes(b"0" * 1000)
    blocks = TMP / "blocks.json"
    blocks.write_text(json.dumps(CARDS, ensure_ascii=False),
                      encoding="utf-8")
    cache = TMP / "words.json"
    cache.write_text(json.dumps(words(), ensure_ascii=False),
                     encoding="utf-8")
    clips = TMP / "clips"

    notes = asyncio.run(pull.fill(head, blocks, out_dir=clips, name="",
                                  words_cache=cache, only=set(), dry=True,
                                  parallel=2))
    check("в сухом прогоне не генерировали", not made, str(made))
    check("сухой прогон назвал число запросов",
          any("3 запросов" in n for n in notes), str(notes))
    check("сухой прогон предупредил про дорожку",
          any("дорожк" in n for n in notes), str(notes))
    check("карточки ушли модели вместе с речью",
          len(asked) == 1 and "звучит" in asked[0], str(asked))
    check("блоки в сухом прогоне не правились",
          "video" not in blocks.read_text(encoding="utf-8"))

    notes = asyncio.run(pull.fill(head, blocks, out_dir=clips, name="",
                                  words_cache=cache, only=set(), dry=False,
                                  parallel=2))
    check("сгенерированы все три", len(made) == 3, str(len(made)))
    check("длина просилась по длине блока",
          all(w > 1.0 for _, _, w in made), str([w for _, _, w in made]))
    cards = json.loads(blocks.read_text(encoding="utf-8"))
    check("клипы прописаны карточкам",
          all(Path(c["video"]).exists() for c in cards), str(cards))
    check("расшифровка заново не снималась",
          any("заново не снималась" in n for n in notes), str(notes))
    check("таблица «что чем сделано» заполнена",
          (clips / higgsfield.CREDITS).exists()
          and len((clips / higgsfield.CREDITS).read_text(
              encoding="utf-8").strip().splitlines()) >= 9,
          str((clips / higgsfield.CREDITS).exists()))

    made.clear()
    notes = asyncio.run(pull.fill(head, blocks, out_dir=clips, name="",
                                  words_cache=cache, only=set(), dry=False,
                                  parallel=2))
    check("второй раз то же не генерируем", not made, str(made))
    check("и сказано это строкой",
          any("уже прописан" in n or "нечего" in n for n in notes),
          str(notes))

    notes = asyncio.run(pull.fill(head, blocks, out_dir=clips, name="",
                                  words_cache=cache, only={2}, dry=False,
                                  parallel=1))
    check("по номеру перегенерировали одну", len(made) == 1, str(made))

    # Оборванный файл: ffprobe читает длину из заголовка, whisper — из
    # данных. Карточки встали бы не на своих словах.
    async def long_probe(video):
        return footage.Probe(duration=60.0, width=1080, height=1920,
                             fps=30.0, has_audio=True)

    pull.footage.probe = long_probe                         # type: ignore
    made.clear()
    try:
        asyncio.run(pull.fill(head, blocks, out_dir=clips, name="",
                              words_cache=cache, only={1}, dry=False,
                              parallel=1))
        check("оборванный файл остановил генерацию", False, "прошла молча")
    except higgsfield.NoClip as e:
        check("оборванный файл остановил генерацию",
              "скачан не целиком" in str(e), str(e))
    check("на оборванном файле не платили", not made, str(made))


keys_and_models()
shape()
waiting()
laying()
through()
raise SystemExit(report())
