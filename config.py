"""Конфигурация из окружения. Единственное место, где читается .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent


def _chats(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


@dataclass(frozen=True)
class Config:
    # Ключа API здесь нет и быть не должно. Модель зовётся через CLI по
    # подписке, авторизация — вход `claude` в терминале, учётные данные
    # лежат в Keychain. Ключ в окружении увёл бы вызовы на API-тариф, то
    # есть на деньги; `cli.clean_env` его в подпроцесс и не пускает.
    model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")

    # Модель и усилие на роль: MODEL_EDITOR, EFFORT_EDITOR. Распаковка и
    # стратегия это суждение, форматирование — нет, платить одинаково незачем.
    #
    # Настройки **старого пути**, того, что ходит через `agent.ask`. На путь
    # через Claude Code (`orchestrator/bridge.py`) не влияют и не должны:
    # `clean_env` не пропускает в подпроцесс ни `ANTHROPIC_MODEL`, ни
    # `MODEL_*`, там моделью распоряжается сам Claude Code.
    #
    # Роли здесь только те, что реально зовут модель. `publisher` не зовёт
    # её вовсе, `ideator` живёт субагентом Claude Code — ключи сняты 01.09,
    # бот и токен сняты 03.09: он поллился, не имея поведения.
    # `intake` здесь не лишний: распаковщик ходит в модель дважды — за
    # вопросами и за файлами профиля, — и формулирование вопросов это
    # не суждение. Без своей строки роль брала общее `high` и платила
    # за это четырьмя минутами молчания в чате у живого клиента.
    _ASK_ROLES = ("assistant", "research", "strategy", "editor", "reels",
                  "design", "intake")

    role_models: dict[str, str] = field(default_factory=lambda: {
        role: os.getenv(f"MODEL_{role.upper()}", "") for role in Config._ASK_ROLES
    })
    role_effort: dict[str, str] = field(default_factory=lambda: {
        role: os.getenv(f"EFFORT_{role.upper()}", "") for role in Config._ASK_ROLES
    })
    default_effort: str = os.getenv("ANTHROPIC_EFFORT", "high")

    def model_for(self, role: str) -> str:
        return self.role_models.get(role) or self.model

    def effort_for(self, role: str) -> str:
        return self.role_effort.get(role) or self.default_effort

    tokens: dict[str, str] = field(default_factory=lambda: {
        "assistant": os.getenv("BOT_ASSISTANT", ""),
        "research":  os.getenv("BOT_RESEARCH", ""),
        "strategy":  os.getenv("BOT_STRATEGY", ""),
        "editor":    os.getenv("BOT_EDITOR", ""),
        "reels":     os.getenv("BOT_REELS", ""),
        "design":    os.getenv("BOT_DESIGN", ""),
        "publisher": os.getenv("BOT_PUBLISHER", ""),
    })

    # Бот распаковки клиента. Живёт в личке и в `cfg.tokens` не входит
    # намеренно: `tokens` это семь лиц завода, их поднимает `registry` и
    # поллит `main`, а у них общий чат-супергруппа и общие топики.
    # Распаковщик работает один на один с клиентом, который тенантом ещё
    # не стал, — свой диспетчер, свой набор апдейтов, своя таблица.
    #
    # Пусто — бот не поднимается вовсе, и завод об этом говорит строкой в
    # логе, а не падает: распаковка не нужна для работы уже собранного
    # бренда.
    intake_token: str = os.getenv("BOT_INTAKE", "")

    allowed_chats: set[int] = field(
        default_factory=lambda: _chats(os.getenv("ALLOWED_CHATS", "")))
    # Куда публикует Публикатор. Числовой id канала (-100…) или @имя.
    # Пусто — публикация выключена, и он об этом говорит вслух.
    publish_channel: str = os.getenv("PUBLISH_CHANNEL", "")

    # Свой профиль Instagram: ник без «@». Пара к `publish_channel`, но
    # работает иначе — в сеть за ним никто не ходит. Страница отдаётся
    # пустой оболочкой, `api/v1` без входа отвечает 401, и снимает профиль
    # `tools/instagram_pull.py` своим Chrome в кэш папки бренда. Сводка
    # читает кэш.
    #
    # Пусто — своей статистики по Instagram в сводке нет, и Ресёрчер
    # говорит об этом дырой. Молчание тут читалось бы как ноль охвата, а
    # это разные вещи: цели этапа считаются по Instagram, и сводка без
    # него отвечает на половину вопроса.
    instagram_profile: str = os.getenv("INSTAGRAM_PROFILE", "")

    # Публикация в Instagram через Graph API. Три переменные, и работает
    # только набор целиком: без `IG_USER_ID` публиковать некуда, без
    # `IG_TOKEN` нечем. Пусто — Instagram остаётся ручной площадкой, слоты
    # помечены ✋, автомат их не касается, и `instapost.ready()` отвечает
    # об этом «нет» коду, а не промпту.
    #
    # `IG_TOKEN` это первый завод долгого токена, а не его дом: Meta
    # выдаёт токен на шестьдесят дней, завод продлевает его сам и пишет
    # продлённый в базу (`ig_token`). Значение здесь читается один раз.
    # Путь входа: `facebook` или `instagram`. Различает их ролик, а не
    # удобство. На фейсбучном пути видео уходит байтами прямо в Meta и
    # проходит до 300 МБ; на инстаграмном — только по публичной ссылке, а
    # ссылку у завода даёт Telegram с потолком 20 МБ, то есть основной
    # формат площадки через него почти никогда не проходит. Поэтому дом по
    # умолчанию фейсбучный, хоть он и требует связанной страницы.
    ig_login: str = os.getenv("IG_LOGIN", "facebook")
    ig_user_id: str = os.getenv("IG_USER_ID", "")
    ig_token: str = os.getenv("IG_TOKEN", "")
    # Версия Graph API. Пусто — путь без версии, Meta отвечает актуальной.
    # Вшитая версия устаревает молча, поэтому называть её нужно только
    # тогда, когда поведение новой версии уже сломало что-то своё.
    ig_api_version: str = os.getenv("IG_API_VERSION", "")

    brands_path: Path = ROOT / os.getenv("BRANDS_PATH", "./Brands")
    db_path: Path = ROOT / os.getenv("DB_PATH", "./factory.db")
    default_tz: str = os.getenv("DEFAULT_TZ", "Europe/Moscow")
    llm_budget_day: int = int(os.getenv("LLM_BUDGET_PER_TENANT_DAY", "200"))

    # Ключ Pexels для `tools/stock_pull.py`. Завод его не зовёт: сток
    # приезжает в фотобанк руками человека, а не по ходу вёрстки —
    # Дизайнер не должен добирать картинку в момент сборки макета.
    pexels_key: str = os.getenv("PEXELS_API_KEY", "")

    # Ключ Gemini для генерации фона (`orchestrator/imagegen.py`).
    # Отдельный от модели завода намеренно: текст идёт через Claude Code
    # по подписке, ключа Anthropic у завода нет с 01.09, а картинки там
    # взять негде. Пусто — генерация не подмешивается к вариантам фона.
    gemini_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_image_model: str = os.getenv("GEMINI_IMAGE_MODEL",
                                        "gemini-3-pro-image-preview")

    # Ключ Apify для `tools/trends_pull.py`: чужие рилсы по хэштегу и по
    # профилю. Платный и зовётся руками, как и сток: прогон стоит денег,
    # а тренд недельный — фоновый опрос сжигал бы бюджет на том, что
    # человек всё равно смотрит раз в неделю. Пусто — Trend Watcher
    # работает на том кэше, что уже лежит, и называет это дырой.
    # Ключ HeyGen для `tools/heygen_pull.py`: говорящая голова в низ
    # сплита, когда съёмки нет. Платный и зовётся руками, как сток и
    # тренды: HeyGen считает минуты рендера, а не вызовы, и фоновый
    # прогон сжигал бы тариф на черновиках. Пусто — инструмент говорит
    # это строкой и в сеть не идёт.
    heygen_key: str = os.getenv("HEYGEN_API_KEY", "")
    # Аватар и голос бренда на этом аккаунте. Держатся в окружении, а не
    # в папке бренда: id привязан к аккаунту HeyGen, а не к клиенту.
    heygen_avatar: str = os.getenv("HEYGEN_AVATAR_ID", "")
    heygen_voice: str = os.getenv("HEYGEN_VOICE_ID", "")

    apify_token: str = os.getenv("APIFY_TOKEN", "")
    # Акторов два, потому что цели разного рода снимаются разными.
    # Профиль — сеткой постов (`instagram-scraper`), тег и ключевое слово —
    # хэштег-скрапером, у которого есть `resultsType: reels`. Первый по
    # тегу отдаёт только фото и карусели, это замерено 14.10.
    apify_actor: str = os.getenv("APIFY_ACTOR", "apify~instagram-scraper")
    apify_actor_tag: str = os.getenv("APIFY_ACTOR_TAG",
                                     "apify~instagram-hashtag-scraper")

    def missing_tokens(self) -> list[str]:
        return [role for role, token in self.tokens.items() if not token]

    def chat_allowed(self, chat_id: int) -> bool:
        # Пустой allowlist — только для локальной отладки.
        return not self.allowed_chats or chat_id in self.allowed_chats


cfg = Config()
