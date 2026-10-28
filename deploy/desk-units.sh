#!/bin/bash
# Install the systemd timers that run Claude's trading day. Run as root.
set -euo pipefail
SRC=$(cd "$(dirname "$0")" && pwd)
install -m 755 "$SRC/desk.sh" /usr/local/bin/desk
U=/etc/systemd/system

# The session lives in this unit's process group, so it stays "active" while Claude runs.
cat > $U/desk-session.service <<'EOF'
[Unit]
Description=Claude trading session (tmux "desk")
After=network-online.target trader-monitor.service
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
User=agent
ExecStart=/usr/local/bin/desk start
ExecStop=/usr/local/bin/desk stop
EOF

cat > $U/desk-session.timer <<'EOF'
[Unit]
Description=Start Claude's trading session on weekday mornings
[Timer]
OnCalendar=Mon..Fri 09:05
Persistent=true
[Install]
WantedBy=timers.target
EOF

for job in nudge close; do
cat > $U/desk-$job.service <<EOF
[Unit]
Description=Claude trading session: $job
[Service]
Type=oneshot
User=agent
ExecStart=/usr/local/bin/desk $job
EOF
done

cat > $U/desk-nudge.timer <<'EOF'
[Unit]
Description=Check in with Claude every 5 minutes in market hours
[Timer]
OnCalendar=Mon..Fri 09..15:00/5
[Install]
WantedBy=timers.target
EOF

cat > $U/desk-close.timer <<'EOF'
[Unit]
Description=End-of-day message to Claude
[Timer]
OnCalendar=Mon..Fri 15:35
[Install]
WantedBy=timers.target
EOF

# End the session before the machine stops at 16:00 (and on days it stays up).
cat > $U/desk-stop.service <<'EOF'
[Unit]
Description=End Claude's trading session
[Service]
Type=oneshot
ExecStart=/bin/systemctl stop desk-session.service
EOF
cat > $U/desk-stop.timer <<'EOF'
[Unit]
Description=End Claude's trading session at 15:55
[Timer]
OnCalendar=Mon..Fri 15:55
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now desk-session.timer desk-nudge.timer desk-close.timer desk-stop.timer >/dev/null 2>&1
systemctl list-timers 'desk-*' --no-pager
