#!/usr/bin/env bash
set -euo pipefail

# ── Restore browser profile if one exists ────────────────────────────
PROFILE_DST="/tmp/chrome-profile"
mkdir -p "${PROFILE_DST}"
# The backend injects only the selected profile before starting this container.
chown -R sandboxuser:sandboxuser "${PROFILE_DST}"

# ── Find Chromium binary ─────────────────────────────────────────────
# The image ships one digest-pinned Chrome for Testing build (Dockerfile.sandbox,
# audited by scripts/ops_image_audit.py). No silent fallback to another browser.
CHROME_BIN="/opt/chrome-for-testing/chrome-linux64/chrome"
if [ ! -x "$CHROME_BIN" ]; then
    echo "ERROR: pinned Chromium binary missing at $CHROME_BIN" >&2
    exit 1
fi
echo "Using Chrome for Testing: $CHROME_BIN"
export CHROME_BIN

# ── Export environment for supervisord children ──────────────────────
export DISPLAY="${DISPLAY:-:1}"
export RESOLUTION="${RESOLUTION:-1280x800x24}"
export CHROME_AUTORESTART="${CHROME_AUTORESTART:-true}"

# ── Start supervisord (manages Xvfb, Chromium, socat CDP forwarder) ──
echo "Starting supervisord..."
exec /usr/local/bin/supervisord -c /etc/supervisor/conf.d/sandbox.conf
