"""Распаковка бренда: из публичных источников в черновик профиля.

Приложенный файл или ссылка заменяют получасовое интервью. Задача не в том,
чтобы придумать бренд, а в том, чтобы описать тот, который уже есть, и
честно назвать, чего в данных не хватило.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from orchestrator import agent, sources

log = logging.getLogger("unpack")

MAX_POSTS = 40
MAX_POST_CHARS = 1200
LOW_CONFIDENCE = 0.6


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
        return (self.suggested_name
                or (self.data.get("identity", {}) or {}).get("brand")
                or (self.data.get("identity", {}) or {}).get("who", "")[:40]
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

def _document(srcs: list[sources.Source], self_text: str) -> str:
    blocks: list[str] = []

    if self_text.strip():
        blocks.append(f"## Что человек сказал о себе\n\n{self_text.strip()}")

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


JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def _parse(raw: str) -> dict[str, Any]:
    """Модель просили отдать чистый JSON, но подстраховаться дешевле."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if m := JSON_BLOCK.search(text):
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    log.error("распаковка вернула не JSON: %s", raw[:200])
    return {}


async def run(chat_id: int, urls: list[str], self_text: str) -> Draft:
    """Прочитать источники и собрать черновик профиля."""
    srcs = await sources.fetch_all(urls, limit=MAX_POSTS)
    draft = Draft(
        read=[s.summary() for s in srcs if s.ok],
        failed=[s.summary() for s in srcs if not s.ok],
        suggested_name=next((s.title for s in srcs
                             if s.ok and s.kind == "telegram" and s.title), ""),
    )

    document = _document(srcs, self_text)
    if not document.strip():
        log.warning("нечего распаковывать: ни один источник не открылся")
        return draft

    answer = await agent.ask(
        "research", chat_id,
        "Распакуй бренд по материалам ниже. Верни только JSON по схеме из "
        "инструкции, без markdown-обёртки.\n\n" + document,
        max_tokens=3000, temperature=0.3)

    draft.data = _parse(answer)
    return draft
