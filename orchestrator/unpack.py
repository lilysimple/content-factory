"""Распаковка бренда: из публичных источников в черновик профиля.

Приложенный файл или ссылка заменяют получасовое интервью. Задача не в том,
чтобы придумать бренд, а в том, чтобы описать тот, который уже есть, и
честно назвать, чего в данных не хватило.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from orchestrator import agent, files, sources

log = logging.getLogger("unpack")

MAX_POSTS = 40
MAX_POST_CHARS = 1200
LOW_CONFIDENCE = 0.6
NAME_CHARS = 40


def _head(who: str) -> str:
    """Первое звено описания как имя: «Лили: консультант по AI, мама» → «Лили».

    Описание из распаковки это фраза, а не имя. Резать её по счётчику
    знаков нельзя: получается «Лили: консультант по AI-трансформации би»,
    и этот обрубок навсегда остаётся в slug.
    """
    head = re.split(r"[:,;.(]", who.strip(), maxsplit=1)[0].strip()
    if not head or len(head) > NAME_CHARS:
        head = (head or who.strip())[:NAME_CHARS].rsplit(" ", 1)[0]
    return head.strip(" -—")


@dataclass
class Draft:
    data: dict[str, Any] = field(default_factory=dict)
    read: list[str] = field(default_factory=list)      # что открылось
    failed: list[str] = field(default_factory=list)    # что не открылось
    suggested_name: str = ""                           # из заголовка канала

    # ── доступ к разделам ─────────────────────────────────────────────

    def brand_name(self) -> str:
        """Имя бренда для slug. Slug неизменен, поэтому берём осмысленное.

        Заголовок канала лучше описания из распаковки: «Лили про AI» это
        имя, а «Консультант по AI в маркетинге» это профессия.
        """
        ident = self.data.get("identity", {}) or {}
        return (self.suggested_name
                or ident.get("brand")
                or _head(ident.get("who", ""))
                or "Новый бренд")

    @property
    def disputed(self) -> list[dict[str, Any]]:
        return self.data.get("disputed", [])[:3]

    @property
    def divergence(self) -> str | None:
        return self.data.get("divergence")

    def weak_blocks(self) -> list[str]:
        conf = self.data.get("confidence", {}) or {}
        return [k for k, v in conf.items() if isinstance(v, (int, float))
                and v < LOW_CONFIDENCE]

    # ── карточка для чата ─────────────────────────────────────────────

    def card(self) -> str:
        d = self.data
        ident = d.get("identity", {}) or {}
        aud = d.get("audience", {}) or {}
        voice = d.get("voice", {}) or {}

        def line(label: str, value: Any) -> str:
            if not value:
                return f"<b>{label}</b>  [уточнить факт]"
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value[:4])
            return f"<b>{label}</b>  {value}"

        rows = [
            line("КТО", ident.get("who")),
            line("АУДИТОРИЯ", aud.get("segments")),
            line("БОЛИ", aud.get("pains")),
            line("ГОЛОС", ", ".join(filter(None, [
                voice.get("sentence"), voice.get("address"),
                voice.get("opening")]))),
            line("НЕЛЬЗЯ", voice.get("stopwords")),
        ]
        out = "\n".join(rows)

        if self.divergence:
            out += f"\n\n⚠️ {self.divergence}"
        return out

    # ── файл профиля ──────────────────────────────────────────────────

    def to_core_md(self, brand_name: str, owner: str = "bot") -> str:
        d = self.data
        ident = d.get("identity", {}) or {}
        aud = d.get("audience", {}) or {}
        voice = d.get("voice", {}) or {}
        fmt = d.get("format", {}) or {}

        def val(x: Any, dash: str = "[уточнить факт]") -> str:
            if not x:
                return dash
            return ", ".join(str(i) for i in x) if isinstance(x, list) else str(x)

        def bullets(items: Any) -> str:
            return ("\n".join(f"- {i}" for i in items) if items
                    else "- [уточнить факт]")

        parts = [
            f"# ЯДРО. {brand_name}",
            "",
            f"owner: {owner}",
            "",
            "> Собрано распаковкой публичных источников и подтверждено "
            "человеком на онбординге.",
            "> Пометка `[уточнить факт]` означает честный пробел, а не "
            "недоработку.",
            "",
            "## 1. Кто это",
            "",
            val(ident.get("who")),
            "",
            f"Ниша: {val(ident.get('niche'))}",
            f"Оффер: {val(ident.get('offer'))}",
            "",
            "## 3. Аудитория",
            "",
            "### Сегменты",
            bullets(aud.get("segments")),
            "",
            "### Боли",
            bullets(aud.get("pains")),
            "",
            "## 9. Голос бренда",
            "",
            f"Предложения: {val(voice.get('sentence'))}",
            f"Начало поста: {val(voice.get('opening'))}",
            f"Конец поста: {val(voice.get('ending'))}",
            f"Обращение: {val(voice.get('address'))}",
            f"Юмор: {val(voice.get('humor'), 'нет')}",
            "",
            "### Свои обороты",
            bullets(voice.get("uses")),
            "",
            "### Чего избегаем",
            bullets(voice.get("avoids")),
            "",
            "### Стоп-слова",
            bullets(voice.get("stopwords")),
            "",
            "## 10. Формат",
            "",
            f"Средняя длина: {val(fmt.get('avg_length'))} знаков",
            f"Эмодзи: {val(fmt.get('emoji'))}",
            f"Структура: {val(fmt.get('structure'))}",
        ]

        if self.divergence:
            parts += ["", "## Расхождение с архивом", "",
                      f"⚠️ {self.divergence}", "",
                      "Для калибровки не использовано, но держать в уме при "
                      "проверке черновиков."]

        if self.failed:
            parts += ["", "## Чего не удалось собрать", ""] + \
                     [f"- {f}" for f in self.failed]

        return "\n".join(parts)


# ── сборка входа для модели ───────────────────────────────────────────

def _document(srcs: list[sources.Source], self_text: str,
              uploads: list = None, current: str = "") -> str:
    blocks: list[str] = []

    # Текущий профиль идёт первым: задача не собрать заново, а поправить
    # по новым материалам. Иначе каждая пересборка теряет уточнения,
    # которые человек уже подтвердил.
    if current.strip():
        blocks.append(
            "## Текущий профиль бренда\n\n"
            "Он уже собран и подтверждён человеком. Твоя задача — уточнить "
            "его по материалам ниже, а не написать с нуля. Что новые данные "
            "не затрагивают, оставь как есть. Что противоречит — исправь и "
            "назови это в поле divergence.\n\n" + current.strip()[:12000])

    if self_text.strip():
        blocks.append(f"## Что человек сказал о себе\n\n{self_text.strip()}")

    # Приложенные файлы идут первыми после рассказа о себе: стратегия или
    # экспорт канала точнее, чем что угодно собранное из открытых источников.
    if uploads:
        if doc := files.as_document(uploads):
            blocks.append(doc)

    for s in srcs:
        if not s.ok:
            continue
        if s.kind == "telegram":
            head = [f"## Telegram: {s.title or s.url}"]
            if s.subscribers:
                head.append(s.subscribers)
            if s.description:
                head.append(s.description)
            posts = []
            for i, p in enumerate(s.posts[:MAX_POSTS], 1):
                views = f" [просмотров {p.views}]" if p.views else ""
                posts.append(f"### Пост {i}{views}\n{p.text[:MAX_POST_CHARS]}")
            blocks.append("\n\n".join(head + posts))
        else:
            blocks.append(f"## Сайт: {s.title or s.url}\n\n{s.text[:8000]}")

    return "\n\n---\n\n".join(blocks)


async def run(chat_id: int, urls: list[str], self_text: str,
              uploads: list | None = None, current: str = "") -> Draft:
    """Прочитать источники и собрать черновик профиля."""
    srcs = await sources.fetch_all(urls, limit=MAX_POSTS)
    uploads = uploads or []
    draft = Draft(
        read=[s.summary() for s in srcs if s.ok]
             + [u.summary() for u in uploads if u.ok],
        failed=[s.summary() for s in srcs if not s.ok]
               + [u.summary() for u in uploads if not u.ok],
        suggested_name=next(
            (s.title for s in srcs if s.ok and s.kind == "telegram" and s.title),
            next((u.name for u in uploads
                  if u.ok and u.kind == "telegram-export"), "")),
    )

    document = _document(srcs, self_text, uploads, current)
    if not document.strip():
        log.warning("нечего распаковывать: ни один источник не открылся")
        return draft

    answer = await agent.ask(
        "research", chat_id,
        ("Уточни профиль бренда по новым материалам."
         if current else "Распакуй бренд по материалам ниже.") +
        " Верни только JSON по схеме из инструкции, без markdown-обёртки."
        "\n\n" + document,
        max_tokens=12000)

    draft.data = agent.parse_json(answer, who="распаковка")
    return draft
