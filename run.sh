#!/bin/zsh
# Запуск завода независимо от сессии Claude.
# Только один экземпляр: Telegram разрешает один опрос на бота.
cd "$(dirname "$0")"
pkill -f "Python main.py" 2>/dev/null; sleep 2
exec ./.venv/bin/python main.py 2>&1 | tee -a bot.log
