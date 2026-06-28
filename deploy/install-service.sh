#!/usr/bin/env bash
# Install (or reinstall) MTG Assistant as a macOS launchd LaunchAgent that
# starts at login and restarts on crash.
#
# Usage:
#   ./deploy/install-service.sh                 # label com.phase.mtg, port 8771
#   MTG_PORT=8772 ./deploy/install-service.sh com.phase.mtgassistant
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="${1:-com.phase.mtg}"
PORT="${MTG_PORT:-8771}"
PLIST_SRC="$APP_DIR/deploy/com.phase.mtg.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "App directory : $APP_DIR"
echo "Service label : $LABEL"
echo "Local port    : $PORT"
echo

# 1. Virtualenv + dependencies (done once here, not on every launch).
if [ ! -d "$APP_DIR/.venv" ]; then
  echo "Creating virtualenv..."
  python3 -m venv "$APP_DIR/.venv"
fi
echo "Installing dependencies..."
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# 2. Render the plist template into LaunchAgents.
mkdir -p "$HOME/Library/LaunchAgents" "$APP_DIR/data"
sed -e "s|__LABEL__|$LABEL|g" \
    -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__PORT__|$PORT|g" \
    "$PLIST_SRC" > "$PLIST_DST"
echo "Wrote $PLIST_DST"

# 3. (Re)load and start the service.
DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST_DST"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo
echo "Service installed and started."
echo "Verify locally:  curl -s http://127.0.0.1:$PORT | head -n 5"
echo "Logs:            $APP_DIR/data/service.out.log / service.err.log"
echo
echo "Next: add the tunnel ingress + DNS for your subdomain (see README)."
