#!/bin/bash
# Sunshine prep_cmd Do — switches the virtual display to client resolution.
# Leaves physical monitors alone (idle-daemon controls those).
#
# Sunshine exports SUNSHINE_CLIENT_WIDTH/HEIGHT/FPS.

INHIBIT_PID_FILE="/tmp/sunshine-sleep-inhibit.pid"

if ! [ -f "$INHIBIT_PID_FILE" ] || ! kill -0 "$(cat "$INHIBIT_PID_FILE" 2>/dev/null)" 2>/dev/null; then
    systemd-inhibit --what=sleep:idle --who="Sunshine" \
        --why="Moonlight streaming session active" --mode=block sleep infinity &
    echo $! > "$INHIBIT_PID_FILE"
fi

exec sudo /usr/local/bin/sunshine-virt-display-handler connect \
    --width  "${SUNSHINE_CLIENT_WIDTH:-1920}" \
    --height "${SUNSHINE_CLIENT_HEIGHT:-1080}" \
    --refresh "${SUNSHINE_CLIENT_FPS:-60}"
