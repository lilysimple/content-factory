"""Единственная точка доступа к профилю бренда.

Роли никогда не работают с путями напрямую — только через этот модуль.
Тогда переезд на объектное хранилище или в базу будет правкой одного файла,
а не всего проекта.

Что здесь лежит: холодная часть профиля (core.md, goals.md, platforms.md,
sources.md) и артефакты (тексты, макеты, фото). Горячее состояние — план,
статусы, метрики — живёт в SQLite, см. storage/db.py.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import subprocess
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger("brand")

# Файлы профиля. Ключ — как их зовут роли, значение — имя на диске.
PROFILE = {
    "core":      "core.md",
    "goals":     "goals.md",
    "platforms": "platforms.md",
    "sources":   "sources.md",
}

SUBDIRS = ("plans", "posts", "research", "voice-samples", "photos",
           "design", "design/platforms", "design/assets", "sources/uploads")

TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def slugify(name: str) -> str:
    """«Лили Космос» → lily-kosmos. Slug после создания не меняется никогда."""
    low = unicodedata.normalize("NFKC", name).strip().lower()
    ascii_ = low.translate(TRANSLIT)
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")
    return slug or "brand"


@dataclass(frozen=True)
class Brand:
    slug: str
    root: Path

    # ── чтение ────────────────────────────────────────────────────────

    def exists(self, key: str) -> bool:
        return (self.root / PROFILE.get(key, key)).exists()

    def read(self, key: str) -> str:
        path = self.root / PROFILE.get(key, key)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def section(self, key: str, needle: str) -> str:
        """Вытащить один раздел markdown по подстроке в заголовке `##`.

        Роли читают только свою секцию, а не файл целиком: одна роль —
        один запрос — одна площадка.
        """
        text = self.read(key)
        if not text:
            return ""
        blocks = re.split(r"^(?=##\s)", text, flags=re.M)
        low = needle.lower()
        for block in blocks:
            head = block.split("\n", 1)[0]
            if head.startswith("##") and low in head.lower():
                return block.strip()
        return ""

    def name(self) -> str:
        """Отображаемое имя живёт внутри core.md, а не в slug."""
        m = re.search(r"^#\s*(?:ЯДРО\.\s*)?(.+)$", self.read("core"), re.M)
        return m.group(1).strip() if m else self.slug

    def owner(self) -> str:
        m = re.search(r"^owner:\s*(\S+)", self.read("core"), re.M)
        return m.group(1) if m else "bot"

    def stopwords(self) -> list[str]:
        """Стоп-слова бренда из раздела «Голос» профиля.

        Их проверяет скрипт, а не модель: слово из стоп-листа это отказ,
        и отказ должен быть детерминированным. Читают их все роли,
        которые пишут текст, поэтому список живёт здесь, а не в одной
        из них.
        """
        voice = self.section("core", "Голос")
        if not voice:
            return []
        tail = voice.split("Стоп-слова", 1)
        if len(tail) < 2:
            return []
        out = []
        for line in tail[1].splitlines():
            line = line.strip()
            if line.startswith("##"):
                break
            if line.startswith("- "):
                out.append(line[2:].strip())
        return [w for w in out if w]

    def version(self) -> str:
        """Версия профиля = коммит репозитория брендов, иначе дата."""
        # Чужой репозиторий выше по дереву дал бы sha коммита кода,
        # выданный за версию профиля. Дата честнее.
        sha = _git(self.root, "rev-parse", "--short", "HEAD") \
            if _own_repo(self.root) else ""
        return sha or date.today().isoformat()

    # ── запись ────────────────────────────────────────────────────────

    def write(self, key: str, content: str, *, reason: str) -> str:
        """Записать файл профиля и зафиксировать версию.

        Пишется только подтверждённый блок: черновики живут в БД.
        """
        path = self.root / PROFILE.get(key, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        _commit(self.root, f"{self.slug}: {reason}")
        return self.version()

    async def awrite(self, key: str, content: str, *, reason: str) -> str:
        """То же, что `write`, но не морозит event loop.

        Внутри `write` живёт `git add -A` плюс `git commit` — два
        подпроцесса с потолком в десять секунд каждый. Синхронный вызов
        из корутины останавливает поллинг **всех** ботов на это время:
        Telegram отдаёт обновления другому инстансу, и мы получаем
        `Conflict` на ровном месте. Ровно этого мост избегает белым
        списком и асинхронным подпроцессом — здесь та же граната лежала
        без чеки.

        Синхронный `write` остаётся: им пользуется стенд и код, который
        и так работает в потоке.
        """
        return await asyncio.to_thread(self.write, key, content, reason=reason)

    def append(self, rel: str, content: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content.rstrip() + "\n")
        return path

    def artifact(self, rel: str, content: str | bytes) -> Path:
        """Положить артефакт: текст поста, макет, дайджест."""
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content.rstrip() + "\n", encoding="utf-8")
        return path

    def path(self, rel: str) -> Path:
        return self.root / rel

    # ── выгрузка клиенту ──────────────────────────────────────────────

    def export_zip(self) -> tuple[str, bytes]:
        """Клиент должен мочь уйти со своими данными к другому подрядчику."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(self.root.rglob("*")):
                if f.is_file() and ".git" not in f.parts:
                    z.write(f, f.relative_to(self.root))
        return f"{self.slug}.zip", buf.getvalue()


