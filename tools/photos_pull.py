"""Фотобанк бренда из альбома «Фото»: забрать новое, ужать, назвать.

    ./.venv/bin/python tools/photos_pull.py                 всё по умолчанию
    ./.venv/bin/python tools/photos_pull.py --brand lily-space
    ./.venv/bin/python tools/photos_pull.py --album "другой альбом"

Зачем это вообще: фон обложки — фото, а фотобанк бренда был папкой,
которую пополняли руками. На девяти файлах ротация выдыхается за неделю,
и `design._pick_photo` начинает повторять фото через день.

Библиотека читается **локальная**, та, что уже синкает iCloud: 28 ГБ
целиком заводу не нужны и в папку клиента им нельзя. Отбор делает
человек — кидает в альбом то, что годится под обложки.

Почему не `osxphotos export --update`: его инкрементальность держится на
именах файлов, а мы файлы переименовываем (в папке бренда имя фото это
слово, которое человек пишет в `photos.md`, а не `IMG_4417`). Поэтому
свой индекс `uuid → имя` рядом с фотографиями.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import cfg                                            # noqa: E402
from orchestrator.imagery import convert, slug                    # noqa: E402

# Потолок фотобанка. Не про диск: папку бренда отдают клиенту, а
# `photos.md` человек раскладывает руками — по шести сотням кадров
# правило не напишешь, и выбор фона выродится в случайность. Альбом это
# отбор под обложки, а не вся съёмка.
SANE = 60

INDEX = ".photos-index.json"
ALBUM_RX = re.compile(r"^Альбом в Фото:\s*(.+?)\s*$", re.M)


def brand_dir(arg: str | None) -> Path:
    root = cfg.brands_path
    if arg:
        path = root / arg
        if not path.is_dir():
            sys.exit(f"нет папки бренда {path}")
        return path
    found = sorted(p for p in root.iterdir() if (p / "core.md").is_file())
    if len(found) != 1:
        sys.exit("брендов несколько или ни одного — назови нужный: --brand")
    return found[0]


def album_name(brand: Path, arg: str | None) -> str:
    """Имя альбома живёт у бренда, а не в коде продукта.

    У следующего клиента альбом называется иначе, и код об этом знать не
    должен. Строка лежит в `design/photos.md` — там же, где правила
    выбора фото, то есть в одном файле про фотобанк.
    """
    if arg:
        return arg
    rules = brand / "design" / "photos.md"
    found = ALBUM_RX.search(rules.read_text(encoding="utf-8")) \
        if rules.is_file() else None
    if not found:
        sys.exit(f"в {rules} нет строки «Альбом в Фото: ...», а --album не задан")
    return found.group(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brand", help="slug папки бренда")
    ap.add_argument("--album", help="имя альбома в «Фото»")
    ap.add_argument("--limit", type=int, default=0,
                    help=f"взять не больше N (по умолчанию потолок {SANE})")
    ap.add_argument("--all", action="store_true",
                    help="взять весь альбом, каким бы большим он ни был")
    ap.add_argument("--download", action="store_true",
                    help="тянуть из iCloud то, чего нет на диске (медленно)")
    ap.add_argument("--dry-run", action="store_true", help="только показать")
    args = ap.parse_args()

    import osxphotos

    brand = brand_dir(args.brand)
    album = album_name(brand, args.album)
    images = brand / "design" / "assets" / "images"
    images.mkdir(parents=True, exist_ok=True)

    index_path = images / INDEX
    index: dict[str, str] = json.loads(index_path.read_text(encoding="utf-8")) \
        if index_path.is_file() else {}

    db = osxphotos.PhotosDB()
    if album not in {a.title for a in db.album_info}:
        sys.exit(f"альбома «{album}» в библиотеке нет")

    photos = [p for p in db.photos(albums=[album]) if not p.ismovie]
    print(f"Альбом «{album}»: {len(photos)} фото, в бренде уже {len(index)}")

    cap = args.limit or (0 if args.all else SANE)
    if cap and len(photos) > cap:
        # Свежие вперёд: если человек кидает в альбом по ходу работы,
        # обрезать надо хвост, а не то, что он добавил сегодня.
        photos.sort(key=lambda p: p.date, reverse=True)
        print(f"Беру {cap} самых свежих: фотобанк бренда — отбор под "
              f"обложки, а не вся съёмка. Нужно больше — --limit N, "
              f"нужно всё — --all.")
        photos = photos[:cap]

    taken = {v for v in index.values()}
    added, missing, failed = [], [], []

    with tempfile.TemporaryDirectory(prefix="photos-pull-") as tmp:
        for p in photos:
            if p.uuid in index and (images / index[p.uuid]).is_file():
                continue
            src = p.path_edited or p.path
            if not src and args.download:
                # Просим Photos достать оригинал из iCloud. Идёт через
                # само приложение, поэтому медленно и по одному файлу —
                # флагом, а не по умолчанию.
                try:
                    got = p.export(tmp, f"icloud-{p.uuid[:8]}.jpg",
                                   use_photos_export=True, timeout=180)
                    src = got[0] if got else ""
                except Exception as e:                       # noqa: BLE001
                    failed.append(f"{p.original_filename}: iCloud, {e}")
                    continue
            if not src:
                # Оригинал не скачан: библиотека в режиме «оптимизировать
                # хранилище». Молча пропускать нельзя — человек будет
                # ждать фото, которого не приедет.
                missing.append(p.original_filename)
                continue

            base = slug(p.title or "") or slug(Path(p.original_filename).stem) \
                or p.uuid[:8].lower()
            name, n = f"{base}.jpg", 2
            while name in taken:
                name, n = f"{base}-{n:02d}.jpg", n + 1

            if args.dry_run:
                print(f"  + {name}  ← {p.original_filename}")
                taken.add(name)
                continue
            try:
                convert(Path(src), Path(tmp) / name)
            except subprocess.CalledProcessError as e:
                failed.append(f"{p.original_filename}: {e.stderr.decode()[:80]}")
                continue
            (images / name).write_bytes((Path(tmp) / name).read_bytes())
            index[p.uuid] = name
            taken.add(name)
            added.append(name)
            # Индекс пишется после каждого файла, а не в конце: на
            # `--download` проход идёт минутами, и прерванный на середине
            # оставлял бы файлы на диске без записи о них — следующий
            # запуск скачал бы то же самое дублями. Проверено практикой.
            index_path.write_text(json.dumps(index, ensure_ascii=False,
                                             indent=2), encoding="utf-8")

    print(f"\nНовых файлов: {len(added)}")
    for name in added:
        print(f"  {name}")
    if missing:
        print(f"\nНе скачаны из iCloud ({len(missing)}): "
              f"{', '.join(missing[:5])}{' …' if len(missing) > 5 else ''}")
        print("  Оригинала нет на диске. Сними «оптимизировать хранилище» "
              "в настройках «Фото» — или запусти с --download, тогда их "
              "достанет само приложение, по одному и медленно.")
    if failed:
        print(f"\nНе преобразовались ({len(failed)}):")
        for line in failed:
            print(f"  {line}")
    if added:
        print(f"\nРазложи новые по целям: {brand / 'design' / 'photos.md'}. "
              "Что не попало в таблицу, идёт ротацией по всей папке.")


if __name__ == "__main__":
    main()
