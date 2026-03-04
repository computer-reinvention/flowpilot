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

# ── Export environment for supervisord children ──────────────────────
export DISPLAY="${DISPLAY:-:1}"
export RESOLUTION="${RESOLUTION:-1280x720x24}"

# ── Start supervisord (manages Xvfb, Chromium, socat CDP forwarder) ──
echo "Starting supervisord..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/sandbox.conf
