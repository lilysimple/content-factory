"""Говорящая голова от HeyGen: текст или своё аудио → mp4 с аватаром.

    ./.venv/bin/python tools/heygen_pull.py --avatars
    ./.venv/bin/python tools/heygen_pull.py --voices --lang Russian
    ./.venv/bin/python tools/heygen_pull.py --avatar <id> --voice <id> \
        --text tmp/script.txt --out tmp/head.mp4
    ./.venv/bin/python tools/heygen_pull.py --avatar <id> \
        --audio tmp/voice.wav --out tmp/head.mp4

Зачем он нужен заводу. Низ сплита (`Motion`, доля 0.70 холста) — это
человек в кадре, и до сих пор там мог оказаться только снятый дубль. Нет
съёмки — нет ролика. Аватар закрывает именно эту дырку: сценарий
Редактора Reels читается головой, а верх сплита остаётся тем же —
панель, записи экрана, счётчики.

**Ключ платный, и считает HeyGen минуты, а не вызовы.** Поэтому
инструмент зовёт человек руками, как `trends_pull.py`: автоматического
вызова из роли у него нет. Пусто в `HEYGEN_API_KEY` — инструмент
говорит это строкой и в сеть не идёт.

Голос у головы бывает двух родов, и это решение, а не настройка:

* `--voice` — голос HeyGen. Быстро, но это не голос бренда.
* `--audio` — своя дорожка (клон из VoiceStudio, `voice/lily.wav`
  бренда): HeyGen только разводит губы под неё. Голос остаётся своим, и
  в ролике совпадает с тем, что звучит в снятых дублях.

Субтитры у готового файла снимает whisper (`footage.captions`), а не
`subtitle_url` ответа: SRT приходит фразами, а караоке сплита идёт по
словам. Лишний проход здесь честнее, чем растянутое по фразе слово.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import cfg                                            # noqa: E402

API = "https://api.heygen.com"
POLL_EVERY = 6           # чаще спрашивать нечего: рендер идёт минутами
POLL_CAP = 900           # дольше — это не медленно, а не собралось


class NoHeyGen(RuntimeError):
    """HeyGen не отдал файл."""


def _req(method: str, path: str, key: str, body: dict | None = None,
         timeout: int = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("x-api-key", key)
    req.add_header("accept", "application/json")
    if data:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise NoHeyGen(f"HeyGen ответил {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise NoHeyGen(f"HeyGen недоступен: {e.reason}") from e


def _payload(data: dict) -> dict:
    """Тело ответа. v2 кладёт полезное в `data`, v3 иногда рядом."""
    inner = data.get("data")
    return inner if isinstance(inner, dict) else data


# ── что вообще есть на аккаунте ───────────────────────────────────────

def avatars(key: str, query: str = "") -> list[dict]:
    out = _payload(_req("GET", "/v2/avatars", key))
    rows: list[dict] = []
    for a in (out.get("avatars") or []):
        rows.append({"id": a.get("avatar_id", ""),
                     "name": a.get("avatar_name", ""),
                     "gender": a.get("gender", "")})
    # Свои (Instant Avatar) приезжают отдельным списком и нужны чаще
    # стоковых: лицо бренда это они.
    for g in (out.get("talking_photos") or []):
        rows.append({"id": g.get("talking_photo_id", ""),
                     "name": (g.get("talking_photo_name") or "") + " (фото)",
                     "gender": ""})
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r["name"].lower() or q in r["id"].lower()]
    return rows


def voices(key: str, lang: str = "") -> list[dict]:
    out = _payload(_req("GET", "/v2/voices", key))
    rows = [{"id": v.get("voice_id", ""), "name": v.get("name", ""),
             "lang": v.get("language", ""), "gender": v.get("gender", "")}
            for v in (out.get("voices") or [])]
    if lang:
        q = lang.lower()
        rows = [r for r in rows if q in str(r["lang"]).lower()]
    return rows


# ── своя дорожка на сторону HeyGen ────────────────────────────────────

def upload(key: str, path: Path) -> str:
    """Файл в хранилище HeyGen, обратно — `asset_id`.

    Потолок 32 МБ держит сам HeyGen; wav на минуту речи весит около
    десяти, так что проверка здесь, а не после загрузки.
    """
    blob = path.read_bytes()
    if len(blob) > 32 * 1024 * 1024:
        raise NoHeyGen(f"{path.name} тяжелее 32 МБ — HeyGen его не возьмёт")

    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    bound = "----cf" + uuid.uuid4().hex
    body = b"".join([
        f"--{bound}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{path.name}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        blob, b"\r\n", f"--{bound}--\r\n".encode(),
    ])
    req = urllib.request.Request(API + "/v3/assets", data=body, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("content-type", f"multipart/form-data; boundary={bound}")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            out = _payload(json.loads(r.read().decode()))
    except urllib.error.HTTPError as e:
        raise NoHeyGen(f"HeyGen не взял файл ({e.code}): "
                       f"{e.read().decode(errors='replace')[:300]}") from e
    asset = out.get("asset_id") or out.get("id")
    if not asset:
        raise NoHeyGen(f"в ответе нет asset_id: {out}")
    return str(asset)


# ── генерация ─────────────────────────────────────────────────────────

def start(key: str, avatar: str, *, text: str = "", voice: str = "",
          audio_asset: str = "", audio_url: str = "",
          aspect: str = "9:16", resolution: str = "1080p",
          background: str = "", webm: bool = False,
          title: str = "") -> str:
    """Задание на рендер, обратно — `video_id`."""
    body: dict = {"type": "avatar", "avatar_id": avatar,
                  "aspect_ratio": aspect, "resolution": resolution}
    if title:
        body["title"] = title
    if audio_asset:
        body["audio_asset_id"] = audio_asset
    elif audio_url:
        body["audio_url"] = audio_url
    else:
        body["script"] = text
        if voice:
            body["voice_id"] = voice
    if webm:
        # Прозрачный фон: голова ложится на панель, а не в свою половину.
        body["output_format"] = "webm"
        body["background"] = {"type": "transparent"}
    elif background:
        body["background"] = {"type": "color", "value": background}

    out = _payload(_req("POST", "/v3/videos", key, body, timeout=120))
    vid = out.get("video_id") or out.get("id")
    if not vid:
        raise NoHeyGen(f"в ответе нет video_id: {out}")
    return str(vid)


def wait(key: str, video_id: str, *, cap: int = POLL_CAP,
         say=print) -> dict:
    """Опрос до готового файла. Обратно — тело статуса со ссылками."""
    waited = 0
    last = ""
    while waited < cap:
        out = _payload(_req("GET", f"/v3/videos/{video_id}", key))
        status = str(out.get("status") or "")
        if status != last:
            say(f"  {status or '—'} ({waited} с)")
            last = status
        if status in ("completed", "complete", "done", "success"):
            return out
        if status in ("failed", "error"):
            why = (out.get("failure_message") or out.get("error")
                   or "причину HeyGen не назвал")
            raise NoHeyGen(f"рендер не собрался: {why}")
        time.sleep(POLL_EVERY)
        waited += POLL_EVERY
    raise NoHeyGen(f"HeyGen не отдал файл за {cap} с (video_id {video_id})")


def fetch(url: str, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "content-factory"})
    with urllib.request.urlopen(req, timeout=600) as r, out.open("wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return out


# ── командная строка ──────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--avatars", action="store_true", help="список аватаров")
    p.add_argument("--voices", action="store_true", help="список голосов")
    p.add_argument("--lang", default="", help="фильтр голосов по языку")
    p.add_argument("--query", default="", help="фильтр аватаров по имени")

    p.add_argument("--avatar", default="", help="avatar_id говорящей головы")
    p.add_argument("--voice", default="", help="voice_id голоса HeyGen")
    p.add_argument("--text", default="", help="файл со сценарием или «-»")
    p.add_argument("--audio", default="", help="своя дорожка (wav/mp3)")
    p.add_argument("--audio-url", default="", help="своя дорожка ссылкой")
    p.add_argument("--aspect", default="9:16", help="9:16, 1:1, 16:9")
    p.add_argument("--resolution", default="1080p")
    p.add_argument("--bg", default="#1F1F1F", help="цвет фона за головой")
    p.add_argument("--webm", action="store_true",
                   help="прозрачный фон вместо цвета (webm)")
    p.add_argument("--out", default="", help="куда положить файл")
    p.add_argument("--video-id", default="",
                   help="только дождаться и скачать уже начатый рендер")
    args = p.parse_args()

    key = cfg.heygen_key
    if not key:
        print("Пусто в HEYGEN_API_KEY — в сеть не идём. Ключ берётся в "
              "кабинете HeyGen (Settings → API) и кладётся в .env.")
        raise SystemExit(1)

    try:
        if args.avatars:
            for a in avatars(key, args.query):
                print(f"{a['id']:<44} {a['name']} {a['gender']}")
            return
        if args.voices:
            for v in voices(key, args.lang):
                print(f"{v['id']:<36} {v['lang']:<12} {v['gender']:<8} {v['name']}")
            return

        if args.video_id:
            vid = args.video_id
        else:
            if not args.avatar:
                p.error("нужен --avatar (список: --avatars)")
            text = ""
            if args.text:
                text = (sys.stdin.read() if args.text == "-"
                        else Path(args.text).read_text(encoding="utf-8")).strip()
            asset = ""
            if args.audio:
                print(f"Кладу дорожку: {args.audio}")
                asset = upload(key, Path(args.audio))
                print(f"  asset_id {asset}")
            if not (text or asset or args.audio_url):
                p.error("нужен --text, --audio или --audio-url")

            vid = start(key, args.avatar, text=text, voice=args.voice,
                        audio_asset=asset, audio_url=args.audio_url,
                        aspect=args.aspect, resolution=args.resolution,
                        background="" if args.webm else args.bg,
                        webm=args.webm, title="content-factory")
            print(f"Рендер начат: video_id {vid}")

        done = wait(key, vid)
        url = done.get("video_url") or done.get("url")
        if not url:
            raise NoHeyGen(f"в ответе нет ссылки на файл: {done}")

        suffix = ".webm" if args.webm else ".mp4"
        out = Path(args.out) if args.out else Path("tmp") / f"heygen-{vid}{suffix}"
        fetch(url, out)
        secs = done.get("duration")
        print(f"\nГотово: {out}" + (f", {secs} с" if secs else ""))
        if done.get("subtitle_url"):
            srt = out.with_suffix(".srt")
            fetch(str(done["subtitle_url"]), srt)
            print(f"Субтитры HeyGen (фразами, не по словам): {srt}")
    except NoHeyGen as e:
        print(f"Не вышло: {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
