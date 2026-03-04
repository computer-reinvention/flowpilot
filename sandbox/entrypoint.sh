#!/usr/bin/env bash
set -euo pipefail

# ── Restore browser profile if one exists ────────────────────────────
PROFILE_SRC="/profiles/chrome-profile"
PROFILE_DST="/tmp/chrome-profile"

if [ -d "${PROFILE_SRC}" ] && [ "$(ls -A "${PROFILE_SRC}" 2>/dev/null)" ]; then
    echo "Restoring browser profile from ${PROFILE_SRC}..."
    cp -a "${PROFILE_SRC}/." "${PROFILE_DST}/"
    chown -R sandboxuser:sandboxuser "${PROFILE_DST}"
    echo "Profile restored ($(du -sh "${PROFILE_DST}" | cut -f1))."
else
    echo "No existing browser profile found — starting fresh."
fi

# ── Find Chromium binary ─────────────────────────────────────────────
# Prefer Playwright's bundled Chromium (works reliably in containers).
# Fall back to system chromium if Playwright's isn't found.
PW_CHROME=$(find /root/.cache/ms-playwright -type f -name 'chrome' 2>/dev/null | head -1 || true)
if [ -n "$PW_CHROME" ] && [ -x "$PW_CHROME" ]; then
    echo "Using Playwright Chromium: $PW_CHROME"
    CHROME_BIN="$PW_CHROME"
elif command -v chromium &>/dev/null; then
    echo "Using system chromium"
    CHROME_BIN="chromium"
elif command -v chromium-browser &>/dev/null; then
    echo "Using system chromium-browser"
    CHROME_BIN="chromium-browser"
else
    echo "ERROR: No Chromium binary found!" >&2
    exit 1
fi
export CHROME_BIN

# ── Export environment for supervisord children ──────────────────────
export DISPLAY="${DISPLAY:-:1}"
export RESOLUTION="${RESOLUTION:-1280x720x24}"

# ── Start supervisord (manages Xvfb, Chromium, socat CDP forwarder) ──
echo "Starting supervisord..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/sandbox.conf
