#!/usr/bin/env bash
set -euo pipefail

# ── VNC password setup ───────────────────────────────────────────────
# TigerVNC uses vncpasswd to generate the hashed password file.
echo "Setting VNC password..."
mkdir -p /root/.vnc
echo "${VNC_PASSWORD:-bookr}" | vncpasswd -f > /root/.vnc/passwd
chmod 600 /root/.vnc/passwd

# ── Restore browser profile if one exists ────────────────────────────
# /profiles is a mounted volume. If it contains a saved profile, copy it
# into the working directory so Chromium picks it up.
PROFILE_SRC="/profiles/chrome-profile"
PROFILE_DST="/tmp/chrome-profile"

if [ -d "${PROFILE_SRC}" ] && [ "$(ls -A "${PROFILE_SRC}" 2>/dev/null)" ]; then
    echo "Restoring browser profile from ${PROFILE_SRC}..."
    cp -a "${PROFILE_SRC}/." "${PROFILE_DST}/"
    echo "Profile restored ($(du -sh "${PROFILE_DST}" | cut -f1))."
else
    echo "No existing browser profile found — starting fresh."
fi

# ── Export environment for supervisord children ──────────────────────
export DISPLAY="${DISPLAY:-:1}"
export RESOLUTION="${RESOLUTION:-1280x720x24}"
export NOVNC_PORT="${NOVNC_PORT:-6080}"
export VNC_PORT="${VNC_PORT:-5900}"

# ── Start supervisord (manages Xvfb, fluxbox, VNC, noVNC) ───────────
echo "Starting supervisord..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/sandbox.conf