# ── хранилище ─────────────────────────────────────────────────────────

class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def free_slug(self, name: str) -> str:
        base = slugify(name)
        slug, n = base, 2
        while (self.root / slug).exists():
            slug, n = f"{base}-{n}", n + 1
        return slug

    def create(self, name: str, *, owner: str = "bot") -> Brand:
        slug = self.free_slug(name)
        brand = Brand(slug, self.root / slug)
        for sub in SUBDIRS:
            (brand.root / sub).mkdir(parents=True, exist_ok=True)

        brand.artifact("stats.csv",
                       "post_id,date,platform,format,goal,funnel_stage,cta_id,"
                       "views_24h,views_72h,views_7d,reach,likes,comments,"
                       "saves,shares,watch_pct,subs_delta,source")
        brand.write("core", f"# ЯДРО. {name}\n\nowner: {owner}\n\n"
                            "> Профиль в сборке. Разделы появятся по мере "
                            "подтверждения на онбординге.\n",
                    reason="создан профиль")
        log.info("создан бренд %s (%s)", name, slug)
        return brand

    def get(self, slug: str) -> Brand | None:
        path = self.root / slug
        return Brand(slug, path) if path.is_dir() else None

    def all(self) -> list[Brand]:
        return [Brand(p.name, p) for p in sorted(self.root.iterdir())
                if p.is_dir() and not p.name.startswith(".")]


# ── git как версионирование, без внешних зависимостей ─────────────────

def _git(cwd: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _own_repo(cwd: Path) -> bool:
    """Свой ли это репозиторий, а не тот, внутри которого мы оказались."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return False                            # не репозиторий — молча мимо
    root = cwd.resolve()
    if Path(top).resolve() in (root, root.parent):
        return True
    log.warning("папка бренда лежит внутри чужого репозитория %s, "
                "версию не трогаю", top)
    return False


def _commit(cwd: Path, message: str) -> None:
    """Зафиксировать версию профиля в репозитории брендов.

    **`git` ищет репозиторий вверх по дереву.** Папка бренда без своего
    `.git` находит первый попавшийся выше — и `add -A` уносит туда всё
    подряд. Стенд на этом поймал сам себя: песочница лежит внутри
    репозитория кода, и прогон тестов сделал три коммита в него.

    Поэтому коммитим, только если найденный репозиторий это сам бренд
    или папка брендов, а не что-то, внутри чего они случайно оказались.
    """
    if not _own_repo(cwd):
        return

    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.name=content-factory",
         "-c", "user.email=bot@content-factory.local",
         "commit", "-q", "-m", message)
