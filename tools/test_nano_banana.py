"""
Тестовый прогон Медиагенератора (Nano Banana / Gemini image) для Контент-завода.

Не часть завода как роли — отдельный проверочный скрипт по инструкциям
скилла content-factory-media-generator. Ничего не пишет в БД и в themes.

Запуск:
    ./.venv/bin/python tools/test_nano_banana.py

Нужен ключ в .env (или в окружении): GEMINI_API_KEY=...
"""
from __future__ import annotations

import base64
import datetime as dt
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from google import genai
except ImportError:
    sys.exit(
        "Не поставлен пакет google-genai.\n"
        "Поставь: ./.venv/bin/pip install google-genai"
    )

API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
if not API_KEY:
    sys.exit(
        "Нет GEMINI_API_KEY (и GOOGLE_API_KEY тоже пуст).\n"
        "Добавь строку в .env: GEMINI_API_KEY=твой_ключ_из_aistudio.google.com"
    )

MODEL = os.getenv("MODEL_MEDIA", "gemini-3.1-flash-image")

# Выдуманное тестовое ТЗ — как от редактора: разбёрнутое текстовое описание сцены.
PROMPT = (
    "Cozy small cafe interior at golden hour sunset, warm soft light through "
    "large window, steam rising from a cup of coffee on a wooden table, "
    "plants and books in the background, nobody in frame, photographic style, "
    "warm terracotta and cream color palette, shallow depth of field, "
    "square 1:1 composition, negative space in upper third for text overlay"
)

OUT_DIR = Path(__file__).parent.parent / "tmp" / "media-test"
OUT_DIR.mkdir(parents=True, exist_ok=True)
stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

print(f"Модель: {MODEL}")
print(f"Промпт: {PROMPT}")
print("Зову Nano Banana...")

client = genai.Client(api_key=API_KEY)
interaction = client.interactions.create(model=MODEL, input=PROMPT)

png_path = OUT_DIR / f"{stamp}.png"
png_path.write_bytes(base64.b64decode(interaction.output_image.data))

log_path = OUT_DIR / f"{stamp}-prompt.md"
log_path.write_text(
    f"# Тестовая генерация\n\n"
    f"Дата: {stamp}\n"
    f"Модель: {MODEL}\n"
    f"Вход: выдуманное ТЗ (тестовый прогон скилла)\n\n"
    f"## Промпт\n\n{PROMPT}\n",
    encoding="utf-8",
)

print(f"Готово: {png_path}")
print(f"Лог: {log_path}")
