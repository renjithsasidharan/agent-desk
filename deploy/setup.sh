#!/bin/bash
# Install or update the trading program. Run as root from the unpacked source folder:
#   sudo bash deploy/setup.sh
set -euo pipefail
SRC=$(cd "$(dirname "$0")/.." && pwd)

# Packages and clock: the trading day runs on India time.
missing=$(for p in tmux rsync git python3-venv python3-pip; do dpkg -s $p >/dev/null 2>&1 || echo $p; done)
if [ -n "$missing" ]; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $missing >/dev/null
fi
[ "$(timedatectl show -p Timezone --value)" = Asia/Kolkata ] || timedatectl set-timezone Asia/Kolkata

id trader >/dev/null 2>&1 || useradd --system --home-dir /var/lib/trader --shell /usr/sbin/nologin trader
id agent >/dev/null 2>&1 || useradd --create-home --shell /bin/bash agent

# Claude Code for the agent user (log in once by hand: see SETUP.md), and a Python with pandas
# for its research. Claude runs without permission prompts: the agent user is the sandbox.
[ -x /home/agent/.local/bin/claude ] || sudo -iu agent bash -c 'curl -fsSL https://claude.ai/install.sh | bash' >/dev/null
[ -x /home/agent/desk/.venv/bin/python ] || sudo -iu agent bash -c \
    'mkdir -p ~/desk && python3 -m venv ~/desk/.venv && ~/desk/.venv/bin/pip install -q pandas numpy'
sudo -iu agent python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
d = json.load(open(p)) if os.path.exists(p) else {}
d.setdefault("skipDangerousModePermissionPrompt", True)
json.dump(d, open(p, "w"), indent=2)
PY

# Code and config: owned by root, so neither the agent nor the trading process can change them.
install -d -m 755 /opt/trader
rsync -a --delete --exclude config.yaml --exclude .venv --exclude '*.egg-info' "$SRC/" /opt/trader/
[ -f /opt/trader/config.yaml ] || install -m 644 "$SRC/config.yaml" /opt/trader/config.yaml
chown -R root:root /opt/trader
[ -x /opt/trader/.venv/bin/python ] || python3 -m venv /opt/trader/.venv
/opt/trader/.venv/bin/pip install -q -e /opt/trader

# State: written by the trading process, readable by the agent. Broker tokens live in private/.
install -d -o trader -g agent -m 750 /var/lib/trader
install -d -o trader -g trader -m 700 /var/lib/trader/private

# Before books existed, the one book's files sat directly in /var/lib/trader: move them to books/main.
if [ -f /var/lib/trader/trades.json ] && [ ! -d /var/lib/trader/books/main ]; then
    systemctl stop trader-monitor 2>/dev/null || true
    install -d -o trader -g trader /var/lib/trader/books/main
    for f in account.json trades.json plans.json events.jsonl paper_orders.json status.json journal; do
        [ -e "/var/lib/trader/$f" ] && mv "/var/lib/trader/$f" /var/lib/trader/books/main/
    done
    echo "moved the existing book to /var/lib/trader/books/main"
fi
install -d -o trader -g trader -m 755 /var/lib/trader/books

# Broker keys: readable by the trading process only.
install -d -o root -g trader -m 750 /etc/trader
[ -f /etc/trader/secrets.env ] || install -o root -g trader -m 640 "$SRC/deploy/secrets.env.example" /etc/trader/secrets.env

install -m 755 "$SRC/deploy/trade" /usr/local/bin/trade
install -m 440 "$SRC/deploy/sudoers" /etc/sudoers.d/trader
visudo -cf /etc/sudoers.d/trader >/dev/null
install -m 644 "$SRC/deploy/trader-monitor.service" /etc/systemd/system/trader-monitor.service
systemctl daemon-reload
systemctl enable --now trader-monitor >/dev/null 2>&1
systemctl restart trader-monitor
echo "installed; monitor: $(systemctl is-active trader-monitor)"
bash "$SRC/deploy/desk-units.sh"

# One desk folder per book (main = /home/agent/desk). CLAUDE.md is the owner's: the shared rules
# plus that book's personality. notes/, research/, SETUPS.md and LEARNINGS.md are Claude's.
for book in $(trade books); do
    if [ "$book" = main ]; then d=/home/agent/desk; else d=/home/agent/desk-$book; fi
    install -d -o agent -g agent "$d" "$d/notes" "$d/research" "$d/data"
    persona="$SRC/deploy/desk/personas/$book.md"
    [ -f "$persona" ] || { echo "missing $persona"; exit 1; }
    # /etc/trader/desk/<book>.md (optional, not in the repo): the owner's own notes for that book,
    # e.g. deploy/desk/live-note.example.md once it trades real money.
    extra=/etc/trader/desk/$book.md
    [ -f "$extra" ] || extra=/dev/null
    cat "$SRC/deploy/desk/RULES.md" "$persona" "$extra" | install -o root -g root -m 644 /dev/stdin "$d/CLAUDE.md"
    [ -f "$d/SETUPS.md" ] || printf '# Setups\n\nOne section per setup name used with --setup: market condition, entry trigger, stop, target.\n' \
        | install -o agent -g agent -m 644 /dev/stdin "$d/SETUPS.md"
    [ -f "$d/LEARNINGS.md" ] || printf '# Learnings\n\nLasting lessons, each with evidence (trade ids, dates).\n' \
        | install -o agent -g agent -m 644 /dev/stdin "$d/LEARNINGS.md"
    [ -e "$d/.venv" ] || sudo -u agent ln -s /home/agent/desk/.venv "$d/.venv"   # shared pandas/numpy
    # Claude asks once whether to trust a new folder; answer it here so the morning start is unattended.
    sudo -u agent python3 - "$d" <<'PY'
import json, os, sys
p = "/home/agent/.claude.json"
d = json.load(open(p)) if os.path.exists(p) else {}
proj = d.setdefault("projects", {})
proj.setdefault(sys.argv[1], dict(proj.get("/home/agent/desk", {})))["hasTrustDialogAccepted"] = True
json.dump(d, open(p, "w"), indent=2)
PY
done
