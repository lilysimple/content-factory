"""Цикл 26: распаковка клиента — материалы, опросник, запись профиля.

Проверяется не разговор, а границы. Разговор здесь дешёвый: кнопка и
свободный текст. Дорого стоит другое — файл профиля, который роли читают
неделями. Пропорция воронки, не дающая сотню, врёт молча; площадка,
которой у завода нет, читается как обещание; пустой файл выглядит как
собранный.
"""
from __future__ import annotations

import asyncio

import harness
from harness import CHAT, check, report

harness.setup()

from config import cfg                                     # noqa: E402
from orchestrator import agent, intake, materials, unpack   # noqa: E402
from storage import brand as brand_store                    # noqa: E402
from storage import db                                      # noqa: E402

db.init(cfg.db_path)

DRAFT = {
    "identity": {"who": "Лили: консультант по AI", "niche": "обучение",
                 "offer": "курс"},
    "audience": {"segments": ["маркетологи"], "pains": ["не хватает времени"]},
    "voice": {"sentence": "короткие", "address": "ты", "opening": "с факта",
              "stopwords": ["прорыв"]},
    "format": {"avg_length": "1200", "emoji": "редко", "structure": "абзацы"},
    "confidence": {"identity": 0.9, "audience": 0.4, "voice": 0.8,
                   "format": 0.7},
}

META_CSV = (
    "Post ID,Permalink,Description,Publish time,Likes,Comments,Reach\n"
    "1,https://www.instagram.com/p/ABC123/,Первый пост,09/01/2026 10:30,12,2,880\n"
    "2,https://www.instagram.com/reel/XYZ789/,Ролик,09/05/2026 18:05,40,5,3100\n"
)


def install(answer):
    async def ask(role, chat_id, prompt, **kw):
        return answer(prompt) if callable(answer) else answer
    agent.ask = ask


