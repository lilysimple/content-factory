"""Общая работа с файлами фотографий: имя и размер.

Отдельным модулем, потому что этим пользуются трое: инструмент выгрузки
из «Фото», сток и Дизайнер. Пока это лежало в `tools/`, продукт зависел
бы от инструмента, а зависимость идёт ровно наоборот.
"""
from __future__ import annotations

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


def convert(src: Path, dst: Path) -> None:
    """HEIC и гигантский JPEG → JPEG под холст. `sips` есть в macOS."""
    subprocess.run(
        ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(QUALITY),
         "-Z", str(LONG_SIDE), str(src), "--out", str(dst)],
        check=True, capture_output=True)
