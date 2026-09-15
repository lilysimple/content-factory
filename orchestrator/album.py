"""Альбом к дублю: запись экрана и картинки для панели сплита.

Второй референс (`dashi_agent`, разбор 15.09) держит на панели не сайт
продукта, а то, что автор снял сам: запись экрана, где промпт печатается
в окне, и результат работы — готовую анимацию. Скачать это по адресу
нельзя, это материал автора. Поэтому он приходит **вместе с дублем,
одним альбомом** в 🎬 Reels:

- первое видео альбома — дубль;
- всё остальное по порядку — материалы панели;
- подпись у файла необязательна и уходит Монтажёру как подсказка.

Порядок в альбоме обычно и есть порядок в речи — автор записывал экран
по ходу рассказа, — но на фразу материал ставит Монтажёр, а не номер.

Telegram отдаёт альбом отдельными сообщениями с общим `media_group_id`,
и приходят они не обязательно по очереди. Каждое ложится своим файлом в
папку альбома под номером сообщения; порядок восстанавливается по номеру,
а не по времени прихода.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("album")

ROOT = "montage/incoming/album"

VIDEO = (".mp4", ".mov", ".m4v", ".mkv", ".webm")
IMAGE = (".jpg", ".jpeg", ".png", ".webp")


@dataclass
class Item:
    path: Path
    kind: str          # video | image
    caption: str = ""


def root(b) -> Path:
    return b.path(ROOT)


def stage(b, group: str, message_id: int, blob: bytes, suffix: str,
          caption: str = "") -> tuple[Path, bool]:
    """Положить файл альбома. Второе значение — альбом новый.

    Новый альбом стирает прежние: к монтажу идёт последний присланный, а
    материалы чужого дубля на панели прочитались бы поломкой.
    """
    base = root(b)
    folder = base / str(group)
    fresh = not folder.exists()
    if fresh and base.exists():
        for old in base.iterdir():
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{message_id:012d}{suffix.lower()}"
    path.write_bytes(blob)
    if caption.strip():
        path.with_suffix(".txt").write_text(caption.strip(), encoding="utf-8")
    log.info("альбом %s: принят %s (%s байт)", group, path.name, len(blob))
    return path, fresh


def items(folder: Path) -> list[Item]:
    """Файлы альбома в порядке сообщений."""
    out: list[Item] = []
    for f in sorted(folder.iterdir()):
        suf = f.suffix.lower()
        kind = "video" if suf in VIDEO else "image" if suf in IMAGE else ""
        if not kind or f.stat().st_size == 0:
            continue
        note = f.with_suffix(".txt")
        out.append(Item(f, kind, note.read_text(encoding="utf-8").strip()
                        if note.is_file() else ""))
    return out


def takes(b) -> list[Path]:
    """Дубль каждого альбома: первое видео по порядку."""
    base = root(b)
    if not base.is_dir():
        return []
    out = []
    for folder in base.iterdir():
        if folder.is_dir():
            video = next((i.path for i in items(folder) if i.kind == "video"),
                         None)
            if video:
                out.append(video)
    return out


def materials(b, take: Path) -> list[Item]:
    """Материалы панели к этому дублю. Дубль не из альбома — пусто."""
    try:
        take.relative_to(root(b))
    except ValueError:
        return []
    return [i for i in items(take.parent) if i.path != take]


def drop(b, take: Path) -> None:
    """Приёмка: альбом этого дубля больше не нужен."""
    try:
        take.relative_to(root(b))
    except ValueError:
        return
    shutil.rmtree(take.parent, ignore_errors=True)
