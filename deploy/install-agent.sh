#!/bin/zsh
# Поставить завод на автозапуск через launchd.
#
# Агент, а не демон: демон стартует до входа в систему, от root и без
# доступа к пользовательскому окружению — Chrome для рендера макетов там
# не работает. Агент поднимается при входе в систему.
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="space.lily.content-factory"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__ROOT__|$ROOT|g" -e "s|__HOME__|$HOME|g" \
  "$ROOT/deploy/$LABEL.plist" > "$TARGET"
echo "плист записан: $TARGET"

# Ручной инстанс сначала гасим: launchd держит один процесс на Label, но
# про запущенное мимо него он не знает, и два поллинга глушат друг друга.
if pids=$(pgrep -f "$ROOT/.venv/bin/python main.py" 2>/dev/null) || \
   pids=$(pgrep -f "main.py" 2>/dev/null); then
  echo "гашу запущенное вручную: $pids"
  kill -9 $pids 2>/dev/null || true
  sleep 1
fi

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$TARGET"
launchctl enable "gui/$UID_NUM/$LABEL"
echo "агент загружен"

sleep 3
launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -E "state|pid|last exit" | head -3
