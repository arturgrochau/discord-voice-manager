#!/bin/bash
# Install/refresh the P&P Sentinel LaunchAgent so the bot runs 24/7 and restarts on failure.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$REPO_DIR/launchd/com.arturgrochau.pnp-sentinel.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.arturgrochau.pnp-sentinel.plist"
LABEL="com.arturgrochau.pnp-sentinel"

if [ ! -f "$REPO_DIR/.env" ]; then
    echo "❌ $REPO_DIR/.env missing — create it with DISCORD_BOT_TOKEN=..." >&2
    exit 1
fi
if [ ! -f "$REPO_DIR/config.json" ]; then
    echo "❌ $REPO_DIR/config.json missing — copy config_template.json and fill it in." >&2
    exit 1
fi

mkdir -p "$REPO_DIR/logs" "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "✅ $LABEL installed and started."
echo "   logs:   tail -f $REPO_DIR/logs/sentinel.log"
echo "   stop:   launchctl bootout gui/$(id -u)/$LABEL"
