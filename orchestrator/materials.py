"""Перенос исходных материалов в папку бренда.

Всё, что человек прислал — файлы и тексты, — должно лежать рядом с
профилем, а не во временной папке процесса. Две причины:

  1. Голос калибруется по образцам. Пустой voice-samples/ означает, что
     профиль описан по декларации, а не по тому, как человек пишет.
  2. Промпты ролей меняются. Без исходников переизвлечь профиль после
     правки промпта нельзя — придётся заново просить материалы.

Во время онбординга бренда ещё нет, поэтому файлы копятся во временной
папке и переезжают в бренд, как только он создан.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path

from config import cfg
from storage import db

log = logging.getLogger("materials")

STAGE = cfg.brands_path.parent / "tmp" / "uploads"
MIN_SAMPLE = 200            # короче этого текст для калибровки бесполезен


def stage_dir(chat_id: int) -> Path:
    d = STAGE / str(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _digest(text: str) -> str:
    """Отпечаток по значимым символам: разметка и пробелы не в счёт."""
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return hashlib.sha1(
        re.sub(r"\s+", " ", body).strip().lower().encode()).hexdigest()


def _slug(text: str, n: int) -> str:
    words = re.findall(r"[a-zа-яё0-9]+", text.lower())[:5]
    tail = "-".join(words) or "sample"
    return f"{n:02d}-{tail[:48]}.md"


def adopt(chat_id: int, brand) -> tuple[int, int]:
    """Перенести файлы и тексты в папку бренда. Возвращает (файлов, образцов)."""
    moved = samples = 0

    # 1. Присланные файлы → sources/uploads/
    src = STAGE / str(chat_id)
    if src.is_dir():
        dst = brand.path("sources/uploads")
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            if f.is_file() and not (dst / f.name).exists():
                shutil.copy2(f, dst / f.name)
                moved += 1

    # 2. Присланные тексты → voice-samples/
    row = db.one("SELECT raw_inputs_json FROM onboarding WHERE chat_id = ?",
                 chat_id)
    raw = json.loads(row["raw_inputs_json"]) if row else []
    folder = brand.path("voice-samples")
    folder.mkdir(parents=True, exist_ok=True)

    # Дедупликация по содержимому, а не по имени: человек присылает одно
    # и то же по нескольку раз, когда кажется, что не дошло.
    seen = {_digest(f.read_text(encoding="utf-8")) for f in folder.glob("*.md")}
    n = len(seen)

    for item in raw:
        text = (item.get("text") or "").strip()
        if len(text) < MIN_SAMPLE:
            continue
        key = _digest(text)
        if key in seen:
            continue
        seen.add(key)
        n += 1
        (folder / _slug(text, n)).write_text(
            f"<!-- прислано человеком на шаге {item.get('step', '?')} -->\n\n"
            f"{text}\n", encoding="utf-8")
        samples += 1

    if moved or samples:
        log.info("в бренд %s перенесено: файлов %s, образцов %s",
                 brand.slug, moved, samples)
    return moved, samples


def summary(moved: int, samples: int) -> str:
    if not (moved or samples):
        return ""
    bits = []
    if moved:
        bits.append(f"{moved} файл. → sources/uploads")
    if samples:
        bits.append(f"{samples} образц. текста → voice-samples")
    return "Исходники сохранены: " + ", ".join(bits) + "."
