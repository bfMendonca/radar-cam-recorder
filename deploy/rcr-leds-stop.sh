#!/bin/sh
# systemd ExecStopPost: leave the LEDs showing why the recorder stopped.
# Runs as the service user (group gpio can write the LEDs); every write is best-effort.
LEDS=/sys/class/leds

off() {
    echo none > "$LEDS/$1/trigger" 2>/dev/null || true
    echo 0 > "$LEDS/$1/brightness" 2>/dev/null || true
}

if [ "$SERVICE_RESULT" != "success" ]; then
    echo none > "$LEDS/rcr-red/trigger" 2>/dev/null || true
    echo 1 > "$LEDS/rcr-red/brightness" 2>/dev/null || true
    off rcr-green
elif [ "$(systemctl is-system-running 2>/dev/null)" = "stopping" ]; then
    : # shutting down: keep the "shutting down" pattern
else
    off rcr-green
    off rcr-red
fi
exit 0