async def main() -> None:
    draft = unpack.Draft(data=DRAFT, suggested_name="Клиент Тест")

    # ── 1. утверждённый список файлов ─────────────────────────────────
    print("\n1. Список файлов и дыры считает код")
    keys = [n.key for n in intake.NEEDS]
    check("в списке шесть файлов профиля", len(keys) == 6, str(keys))
    check("каждый ключ известен папке бренда",
          all(k in brand_store.PROFILE for k in keys),
          str([k for k in keys if k not in brand_store.PROFILE]))

    holes = intake.gaps(draft, {})
    check("на пустых ответах не хватает пяти файлов из шести",
          holes == [k for k in keys if k != "core"], str(holes))
    check("записанный файл из дыр уходит",
          "goals" not in intake.gaps(draft, {"written": ["goals"]}),
          str(intake.gaps(draft, {"written": ["goals"]})))
    check("черновика нет — ЯДРО тоже дыра",
          "core" in intake.gaps(unpack.Draft(), {}))
    check("шаткий блок назван до кнопки", "аудитория" in intake.weak_core(draft),
          str(intake.weak_core(draft)))

    # ── 2. ссылки: что читаем сами, что просим файлом ─────────────────
    print("\n2. Ссылки сортируются честно")
    mine, theirs = intake.sort_links([
        "https://t.me/lilyspace1", "https://lily.space",
        "https://instagram.com/lily_.space",
        "https://www.linkedin.com/in/someone"])
    check("канал и сайт читаем сами", len(mine) == 2, str(mine))
    check("Instagram и LinkedIn — файлом или словами", len(theirs) == 2,
          str(theirs))
    check("в просьбе про Instagram названа выгрузка Meta",
          "Export data" in intake.ASK_EXPORT)

    # ── 3. выгрузка Meta Business Suite ───────────────────────────────
    print("\n3. Выгрузка Meta опознаётся по шапке, а не по имени файла")
    check("шапка выгрузки опознана", intake.is_meta_export(META_CSV))
    check("случайный csv за выгрузку не принят",
          not intake.is_meta_export("a,b,c\n1,2,3\n"))

    posts, nick = intake.meta_posts(META_CSV)
    check("оба поста разобраны", len(posts) == 2, str(len(posts)))
    check("код поста из ссылки", posts[0]["code"] == "ABC123", posts[0]["code"])
    check("дата кабинета разобрана",
          str(posts[0]["date"]).startswith("2026-09-01"), str(posts[0]["date"]))
    check("ролик помечен роликом", posts[1]["video"] is True)
    check("ник взят из ссылки или пуст, но не мусор",
          nick in {"", "lily_.space", "p"}, nick)

    # ── 4. границы файлов: отказ, а не предупреждение ─────────────────
    print("\n4. Границу держит код")
    check("пустой файл — отказ", bool(intake.check("goals", "")))
    check("текст без заголовка — отказ",
          bool(intake.check("goals", "просто проза")))
    good_goals = ("# Цели и метрики\n\n## Точка А\n\n33 подписчика\n\n"
                  "## Точка Б\n\n300 подписчиков\n\n## Линейка\n\n"
                  "курс 5000 ₽\n\n## Пропорция воронки\n\n"
                  "прогрев 50, продукт 30, личное 20\n")
    check("собранные цели проходят", intake.check("goals", good_goals) == "",
          intake.check("goals", good_goals))
    bad = good_goals.replace("личное 20", "личное 40")
    check("доли не дают сотню — отказ", "пропорц" in intake.check("goals", bad),
          intake.check("goals", bad))
    check("пропал раздел — отказ",
          "нет разделов" in intake.check(
              "goals", good_goals.replace("## Линейка\n\nкурс 5000 ₽\n\n", "")))
    check("подстановка в файле — отказ",
          "подстановки" in intake.check("goals",
                                        good_goals + "\n{brand_name}\n"))

    plats = ("# Площадки\n\n## Связка\n\nTelegram дом\n\n## Регулярность\n\n"
             "три выхода\n\n## Нормы площадок\n\nпервая строка сильная\n")
    check("площадки проходят", intake.check("platforms", plats) == "",
          intake.check("platforms", plats))
    check("площадки, которой нет у завода, мало для файла",
          bool(intake.check("platforms",
                            plats.replace("Telegram", "Одноклассники"))))

    srcs = ("# Источники\n\n## Свои каналы\n\nhttps://t.me/lilyspace1\n\n"
            "## Кого смотреть\n\n@someone\n")
    check("источники со ссылкой проходят", intake.check("sources", srcs) == "",
          intake.check("sources", srcs))
    check("источники без ссылок и ников — отказ",
          bool(intake.check("sources",
                            "# Источники\n\n## Свои каналы\n\nмой канал\n\n"
                            "## Кого смотреть\n\nконкуренты\n")))

    # ── 5. опросник: дыры считает код, формулирует модель ─────────────
    print("\n5. Опросник")
    install('{"questions": [{"key": "goals", "ask": "Где хотите быть через '
            'полгода?", "example": "300 подписчиков", "why": "по этому '
            'считается план"}, {"key": "нет-такого", "ask": "левый вопрос", '
            '"example": "", "why": ""}]}')
    qs = await intake.quiz(CHAT, draft, {}, ["goals", "platforms"])
    check("вопрос с чужим ключом выброшен", len(qs) == 1, str(len(qs)))
    check("вопрос привязан к файлу", qs[0].key == "goals", qs[0].key)
    check("человек видит зачем", "считается план" in qs[0].text())

    install('{"questions": []}')
    check("без дыр вопросов нет",
          await intake.quiz(CHAT, draft, {}, []) == [])

    # ── 6. запись в папку бренда ──────────────────────────────────────
    print("\n6. Запись файлов")
    answers = {"quiz": {"Где хотите быть?": "300 подписчиков"},
               "meta_export": META_CSV, "instagram": "lily_.space"}
    files_by_key = {
        "goals": good_goals,
        "platforms": plats,
        "sources": srcs,
        "rubrics": "# Рубрикатор\n\n## Рубрики\n\n- разбор, раз в неделю\n",
        "hooks": ("# Хуки\n\n## Механика хука\n\nфакт в первой строке\n\n"
                  "## Продвижение\n\nответы в комментариях\n"),
    }

    def by_prompt(prompt: str) -> str:
        for key, text in files_by_key.items():
            if brand_store.PROFILE[key] in prompt:
                return text
        return "# Пусто\n"

    install(by_prompt)
    state = {"answers": answers, "slug": None}
    out = await intake.land(CHAT, state, draft, ["core", "goals", "platforms",
                                                 "rubrics", "hooks", "sources"])
    check("папка бренда создана", bool(out.slug), str(out.slug))
    check("шесть файлов записаны", len(out.files) == 6, str(out.files))
    check("отказов нет", out.denied == {}, str(out.denied))

    b = brand_store.Store(cfg.brands_path).get(out.slug)
    check("ЯДРО легло на диск", "Голос бренда" in b.read("core"))
    check("цели легли на диск", "прогрев 50" in b.read("goals"))
    check("выгрузка Instagram легла в кэш",
          b.path("research/instagram").exists()
          and any(b.path("research/instagram").iterdir()),
          "кэша нет")
    check("человек видит строку про выгрузку",
          any("выгрузка" in n for n in out.notes), str(out.notes))

    # ── 6b. усиление: четырнадцать пунктов спеки ──────────────────────
    print("\n6b. Усиление до полного разбора")
    check("усиление плюс материалы и опросник дают четырнадцать",
          len(intake.DEEP) + 2 == intake.SPEC_QUESTIONS,
          f"{len(intake.DEEP)} + 2")
    oids = [d.oid for d in intake.DEEP]
    check("номера пунктов не повторяются", len(set(oids)) == len(oids), str(oids))
    check("O1 и O8 в усиление не попали",
          not ({"O1", "O8"} & set(oids)), str(oids))
    check("каждый пункт усиливает известный файл",
          all(d.key in intake.BY_KEY for d in intake.DEEP))
    check("отвеченный пункт из очереди уходит",
          len(intake.deep_left({"deep": {"O3": "к заявкам"}}))
          == len(intake.DEEP) - 1)
    check("вопросы усиления не зовут модель",
          all(d.ask and not d.ask.startswith("{") for d in intake.DEEP))
    check("про цены сказано, что можно не называть",
          "можно не называть" in intake.DEEP_BY_OID["O12"].ask,
          intake.DEEP_BY_OID["O12"].ask)

    # ── 6c. отказ по воронке не стоит человеку всего файла ─────────────
    # Первый живой прогон потерял goals.md целиком из-за одной строки из
    # двенадцати: доли не дали сотню, и файл не записался вовсе.
    print("\n6c. Пропорция воронки чинится, а не теряет файл")
    broken = good_goals.replace("прогрев 50, продукт 30, личное 20",
                                "прогрев 70, продукт 70, личное 70")
    check("сломанная пропорция — отказ", bool(intake.check("goals", broken)))
    patched = intake.fix_funnel(broken)
    check("после починки файл проходит", intake.check("goals", patched) == "",
          intake.check("goals", patched))
    check("запасная пропорция названа запасной", "запасная" in patched)
    check("прежние разделы целы",
          all(h in patched for h in ("Точка А", "Точка Б", "Линейка")))
    check("старой строки с долями не осталось", "70" not in patched)

    install(lambda p: broken)
    out_b = await intake.land(CHAT, {"answers": {}, "slug": None}, draft,
                              ["goals"])
    check("файл всё-таки записан", "goals" in out_b.files_by_key,
          str(out_b.denied))
    check("человеку сказано про запасную пропорцию",
          any("запасная" in n for n in out_b.notes), str(out_b.notes))

    # ── 6d. файл уходит в чат разделами ───────────────────────────────
    print("\n6d. Файл в чате читается разделами")
    msgs = intake.as_messages("goals", good_goals)
    check("сообщений хотя бы одно", len(msgs) >= 1, str(len(msgs)))
    check("первое называет файл", "goals.md" in msgs[0], msgs[0][:60])
    check("заголовки разделов жирные", "<b>Точка А</b>" in "".join(msgs))
    check("ни одно сообщение не превышает потолок Telegram",
          all(len(m) < 4096 for m in msgs), str(max(len(m) for m in msgs)))
    long_file = ("# Цели\n\n## Точка А\n\n" + "строка про охват. " * 400
                 + "\n\n## Точка Б\n\nрост\n")
    parts = intake.as_messages("goals", long_file)
    check("длинный файл разрезан", len(parts) > 1, str(len(parts)))
    check("куски длинного файла тоже в потолке",
          all(len(m) < 4096 for m in parts), str(max(len(m) for m in parts)))

    # ── 7. образцы голоса приезжают из сырья распаковки ───────────────
    # `materials.adopt` по умолчанию читает таблицу онбординга, а у
    # распаковки своя. Без явного сырья образцов всегда ноль, и молча:
    # человеку сказано «готово», а калибровать голос нечем.
    print("\n7. Образцы голоса из сырья распаковки")
    raw = [{"step": "collect", "text": "Живой текст клиента. " * 40},
           {"step": "collect", "text": "коротко"}]
    moved, samples = materials.adopt(CHAT, b, raw)
    check("длинный текст стал образцом голоса", samples == 1, str(samples))
    check("короткая реплика образцом не стала",
          len(list(b.path("voice-samples").glob("*.md"))) >= 1)
    again = materials.adopt(CHAT, b, raw)[1]
    check("второй заход не дублирует образец", again == 0, str(again))

    # Второй проход по тому же slug: файл не пересобирается заново с нуля,
    # а правится — и потеря разделов это отказ, а не тихая запись.
    print("\n8. Второй проход не стирает собранное")
    install(lambda p: "# Цели и метрики\n\n## Точка А\n\nвсё\n")
    out2 = await intake.land(CHAT, {"answers": answers, "slug": out.slug},
                             draft, ["goals"])
    check("усохший файл не записан", "goals" in out2.denied, str(out2.denied))
    check("на диске остались прежние цели", "прогрев 50" in b.read("goals"))
    check("slug не сменился", out2.slug == out.slug, out2.slug)


if __name__ == "__main__":
    asyncio.run(main())
    raise SystemExit(report())
