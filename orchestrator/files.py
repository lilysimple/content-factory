"""Чтение приложенных файлов для распаковки.

Приложенный файл заменяет получасовое интервью: маркетинговая стратегия,
брендбук, экспорт канала, презентация. Задача модуля — вытащить из них
текст, а не пересказать содержимое.

Экспорт Telegram (`result.json` из Desktop) обрабатывается отдельно: это
самый ценный вход для калибровки голоса, потому что там настоящие тексты
человека, а не то, как он себя описывает.
"""
from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass

log = logging.getLogger("files")

MAX_CHARS = 60_000          # больше в промпт всё равно не нужно
TG_EXPORT_POSTS = 60        # сколько постов брать из экспорта канала

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".rst", ".html", ".htm"}


@dataclass
class Extracted:
    name: str
    kind: str                # pdf | docx | telegram-export | text | json
    text: str = ""
    posts: list[str] | None = None      # для экспорта канала
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def summary(self) -> str:
        if self.error:
            return f"{self.name} — не прочитался: {self.error}"
        if self.posts:
            return f"{self.name}: экспорт канала, {len(self.posts)} постов"
        return f"{self.name}: {self.kind}, {len(self.text)} знаков"


# ── экспорт Telegram Desktop ──────────────────────────────────────────

def _tg_text(value) -> str:
    """Поле text в экспорте это строка либо список кусков с разметкой."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        out = []
        for part in value:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                out.append(str(part.get("text", "")))
        return "".join(out)
    return ""


def _telegram_export(data: dict) -> tuple[list[str], str]:
    """Вернуть тексты постов и заголовок канала."""
    posts: list[str] = []
    for m in data.get("messages", []):
        if m.get("type") != "message":
            continue
        text = _tg_text(m.get("text", "")).strip()
        if len(text) > 40:                       # реплики и подписи пропускаем
            posts.append(text)
    return posts[-TG_EXPORT_POSTS:], str(data.get("name", ""))


# ── разбор по типам ───────────────────────────────────────────────────

def extract(blob: bytes, filename: str) -> Extracted:
    name = filename or "файл"
    low = name.lower()

    try:
        if low.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(blob))
            pages = [(p.extract_text() or "") for p in reader.pages]
            text = "\n\n".join(pages).strip()
            if not text:
                return Extracted(name, "pdf",
                                 error="в PDF нет текстового слоя, "
                                       "похоже это скан")
            return Extracted(name, "pdf", text=text[:MAX_CHARS])

        if low.endswith(".docx"):
            import docx
            doc = docx.Document(io.BytesIO(blob))
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            return Extracted(name, "docx", text=text[:MAX_CHARS])

        if low.endswith(".json"):
            data = json.loads(blob.decode("utf-8", "replace"))
            if isinstance(data, dict) and "messages" in data:
                posts, title = _telegram_export(data)
                if posts:
                    return Extracted(title or name, "telegram-export",
                                     posts=posts)
            return Extracted(name, "json",
                             text=json.dumps(data, ensure_ascii=False,
                                             indent=1)[:MAX_CHARS])

        if any(low.endswith(e) for e in TEXT_EXT):
            return Extracted(name, "text",
                             text=blob.decode("utf-8", "replace")[:MAX_CHARS])

        return Extracted(name, "?", error="формат не поддерживается. "
                                          "Подойдут pdf, docx, txt, md, "
                                          "json-экспорт канала")

    except Exception as e:                                   # noqa: BLE001
        log.warning("не разобрался с %s: %s", name, e)
        return Extracted(name, "?", error=type(e).__name__)


def as_document(items: list[Extracted]) -> str:
    """Собрать блок для промпта распаковки."""
    blocks: list[str] = []
    for it in items:
        if not it.ok:
            continue
        if it.posts:
            head = f"## Экспорт канала: {it.name}"
            body = "\n\n".join(f"### Пост {i}\n{p[:1200]}"
                               for i, p in enumerate(it.posts, 1))
            blocks.append(f"{head}\n\n{body}")
        else:
            blocks.append(f"## Файл: {it.name}\n\n{it.text}")
    return "\n\n---\n\n".join(blocks)
