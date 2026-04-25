#!/bin/bash
# Sunshine prep_cmd Undo — restores baseline placeholder on the virtual display.

INHIBIT_PID_FILE="/tmp/sunshine-sleep-inhibit.pid"

if [ -f "$INHIBIT_PID_FILE" ]; then
    kill "$(cat "$INHIBIT_PID_FILE" 2>/dev/null)" 2>/dev/null
    rm -f "$INHIBIT_PID_FILE"
fi

exec sudo /usr/local/bin/sunshine-virt-display-handler disconnect
