#!/bin/bash
# Runs Claude for the trading day in tmux session "desk" (as the agent user).
#   desk start [--force]   open the session and give the morning brief (weekdays 08:30-15:00 unless --force)
#   desk nudge             check-in message (09:20-15:20)
#   desk close             end-of-day message
#   desk stop              end the session
#   desk watch             (runs in the session's "events" window) pass monitor events to Claude
# Watch it: sudo -iu agent tmux attach -t desk   (detach: Ctrl-b then d)
set -euo pipefail
export HOME=/home/agent PATH=/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin
S=desk
W="$S:claude"
EVENTS=/var/lib/trader/events.jsonl
cd /home/agent/desk

send() {
    tmux send-keys -t "$W" -l "$1"
    sleep 0.5
    tmux send-keys -t "$W" Enter
}

in_window() {  # weekday and HHMM between $1 and $2
    local d t
    d=$(date +%u) t=$(date +%H%M)
    [ "$d" -le 5 ] && [ "$t" -ge "$1" ] && [ "$t" -le "$2" ]
}

case "${1:-}" in
start)
    [ "${2:-}" = "--force" ] || in_window 0830 1500 || exit 0
    tmux has-session -t "$S" 2>/dev/null && exit 0
    tmux new-session -d -s "$S" -n claude -x 200 -y 50 "claude --dangerously-skip-permissions"
    tmux new-window -d -t "$S" -n events "/usr/local/bin/desk watch"
    sleep 20
    send "Good morning, it is $(date '+%A %F %H:%M') IST. Run today's session as CLAUDE.md describes: read LEARNINGS.md and your recent notes, research, write the plan to notes/$(date +%F).md, set up planned entries, and trade from 09:20. You will get a check-in message every 5 minutes until 15:20, an EVENT message whenever the monitor acts, and an end-of-day message."
    ;;
watch)
    # Each new line the monitor appends becomes a message to Claude.
    tail -n0 -F "$EVENTS" 2>/dev/null | while IFS= read -r line; do
        send "EVENT from the monitor: $line -- check trade status and react if needed."
    done
    ;;
nudge)
    in_window 0920 1520 || exit 0
    tmux has-session -t "$S" 2>/dev/null || exit 0
    send "Check-in $(date +%H:%M) IST: run trade status, manage open trades, act on your plan. Keep it short."
    ;;
close)
    tmux has-session -t "$S" 2>/dev/null || exit 0
    send "Market closed. Write today's results and lessons to notes/$(date +%F).md and update LEARNINGS.md. The machine shuts down at 16:00."
    ;;
stop)
    tmux kill-session -t "$S" 2>/dev/null || true
    ;;
*)
    sed -n '2,8p' "$0"
    exit 1
    ;;
esac
