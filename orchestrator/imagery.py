"""Общая работа с файлами фотографий: имя, размер, индекс.

Отдельным модулем, потому что этим пользуются четверо: инструмент
выгрузки из «Фото», сток, генерация и Дизайнер. Пока это лежало в
`tools/`, продукт зависел бы от инструмента, а зависимость идёт ровно
наоборот.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# Длинная сторона. Chrome рендерит с `--force-device-scale-factor=2`, то
# есть холст 1080×1920 снимается как 2160×3840: фото мельче поедет мылом.
# Оригинал с телефона (4032) режем до 4000 — запас есть, вес падает вдвое.
LONG_SIDE = 4000
QUALITY = 85

# Имена файлов уезжают в HTML макета и в `photos.md`, который человек
# правит руками. Латиница здесь не педантизм: папку бренда отдают
# клиенту, а кириллица в путях переживает не каждый архиватор.
TRANS = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", " ": "-", "_": "-",
})


def slug(name: str) -> str:
    out = re.sub(r"[^a-z0-9-]", "", name.lower().translate(TRANS))
    return re.sub(r"-{2,}", "-", out).strip("-")


# Индекс «откуда файл» → имя в папке бренда. Один на фотобанк, потому что
# источников у него уже два: альбом «Фото» кладёт сюда uuid снимка
# (`tools/photos_pull.py`), присланное в топик — id файла в Telegram
# (`design.stash_photo`). Ключи разных источников не путаются: у второго
# префикс `tg:`.
INDEX = ".photos-index.json"


def measure(path: Path) -> tuple[int, int]:
    """Ширина и высота в пикселях. `sips` есть в macOS."""
    # Без `text=True` намеренно: `stderr` у `sips` тогда байты и у этого
    # вызова, и у `convert` — а разбирает их один и тот же обработчик.
    out = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        check=True, capture_output=True).stdout.decode(errors="replace")
    got = {k: int(v) for k, v in re.findall(r"pixel(Width|Height): (\d+)", out)}
    return got.get("Width", 0), got.get("Height", 0)


def convert(src: Path, dst: Path) -> None:
    """HEIC и гигантский JPEG → JPEG под холст. `sips` есть в macOS.

    Мелкое **не растягивается**: `-Z` у `sips` работает в обе стороны, и
    сжатое телеграмом фото на 1280 точек он честно раздувает до 4000 —
    вес растёт вчетверо, резкости не прибавляется ни на пиксель. Поэтому
    потолок ставится только тому, кто его превышает.
    """
    fit = ["-Z", str(LONG_SIDE)] if max(measure(src)) > LONG_SIDE else []
    subprocess.run(
        ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(QUALITY),
         *fit, str(src), "--out", str(dst)],
        check=True, capture_output=True)


def free_name(folder: Path, base: str, suffix: str = ".jpg") -> str:
    """Свободное имя `base.jpg`, `base-02.jpg`, … Ничего не затирая.

    Затирать нельзя: два кадра одной съёмки приезжают под одним именем, и
    молчаливая перезапись стоила бы человеку первого.
    """
    taken = {f.name for f in folder.iterdir()} if folder.is_dir() else set()
    name, n = f"{base}{suffix}", 1
    while name in taken:
        n += 1
        name = f"{base}-{n:02d}{suffix}"
    return name


def index_read(folder: Path) -> dict[str, str]:
    path = folder / INDEX
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def index_write(folder: Path, index: dict[str, str]) -> None:
    (folder / INDEX).write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
