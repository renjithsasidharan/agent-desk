#!/bin/bash
# Claude trading desks: one tmux session "desk-<book>" per book, all run as the agent user.
#   desk list                     books, their folders, and whether each session is running
#   desk attach <book>            watch a session (detach: Ctrl-b then d; Ctrl-b 1 = events window)
#   desk start [--force] [book]   open sessions with the morning brief (weekdays 08:30-15:00 unless --force)
#   desk nudge                    check-in message to each running session
#   desk close                    end-of-day message to each running session
#   desk stop                     end all sessions
#   desk watch <book>             (runs inside a session) pass that book's monitor events to Claude
set -euo pipefail
if [ "$(id -un)" != agent ]; then
    exec sudo -u agent /usr/local/bin/desk "$@"
fi
export HOME=/home/agent PATH=/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin
cd /home/agent

books() { trade books; }
folder() { if [ "$1" = main ]; then echo /home/agent/desk; else echo "/home/agent/desk-$1"; fi; }
running() { tmux has-session -t "desk-$1" 2>/dev/null; }

send() {
    tmux send-keys -t "desk-$1:claude" -l "$2"
    sleep 0.5
    tmux send-keys -t "desk-$1:claude" Enter
}

in_window() {  # weekday and HHMM between $1 and $2
    local d t
    d=$(date +%u) t=$(date +%H%M)
    [ "$d" -le 5 ] && [ "$t" -ge "$1" ] && [ "$t" -le "$2" ]
}

busy() {  # open trades or active plans in this book
    TRADE_BOOK=$1 trade status 2>/dev/null | python3 -c 'import json,sys
d = json.load(sys.stdin)
print(1 if d.get("open") or d.get("active_plans") else 0)' 2>/dev/null || echo 1
}

case "${1:-}" in
list)
    for b in $(books); do
        printf '%-10s %-8s %s\n' "$b" "$(running "$b" && echo running || echo stopped)" "$(folder "$b")"
    done
    ;;
attach)
    exec tmux attach -t "desk-${2:?usage: desk attach <book>}"
    ;;
start)
    shift
    force=0
    if [ "${1:-}" = --force ]; then force=1; shift; fi
    [ $force = 1 ] || in_window 0830 1500 || exit 0
    targets=${1:-$(books)}
    started=""
    for b in $targets; do
        running "$b" && continue
        tmux new-session -d -s "desk-$b" -n claude -x 200 -y 50 -c "$(folder "$b")" -e TRADE_BOOK="$b" \
            "claude --dangerously-skip-permissions"
        tmux new-window -d -t "desk-$b" -n events -e TRADE_BOOK="$b" "/usr/local/bin/desk watch $b"
        started="$started $b"
    done
    [ -n "$started" ] || exit 0
    sleep 20
    for b in $started; do
        send "$b" "Good morning, it is $(date '+%A %F %H:%M') IST. Run today's session as CLAUDE.md describes: read LEARNINGS.md and your recent notes, research, write the plan to notes/$(date +%F).md, set up planned entries, and trade from 09:20. You will get check-in messages until 15:20, an EVENT message whenever the monitor acts, and an end-of-day message."
    done
    ;;
nudge)
    in_window 0920 1520 || exit 0
    for b in $(books); do
        running "$b" || continue
        # Every 5 minutes while there is something to watch (and until 09:45); otherwise every 15.
        if [ "$(busy "$b")" = 0 ] && [ "$(date +%H%M)" -gt 0945 ] && [ $((10#$(date +%M) % 15)) -ne 0 ]; then
            continue
        fi
        send "$b" "Check-in $(date +%H:%M) IST: run trade status, manage open trades, act on your plan. Keep it short."
    done
    ;;
close)
    for b in $(books); do
        running "$b" || continue
        send "$b" "Market closed. Run trade review --days 1, write today's results and lessons to notes/$(date +%F).md and update LEARNINGS.md. The machine shuts down at 16:00."
    done
    ;;
stop)
    for b in $(books); do tmux kill-session -t "desk-$b" 2>/dev/null || true; done
    tmux kill-session -t desk 2>/dev/null || true   # the old single session, if still running
    ;;
watch)
    b=${2:?usage: desk watch <book>}
    # Each new line the monitor appends to this book's events becomes a message to its Claude.
    tail -n0 -F "/var/lib/trader/books/$b/events.jsonl" 2>/dev/null | while IFS= read -r line; do
        send "$b" "EVENT from the monitor: $line -- check trade status and react if needed."
    done
    ;;
*)
    sed -n '2,10p' "$0"
    exit 1
    ;;
esac
