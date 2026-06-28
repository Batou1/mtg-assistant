#!/usr/bin/env bash
# Stop and remove the launchd LaunchAgent for the app.
# Usage: ./deploy/uninstall-service.sh [label]   (default com.phase.mtg)
set -euo pipefail

LABEL="${1:-com.phase.mtg}"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
rm -f "$PLIST_DST"
echo "Removed service $LABEL ($PLIST_DST)."
echo "Note: this does not touch your Cloudflare tunnel config or DNS."
