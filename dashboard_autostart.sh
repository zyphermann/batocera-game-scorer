#!/bin/sh

# Starts the local scoring dashboard on Batocera.
# Intended path on Batocera: /userdata/scoring/dashboard_autostart.sh

SCORING_DIR="${SCORING_DIR:-/userdata/scoring}"
DASHBOARD_SCRIPT="${DASHBOARD_SCRIPT:-$SCORING_DIR/web_dashboard.py}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"
START_DELAY="${START_DELAY:-10}"
LOG_DIR="${LOG_DIR:-$SCORING_DIR/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/web_dashboard.log}"
PID_FILE="${PID_FILE:-$SCORING_DIR/web_dashboard.pid}"

log() {
    mkdir -p "$LOG_DIR"
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

is_running() {
    [ -f "$PID_FILE" ] || return 1
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

start_dashboard() {
    mkdir -p "$LOG_DIR"

    if is_running; then
        log "Dashboard already running with PID $(cat "$PID_FILE")."
        return 0
    fi

    if [ ! -d "$SCORING_DIR" ]; then
        log "Scoring directory missing: $SCORING_DIR"
        return 1
    fi

    if [ ! -f "$DASHBOARD_SCRIPT" ]; then
        log "Dashboard script missing: $DASHBOARD_SCRIPT"
        return 1
    fi

    if [ "$START_DELAY" -gt 0 ] 2>/dev/null; then
        log "Waiting $START_DELAY seconds before starting dashboard."
        sleep "$START_DELAY"
    fi

    cd "$SCORING_DIR" || {
        log "Cannot cd to $SCORING_DIR"
        return 1
    }

    log "Starting dashboard on $DASHBOARD_HOST:$DASHBOARD_PORT"
    nohup "$PYTHON_BIN" "$DASHBOARD_SCRIPT" \
        --host "$DASHBOARD_HOST" \
        --port "$DASHBOARD_PORT" \
        >> "$LOG_FILE" 2>&1 &
    echo "$!" > "$PID_FILE"
    log "Dashboard started with PID $(cat "$PID_FILE")."
}

stop_dashboard() {
    if ! is_running; then
        log "Dashboard is not running."
        rm -f "$PID_FILE"
        return 0
    fi

    pid="$(cat "$PID_FILE")"
    log "Stopping dashboard PID $pid."
    kill "$pid" 2>/dev/null

    i=0
    while kill -0 "$pid" 2>/dev/null; do
        i=$((i + 1))
        if [ "$i" -ge 10 ]; then
            log "Dashboard did not stop after 10 seconds; sending SIGKILL."
            kill -9 "$pid" 2>/dev/null
            break
        fi
        sleep 1
    done

    rm -f "$PID_FILE"
    log "Dashboard stopped."
}

status_dashboard() {
    if is_running; then
        echo "Dashboard running with PID $(cat "$PID_FILE")"
        return 0
    fi
    echo "Dashboard not running"
    return 1
}

case "${1:-start}" in
    start)
        start_dashboard
        ;;
    stop)
        stop_dashboard
        ;;
    restart)
        stop_dashboard
        start_dashboard
        ;;
    status)
        status_dashboard
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 2
        ;;
esac
