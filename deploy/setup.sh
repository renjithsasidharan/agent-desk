#!/bin/bash
# Install or update the trading program. Run as root from the unpacked source folder:
#   sudo bash deploy/setup.sh
set -euo pipefail
SRC=$(cd "$(dirname "$0")/.." && pwd)

id trader >/dev/null 2>&1 || useradd --system --home-dir /var/lib/trader --shell /usr/sbin/nologin trader
id agent >/dev/null 2>&1 || useradd --create-home --shell /bin/bash agent

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

# Claude's desk: instructions are the owner's; notes, research and LEARNINGS.md are Claude's.
install -d -o agent -g agent /home/agent/desk /home/agent/desk/notes /home/agent/desk/research /home/agent/desk/data
install -o root -g root -m 644 "$SRC/deploy/desk/CLAUDE.md" /home/agent/desk/CLAUDE.md
[ -f /home/agent/desk/SETUPS.md ] || printf '# Setups\n\nOne section per setup name used with --setup: market condition, entry trigger, stop, target.\n' \
    | install -o agent -g agent -m 644 /dev/stdin /home/agent/desk/SETUPS.md
